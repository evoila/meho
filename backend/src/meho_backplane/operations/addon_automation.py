# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Paired automation surface — the read the MCP / CLI / console twins share (#3029).

Initiative #2900's paired-surface activation (Task #3029) exposes a paired
automation add-on across three fronts — the ``meho_automation_list`` meta-tool,
the ``meho automation list`` CLI verb, and the ``/ui/automation`` console panel.
All three answer the same question ("is an automation add-on paired and healthy,
and what surface does it advertise?") and must give the same answer, so the read
lives here once rather than three times.

It is a thin projection over the two planes #3025 / #3026 built: the pairing
lifecycle (:class:`~meho_backplane.operations.addon_pairing.AddonPairingService`)
for health / liveness, and the capability activation view
(:class:`~meho_backplane.operations.addon_capability.AddonCapabilityService`) for
the declared surfaces. A capability is included only while its pairing is
contract-healthy (the same ``is_contract_compatible`` gate every other activation
read applies), so an unpaired or contract-skewed add-on contributes nothing —
the surface deactivates without any row being deleted.

Narrow-waist note (CLAUDE.md postulate 5): the projected data is *advertised
surface* — capability ``(kind, name)`` rows — never a paired add-on's own entity
identifiers (blueprint names, workflow names), which stay data driven through the
add-on itself, never names on any MEHO surface.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict

from meho_backplane.operations.addon_capability import AddonCapabilityService
from meho_backplane.operations.addon_capability_schemas import CapabilityKind
from meho_backplane.operations.addon_pairing import AddonPairingService
from meho_backplane.operations.addon_pairing_contract import is_contract_compatible

__all__ = [
    "AUTOMATION_FAMILY",
    "AutomationProvider",
    "AutomationSurfaceEntry",
    "AutomationSurfaceResponse",
    "active_automation_surface",
]

#: The meta-tool family name a paired add-on advertises (a ``meta_tool_family``
#: capability) to activate the automation surface. Single source of truth for
#: the MCP gate (``required_addon_family``), the REST route, the CLI verb, and
#: the console panel. "automation" is the pairing / family name the
#: capability-plane tests already exercise as the exemplar add-on.
AUTOMATION_FAMILY: Final[str] = "automation"


class AutomationSurfaceEntry(BaseModel):
    """One advertised surface of a paired automation add-on."""

    model_config = ConfigDict(frozen=True)

    kind: CapabilityKind
    name: str
    display_label: str | None


class AutomationProvider(BaseModel):
    """A paired add-on advertising the automation family, with its live health.

    ``paired_at`` / ``last_seen_at`` are :class:`~datetime.datetime` so the
    console panel can render them relatively; the MCP wire (``model_dump(
    mode="json")``) and the REST response serialise them to ISO-8601 strings,
    matching the meta-tool's ``outputSchema``.
    """

    model_config = ConfigDict(frozen=True)

    addon: str
    contract_version: int
    contract_compatible: bool
    paired_at: datetime
    last_seen_at: datetime | None
    surfaces: list[AutomationSurfaceEntry]


class AutomationSurfaceResponse(BaseModel):
    """The tenant's active automation surface — zero or more providers."""

    model_config = ConfigDict(frozen=True)

    providers: list[AutomationProvider]


async def active_automation_surface(
    tenant_id: uuid.UUID,
    *,
    family: str = AUTOMATION_FAMILY,
) -> AutomationSurfaceResponse:
    """Return the paired add-on(s) advertising *family* and their advertised surface.

    Reads the tenant's active capabilities once, keeps only add-ons that
    advertise a ``meta_tool_family`` capability named *family* (the set the MCP
    surface gate keys on), and stamps each with its pairing health
    (``contract_compatible`` recomputed live against this backplane, mirroring
    the ``/ui/pairing`` panel) and its full declared surface. ``providers`` is
    empty when nothing is paired or every candidate pairing is
    contract-incompatible — the fail-closed, unpaired baseline every twin
    renders identically.
    """
    capability_service = AddonCapabilityService()
    active = await capability_service.active_capabilities(tenant_id)

    provider_addons = sorted(
        {
            cap.addon
            for cap in active
            if cap.kind is CapabilityKind.META_TOOL_FAMILY and cap.name == family
        }
    )
    surfaces_by_addon: dict[str, list[AutomationSurfaceEntry]] = {
        addon: [] for addon in provider_addons
    }
    for cap in active:
        if cap.addon in surfaces_by_addon:
            surfaces_by_addon[cap.addon].append(
                AutomationSurfaceEntry(
                    kind=cap.kind,
                    name=cap.name,
                    display_label=cap.display_label,
                )
            )

    pairing_service = AddonPairingService()
    providers: list[AutomationProvider] = []
    for addon in provider_addons:
        pairing = await pairing_service.get(tenant_id, addon)
        if pairing is None:
            # Raced with an unpair between the capability read and this lookup;
            # the add-on is no longer paired, so it drops off the surface.
            continue
        providers.append(
            AutomationProvider(
                addon=addon,
                contract_version=pairing.contract_version,
                contract_compatible=is_contract_compatible(
                    addon_contract_version=pairing.addon_contract_version,
                    addon_min_backplane_version=pairing.addon_min_backplane_version,
                ),
                paired_at=pairing.paired_at,
                last_seen_at=pairing.last_seen_at,
                surfaces=surfaces_by_addon[addon],
            )
        )

    return AutomationSurfaceResponse(providers=providers)
