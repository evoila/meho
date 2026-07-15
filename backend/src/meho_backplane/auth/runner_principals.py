# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Runner-principal lifecycle service — register / list / get / revoke.

Initiative #2415 (#2502) under Goal #221. The single code path the REST
routes (:mod:`meho_backplane.api.v1.runner_principals`) and the Go CLI
verbs (``meho runner-principal``) call through. Directly moulded on the
agent-principal lifecycle service (#815,
:mod:`meho_backplane.auth.agent_principals`) — same Keycloak-first
two-phase consistency, same orphan-client rollback, same revoke ordering
contract — but mints a **read-only, runner-kind** identity instead of an
agent-kind one.

Runner-specific carve-outs (vs the agent mould)
-----------------------------------------------

* **``keycloak_client_id`` convention** is ``runner:<name>`` (vs
  ``agent:<name>``) so runner clients are visually distinct in the
  Keycloak Admin Console and occupy their own client-id namespace.
* **``tenant_role`` stamped on the token is ``read_only``** (vs the
  agent's ``tenant_admin``). This is defence in depth: even if the
  negative route cage regressed, ``read_only`` (rank 0 in
  :data:`~meho_backplane.auth.rbac._ROLE_ORDER`) 403s every
  ``require_role(OPERATOR/TENANT_ADMIN)`` route. A runner gets no Vault
  credential reach of its own — assignment payloads carry credential
  scope (#2499).
* **``runner_id`` is generated up front** (``uuid.uuid4()`` before Phase 1)
  so the same value lands both in the Keycloak hardcoded-claim mapper
  (``extra_hardcoded_claims={"runner_id": ...}``) and as the DB row's
  explicit ``id``. The row is the canonical runner identity; the wire /
  route identity across the gateway set is the principal **name**, and
  :func:`~meho_backplane.auth.runner_guard.assert_runner_scope` binds the
  two.
* **``principal_kind=runner`` is a hardcoded Keycloak mapper**, not a
  caller-supplied claim — the unforgeable discriminator the cage keys on
  (lesson from #2489: kind-scoped enforcement must be fail-closed at
  token level).

Consistency strategy (identical to the agent mould): register creates the
Keycloak client then inserts the DB row (Keycloak failure -> no row; any
failure *after* the client is created — secret capture, Vault persist, or
the DB write — rolls the just-created client back before the error
propagates); revoke disables the Keycloak client (``enabled=false``)
*before* it commits ``revoked=true``, so MEHO never reports a still-live,
token-issuing runner as revoked.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Final

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
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunnerPrincipal
from meho_backplane.scheduler.vault_credentials import (
    SchedulerVaultNotConfiguredError,
    write_agent_secret,
)
from meho_backplane.settings import get_settings

__all__ = [
    "NAME_MAX_LENGTH",
    "RunnerPrincipalCreate",
    "RunnerPrincipalExistsError",
    "RunnerPrincipalNotFoundError",
    "RunnerPrincipalRead",
    "RunnerPrincipalService",
]

#: Regex for the runner name: letters, digits, hyphen, underscore, dot.
#: Mirrors the agent-principal name pattern.
_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_\-\.]+$")

#: Maximum length of a runner-principal name. Shared by the intake schema
#: (:class:`RunnerPrincipalCreate`) and the REST by-name lookup ``Path``
#: params in :mod:`meho_backplane.api.v1.runner_principals`. The #2501 kill
#: switch keys on the name, and show / revoke bound the name at this length
#: (``Path(max_length=...)`` -> 422); without the same cap on intake a name
#: past this length would register (201) yet be un-showable and un-revocable
#: by name — an orphaned kill switch. Sourcing both bounds from one constant
#: is what stops the intake and lookup limits from drifting apart.
NAME_MAX_LENGTH: Final[int] = 128

#: Convention: the Keycloak clientId for a runner principal.
_CLIENT_ID_PREFIX: str = "runner:"

#: ``tenant_role`` stamped into the runner client's access token. Read-only
#: credential scope in v1 (vs the agent's ``tenant_admin``): a runner is a
#: dumb executor that fetches its own assignment and submits its own
#: results; ``read_only`` (rank 0) is the defence-in-depth backstop behind
#: the negative route cage.
_RUNNER_TENANT_ROLE: str = TenantRole.READ_ONLY.value


def _keycloak_client_id(name: str) -> str:
    """Return the canonical Keycloak clientId for runner *name*."""
    return f"{_CLIENT_ID_PREFIX}{name}"


def _is_unique_violation(exc: IntegrityError) -> bool:
    """Return whether *exc* is a unique-constraint violation."""
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return sqlstate == "23505" or "UNIQUE constraint failed" in str(orig or exc)


class RunnerPrincipalExistsError(Exception):
    """Raised when register collides with an existing (tenant_id, name)."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"runner principal {name!r} already exists for this tenant")


class RunnerPrincipalNotFoundError(Exception):
    """Raised when get/revoke finds no matching row."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"runner principal {name!r} not found")


class RunnerPrincipalCreate(BaseModel):
    """Input shape for :meth:`RunnerPrincipalService.register`."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=NAME_MAX_LENGTH)
    owner_sub: str | None = None


class RunnerPrincipalRead(BaseModel):
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


class RunnerPrincipalService:
    """Tenant-scoped register / list / get / revoke for runner principals.

    Stateless; instantiate once per request and call freely.
    """

    def __init__(self) -> None:
        self._log = structlog.get_logger()

    async def register(
        self,
        tenant_id: uuid.UUID,
        created_by_sub: str,
        payload: RunnerPrincipalCreate,
    ) -> RunnerPrincipalRead:
        """Register a new runner principal.

        Generates the runner id up front, creates the Keycloak client
        (``kind=runner``, ``principal_kind=runner``, ``tenant_role=read_only``,
        hardcoded ``runner_id=<generated id>``), captures its generated
        secret and persists it to Vault, then inserts the DB row with an
        explicit ``id`` equal to the generated runner id. If any step fails
        the Keycloak client is rolled back and the DB row is never written.

        Raises
        ------
        ValueError
            When *name* contains characters outside the safe alphabet.
        RunnerPrincipalExistsError
            Duplicate ``(tenant_id, name)`` (DB unique-index or Keycloak 409).
        KeycloakAdminNotConfiguredError / KeycloakAdminError
            Keycloak admin unconfigured / any Admin API failure.
        SchedulerVaultBrokerError
            Vault configured but the secret write failed.
        """
        if not _NAME_PATTERN.fullmatch(payload.name):
            raise ValueError(
                f"runner principal name {payload.name!r} contains characters "
                "outside the safe set (allowed: letters, digits, hyphen, "
                "underscore, dot)"
            )
        owner = payload.owner_sub or created_by_sub
        client_id = _keycloak_client_id(payload.name)
        audience = get_settings().keycloak_audience
        # Generate the runner id BEFORE Phase 1 so the same value lands in
        # the Keycloak hardcoded-claim mapper and as the row's explicit id
        # (token claim == row id is the guard's binding invariant).
        runner_id = uuid.uuid4()

        # Phase 1: create the Keycloak client + capture its generated secret.
        # Any failure after create_client rolls the just-created client back
        # (see _provision_keycloak_client) so register never orphans an
        # un-revocable identity before any DB row exists.
        internal_id, client_secret = await self._provision_keycloak_client(
            name=payload.name,
            tenant_id=tenant_id,
            owner_sub=owner,
            audience=audience,
            runner_id=runner_id,
        )

        # Phase 1b: persist the captured secret to Vault under the runner
        # client-id. Same posture as agents: an unset scheduler token is a
        # skip-with-warning (env-var fallback); a configured-but-failed
        # write rolls back the Keycloak client and surfaces the error.
        await self._persist_secret_to_vault(
            client_id,
            client_secret,
            internal_id=internal_id,
            tenant_id=tenant_id,
            name=payload.name,
        )

        # Phase 2: insert the DB row with the explicit generated id (rolls
        # back the Keycloak client on any failure — see _insert_row).
        row = RunnerPrincipal(
            id=runner_id,
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
            "runner_principal_register",
            tenant_id=str(tenant_id),
            name=payload.name,
            keycloak_client_id=client_id,
            runner_id=str(runner_id),
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
        runner_id: uuid.UUID,
    ) -> tuple[str, str]:
        """Create the runner's Keycloak client and read back its secret.

        Phase 1 of :meth:`register`, isolated so its rollback contract is a
        single unit. Creates the confidential client (clientId
        ``runner:<name>``) with the runner-kind mapper set
        (``principal_kind=runner``, ``tenant_role=read_only``, hardcoded
        ``runner_id``) so its ``client_credentials`` token validates through
        the JWT chain as a caged, read-only runner, then reads back the
        generated secret in the same admin session.

        Rollback contract: if *anything* after ``create_client`` raises — most
        importantly ``get_client_secret`` — the just-created, live client is
        deleted before the error propagates, so register never orphans an
        un-revocable identity. ``internal_id`` is still ``None`` when
        ``create_client`` itself failed (nothing created, nothing to roll
        back). A 409 conflict surfaces as :class:`RunnerPrincipalExistsError`
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
                    tenant_role=_RUNNER_TENANT_ROLE,
                    principal_kind="runner",
                    kind_attribute="runner",
                    extra_hardcoded_claims={"runner_id": str(runner_id)},
                )
                client_secret = await kc_client.get_client_secret(internal_id)
        except KeycloakClientConflictError as exc:
            raise RunnerPrincipalExistsError(name) from exc
        except BaseException as exc:
            if internal_id is not None:
                await self._rollback_orphan_client(
                    internal_id, tenant_id=tenant_id, name=name, cause=exc
                )
            raise
        return internal_id, client_secret

    async def _insert_row(
        self,
        row: RunnerPrincipal,
        *,
        internal_id: str,
        name: str,
    ) -> RunnerPrincipalRead:
        """Insert the runner-principal DB row; roll back the KC client on failure.

        If anything fails after the Keycloak client was created, delete
        that client before surfacing the error: a created client with no
        MEHO row is an orphaned, token-issuing identity that can never be
        listed or revoked through MEHO.
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
                        raise RunnerPrincipalExistsError(name) from exc
                    raise
                await session.refresh(row)
                entry = RunnerPrincipalRead.model_validate(row)
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
        """Persist the captured Keycloak secret to Vault (mould #1478).

        Two failure postures, identical to the agent path:

        * **Vault not configured** (``VAULT_SCHEDULER_TOKEN`` unset) — skip
          the write with a WARN and continue (env-var fallback).
        * **Vault configured but the write failed** — roll back the
          just-created Keycloak client and surface the error (fail closed,
          same posture as a Phase-2 DB failure).
        """
        try:
            await write_agent_secret(client_id, client_secret)
        except SchedulerVaultNotConfiguredError:
            self._log.warning(
                "runner_principal_register_vault_skip",
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

        Called only after :meth:`create_client` succeeded but a later phase
        raised. A cleanup failure is logged (the orphan needs manual
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
                "runner_principal_register_orphan_cleanup_failed",
                tenant_id=str(tenant_id),
                name=name,
                keycloak_internal_id=internal_id,
                cause=type(cause).__name__,
                error=type(cleanup_exc).__name__,
            )
            return
        self._log.warning(
            "runner_principal_register_rolled_back_keycloak_client",
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
    ) -> list[RunnerPrincipalRead]:
        """Return runner principals for *tenant_id*, name-sorted.

        Revoked principals are excluded by default; ``include_revoked=True``
        is reserved for audit inspection.
        """
        if limit < 0:
            raise ValueError(f"limit must be >= 0; got {limit}")
        if offset < 0:
            raise ValueError(f"offset must be >= 0; got {offset}")
        if limit == 0:
            return []
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            q = select(RunnerPrincipal).where(RunnerPrincipal.tenant_id == tenant_id)
            if not include_revoked:
                q = q.where(RunnerPrincipal.revoked.is_(False))
            q = q.order_by(RunnerPrincipal.name).limit(limit).offset(offset)
            result = await session.execute(q)
            rows = result.scalars().all()
        return [RunnerPrincipalRead.model_validate(row) for row in rows]

    async def get(
        self,
        tenant_id: uuid.UUID,
        name: str,
    ) -> RunnerPrincipalRead | None:
        """Fetch one principal by ``(tenant_id, name)``; ``None`` if absent."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(RunnerPrincipal).where(
                    RunnerPrincipal.tenant_id == tenant_id,
                    RunnerPrincipal.name == name,
                )
            )
            row = result.scalar_one_or_none()
        if row is None:
            return None
        return RunnerPrincipalRead.model_validate(row)

    async def revoke(
        self,
        tenant_id: uuid.UUID,
        name: str,
    ) -> RunnerPrincipalRead:
        """Revoke a runner principal (kill switch).

        Phase 1: look up the principal (must exist and not already be
        revoked) to obtain its ``keycloak_internal_id``.
        Phase 2: disable the Keycloak client (``enabled=false``) — the
        authoritative kill switch.
        Phase 3: commit ``revoked=true`` in the DB.

        Keycloak is disabled *before* the DB row is marked so a Keycloak
        failure aborts the revoke without falsely reporting a runner as
        revoked while it can still mint tokens. A Keycloak *not-found* is
        treated as success and the DB row is still marked revoked.

        Raises
        ------
        RunnerPrincipalNotFoundError
            When no row matches ``(tenant_id, name)`` or the row is already
            revoked.
        KeycloakAdminNotConfiguredError
            When Keycloak admin credentials are not configured.
        KeycloakAdminError
            On a non-404 Keycloak Admin API failure — the DB row is left
            unchanged, so the runner stays active and the operator can retry.
        """
        sessionmaker = get_sessionmaker()
        # Phase 1: validate + fetch the Keycloak internal id (read-only).
        async with sessionmaker() as session:
            result = await session.execute(
                select(RunnerPrincipal).where(
                    RunnerPrincipal.tenant_id == tenant_id,
                    RunnerPrincipal.name == name,
                    RunnerPrincipal.revoked.is_(False),
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise RunnerPrincipalNotFoundError(name)
            keycloak_internal_id = row.keycloak_internal_id

        # Phase 2: disable the Keycloak client FIRST (authoritative kill
        # switch). A non-404 failure propagates before any DB write, so a
        # runner is never marked revoked while it can still mint tokens.
        kc_client = KeycloakAdminClient.from_settings()
        try:
            async with kc_client:
                await kc_client.disable_client(keycloak_internal_id)
        except KeycloakClientNotFoundError:
            self._log.warning(
                "runner_principal_revoke_keycloak_not_found",
                tenant_id=str(tenant_id),
                name=name,
                keycloak_internal_id=keycloak_internal_id,
            )

        # Phase 3: persist revoked=true now that the client is disabled.
        async with sessionmaker() as session:
            result = await session.execute(
                select(RunnerPrincipal).where(
                    RunnerPrincipal.tenant_id == tenant_id,
                    RunnerPrincipal.name == name,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                # Raced deletion between Phase 1 and Phase 3.
                raise RunnerPrincipalNotFoundError(name)
            if not row.revoked:
                row.revoked = True
                row.updated_at = datetime.now(UTC)
                await session.flush()
            await session.refresh(row)
            entry = RunnerPrincipalRead.model_validate(row)
            await session.commit()

        self._log.info(
            "runner_principal_revoke",
            tenant_id=str(tenant_id),
            name=name,
            keycloak_internal_id=keycloak_internal_id,
        )
        return entry
