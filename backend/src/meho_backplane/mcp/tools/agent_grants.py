# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Admin MCP tools for the agent permission grant surface.

G11.2-T6 (#819) under Initiative #803 — five ``meho.agents.grant.*``
tools that mirror the REST surface
(``/api/v1/agents/grants``) onto the MCP transport:

* ``meho.agents.grant.list`` — list grants in the operator's tenant.
  Role: ``tenant_admin``.
* ``meho.agents.grant.show`` — fetch one grant by id.
  Role: ``tenant_admin``.
* ``meho.agents.grant.create`` — create a grant (permanent or
  time-bounded). Role: ``tenant_admin``.
* ``meho.agents.grant.elevate`` — create a time-bounded elevation
  (``expires_at`` required). Role: ``tenant_admin``.
* ``meho.agents.grant.revoke`` — revoke (delete) a grant.
  Role: ``tenant_admin``.

RBAC enforcement
================

The registry filter (``required_role``) hides these tools from
``tools/list`` for non-admins. The service layer does not re-check
roles — it trusts the caller.

In-process call pattern
=======================

Each handler instantiates the stateless
:class:`~meho_backplane.agents.grants.AgentGrantService` and
translates the result into the MCP wire shape. Error mapping mirrors
``meho_backplane.mcp.tools.agents``:
:exc:`~meho_backplane.agents.grants.GrantValidationError` maps to
:class:`~meho_backplane.mcp.server.McpInvalidParamsError`.

Audit + broadcast
=================

Each handler binds the same audit-side contextvars the REST handlers
do (``audit_op_id``, ``audit_op_class``, ``audit_agent_name`` on
mutations), so an MCP call produces an audit row identical in shape
to the REST call's row (modulo ``method="MCP"``).
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

import structlog
from pydantic import ValidationError

from meho_backplane.agents.grant_schemas import (
    AgentElevationCreate,
    AgentGrantCreate,
    AgentGrantRead,
    GrantVerdict,
)
from meho_backplane.agents.grants import AgentGrantService, GrantValidationError
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError

_log = structlog.get_logger(__name__)

_GRANT_OP_IDS: Final[dict[str, str]] = {
    "list": "agent.grant.list",
    "show": "agent.grant.show",
    "create": "agent.grant.create",
    "elevate": "agent.grant.elevate",
    "revoke": "agent.grant.revoke",
}


def _mirror_grant_id(payload: dict[str, Any]) -> dict[str, Any]:
    """Mirror the native ``id`` response key as ``grant_id``.

    The row model :class:`AgentGrantRead` is shared with the REST route
    ``GET /api/v1/agents/grants`` (#1612 kept shared models out of a
    rename's scope), so the MCP handlers mirror the id at the wire
    boundary instead: every grant row carries ``grant_id`` — the field
    ``meho.agents.grant.show`` / ``meho.agents.grant.revoke`` accept
    verbatim — alongside the model's native ``id``.
    """
    if "id" in payload:
        payload["grant_id"] = payload["id"]
    return payload


def _row_to_dict(entry: AgentGrantRead) -> dict[str, Any]:
    return _mirror_grant_id(entry.model_dump(mode="json"))


def _require_str(arguments: dict[str, Any], key: str) -> str:
    val = arguments.get(key)
    if not isinstance(val, str) or not val:
        raise McpInvalidParamsError(f"{key} is required and must be a non-empty string")
    return val


def _require_uuid(arguments: dict[str, Any], key: str) -> UUID:
    raw = _require_str(arguments, key)
    try:
        return UUID(raw)
    except ValueError:
        raise McpInvalidParamsError(f"{key} must be a valid UUID string") from None


# ---------------------------------------------------------------------------
# meho.agents.grant.list
# ---------------------------------------------------------------------------


async def _list_grants_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    structlog.contextvars.bind_contextvars(
        audit_op_id=_GRANT_OP_IDS["list"],
        audit_op_class="read",
    )
    principal_sub: str | None = arguments.get("principal_sub") or None
    include_expired: bool = bool(arguments.get("include_expired", False))
    service = AgentGrantService()
    grants = await service.list_(
        operator.tenant_id,
        principal_sub=principal_sub,
        include_expired=include_expired,
    )
    return {"grants": [_row_to_dict(g) for g in grants]}


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.agents.grant.list",
        description=(
            "List agent permission grants for the operator's tenant "
            "(G11.2-T6). Returns {grants: [...]}; each row carries "
            "`grant_id` (accepted verbatim by meho.agents.grant.show / "
            ".revoke) alongside the model-native `id`. "
            "Optional principal_sub filters to one agent. "
            "include_expired=true includes past elevations."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "principal_sub": {
                    "type": "string",
                    "description": "Filter by agent principal JWT sub (optional).",
                },
                "include_expired": {
                    "type": "boolean",
                    "description": "Include expired elevations (default false).",
                    "default": False,
                },
            },
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class="read",
    ),
    handler=_list_grants_handler,
)


# ---------------------------------------------------------------------------
# meho.agents.grant.show
# ---------------------------------------------------------------------------


async def _show_grant_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    grant_id = _require_uuid(arguments, "grant_id")
    structlog.contextvars.bind_contextvars(
        audit_op_id=_GRANT_OP_IDS["show"],
        audit_op_class="read",
    )
    service = AgentGrantService()
    entry = await service.get(operator.tenant_id, grant_id)
    if entry is None:
        raise McpInvalidParamsError(f"grant {grant_id!s} not found")
    return _row_to_dict(entry)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.agents.grant.show",
        description=(
            "Fetch one agent permission grant by id (G11.2-T6). "
            "Returns the grant row (carrying both `grant_id` and the "
            "model-native `id`) or raises not-found."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "grant_id": {
                    "type": "string",
                    "description": "UUID of the grant to fetch.",
                },
            },
            "required": ["grant_id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class="read",
    ),
    handler=_show_grant_handler,
)


# ---------------------------------------------------------------------------
# meho.agents.grant.create
# ---------------------------------------------------------------------------


async def _create_grant_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    try:
        payload = AgentGrantCreate.model_validate(arguments)
    except ValidationError as exc:
        raise McpInvalidParamsError(str(exc)) from exc
    structlog.contextvars.bind_contextvars(
        audit_op_id=_GRANT_OP_IDS["create"],
        audit_op_class="write",
        audit_agent_name=payload.principal_sub,
    )
    service = AgentGrantService()
    try:
        entry = await service.grant(operator.tenant_id, operator.sub, payload)
    except GrantValidationError as exc:
        raise McpInvalidParamsError(exc.message) from exc
    return _row_to_dict(entry)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.agents.grant.create",
        description=(
            "Grant a permission to an agent principal (G11.2-T6). "
            "verdict: auto-execute | needs-approval | deny. "
            "expires_at (ISO-8601 UTC) makes the grant time-bounded. "
            "Omit expires_at for a permanent grant. The created row "
            "carries `grant_id` (accepted verbatim by "
            "meho.agents.grant.show / .revoke) alongside the native `id`."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "principal_sub": {
                    "type": "string",
                    "description": "JWT sub of the agent principal receiving the grant.",
                },
                "op_pattern": {
                    "type": "string",
                    "description": "fnmatch glob matching operation IDs. '*' = all ops.",
                },
                "target_scope": {
                    "type": "string",
                    "description": ("Target UUID, '*', or omit for any target."),
                },
                "verdict": {
                    "type": "string",
                    "enum": [v.value for v in GrantVerdict],
                    "description": "Permission verdict for matching dispatches.",
                },
                "expires_at": {
                    "type": "string",
                    "format": "date-time",
                    "description": "UTC expiry for a time-bounded elevation (optional).",
                },
            },
            "required": ["principal_sub", "op_pattern", "verdict"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class="write",
    ),
    handler=_create_grant_handler,
)


# ---------------------------------------------------------------------------
# meho.agents.grant.elevate
# ---------------------------------------------------------------------------


async def _elevate_grant_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    try:
        payload = AgentElevationCreate.model_validate(arguments)
    except ValidationError as exc:
        raise McpInvalidParamsError(str(exc)) from exc
    structlog.contextvars.bind_contextvars(
        audit_op_id=_GRANT_OP_IDS["elevate"],
        audit_op_class="write",
        audit_agent_name=payload.principal_sub,
    )
    service = AgentGrantService()
    try:
        entry = await service.grant(operator.tenant_id, operator.sub, payload)
    except GrantValidationError as exc:
        raise McpInvalidParamsError(exc.message) from exc
    return _row_to_dict(entry)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.agents.grant.elevate",
        description=(
            "Grant a time-bounded elevation to an agent principal (G11.2-T6). "
            "expires_at is required. The grant-expiry sweeper reverts the "
            "agent to baseline automatically after the window ends. The "
            "created row carries `grant_id` (accepted verbatim by "
            "meho.agents.grant.show / .revoke) alongside the native `id`."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "principal_sub": {
                    "type": "string",
                    "description": "JWT sub of the agent principal.",
                },
                "op_pattern": {
                    "type": "string",
                    "description": "fnmatch glob matching operation IDs.",
                },
                "target_scope": {
                    "type": "string",
                    "description": "Target UUID or '*' for any target (optional).",
                },
                "verdict": {
                    "type": "string",
                    "enum": [v.value for v in GrantVerdict],
                    "description": "Permission verdict for the elevation window.",
                },
                "expires_at": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Required UTC expiry for the elevation.",
                },
            },
            "required": ["principal_sub", "op_pattern", "verdict", "expires_at"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class="write",
    ),
    handler=_elevate_grant_handler,
)


# ---------------------------------------------------------------------------
# meho.agents.grant.revoke
# ---------------------------------------------------------------------------


async def _revoke_grant_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    grant_id = _require_uuid(arguments, "grant_id")
    structlog.contextvars.bind_contextvars(
        audit_op_id=_GRANT_OP_IDS["revoke"],
        audit_op_class="write",
    )
    service = AgentGrantService()
    deleted = await service.revoke(operator.tenant_id, grant_id)
    if not deleted:
        raise McpInvalidParamsError(f"grant {grant_id!s} not found")
    return {"revoked": str(grant_id)}


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.agents.grant.revoke",
        description=(
            "Revoke (delete) a permission grant by id (G11.2-T6). "
            "Returns {revoked: <grant_id>} on success."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "grant_id": {
                    "type": "string",
                    "description": "UUID of the grant to revoke.",
                },
            },
            "required": ["grant_id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class="write",
    ),
    handler=_revoke_grant_handler,
)
