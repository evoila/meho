# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add-on pairing lifecycle service — pair / unpair / list / heartbeat (#3025).

The single code path the REST routes
(:mod:`meho_backplane.api.v1.addon_pairing`) and the operator console call
through. Foundation of Initiative #2900: it turns a sibling add-on product
into a first-class paired peer of the backplane — a Keycloak
client-credentials **service** principal bound to a negotiated
integration-contract version — while keeping an unpaired backplane
byte-identical to a never-paired one.

Design
------

* **Stateless and method-scoped** — each method opens its own DB session,
  commits, and closes. Same shape as
  :class:`~meho_backplane.auth.agent_principals.AgentPrincipalService` and
  :class:`~meho_backplane.operations.service_grants.ServicePrincipalGrantService`.
* **Keycloak-first on both paths** (Keycloak has no XA participant, so the
  DB commit and the Keycloak call are not one ACID unit). ``pair`` creates
  the Keycloak client, then inserts the row (a DB failure rolls the
  just-created client back); ``unpair`` deletes the Keycloak client first —
  the authoritative kill switch — then deletes the row, so the backplane
  never reports an add-on as unpaired while it can still mint tokens.
* **Scoped principal — no blanket admin.** The client is minted with
  ``principal_kind=service`` and ``tenant_role=read_only``; the non-agent
  policy gate parks every mutating op for a service principal by default, so
  a paired add-on starts with zero standing write authority. Operators widen
  it per-op via ``ServicePrincipalGrant`` — never here.
* **``keycloak_client_id`` convention** — ``addon:<name>`` (forward-slash
  forbidden in Keycloak client ids), keeping add-on clients visually
  distinct from agent / runner / user clients in the Admin Console.
* **RBAC not enforced here** — the route / console layers gate the surface;
  this service assumes that check has already run.

Error contract
--------------

* :class:`AddonAlreadyPairedError` — pair collided with an existing
  ``(tenant_id, name)`` or ``keycloak_client_id``.
* :class:`AddonNotPairedError` — heartbeat on an absent pairing (unpair
  reports absence via a ``False`` return, not this error).
* :class:`~meho_backplane.operations.addon_pairing_contract.ContractSkewError`
  — the contract versions cannot pair (both directions pinned).
* :class:`~meho_backplane.auth.keycloak_admin.KeycloakAdminNotConfiguredError`
  / :class:`~meho_backplane.auth.keycloak_admin.KeycloakAdminError`
  — Keycloak admin unconfigured (503 at the boundary) / any other Keycloak
  Admin API failure (502 at the boundary).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from meho_backplane.auth.keycloak_admin import (
    KeycloakAdminClient,
    KeycloakClientConflictError,
    KeycloakClientNotFoundError,
)
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AddonPairing
from meho_backplane.operations.addon_pairing_contract import negotiate
from meho_backplane.operations.addon_pairing_schemas import (
    PairAddonRequest,
    PairAddonResult,
    PairedAddonRead,
)
from meho_backplane.settings import get_settings

__all__ = [
    "AddonAlreadyPairedError",
    "AddonNotPairedError",
    "AddonPairingService",
]

#: Add-on name alphabet: letters, digits, hyphen, underscore, dot. Mirrors
#: the agent / runner principal name discipline.
_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_\-\.]+$")

#: Convention: the Keycloak clientId for a paired add-on.
_CLIENT_ID_PREFIX: str = "addon:"

#: ``tenant_role`` stamped into the add-on client's access token. Read-only
#: — the add-on carries no coarse write authority; the non-agent service
#: gate parks mutating ops until an operator issues a scoped grant.
_ADDON_TENANT_ROLE: str = TenantRole.READ_ONLY.value


def _keycloak_client_id(name: str) -> str:
    """Return the canonical Keycloak clientId for add-on *name*."""
    return f"{_CLIENT_ID_PREFIX}{name}"


def _is_unique_violation(exc: IntegrityError) -> bool:
    """Return whether *exc* is a unique-constraint violation."""
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return sqlstate == "23505" or "UNIQUE constraint failed" in str(orig or exc)


class AddonAlreadyPairedError(Exception):
    """Raised when pair collides with an existing (tenant_id, name)."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"add-on {name!r} is already paired for this tenant")


class AddonNotPairedError(Exception):
    """Raised when a heartbeat targets an add-on with no active pairing."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"add-on {name!r} is not paired")


class AddonPairingService:
    """Tenant-scoped pair / unpair / list / heartbeat for add-on pairings.

    Stateless; instantiate once per request and call freely.
    """

    def __init__(self) -> None:
        self._log = structlog.get_logger()

    async def pair(
        self,
        tenant_id: uuid.UUID,
        created_by_sub: str,
        payload: PairAddonRequest,
    ) -> PairAddonResult:
        """Pair an add-on: negotiate the contract, mint the principal, persist.

        Negotiation runs first (cheap, side-effect-free) so a contract-skew
        rejection never provisions a Keycloak client. On success a
        confidential ``kind=service`` client is created and its generated
        secret captured, then the pairing row is inserted; any failure after
        the client is created rolls it back so pairing never orphans an
        un-revocable identity.

        Returns the one-time :class:`PairAddonResult` carrying the freshly
        minted ``client_secret`` — the add-on's only chance to read it.

        Raises
        ------
        ValueError
            When *name* contains characters outside the safe alphabet.
        ContractSkewError
            When the contract versions cannot pair (both directions pinned).
        AddonAlreadyPairedError
            Duplicate ``(tenant_id, name)`` (DB unique-index or Keycloak 409).
        KeycloakAdminNotConfiguredError / KeycloakAdminError
            Keycloak admin unconfigured / any Admin API failure.
        """
        if not _NAME_PATTERN.fullmatch(payload.name):
            raise ValueError(
                f"add-on name {payload.name!r} contains characters outside the "
                "safe set (allowed: letters, digits, hyphen, underscore, dot)"
            )
        negotiated = negotiate(
            addon_contract_version=payload.addon_contract_version,
            addon_min_backplane_version=payload.addon_min_backplane_version,
        )
        owner = payload.owner_sub or created_by_sub
        client_id = _keycloak_client_id(payload.name)
        audience = get_settings().keycloak_audience

        internal_id, client_secret = await self._provision_keycloak_client(
            name=payload.name,
            tenant_id=tenant_id,
            owner_sub=owner,
            audience=audience,
        )

        row = AddonPairing(
            tenant_id=tenant_id,
            name=payload.name,
            keycloak_client_id=client_id,
            keycloak_internal_id=internal_id,
            owner_sub=owner,
            contract_version=negotiated.negotiated_version,
            addon_contract_version=payload.addon_contract_version,
            addon_min_backplane_version=payload.addon_min_backplane_version,
            created_by_sub=created_by_sub,
        )
        entry = await self._insert_row(row, internal_id=internal_id, name=payload.name)
        self._log.info(
            "addon_pair",
            tenant_id=str(tenant_id),
            name=payload.name,
            keycloak_client_id=client_id,
            contract_version=negotiated.negotiated_version,
            created_by_sub=created_by_sub,
        )
        return PairAddonResult(
            pairing=entry,
            client_id=client_id,
            client_secret=client_secret,
            backplane_contract_version=negotiated.backplane_contract_version,
            negotiated_contract_version=negotiated.negotiated_version,
        )

    async def _provision_keycloak_client(
        self,
        *,
        name: str,
        tenant_id: uuid.UUID,
        owner_sub: str,
        audience: str,
    ) -> tuple[str, str]:
        """Create the add-on's ``kind=service`` client and read back its secret.

        Isolated so its rollback contract is one unit: if anything after
        ``create_client`` raises — most importantly ``get_client_secret`` —
        the just-created live client is deleted before the error propagates.
        A 409 conflict surfaces as :class:`AddonAlreadyPairedError` (the
        conflicting client belongs to a prior pairing and is not ours to
        delete).

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
                    tenant_role=_ADDON_TENANT_ROLE,
                    principal_kind="service",
                    kind_attribute="service",
                )
                client_secret = await kc_client.get_client_secret(internal_id)
        except KeycloakClientConflictError as exc:
            raise AddonAlreadyPairedError(name) from exc
        except BaseException as exc:
            if internal_id is not None:
                await self._rollback_orphan_client(
                    internal_id, tenant_id=tenant_id, name=name, cause=exc
                )
            raise
        return internal_id, client_secret

    async def _insert_row(
        self,
        row: AddonPairing,
        *,
        internal_id: str,
        name: str,
    ) -> PairedAddonRead:
        """Insert the pairing row; roll back the Keycloak client on failure.

        A created client with no MEHO row is an orphaned, token-issuing
        identity that can never be listed or unpaired through MEHO — the
        unreachable-kill-switch failure this lifecycle exists to prevent.
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
                        raise AddonAlreadyPairedError(name) from exc
                    raise
                await session.refresh(row)
                entry = PairedAddonRead.model_validate(row)
                await session.commit()
        except BaseException as exc:
            await self._rollback_orphan_client(
                internal_id, tenant_id=row.tenant_id, name=name, cause=exc
            )
            raise
        return entry

    async def _rollback_orphan_client(
        self,
        internal_id: str,
        *,
        tenant_id: uuid.UUID,
        name: str,
        cause: BaseException,
    ) -> None:
        """Best-effort delete of a Keycloak client whose row failed to write.

        A cleanup failure is logged (the orphan needs manual removal) but
        never masks the original *cause*, which the caller re-raises.
        """
        try:
            kc_client = KeycloakAdminClient.from_settings()
            async with kc_client:
                await kc_client.delete_client(internal_id)
        except KeycloakClientNotFoundError:
            return
        except Exception as cleanup_exc:
            self._log.error(
                "addon_pair_orphan_cleanup_failed",
                tenant_id=str(tenant_id),
                name=name,
                keycloak_internal_id=internal_id,
                cause=type(cause).__name__,
                error=type(cleanup_exc).__name__,
            )
            return
        self._log.warning(
            "addon_pair_rolled_back_keycloak_client",
            tenant_id=str(tenant_id),
            name=name,
            keycloak_internal_id=internal_id,
            cause=type(cause).__name__,
        )

    async def unpair(
        self,
        tenant_id: uuid.UUID,
        name: str,
    ) -> bool:
        """Unpair an add-on: delete the Keycloak client, then the row.

        Reversible: the row is hard-deleted (no soft-kept residue), so the
        add-on can pair again cleanly and an unpaired backplane is
        byte-identical to a never-paired one; the append-only ``audit_log``
        retains the history.

        Keycloak is deleted *before* the row so the backplane never reports
        an add-on unpaired while it can still mint tokens. A Keycloak
        *not-found* is treated as success (the client was cleaned up out of
        band). Returns ``True`` on success, ``False`` when no pairing matches
        ``(tenant_id, name)``.

        Raises
        ------
        KeycloakAdminNotConfiguredError
            When Keycloak admin credentials are not configured.
        KeycloakAdminError
            On a non-404 Keycloak Admin API failure — the row is left in
            place, so the pairing stays active and the operator can retry.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(AddonPairing).where(
                    AddonPairing.tenant_id == tenant_id,
                    AddonPairing.name == name,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return False
            keycloak_internal_id = row.keycloak_internal_id
            row_id = row.id

        kc_client = KeycloakAdminClient.from_settings()
        try:
            async with kc_client:
                await kc_client.delete_client(keycloak_internal_id)
        except KeycloakClientNotFoundError:
            self._log.warning(
                "addon_unpair_keycloak_not_found",
                tenant_id=str(tenant_id),
                name=name,
                keycloak_internal_id=keycloak_internal_id,
            )

        async with sessionmaker() as session:
            await session.execute(delete(AddonPairing).where(AddonPairing.id == row_id))
            await session.commit()

        self._log.info(
            "addon_unpair",
            tenant_id=str(tenant_id),
            name=name,
            keycloak_internal_id=keycloak_internal_id,
        )
        return True

    async def list_(
        self,
        tenant_id: uuid.UUID,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PairedAddonRead]:
        """Return active pairings for *tenant_id*, name-sorted."""
        if limit < 0:
            raise ValueError(f"limit must be >= 0; got {limit}")
        if offset < 0:
            raise ValueError(f"offset must be >= 0; got {offset}")
        if limit == 0:
            return []
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            q = (
                select(AddonPairing)
                .where(AddonPairing.tenant_id == tenant_id)
                .order_by(AddonPairing.name)
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(q)
            rows = result.scalars().all()
        return [PairedAddonRead.model_validate(row) for row in rows]

    async def get(
        self,
        tenant_id: uuid.UUID,
        name: str,
    ) -> PairedAddonRead | None:
        """Fetch one pairing by ``(tenant_id, name)``; ``None`` if absent."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(AddonPairing).where(
                    AddonPairing.tenant_id == tenant_id,
                    AddonPairing.name == name,
                )
            )
            row = result.scalar_one_or_none()
        if row is None:
            return None
        return PairedAddonRead.model_validate(row)

    async def get_by_client_id(
        self,
        keycloak_client_id: str,
    ) -> PairedAddonRead | None:
        """Fetch one live pairing by its Keycloak ``clientId``; ``None`` if absent.

        The clientId is globally unique (Keycloak has no per-tenant clientId
        namespace; ``addon_pairing_keycloak_client_id_idx``), so this needs no
        tenant scope — the returned row *carries* its ``tenant_id`` for the
        caller to cross-check against the request's tenant. This is the lookup
        the #3028 add-on parent-linkage seam uses to answer "is this dispatch's
        service principal a paired add-on, and which pairing?".
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(AddonPairing).where(
                    AddonPairing.keycloak_client_id == keycloak_client_id,
                )
            )
            row = result.scalar_one_or_none()
        if row is None:
            return None
        return PairedAddonRead.model_validate(row)

    async def heartbeat(
        self,
        tenant_id: uuid.UUID,
        name: str,
    ) -> PairedAddonRead:
        """Stamp the add-on's liveness ``last_seen_at`` to now.

        Called by the paired add-on itself (authenticating as its service
        principal). Raises :class:`AddonNotPairedError` when no pairing
        matches ``(tenant_id, name)``.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(AddonPairing).where(
                    AddonPairing.tenant_id == tenant_id,
                    AddonPairing.name == name,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise AddonNotPairedError(name)
            row.last_seen_at = datetime.now(UTC)
            row.updated_at = datetime.now(UTC)
            await session.flush()
            await session.refresh(row)
            entry = PairedAddonRead.model_validate(row)
            await session.commit()
        return entry
