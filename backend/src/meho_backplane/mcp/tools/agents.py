# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Admin MCP tools for the agent-definition CRUD surface.

G11.1-T2 (#809) under Initiative #802 -- five ``meho.agents.*`` tools
that mirror the REST surface (``/api/v1/agents``) onto the MCP
transport:

* ``meho.agents.list`` -- list the operator's tenant's definitions.
  Role: ``operator``.
* ``meho.agents.show`` -- fetch one definition by name. Role:
  ``operator``.
* ``meho.agents.create`` -- create a definition. Role: ``tenant_admin``.
* ``meho.agents.edit`` -- partial update by name. Role: ``tenant_admin``.
* ``meho.agents.delete`` -- delete a definition by name. Role:
  ``tenant_admin``.

RBAC enforcement happens at two layers: the registry filter
(``required_role`` hides write tools from ``tools/list`` for non-admins)
and the dispatcher's call-time re-check. Reads require ``operator``;
writes require ``tenant_admin``.

In-process call into the service
================================

Each handler instantiates the stateless
:class:`~meho_backplane.agents.service.AgentDefinitionService` (which
opens its own session per method) and translates the result into the
MCP wire shape (the
:class:`~meho_backplane.agents.schemas.AgentDefinitionRead` Pydantic
model dumped to JSON). Service-level errors map to the MCP wire shape:

* :class:`~meho_backplane.agents.service.AgentDefinitionExistsError`
  (duplicate create) and a ``None`` / ``False`` absence on
  show / edit / delete both surface as
  :class:`~meho_backplane.mcp.server.McpInvalidParamsError` (JSON-RPC
  ``-32602``) -- the closest spec-blessed shape for "conflict" /
  "not found", matching :mod:`meho_backplane.mcp.tools.broadcast_overrides`.

Audit + broadcast inheritance
=============================

Each handler binds the same audit-side contextvars the REST handlers do
(``audit_op_id``, ``audit_op_class``, ``audit_agent_name`` on
mutations), so an MCP call produces an audit row + broadcast event
identical in shape to the REST call's row (modulo the ``method="MCP"``
distinction the chassis records).
"""

from __future__ import annotations

from typing import Any, Final

import structlog
from pydantic import ValidationError

from meho_backplane.agents.schemas import (
    AgentDefinitionCreate,
    AgentDefinitionRead,
    AgentDefinitionUpdate,
)
from meho_backplane.agents.service import (
    AgentDefinitionExistsError,
    AgentDefinitionService,
    AgentIdentityRefInvalidError,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError

_log = structlog.get_logger(__name__)

#: Canonical operation identifiers bound into ``audit_op_id`` per tool.
#: Same identifiers the REST routes use so a row's op_id is transport-
#: independent -- ``meho audit query`` cannot tell whether a definition
#: was created via REST or MCP except by the ``method`` column.
_AGENT_OP_IDS: Final[dict[str, str]] = {
    "list": "agent.list",
    "show": "agent.show",
    "create": "agent.create",
    "edit": "agent.edit",
    "delete": "agent.delete",
}


def _row_to_dict(entry: AgentDefinitionRead) -> dict[str, Any]:
    """Serialise an :class:`AgentDefinitionRead` to the MCP wire dict."""
    return entry.model_dump(mode="json")


def _require_name(arguments: dict[str, Any]) -> str:
    """Extract a required string ``name`` argument or raise invalid-params."""
    name = arguments.get("name")
    if not isinstance(name, str) or not name:
        raise McpInvalidParamsError("name is required and must be a non-empty string")
    return name


# ---------------------------------------------------------------------------
# meho.agents.list
# ---------------------------------------------------------------------------


async def _list_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    structlog.contextvars.bind_contextvars(
        audit_op_id=_AGENT_OP_IDS["list"],
        audit_op_class="read",
    )
    service = AgentDefinitionService()
    agents = await service.list_(operator.tenant_id)
    return {"agents": [_row_to_dict(a) for a in agents]}


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.agents.list",
        description=(
            "List agent definitions for the operator's tenant "
            "(Initiative #802). Operator-level read. Returns "
            "{agents: [definition, ...]} sorted by name."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class="read",
    ),
    handler=_list_handler,
)


# ---------------------------------------------------------------------------
# meho.agents.show
# ---------------------------------------------------------------------------


async def _show_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    name = _require_name(arguments)
    structlog.contextvars.bind_contextvars(
        audit_op_id=_AGENT_OP_IDS["show"],
        audit_op_class="read",
    )
    service = AgentDefinitionService()
    entry = await service.get(operator.tenant_id, name)
    if entry is None:
        raise McpInvalidParamsError("agent_not_found")
    return {"agent": _row_to_dict(entry)}


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.agents.show",
        description=(
            "Fetch one agent definition by name for the operator's "
            "tenant (Initiative #802). Operator-level read. Returns "
            "{agent: {...}}. A missing / cross-tenant name returns an "
            "error with detail 'agent_not_found' -- existence is not "
            "leaked across tenant boundaries."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": "Agent definition name.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class="read",
    ),
    handler=_show_handler,
)


# ---------------------------------------------------------------------------
# meho.agents.create
# ---------------------------------------------------------------------------


async def _create_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    # Re-validate through Pydantic so the same field constraints (name
    # pattern, turn-budget bounds, extra="forbid") run for MCP as for
    # REST. The inputSchema does first-pass shape checks; Pydantic runs
    # the typed-field validators.
    try:
        payload = AgentDefinitionCreate.model_validate(arguments)
    except ValidationError as exc:
        raise McpInvalidParamsError(f"invalid arguments: {exc}") from exc
    structlog.contextvars.bind_contextvars(
        audit_op_id=_AGENT_OP_IDS["create"],
        audit_op_class="write",
        audit_agent_name=payload.name,
    )
    service = AgentDefinitionService()
    try:
        entry = await service.create(
            tenant_id=operator.tenant_id,
            created_by_sub=operator.sub,
            payload=payload,
        )
    except AgentDefinitionExistsError as exc:
        raise McpInvalidParamsError("agent_already_exists") from exc
    except AgentIdentityRefInvalidError as exc:
        raise McpInvalidParamsError("identity_ref_unknown") from exc
    return {"name": entry.name, "agent": _row_to_dict(entry)}


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.agents.create",
        description=(
            "Create an agent definition for the operator's tenant "
            "(Initiative #802). Tenant-admin only. Args: name (slug), "
            "identity_ref (must name a registered, non-revoked agent "
            "principal in the operator's tenant -- typically "
            "'agent:<principal-name>'), model_tier (standard|fast|deep), "
            "system_prompt, turn_budget (1-1000), optional toolset and "
            "output_schema objects, optional enabled (default true). A "
            "duplicate name returns an error with detail "
            "'agent_already_exists'; an unknown / cross-tenant / revoked "
            "identity_ref returns 'identity_ref_unknown' (G11.2-T8 #1099). "
            "Response: {name, agent: {...}}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": r"^[A-Za-z0-9_\-\.]+$",
                    "description": "Agent name (slug; safe-URL alphabet).",
                },
                "identity_ref": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "description": "Reference to the agent principal (G11.2).",
                },
                "model_tier": {
                    "type": "string",
                    "enum": ["standard", "fast", "deep"],
                    "description": "Logical model tier (resolved at run time by G11.5).",
                },
                "system_prompt": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The agent's system prompt.",
                },
                "toolset": {
                    "type": "object",
                    "description": "Allowed meta-tools / connector-ops spec (resolved by T3).",
                },
                "turn_budget": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                    "description": "Max model turns the runtime allows.",
                },
                "output_schema": {
                    "type": ["object", "null"],
                    "description": "Optional JSON Schema for structured output.",
                },
                "enabled": {
                    "type": "boolean",
                    "description": "Soft on/off switch (default true).",
                },
            },
            "required": [
                "name",
                "identity_ref",
                "model_tier",
                "system_prompt",
                "turn_budget",
            ],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class="write",
    ),
    handler=_create_handler,
)


# ---------------------------------------------------------------------------
# meho.agents.edit
# ---------------------------------------------------------------------------


async def _edit_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    name = _require_name(arguments)
    # The name is the path key, not an updatable field; strip it before
    # validating the update body so a stray ``name`` doesn't trip
    # extra="forbid".
    update_args = {k: v for k, v in arguments.items() if k != "name"}
    try:
        payload = AgentDefinitionUpdate.model_validate(update_args)
    except ValidationError as exc:
        raise McpInvalidParamsError(f"invalid arguments: {exc}") from exc
    structlog.contextvars.bind_contextvars(
        audit_op_id=_AGENT_OP_IDS["edit"],
        audit_op_class="write",
        audit_agent_name=name,
    )
    service = AgentDefinitionService()
    try:
        entry = await service.update(operator.tenant_id, name, payload)
    except AgentIdentityRefInvalidError as exc:
        raise McpInvalidParamsError("identity_ref_unknown") from exc
    if entry is None:
        raise McpInvalidParamsError("agent_not_found")
    return {"name": entry.name, "agent": _row_to_dict(entry)}


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.agents.edit",
        description=(
            "Apply a partial update to an agent definition by name "
            "(Initiative #802). Tenant-admin only. Supply name plus any "
            "of identity_ref, model_tier, system_prompt, toolset, "
            "turn_budget, output_schema, enabled -- only supplied "
            "fields change. name itself is not renamable. A missing / "
            "cross-tenant name returns 'agent_not_found'; an "
            "identity_ref update that doesn't resolve to a registered, "
            "non-revoked tenant principal returns 'identity_ref_unknown' "
            "(G11.2-T8 #1099). Response: {name, agent: {...}}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": "Agent definition name (the key; not renamable).",
                },
                "identity_ref": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                },
                "model_tier": {
                    "type": "string",
                    "enum": ["standard", "fast", "deep"],
                },
                "system_prompt": {"type": "string", "minLength": 1},
                "toolset": {"type": "object"},
                "turn_budget": {"type": "integer", "minimum": 1, "maximum": 1000},
                "output_schema": {"type": ["object", "null"]},
                "enabled": {"type": "boolean"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class="write",
    ),
    handler=_edit_handler,
)


# ---------------------------------------------------------------------------
# meho.agents.delete
# ---------------------------------------------------------------------------


async def _delete_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    name = _require_name(arguments)
    structlog.contextvars.bind_contextvars(
        audit_op_id=_AGENT_OP_IDS["delete"],
        audit_op_class="write",
        audit_agent_name=name,
    )
    service = AgentDefinitionService()
    deleted = await service.delete(operator.tenant_id, name)
    if not deleted:
        raise McpInvalidParamsError("agent_not_found")
    return {"removed": True}


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.agents.delete",
        description=(
            "Delete an agent definition by name for the operator's "
            "tenant (Initiative #802). Tenant-admin only. Returns "
            "{removed: true} on success. A missing / cross-tenant name "
            "returns 'agent_not_found' -- existence is not leaked "
            "across tenant boundaries."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": "Agent definition name.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class="write",
    ),
    handler=_delete_handler,
)
