# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Admin MCP tools for the connector review + state-machine surface (G0.7-T7).

Six tools under the ``meho.connector.*`` namespace, deliberately
separate from the agent-surface meta-tools
(:mod:`~meho_backplane.mcp.tools.operations`):

* ``meho.connector.list`` — list ingested connectors. **operator**.
* ``meho.connector.review`` — get the review payload. **operator**.
* ``meho.connector.edit_group`` — edit one group. **tenant_admin**.
* ``meho.connector.edit_op`` — edit one op override. **tenant_admin**.
* ``meho.connector.enable`` — flip the connector to enabled. **tenant_admin**.
* ``meho.connector.disable`` — flip the connector to disabled. **tenant_admin**.

The ingest-pipeline tools (``meho.connector.ingest`` +
``meho.connector.ingest_status``) live in the sibling
:mod:`meho_backplane.mcp.tools.connector_ingest` module; the two files
split the admin tools by responsibility (pipeline vs. review) so
neither grows past the code-quality file-size budget. The shared
``connector_id`` / ``tenant_id`` schema snippets, op-class taxonomy
strings, and JSON-safe serialiser live in
:mod:`meho_backplane.mcp.tools._connector_shared`.

Why these are not in the agent surface
======================================

Per ``CLAUDE.md`` postulate 5 and the "What MEHO is NOT" section: the
agent surface is the ~17 meta-tools (``search_connectors``,
``call_operation``, etc.). Vendor-specific identifiers and
administrative verbs never reach the agent's tool list. These admin
tools live in the ``meho.*`` namespace and are visible only to
``tenant_admin`` (mutators) or ``operator`` (the two read tools), so an
agent dispatched with ``read_only`` role sees none of them and an
agent with the default ``operator`` role sees only the two read
helpers.

That partition is enforced by the registry's
:func:`~meho_backplane.mcp.registry.all_tools_for` filter, which lists
each tool only when the requesting operator's
:class:`~meho_backplane.auth.operator.TenantRole` meets or exceeds the
tool's :attr:`~meho_backplane.mcp.registry.ToolDefinition.required_role`.
The dispatcher repeats the check at call time (see
:func:`~meho_backplane.mcp.handlers.handle_tools_call`), so a tool
that's hidden from ``tools/list`` is also un-callable if a client
guesses its name.

Tool wiring discipline
======================

Each tool's handler is a thin shim that wraps the canonical
service layer T5 (CLI) and T6 (REST routes) also consume — there
is no parallel "admin service" class here:

1. ``meho.connector.list`` calls the
   :func:`list_ingested_connectors` query helper directly.
2. Every other tool constructs :class:`ReviewService` and
   delegates to its existing read / edit / state-machine methods.

PATCH-style handlers (``edit_group`` / ``edit_op``) build the
service-layer kwargs dict from ``if "field" in arguments``
key-presence checks so that an omitted field never reaches
:class:`ReviewService`; otherwise ``arguments.get(...)`` would make
"omitted" and "explicit ``null``" indistinguishable and the
PATCH semantics would blur. Only fields the operator explicitly
named are forwarded.

Responses are ``model_dump(mode="json")``-ed so the dispatcher
can wrap them in the MCP ``content`` array. Keeping the dispatch
logic single-source — same service classes the REST router calls —
is the contract called out in #407's task body and the T6 merge
(#488) that promoted ``IngestionPipelineService`` /
``list_ingested_connectors`` / ``api_schemas.IngestRequest`` to the
shared public surface.

Per-tool ``inputSchema`` notes
==============================

Every schema uses ``additionalProperties: false`` (issue AC 4). The
``connector_id`` field appears on all six tools and is documented
identically across them via the shared
:data:`~meho_backplane.mcp.tools._connector_shared._CONNECTOR_ID_DESCRIPTION`
snippet: an operator-facing ``<impl_id>-<version>`` string.

The ``tenant_id`` field is optional everywhere. ``null`` / omitted →
the built-in scope, which is accessible only to ``tenant_admin`` per
the service layer's authorisation rule. Tenant operators pass their
own tenant's UUID when they want to operate on their own
tenant-curated connectors.
"""

from __future__ import annotations

from typing import Any

import structlog

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.tools._connector_shared import (
    _CONNECTOR_ID_DESCRIPTION,
    _OP_CLASS_READ,
    _OP_CLASS_WRITE,
    _TENANT_ID_PROPERTY,
    _coerce_tenant_id,
    _model_dump_json_safe,
)
from meho_backplane.operations.ingest import (
    ConnectorStatusFilter,
    ReviewService,
    list_ingested_connectors,
)

__all__: list[str] = []

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Handler implementations
# ---------------------------------------------------------------------------


async def _list_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Return connectors visible to the operator's tenant + built-in.

    Delegates to :func:`list_ingested_connectors` — the same query
    helper the REST route at ``GET /api/v1/connectors`` uses. The
    ``status`` argument is the canonical
    :data:`ConnectorStatusFilter` literal; the JSON-Schema validator
    has already restricted it to one of the four legal values before
    this handler runs.
    """
    raw_status = arguments.get("status", "all")
    # Normalise the JSON-Schema-validated string into the canonical
    # Literal type the query helper takes. ``"all"`` is the explicit
    # no-filter sentinel; the function itself also accepts ``None``.
    status: ConnectorStatusFilter = raw_status
    connectors = await list_ingested_connectors(operator=operator, status=status)
    return {
        "connectors": [item.model_dump(mode="json") for item in connectors],
    }


async def _review_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Return the full review payload for one connector."""
    connector_id: str = arguments["connector_id"]
    tenant_id = _coerce_tenant_id(arguments.get("tenant_id"))
    service = ReviewService(operator)
    payload = await service.get_review_payload(connector_id, tenant_id)
    return _model_dump_json_safe(payload)


async def _edit_group_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Update one group's ``when_to_use`` and/or display ``name``.

    PATCH semantics: only fields the operator explicitly named are
    forwarded to :meth:`ReviewService.edit_group`. ``arguments.get(...)``
    would conflate "field omitted" with "field=null" — the service
    layer's empty-body rejection then can't distinguish "operator
    cleared the field" from "operator left the field alone".
    Key-presence checks preserve the intent.
    """
    connector_id: str = arguments["connector_id"]
    group_key: str = arguments["group_key"]
    tenant_id = _coerce_tenant_id(arguments.get("tenant_id"))
    patch: dict[str, Any] = {}
    if "when_to_use" in arguments:
        patch["when_to_use"] = arguments["when_to_use"]
    if "name" in arguments:
        patch["name"] = arguments["name"]
    service = ReviewService(operator)
    await service.edit_group(
        connector_id,
        group_key,
        tenant_id=tenant_id,
        **patch,
    )
    return {"connector_id": connector_id, "group_key": group_key, "ok": True}


async def _edit_op_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Update one per-op override (any of four fields).

    Same PATCH-semantic discipline as :func:`_edit_group_handler`:
    fields not present in ``arguments`` are never forwarded.

    ``warnings`` mirrors the REST route's
    :class:`~meho_backplane.operations.ingest.EditOpResponse` field
    (G0.23-T4 #1630): enable-time advisories from the shared service
    layer — today only the ``unreplaced_auto_shim`` probe —
    serialized per-entry via ``model_dump(mode="json")``. Empty list
    on the clean path; never blocks the write (``ok`` stays
    ``True``). Surfacing it here keeps MCP↔REST parity, the same
    discipline the ingest error envelopes follow (#1534 / #1610).
    """
    connector_id: str = arguments["connector_id"]
    op_id: str = arguments["op_id"]
    tenant_id = _coerce_tenant_id(arguments.get("tenant_id"))
    patch: dict[str, Any] = {}
    if "custom_description" in arguments:
        patch["custom_description"] = arguments["custom_description"]
    if "safety_level" in arguments:
        patch["safety_level"] = arguments["safety_level"]
    if "requires_approval" in arguments:
        patch["requires_approval"] = arguments["requires_approval"]
    if "is_enabled" in arguments:
        patch["is_enabled"] = arguments["is_enabled"]
    service = ReviewService(operator)
    warnings = await service.edit_op(
        connector_id,
        op_id,
        tenant_id=tenant_id,
        **patch,
    )
    return {
        "connector_id": connector_id,
        "op_id": op_id,
        "ok": True,
        "warnings": [warning.model_dump(mode="json") for warning in warnings],
    }


async def _enable_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Flip every group of a connector to ``review_status='enabled'``."""
    connector_id: str = arguments["connector_id"]
    tenant_id = _coerce_tenant_id(arguments.get("tenant_id"))
    service = ReviewService(operator)
    await service.enable_connector(connector_id, tenant_id=tenant_id)
    return {"connector_id": connector_id, "status": "enabled", "ok": True}


async def _disable_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Flip every group of a connector to ``review_status='disabled'``."""
    connector_id: str = arguments["connector_id"]
    tenant_id = _coerce_tenant_id(arguments.get("tenant_id"))
    service = ReviewService(operator)
    await service.disable_connector(connector_id, tenant_id=tenant_id)
    return {"connector_id": connector_id, "status": "disabled", "ok": True}


# ---------------------------------------------------------------------------
# Tool registrations
# ---------------------------------------------------------------------------


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.connector.list",
        description=(
            "List ingested connectors visible to the operator's tenant "
            "(plus built-in / global). Returns per-connector counts and "
            "the aggregate review status (staged / enabled / disabled). "
            "Use as the entry point when an operator needs to find a "
            "connector to review or to see what's already in place. "
            "Filter via status=staged to surface only connectors that "
            "need review. Pair with meho.connector.review to drill into "
            "an individual connector. Read-only; visible to operator and "
            "tenant_admin roles."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["staged", "enabled", "disabled", "all"],
                    "default": "all",
                    "description": (
                        "Aggregate-status filter. 'staged' = at least "
                        "one group still staged; 'enabled' = at least "
                        "one group enabled and none staged; "
                        "'disabled' = every group disabled; 'all' = "
                        "no filter."
                    ),
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class=_OP_CLASS_READ,
    ),
    handler=_list_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.connector.review",
        description=(
            "Get the full review payload for one connector (groups + "
            "per-group operations + per-op flags). Use after "
            "meho.connector.list has surfaced a connector that needs "
            "review, or whenever you need to inspect an already-enabled "
            "connector's surface. The payload includes "
            "operator-editable fields (when_to_use, custom_description, "
            "safety_level, requires_approval, is_enabled) — pair with "
            "meho.connector.edit_group / meho.connector.edit_op to "
            "modify them. Read-only; visible to operator and "
            "tenant_admin roles. Tenant-scoped: cross-tenant reads are "
            "rejected with a ConnectorNotFoundError."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connector_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": _CONNECTOR_ID_DESCRIPTION,
                },
                "tenant_id": _TENANT_ID_PROPERTY,
            },
            "required": ["connector_id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class=_OP_CLASS_READ,
    ),
    handler=_review_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.connector.edit_group",
        description=(
            "Edit one operation group's when_to_use prose or display "
            "name (tenant_admin only). Use after meho.connector.review "
            "has surfaced a group whose LLM-generated description needs "
            "operator-authored prose. Pass at least one of when_to_use "
            "or name — passing neither is rejected. Each edit writes one "
            "audit row. Do NOT use this to change group membership "
            "(operation-to-group assignment): that requires re-running "
            "ingest or editing per-op assignments via "
            "meho.connector.edit_op."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connector_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": _CONNECTOR_ID_DESCRIPTION,
                },
                "group_key": {"type": "string", "minLength": 1},
                "when_to_use": {"type": ["string", "null"]},
                "name": {"type": ["string", "null"]},
                "tenant_id": _TENANT_ID_PROPERTY,
            },
            "required": ["connector_id", "group_key"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_edit_group_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.connector.edit_op",
        description=(
            "Edit one operation's per-op overrides (tenant_admin only). "
            "Editable fields: custom_description (operator-authored "
            "agent-facing description), safety_level (safe / caution / "
            "dangerous), requires_approval (gate the operation behind "
            "out-of-band approval), is_enabled (per-op enable/disable "
            "that survives connector-level enable/disable cycles). Pass "
            "at least one field; passing none is rejected. Use during "
            "review (after meho.connector.review surfaces the op) when "
            "an LLM-generated description is misleading or when a "
            "DELETE-shaped op needs the safety_level promoted from "
            "'dangerous' to require approval. is_enabled=false here "
            "STICKS: a subsequent meho.connector.enable will NOT "
            "clobber the operator's per-op disable. Do not use this "
            "to flip whole-connector state — pair with "
            "meho.connector.enable / .disable for that."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connector_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": _CONNECTOR_ID_DESCRIPTION,
                },
                "op_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": ("Connector-side op_id (e.g. 'GET:/api/vcenter/cluster')."),
                },
                "custom_description": {"type": ["string", "null"]},
                "safety_level": {
                    "type": ["string", "null"],
                    "enum": ["safe", "caution", "dangerous", None],
                },
                "requires_approval": {"type": ["boolean", "null"]},
                "is_enabled": {"type": ["boolean", "null"]},
                "tenant_id": _TENANT_ID_PROPERTY,
            },
            "required": ["connector_id", "op_id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_edit_op_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.connector.enable",
        description=(
            "Flip every group of a connector to review_status='enabled' "
            "(tenant_admin only). Cascades is_enabled=true to every "
            "child op except those an operator previously set "
            "is_enabled=false on via meho.connector.edit_op (operator "
            "overrides stick). Use AFTER reviewing the connector with "
            "meho.connector.review + applying any needed edits. "
            "Idempotent — already-enabled groups are a no-op (no audit "
            "row written). DO NOT use this on a connector that's still "
            "actively being reviewed; the agent surface starts "
            "dispatching its operations the moment this returns."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connector_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": _CONNECTOR_ID_DESCRIPTION,
                },
                "tenant_id": _TENANT_ID_PROPERTY,
            },
            "required": ["connector_id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_enable_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.connector.disable",
        description=(
            "Flip every group of a connector to "
            "review_status='disabled' (tenant_admin only). Cascades "
            "is_enabled=false to every child op (the connector-level "
            "disable overrides per-op operator overrides for the "
            "duration of the disabled state). Use to roll back a "
            "regression — the connector's operations stop showing up "
            "in search_operations the moment this returns. Idempotent. "
            "Do NOT use as a soft toggle during normal review; pair "
            "with meho.connector.enable to bring the connector back "
            "after the regression is fixed. Operator-set per-op "
            "disables are recovered from the audit log when the re-"
            "enable cascade fires."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connector_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": _CONNECTOR_ID_DESCRIPTION,
                },
                "tenant_id": _TENANT_ID_PROPERTY,
            },
            "required": ["connector_id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_disable_handler,
)
