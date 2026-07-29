# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Agent-principal lifecycle service — register / list / revoke.

G11.2-T1 (#815) under Initiative #803. The single code path the REST
routes (:mod:`meho_backplane.api.v1.agent_principals`), MCP tools
(:mod:`meho_backplane.mcp.tools.agent_principals`), and Go CLI verbs
(``meho agent-principal``) all call through. Enforces the tenant
boundary, name uniqueness, and the two-phase commit (DB row + Keycloak
client) in one place.

Design
------

* **Stateless and method-scoped** — same concurrency model as
  :class:`~meho_backplane.agents.service.AgentDefinitionService`. Each
  method opens its own DB session, commits, and closes.
* **Keycloak admin calls are async** — delegated to
  :class:`~meho_backplane.auth.keycloak_admin.KeycloakAdminClient`.
  The DB commit and the Keycloak call are *not* in the same ACID
  transaction (Keycloak has no XA participant). The consistency
  strategy is **Keycloak-first on both paths**: register creates the
  Keycloak client then inserts the DB row (on Keycloak failure the row
  is never written; on a DB failure the just-created client is rolled
  back); revoke disables the Keycloak client then commits
  ``revoked=true`` (on a non-404 Keycloak failure the revoke surfaces an
  error and the row is *not* marked revoked, so MEHO never reports a
  still-live, token-issuing principal as revoked). Keycloak's
  ``enabled=false`` is the authoritative kill switch.
* **``keycloak_client_id`` convention** — the OAuth client id for a
  registered agent is ``agent:<name>`` (forward-slash forbidden in
  Keycloak client ids). This convention lets operators distinguish
  agent clients from user / service clients in the Admin Console and
  is enforced by this module (not just by convention on the caller
  side).
* **RBAC not enforced here** — the route / tool layers gate on
  ``tenant_admin``; this service assumes that check has already run.

Error contract
--------------

* :class:`AgentPrincipalExistsError` — register collided with an
  existing ``(tenant_id, name)`` or ``keycloak_client_id``.
* :class:`AgentPrincipalNotFoundError` — revoke / get on a name that
  is absent or belongs to another tenant.
* :class:`~meho_backplane.auth.keycloak_admin.KeycloakAdminNotConfiguredError`
  — Keycloak admin URL / credentials not set (503 at the boundary).
* :class:`~meho_backplane.auth.keycloak_admin.KeycloakAdminError`
  — other Keycloak API failure (502 at the boundary).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from meho_backplane.auth.keycloak_admin import (
    KeycloakAdminClient,
    KeycloakClientConflictError,
    KeycloakClientNotFoundError,
)
from meho_backplane.auth.operator import TenantRole

# Reuse the runner path's shared name-length bound (#2508) rather than a
# second literal, so the agent intake schema and the by-name lookup ``Path``
# params in :mod:`meho_backplane.api.v1.agent_principals` cannot drift from
# each other — or from the runner path's — as #2502 established (#2523).
from meho_backplane.auth.runner_principals import NAME_MAX_LENGTH
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPrincipal
from meho_backplane.scheduler.vault_credentials import (
    SchedulerVaultNotConfiguredError,
    write_agent_secret,
)
from meho_backplane.settings import get_settings

__all__ = [
    "NAME_MAX_LENGTH",
    "AgentPrincipalCreate",
    "AgentPrincipalExistsError",
    "AgentPrincipalNotFoundError",
    "AgentPrincipalRead",
    "AgentPrincipalService",
]

#: Regex for the agent name: letters, digits, hyphen, underscore, dot.
#: Mirrors ``meho_backplane.agents.schemas.NAME_PATTERN``.
_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_\-\.]+$")

#: Convention: the Keycloak clientId for an agent principal.
_CLIENT_ID_PREFIX: str = "agent:"

#: ``tenant_role`` stamped into the agent client's access token (#1487).
#: An agent acts with tenant-admin scope inside its tenant; the
#: per-principal permission model (G11.2-T3) is the finer-grained gate,
#: not this coarse JWT role. Matches the ``agent:test-bot`` integration
#: realm fixture, the only agent client that authenticates end-to-end.
_AGENT_TENANT_ROLE: str = TenantRole.TENANT_ADMIN.value


def _keycloak_client_id(name: str) -> str:
    """Return the canonical Keycloak clientId for agent *name*."""
    return f"{_CLIENT_ID_PREFIX}{name}"


def _is_unique_violation(exc: IntegrityError) -> bool:
    """Return whether *exc* is a unique-constraint violation."""
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return sqlstate == "23505" or "UNIQUE constraint failed" in str(orig or exc)


class AgentPrincipalExistsError(Exception):
    """Raised when register collides with an existing (tenant_id, name)."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"agent principal {name!r} already exists for this tenant")


class AgentPrincipalNotFoundError(Exception):
    """Raised when get/revoke finds no matching row."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"agent principal {name!r} not found")


class AgentPrincipalCreate(BaseModel):
    """Input shape for :meth:`AgentPrincipalService.register`."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=NAME_MAX_LENGTH)
    owner_sub: str | None = None


class AgentPrincipalRead(BaseModel):
    """Row representation returned by every accessor."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    keycloak_client_id: str
    keycloak_internal_id: str
    owner_sub: str
    revoked: bool
    created_by_sub: str
    created_at: datetime
    updated_at: datetime


class AgentPrincipalService:
    """Tenant-scoped register / list / revoke for agent principals.

    Stateless; instantiate once per request and call freely.
    """

    def __init__(self) -> None:
        self._log = structlog.get_logger()

    async def register(
        self,
        tenant_id: uuid.UUID,
        created_by_sub: str,
        payload: AgentPrincipalCreate,
    ) -> AgentPrincipalRead:
        """Register a new agent principal.

        Creates the Keycloak client first, captures its generated
        ``client_credentials`` secret, and persists that secret to Vault
        via :meth:`_persist_secret_to_vault` (so the operator-less
        scheduler can read it back — G0.19-T2 #1478); on success inserts
        the DB row. If any step fails the Keycloak client is rolled back
        and the DB row is never written.

        Raises
        ------
        ValueError
            When *name* contains characters outside the safe alphabet.
        AgentPrincipalExistsError
            Duplicate ``(tenant_id, name)`` (DB unique-index or Keycloak 409).
        KeycloakAdminNotConfiguredError / KeycloakAdminError
            Keycloak admin unconfigured / any Admin API failure.
        SchedulerVaultBrokerError
            Vault configured but the secret write failed — see
            :meth:`_persist_secret_to_vault` (an *unset* token is a
            skip-with-warning, not a raise).
        """
        if not _NAME_PATTERN.fullmatch(payload.name):
            raise ValueError(
                f"agent principal name {payload.name!r} contains characters "
                "outside the safe set (allowed: letters, digits, hyphen, "
                "underscore, dot)"
            )
        owner = payload.owner_sub or created_by_sub
        client_id = _keycloak_client_id(payload.name)
        audience = get_settings().keycloak_audience

        # Phase 1: create the Keycloak client + capture its generated secret.
        # Any failure after create_client rolls the just-created client back
        # (see _provision_keycloak_client) so register never orphans an
        # un-revocable identity before any DB row exists.
        internal_id, client_secret = await self._provision_keycloak_client(
            name=payload.name,
            tenant_id=tenant_id,
            owner_sub=owner,
            audience=audience,
        )

        # Phase 1b: persist the captured secret to Vault for the scheduler.
        await self._persist_secret_to_vault(
            client_id,
            client_secret,
            internal_id=internal_id,
            tenant_id=tenant_id,
            name=payload.name,
        )

        # Phase 2: insert the DB row (rolls back the Keycloak client on
        # any failure — see _insert_row).
        row = AgentPrincipal(
            tenant_id=tenant_id,
            name=payload.name,
            keycloak_client_id=client_id,
            keycloak_internal_id=internal_id,
            owner_sub=owner,
            revoked=False,
            created_by_sub=created_by_sub,
        )
        entry = await self._insert_row(row, internal_id=internal_id, name=payload.name)
        self._log.info(
            "agent_principal_register",
            tenant_id=str(tenant_id),
            name=payload.name,
            keycloak_client_id=client_id,
            created_by_sub=created_by_sub,
        )
        return entry

    async def _provision_keycloak_client(
        self,
        *,
        name: str,
        tenant_id: uuid.UUID,
        owner_sub: str,
        audience: str,
    ) -> tuple[str, str]:
        """Create the agent's Keycloak client and read back its secret.

        Phase 1 of :meth:`register`, isolated so its rollback contract is a
        single unit. Creates the confidential client (clientId
        ``agent:<name>``) with the audience + tenant-claim mappers and the
        default scopes that carry ``sub``, so its ``client_credentials``
        token validates through the JWT chain with no manual Keycloak
        surgery (#1487), then reads back the generated secret in the same
        admin session (``create_client`` returns only the internal UUID;
        Keycloak never echoes the generated secret on create).

        Rollback contract: if *anything* after ``create_client`` raises — most
        importantly ``get_client_secret`` — the just-created, live client is
        deleted before the error propagates, so register never orphans an
        un-revocable identity. ``internal_id`` is still ``None`` when
        ``create_client`` itself failed (nothing created, nothing to roll
        back). A 409 conflict surfaces as :class:`AgentPrincipalExistsError`
        — the conflicting client belongs to a prior registration and is not
        ours to delete.

        Returns the ``(keycloak_internal_id, client_secret)`` pair.
        """
        client_id = _keycloak_client_id(name)
        internal_id: str | None = None
        kc_client = KeycloakAdminClient.from_settings()
        try:
            async with kc_client:
                internal_id = await kc_client.create_client(
                    client_id=client_id,
                    name=name,
                    tenant_id=str(tenant_id),
                    owner_sub=owner_sub,
                    audience=audience,
                    tenant_role=_AGENT_TENANT_ROLE,
                )
                client_secret = await kc_client.get_client_secret(internal_id)
        except KeycloakClientConflictError as exc:
            raise AgentPrincipalExistsError(name) from exc
        except BaseException as exc:
            if internal_id is not None:
                await self._rollback_orphan_client(
                    internal_id, tenant_id=tenant_id, name=name, cause=exc
                )
            raise
        return internal_id, client_secret

    async def _insert_row(
        self,
        row: AgentPrincipal,
        *,
        internal_id: str,
        name: str,
    ) -> AgentPrincipalRead:
        """Insert the agent-principal DB row; roll back the KC client on failure.

        If anything fails after the Keycloak client was created, delete
        that client before surfacing the error: a created client with no
        MEHO row is an orphaned, token-issuing identity that can never be
        listed or revoked through MEHO — exactly the unreachable-kill-switch
        failure this lifecycle exists to prevent.
        """
        sessionmaker = get_sessionmaker()
        try:
            async with sessionmaker() as session:
                session.add(row)
                try:
                    await session.flush()
                except IntegrityError as exc:
                    await session.rollback()
                    if _is_unique_violation(exc):
                        raise AgentPrincipalExistsError(name) from exc
                    raise
                await session.refresh(row)
                entry = AgentPrincipalRead.model_validate(row)
                await session.commit()
        except BaseException as exc:
            await self._rollback_orphan_client(
                internal_id, tenant_id=row.tenant_id, name=name, cause=exc
            )
            raise
        return entry

    async def _persist_secret_to_vault(
        self,
        client_id: str,
        client_secret: str,
        *,
        internal_id: str,
        tenant_id: uuid.UUID,
        name: str,
    ) -> None:
        """Persist the captured Keycloak secret to Vault (G0.19-T2 #1478).

        Two failure postures:

        * **Vault not configured** (``VAULT_SCHEDULER_TOKEN`` unset) — skip
          the write with a WARN and continue. The deployment has opted out
          of the Vault path; the agent stays schedulable via the env-var
          fallback (the documented break-glass). Keeps registration
          backward-compatible with env-var-only deployments rather than
          hard-failing them on the new requirement.
        * **Vault configured but the write failed** (unreachable / denied)
          — roll back the just-created Keycloak client and surface the
          error. A client whose secret was *meant* to reach Vault but
          didn't would be unschedulable with no signal, so we fail closed,
          same posture as a Phase-2 DB failure.
        """
        try:
            await write_agent_secret(client_id, client_secret)
        except SchedulerVaultNotConfiguredError:
            self._log.warning(
                "agent_principal_register_vault_skip",
                tenant_id=str(tenant_id),
                name=name,
                reason="scheduler_vault_not_configured",
            )
        except BaseException as exc:
            await self._rollback_orphan_client(
                internal_id, tenant_id=tenant_id, name=name, cause=exc
            )
            raise

    async def _rollback_orphan_client(
        self,
        internal_id: str,
        *,
        tenant_id: uuid.UUID,
        name: str,
        cause: BaseException,
    ) -> None:
        """Best-effort delete of a Keycloak client whose DB row failed to write.

        Called only after :meth:`create_client` succeeded but the Phase-2 DB
        write raised. A cleanup failure is logged (the orphan needs manual
        removal) but never masks the original *cause*, which the caller
        re-raises.
        """
        try:
            kc_client = KeycloakAdminClient.from_settings()
            async with kc_client:
                await kc_client.delete_client(internal_id)
        except KeycloakClientNotFoundError:
            return  # Already gone — nothing to roll back.
        except Exception as cleanup_exc:
            self._log.error(
                "agent_principal_register_orphan_cleanup_failed",
                tenant_id=str(tenant_id),
                name=name,
                keycloak_internal_id=internal_id,
                cause=type(cause).__name__,
                error=type(cleanup_exc).__name__,
            )
            return
        self._log.warning(
            "agent_principal_register_rolled_back_keycloak_client",
            tenant_id=str(tenant_id),
            name=name,
            keycloak_internal_id=internal_id,
            cause=type(cause).__name__,
        )

    async def list_(
        self,
        tenant_id: uuid.UUID,
        *,
        include_revoked: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AgentPrincipalRead]:
        """Return agent principals for *tenant_id*, name-sorted.

        Revoked principals are excluded by default (``include_revoked=False``)
        because the operator-facing view is the active identity inventory;
        passing ``include_revoked=True`` is reserved for audit inspection.
        """
        if limit < 0:
            raise ValueError(f"limit must be >= 0; got {limit}")
        if offset < 0:
            raise ValueError(f"offset must be >= 0; got {offset}")
        if limit == 0:
            return []
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            q = select(AgentPrincipal).where(AgentPrincipal.tenant_id == tenant_id)
            if not include_revoked:
                q = q.where(AgentPrincipal.revoked.is_(False))
            q = q.order_by(AgentPrincipal.name).limit(limit).offset(offset)
            result = await session.execute(q)
            rows = result.scalars().all()
        return [AgentPrincipalRead.model_validate(row) for row in rows]

    async def get(
        self,
        tenant_id: uuid.UUID,
        name: str,
    ) -> AgentPrincipalRead | None:
        """Fetch one principal by ``(tenant_id, name)``; ``None`` if absent."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(AgentPrincipal).where(
                    AgentPrincipal.tenant_id == tenant_id,
                    AgentPrincipal.name == name,
                )
            )
            row = result.scalar_one_or_none()
        if row is None:
            return None
        return AgentPrincipalRead.model_validate(row)

    async def revoke(
        self,
        tenant_id: uuid.UUID,
        name: str,
    ) -> AgentPrincipalRead:
        """Revoke an agent principal (kill switch).

        Phase 1: look up the principal (must exist and not already be
        revoked) to obtain its ``keycloak_internal_id``.
        Phase 2: disable the Keycloak client (``enabled=false``) — the
        authoritative kill switch.
        Phase 3: commit ``revoked=true`` in the DB.

        Keycloak is disabled *before* the DB row is marked so a Keycloak
        failure aborts the revoke without falsely reporting a principal
        as revoked while it can still mint tokens. A Keycloak *not-found*
        is treated as success (the client was cleaned up out of band) and
        the DB row is still marked revoked. No DB transaction is held
        open across the Keycloak network call.

        Raises
        ------
        AgentPrincipalNotFoundError
            When no row matches ``(tenant_id, name)`` or the row is
            already revoked.
        KeycloakAdminNotConfiguredError
            When Keycloak admin credentials are not configured.
        KeycloakAdminError
            On a non-404 Keycloak Admin API failure — the DB row is left
            unchanged, so the principal stays active and the operator can
            retry.
        """
        sessionmaker = get_sessionmaker()
        # Phase 1: validate + fetch the Keycloak internal id (read-only).
        async with sessionmaker() as session:
            result = await session.execute(
                select(AgentPrincipal).where(
                    AgentPrincipal.tenant_id == tenant_id,
                    AgentPrincipal.name == name,
                    AgentPrincipal.revoked.is_(False),
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise AgentPrincipalNotFoundError(name)
            keycloak_internal_id = row.keycloak_internal_id

        # Phase 2: disable the Keycloak client FIRST (authoritative kill
        # switch). A non-404 failure propagates before any DB write, so a
        # principal is never marked revoked while it can still mint tokens.
        kc_client = KeycloakAdminClient.from_settings()
        try:
            async with kc_client:
                await kc_client.disable_client(keycloak_internal_id)
        except KeycloakClientNotFoundError:
            self._log.warning(
                "agent_principal_revoke_keycloak_not_found",
                tenant_id=str(tenant_id),
                name=name,
                keycloak_internal_id=keycloak_internal_id,
            )

        # Phase 3: persist revoked=true now that the client is disabled.
        async with sessionmaker() as session:
            result = await session.execute(
                select(AgentPrincipal).where(
                    AgentPrincipal.tenant_id == tenant_id,
                    AgentPrincipal.name == name,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                # Raced deletion between Phase 1 and Phase 3.
                raise AgentPrincipalNotFoundError(name)
            if not row.revoked:
                row.revoked = True
                row.updated_at = datetime.now(UTC)
                await session.flush()
            await session.refresh(row)
            entry = AgentPrincipalRead.model_validate(row)
            await session.commit()

        self._log.info(
            "agent_principal_revoke",
            tenant_id=str(tenant_id),
            name=name,
            keycloak_internal_id=keycloak_internal_id,
        )
        return entry
