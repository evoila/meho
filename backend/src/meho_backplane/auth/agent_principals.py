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
  strategy is: Keycloak-first on register (create Keycloak client →
  insert DB row; on Keycloak failure the row is never written),
  DB-first on revoke (mark revoked in DB → disable Keycloak client;
  on Keycloak failure the row is already revoked so no new tokens
  can be linked to an active MEHO principal).
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
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from meho_backplane.auth.keycloak_admin import (
    KeycloakAdminClient,
    KeycloakClientConflictError,
    KeycloakClientNotFoundError,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPrincipal

__all__ = [
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

    name: str
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

        Creates the Keycloak client first; on success inserts the DB row.
        If the Keycloak creation fails the DB row is never written.

        Raises
        ------
        ValueError
            When *name* contains characters outside the safe alphabet.
        AgentPrincipalExistsError
            When a principal with the same name already exists in this
            tenant (unique-index violation on DB or Keycloak 409).
        KeycloakAdminNotConfiguredError
            When Keycloak admin credentials are not configured.
        KeycloakAdminError
            On any Keycloak Admin API failure.
        """
        if not _NAME_PATTERN.fullmatch(payload.name):
            raise ValueError(
                f"agent principal name {payload.name!r} contains characters "
                "outside the safe set (allowed: letters, digits, hyphen, "
                "underscore, dot)"
            )
        owner = payload.owner_sub or created_by_sub
        client_id = _keycloak_client_id(payload.name)

        # Phase 1: create Keycloak client (fail before DB on error).
        kc_client = KeycloakAdminClient.from_settings()
        try:
            async with kc_client:
                internal_id = await kc_client.create_client(
                    client_id=client_id,
                    name=payload.name,
                    tenant_id=str(tenant_id),
                    owner_sub=owner,
                )
        except KeycloakClientConflictError as exc:
            raise AgentPrincipalExistsError(payload.name) from exc

        # Phase 2: insert the DB row. If anything fails after the Keycloak
        # client was created, delete that client before surfacing the error:
        # a created client with no MEHO row is an orphaned, token-issuing
        # identity that can never be listed or revoked through MEHO — exactly
        # the unreachable-kill-switch failure this lifecycle exists to prevent.
        row = AgentPrincipal(
            tenant_id=tenant_id,
            name=payload.name,
            keycloak_client_id=client_id,
            keycloak_internal_id=internal_id,
            owner_sub=owner,
            revoked=False,
            created_by_sub=created_by_sub,
        )
        sessionmaker = get_sessionmaker()
        try:
            async with sessionmaker() as session:
                session.add(row)
                try:
                    await session.flush()
                except IntegrityError as exc:
                    await session.rollback()
                    if _is_unique_violation(exc):
                        raise AgentPrincipalExistsError(payload.name) from exc
                    raise
                await session.refresh(row)
                entry = AgentPrincipalRead.model_validate(row)
                await session.commit()
        except BaseException as exc:
            await self._rollback_orphan_client(
                internal_id, tenant_id=tenant_id, name=payload.name, cause=exc
            )
            raise
        self._log.info(
            "agent_principal_register",
            tenant_id=str(tenant_id),
            name=payload.name,
            keycloak_client_id=client_id,
            created_by_sub=created_by_sub,
        )
        return entry

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

        Phase 1: mark ``revoked=true`` in the DB.
        Phase 2: disable the Keycloak client (``enabled=false``).

        The DB is written first so even a Keycloak failure leaves the
        principal marked revoked (no new tokens can be issued under an
        active MEHO principal). A Keycloak-not-found is logged and
        swallowed — the client may have been cleaned up out of band.

        Raises
        ------
        AgentPrincipalNotFoundError
            When no row matches ``(tenant_id, name)`` or the row is
            already revoked.
        KeycloakAdminNotConfiguredError
            When Keycloak admin credentials are not configured.
        KeycloakAdminError
            On a non-404 Keycloak Admin API failure.
        """
        sessionmaker = get_sessionmaker()
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
            row.revoked = True
            row.updated_at = datetime.now(UTC)
            await session.flush()
            await session.refresh(row)
            entry = AgentPrincipalRead.model_validate(row)
            keycloak_internal_id = row.keycloak_internal_id
            await session.commit()

        # Phase 2: disable Keycloak client (best-effort; 404 is swallowed).
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
        self._log.info(
            "agent_principal_revoke",
            tenant_id=str(tenant_id),
            name=name,
            keycloak_internal_id=keycloak_internal_id,
        )
        return entry
