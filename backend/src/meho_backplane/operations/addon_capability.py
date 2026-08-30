# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add-on capability advertisement service — declare / read / activate (#3026).

The single code path the REST routes
(:mod:`meho_backplane.api.v1.addon_capability`) call through to persist an
add-on's advertised surfaces and to read back what is *active*. Built on the
#3025 pairing (Initiative #2900): a paired add-on declares the surfaces it
contributes — meta-tool families, CLI verb families, console panels, event
kinds — against the integration-contract version it negotiated, and the
backplane activates them only while the pairing is present **and**
contract-healthy.

Design
------

* **Stateless and method-scoped** — each method opens its own DB session,
  commits, and closes, mirroring
  :class:`~meho_backplane.operations.addon_pairing.AddonPairingService`.
* **Replace-all declaration** — :meth:`declare` deletes the pairing's prior
  capability rows and inserts the new set in one transaction, so a dropped
  capability leaves no residue. The whole operation runs in a single session
  bound to the pairing row it resolved, so an unpair racing the declare
  surfaces as a clean failure rather than a half-written set.
* **Activation is derived, never stored** — a capability is active only while
  its pairing satisfies
  :func:`~meho_backplane.operations.addon_pairing_contract.is_contract_compatible`.
  :meth:`active_capabilities` is the tenant-wide activation view downstream
  surfaces (event push, console) read; per-add-on reads carry an ``active``
  flag computed the same way. No surface is written twice as health flips.
* **Cleanup is the pairing's cascade** — this service never deletes on
  unpair; the ``addon_capability.pairing_id`` ``ON DELETE CASCADE`` FK does
  (unpair hard-deletes the pairing row), so the foundation carries no
  dependency on this feature.

Error contract
--------------

* :class:`~meho_backplane.operations.addon_pairing.AddonNotPairedError` —
  declare / read targeted an add-on with no active pairing.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AddonCapability, AddonPairing
from meho_backplane.operations.addon_capability_schemas import (
    ActiveCapabilityRead,
    CapabilityDeclarationResponse,
    CapabilityKind,
    CapabilityRead,
    DeclareCapabilitiesRequest,
)
from meho_backplane.operations.addon_pairing import AddonNotPairedError
from meho_backplane.operations.addon_pairing_contract import is_contract_compatible

__all__ = ["AddonCapabilityService"]


def _is_healthy(pairing: AddonPairing) -> bool:
    """Return whether *pairing* is contract-healthy against the current build."""
    return is_contract_compatible(
        addon_contract_version=pairing.addon_contract_version,
        addon_min_backplane_version=pairing.addon_min_backplane_version,
    )


class AddonCapabilityService:
    """Declare / read / activate an add-on's advertised surfaces.

    Stateless; instantiate once per request and call freely.
    """

    def __init__(self) -> None:
        self._log = structlog.get_logger()

    async def declare(
        self,
        tenant_id: uuid.UUID,
        addon_name: str,
        request: DeclareCapabilitiesRequest,
    ) -> CapabilityDeclarationResponse:
        """Persist an add-on's complete surface set (replace-all).

        Resolves the pairing, deletes its prior capability rows, and inserts
        the declared set stamped with the pairing's negotiated contract
        version — all in one transaction. Returns the persisted declaration
        with its live ``active`` state.

        Raises
        ------
        AddonNotPairedError
            When no pairing matches ``(tenant_id, addon_name)``.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            pairing = (
                await session.execute(
                    select(AddonPairing).where(
                        AddonPairing.tenant_id == tenant_id,
                        AddonPairing.name == addon_name,
                    )
                )
            ).scalar_one_or_none()
            if pairing is None:
                raise AddonNotPairedError(addon_name)

            await session.execute(
                delete(AddonCapability).where(AddonCapability.pairing_id == pairing.id)
            )
            for cap in request.capabilities:
                session.add(
                    AddonCapability(
                        pairing_id=pairing.id,
                        kind=cap.kind.value,
                        name=cap.name,
                        display_label=cap.display_label,
                        declared_contract_version=pairing.contract_version,
                    )
                )
            await session.flush()
            rows = await self._pairing_capabilities(session, pairing.id)
            response = self._build_declaration(pairing, rows)
            await session.commit()

        self._log.info(
            "addon_capabilities_declared",
            tenant_id=str(tenant_id),
            addon=addon_name,
            count=len(response.capabilities),
            declared_contract_version=response.declared_contract_version,
            active=response.active,
        )
        return response

    async def list_declared(
        self,
        tenant_id: uuid.UUID,
        addon_name: str,
    ) -> CapabilityDeclarationResponse | None:
        """Return the add-on's declared surfaces + live activation state.

        ``None`` when no pairing matches ``(tenant_id, addon_name)`` (the
        route maps that to 404). A paired add-on that has declared nothing
        yet returns an empty ``capabilities`` list — paired, no surfaces.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            pairing = (
                await session.execute(
                    select(AddonPairing).where(
                        AddonPairing.tenant_id == tenant_id,
                        AddonPairing.name == addon_name,
                    )
                )
            ).scalar_one_or_none()
            if pairing is None:
                return None
            rows = await self._pairing_capabilities(session, pairing.id)
            return self._build_declaration(pairing, rows)

    async def active_capabilities(
        self,
        tenant_id: uuid.UUID,
        *,
        kind: CapabilityKind | None = None,
    ) -> list[ActiveCapabilityRead]:
        """Return the tenant's *active* capabilities — paired and healthy only.

        A capability is active only while its pairing is contract-healthy; an
        unhealthy (contract-skewed) pairing contributes nothing here, so the
        activation view flips with health without any surface being deleted.
        Optionally scoped to one :class:`CapabilityKind` (the shape downstream
        surfaces use — e.g. "which add-ons want ``event_kind`` X").
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            pairings = (
                (
                    await session.execute(
                        select(AddonPairing).where(AddonPairing.tenant_id == tenant_id)
                    )
                )
                .scalars()
                .all()
            )
            healthy: dict[uuid.UUID, str] = {
                pairing.id: pairing.name for pairing in pairings if _is_healthy(pairing)
            }
            if not healthy:
                return []

            query = select(AddonCapability).where(AddonCapability.pairing_id.in_(healthy.keys()))
            if kind is not None:
                query = query.where(AddonCapability.kind == kind.value)
            rows = (await session.execute(query)).scalars().all()

        active = [
            ActiveCapabilityRead(
                addon=healthy[row.pairing_id],
                kind=CapabilityKind(row.kind),
                name=row.name,
                display_label=row.display_label,
            )
            for row in rows
        ]
        active.sort(key=lambda c: (c.addon, c.kind.value, c.name))
        return active

    @staticmethod
    async def _pairing_capabilities(
        session: AsyncSession, pairing_id: uuid.UUID
    ) -> list[AddonCapability]:
        """Return a pairing's capability rows, kind- then name-sorted."""
        result = await session.execute(
            select(AddonCapability)
            .where(AddonCapability.pairing_id == pairing_id)
            .order_by(AddonCapability.kind, AddonCapability.name)
        )
        return list(result.scalars().all())

    @staticmethod
    def _build_declaration(
        pairing: AddonPairing,
        rows: list[AddonCapability],
    ) -> CapabilityDeclarationResponse:
        """Assemble the per-add-on declaration response from persisted rows."""
        declared_version = rows[0].declared_contract_version if rows else pairing.contract_version
        return CapabilityDeclarationResponse(
            addon=pairing.name,
            declared_contract_version=declared_version,
            active=_is_healthy(pairing),
            capabilities=[CapabilityRead.model_validate(row) for row in rows],
        )
