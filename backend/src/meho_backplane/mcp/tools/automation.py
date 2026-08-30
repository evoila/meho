# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho_automation_list`` — the paired-surface automation family (Task #3029).

The first tool of the ``automation`` meta-tool family, and the first surface
gated on **add-on pairing state** rather than a static tenant capability. It is
the automation analogue of ``meho_connector_list`` (``mcp/tools/connector_admin``
neighbour): where ``meho_connector_list`` answers "what managed systems can I
act on?", this answers "is the paired automation add-on live, and what surface
does it advertise?".

Narrow-waist discipline (CLAUDE.md postulate 5)
===============================================

The family is **small, stable, first-party** — the knowledge / docs precedent.
A paired add-on's own entity identifiers (blueprint names, workflow names) are
**data** carried in this tool's *results*, never tool names on the agent waist.
There is no per-blueprint / per-workflow tool anywhere; the whole automation
surface is this one meta-tool (and its CLI / console twins), which is exactly
what activates and deactivates with pairing.

The pairing gate (Initiative #2900, Task #3029)
===============================================

The tool declares ``required_addon_family="automation"``. The registry +
dispatcher filter it out of ``tools/list`` (true absence) and reject a direct
``tools/call`` (403-class, handler never runs) unless a **paired,
contract-healthy** add-on advertises a ``meta_tool_family`` capability named
``automation`` for the caller's tenant
(:func:`~meho_backplane.mcp.registry.addon_family_active`, resolved from
:meth:`~meho_backplane.operations.addon_capability.AddonCapabilityService.active_meta_tool_families`).
Unlike ``required_capability`` (a static tenant JWT claim), the gate tracks
**live pairing health**: the family disappears the moment the add-on unpairs or
drifts contract-incompatible, and reappears when health recovers, with no tool
re-registration. An unpaired backplane never lists it — its ``tools/list`` stays
byte-identical to a build that never carried the family.

Because the gate has already proven the automation add-on active by the time the
handler runs, the handler reports what that add-on advertises: its pairing health
and its declared surfaces (meta-tool families, CLI verb families, console panels,
event kinds), read from the same activation plane the gate consulted.

Audit
=====

The dispatcher writes one ``audit_log`` row per ``tools/call``; the handler binds
``audit_op_id="meho.automation.list"`` (``op_class="read"``) so a ``query_audit``
filter on ``op_id="meho.automation.*"`` catches the read across REST + CLI + MCP,
the canonical-op_id discipline every meta-tool family follows.
"""

from __future__ import annotations

from typing import Any, Final

import structlog

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import ToolDefinition, ToolSurface, register_mcp_tool
from meho_backplane.operations.addon_automation import AUTOMATION_FAMILY, active_automation_surface

__all__: list[str] = []


#: Read op-class — surfacing the advertised automation surface never mutates.
_OP_CLASS_READ: Final[str] = "read"

#: Canonical audit op_id — the ``meho.automation.*`` family the REST route and
#: CLI verb bind too, so a ``query_audit`` filter catches the read
#: transport-independently.
_LIST_OP_ID: Final[str] = "meho.automation.list"


_LIST_DESCRIPTION: Final[str] = (
    "List the surface a paired automation add-on advertises — the automation "
    "analogue of `meho_connector_list`. Only present while an automation "
    "add-on is paired AND contract-healthy; it disappears cleanly when the "
    "add-on unpairs. Returns, per providing add-on, its `contract_version`, "
    "whether that version is still `contract_compatible` with this backplane, "
    "its `paired_at` / `last_seen_at` liveness, and its declared `surfaces` "
    "(each a `{kind, name, display_label}` where `kind` is one of "
    "`meta_tool_family` / `cli_verb_family` / `console_panel` / `event_kind`).\n\n"
    "WHEN TO CALL: to discover whether governed automation is available in this "
    "tenant and what it exposes, before reaching for automation workflows. "
    "Blueprint and workflow identifiers are the add-on's own data (driven "
    "through the add-on, not the backplane waist) — this tool reports the "
    "advertised surface, never a per-workflow tool."
)


_LIST_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


_SURFACE_ITEM_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string"},
        "name": {"type": "string"},
        "display_label": {"type": ["string", "null"]},
    },
    "required": ["kind", "name", "display_label"],
}

_PROVIDER_ITEM_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "addon": {"type": "string"},
        "contract_version": {"type": "integer"},
        "contract_compatible": {"type": "boolean"},
        "paired_at": {"type": ["string", "null"]},
        "last_seen_at": {"type": ["string", "null"]},
        "surfaces": {"type": "array", "items": _SURFACE_ITEM_SCHEMA},
    },
    "required": [
        "addon",
        "contract_version",
        "contract_compatible",
        "paired_at",
        "last_seen_at",
        "surfaces",
    ],
}

_LIST_OUTPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "providers": {"type": "array", "items": _PROVIDER_ITEM_SCHEMA},
    },
    "required": ["providers"],
}


async def _automation_list_handler(
    operator: Operator,
    _arguments: dict[str, Any],
) -> dict[str, Any]:
    """Return the paired automation add-on(s) and their advertised surface.

    The dispatcher's call-time ``required_addon_family`` gate has already
    proven at least one paired, contract-healthy add-on advertises the
    ``automation`` meta-tool family, so ``providers`` is non-empty on the happy
    path. Delegates to the shared
    :func:`~meho_backplane.operations.addon_automation.active_automation_surface`
    read so the meta-tool, the REST route, and the console panel render one
    identical answer, then serialises to the JSON-RPC wire dict
    (``model_dump(mode="json")`` renders the ``CapabilityKind`` enum as its
    value and the timestamps as ISO-8601 strings, matching ``outputSchema``).
    """
    structlog.contextvars.bind_contextvars(audit_op_id=_LIST_OP_ID)
    surface = await active_automation_surface(operator.tenant_id)
    return surface.model_dump(mode="json")


register_mcp_tool(
    definition=ToolDefinition(
        feature="addon_pairing",
        name="meho_automation_list",
        surface=ToolSurface.WORKING,
        description=_LIST_DESCRIPTION,
        inputSchema=_LIST_INPUT_SCHEMA,
        outputSchema=_LIST_OUTPUT_SCHEMA,
        required_role=TenantRole.READ_ONLY,
        op_class=_OP_CLASS_READ,
        required_addon_family=AUTOMATION_FAMILY,
    ),
    handler=_automation_list_handler,
)
