# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Admin MCP tools for the scheduled-trigger CRUD surface.

G11.3-T5 (#826) under Initiative #804 -- three ``meho.scheduler.*``
tools that mirror the REST surface (``/api/v1/scheduler/triggers``)
onto the MCP transport:

* ``meho.scheduler.list`` -- list the operator's tenant's triggers
  (with optional kind / status filters). Role: ``operator``.
* ``meho.scheduler.create`` -- create a trigger. Role: ``tenant_admin``.
* ``meho.scheduler.cancel`` -- cancel a trigger by id (terminal
  ``status='cancelled'`` transition). Role: ``tenant_admin``.

Why three tools rather than one parametric ``manage_scheduled_trigger``
======================================================================

The :mod:`meho_backplane.mcp.tools.agents` precedent ships five
separate verbs (one per CRUD action); the operations meta-tool ships a
single parametric :func:`call` that dispatches inside. The deciding
factor here is what an MCP client sees in ``tools/list``: three
verbs make the available actions discoverable (the operator's MCP
client renders three buttons; an agent's tool selector sees three
distinct option shapes); one parametric verb hides the actions behind
a free-form ``action`` arg the client has no way to enumerate.
Discoverability wins for an admin surface a human operator is likely
to drive interactively.

RBAC enforcement happens at two layers: the registry filter
(``required_role`` hides write tools from ``tools/list`` for non-admins)
and the dispatcher's call-time re-check. Reads require ``operator``;
writes require ``tenant_admin``.

In-process call into the service
================================

Each handler instantiates the stateless
:class:`~meho_backplane.scheduler.service.SchedulerAdminService` (which
opens its own session per method) and translates the result into the
MCP wire shape (the :class:`ScheduledTriggerRead` Pydantic model
dumped to JSON). Service-level errors map to the MCP wire shape: a
missing FK (:class:`AgentDefinitionMissingError`) and a not-found /
cross-tenant cancel target both surface as
:class:`~meho_backplane.mcp.server.McpInvalidParamsError`.

Audit + broadcast inheritance
=============================

Each handler binds the same audit-side contextvars the REST handlers do
(``audit_op_id``, ``audit_op_class``, ``audit_trigger_kind`` /
``audit_trigger_id`` on mutations), so an MCP call produces an audit
row + broadcast event identical in shape to the REST call's row
(modulo the ``method="MCP"`` distinction the chassis records).
"""

from __future__ import annotations

import uuid
from typing import Any, Final

import structlog
from pydantic import ValidationError

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.scheduler.schemas import (
    ScheduledTriggerCreate,
    ScheduledTriggerRead,
)
from meho_backplane.scheduler.service import (
    AgentDefinitionMissingError,
    SchedulerAdminService,
)

_log = structlog.get_logger(__name__)

#: Canonical operation identifiers bound into ``audit_op_id`` per tool.
#: Same identifiers the REST routes use so a row's op_id is transport-
#: independent -- ``meho audit query`` cannot tell whether a trigger was
#: created via REST or MCP except by the ``method`` column.
_SCHEDULER_OP_IDS: Final[dict[str, str]] = {
    "list": "scheduler.list",
    "create": "scheduler.create",
    "cancel": "scheduler.cancel",
}


def _row_to_dict(entry: ScheduledTriggerRead) -> dict[str, Any]:
    """Serialise a :class:`ScheduledTriggerRead` to the MCP wire dict."""
    return entry.model_dump(mode="json")


def _require_trigger_id(arguments: dict[str, Any]) -> uuid.UUID:
    """Extract a required ``trigger_id`` UUID or raise invalid-params."""
    raw = arguments.get("trigger_id")
    if not isinstance(raw, str) or not raw:
        raise McpInvalidParamsError("trigger_id is required and must be a non-empty string")
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise McpInvalidParamsError(f"trigger_id is not a valid UUID: {raw!r}") from exc


# ---------------------------------------------------------------------------
# meho.scheduler.list
# ---------------------------------------------------------------------------


async def _list_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    structlog.contextvars.bind_contextvars(
        audit_op_id=_SCHEDULER_OP_IDS["list"],
        audit_op_class="read",
    )
    kind = arguments.get("kind")
    status = arguments.get("status")
    limit_raw = arguments.get("limit", 100)
    offset_raw = arguments.get("offset", 0)
    # The inputSchema does first-pass shape + bounds; the int() cast is
    # a defensive narrow for the static type-checker.
    limit = int(limit_raw)
    offset = int(offset_raw)
    service = SchedulerAdminService()
    triggers = await service.list_(
        operator.tenant_id,
        kind=kind if isinstance(kind, str) else None,
        status=status if isinstance(status, str) else None,
        limit=limit,
        offset=offset,
    )
    structlog.contextvars.bind_contextvars(audit_row_count=len(triggers))
    return {"triggers": [_row_to_dict(t) for t in triggers]}


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.scheduler.list",
        description=(
            "List scheduled triggers for the operator's tenant "
            "(Initiative #804). Operator-level read. Returns "
            "{triggers: [trigger, ...]} sorted newest-first. "
            "Optional filters: kind ('cron'|'one_off'|'event'), "
            "status ('active'|'paused'|'cancelled'|'fired'). "
            "Tenant-scoped via the JWT; cross-tenant listing is not "
            "exposed on the MCP transport."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["cron", "one_off", "event"],
                    "description": "Optional kind filter.",
                },
                "status": {
                    "type": "string",
                    "enum": ["active", "paused", "cancelled", "fired"],
                    "description": "Optional status filter.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": "Max triggers per page (default 100).",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Page offset (default 0).",
                },
            },
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class="read",
    ),
    handler=_list_handler,
)


# ---------------------------------------------------------------------------
# meho.scheduler.create
# ---------------------------------------------------------------------------


async def _create_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    # Re-validate through Pydantic so the discriminated-union check, the
    # cron-expr syntax check, and the timezone check all run for MCP as
    # for REST. The inputSchema does first-pass shape checks.
    try:
        payload = ScheduledTriggerCreate.model_validate(arguments)
    except ValidationError as exc:
        raise McpInvalidParamsError(f"invalid arguments: {exc}") from exc
    # Cross-tenant admin: when payload.tenant_id is provided, route the
    # create under that tenant -- but only for tenant_admin callers.
    # Operator-role callers attempting cross-tenant create surface as
    # invalid-params (mirrors the REST 403 contract). A silent drop
    # would mis-place the trigger under the caller's own tenant and
    # quietly break the cross-tenant admin path on the MCP transport
    # (review M1 on PR #1128).
    target_tenant = operator.tenant_id
    if payload.tenant_id is not None:
        if operator.tenant_role != TenantRole.TENANT_ADMIN:
            raise McpInvalidParamsError("tenant_id_requires_tenant_admin")
        target_tenant = payload.tenant_id
    structlog.contextvars.bind_contextvars(
        audit_op_id=_SCHEDULER_OP_IDS["create"],
        audit_op_class="write",
        audit_trigger_kind=payload.kind.value,
        audit_tenant_scope=("other" if target_tenant != operator.tenant_id else "self"),
    )
    service = SchedulerAdminService()
    try:
        entry = await service.create(
            tenant_id=target_tenant,
            created_by_sub=operator.sub,
            payload=payload,
        )
    except AgentDefinitionMissingError as exc:
        raise McpInvalidParamsError("agent_definition_not_found") from exc
    structlog.contextvars.bind_contextvars(audit_trigger_id=str(entry.id))
    return {"trigger_id": str(entry.id), "trigger": _row_to_dict(entry)}


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.scheduler.create",
        description=(
            "Create one scheduled trigger under the operator's tenant "
            "(Initiative #804). Tenant_admin only. Args: kind "
            "('cron'|'one_off'|'event'), agent_definition_id (UUID of an "
            "agent definition in the operator's tenant), and exactly one "
            "of cron_expr (for kind=cron), fire_at (ISO 8601 string for "
            "kind=one_off), or event_filter (object for kind=event). "
            "Optional: timezone (IANA name, default 'UTC'), inputs (JSON "
            "object), identity_sub (default '__scheduler__'), "
            "in_flight_policy ('fail_into_audit'|'resume', default "
            "'fail_into_audit'), tenant_id (UUID; tenant_admin-only "
            "cross-tenant target). Invalid cron expression -> error with "
            "detail 'invalid_arguments'; unknown agent_definition_id -> "
            "'agent_definition_not_found'; non-admin passing tenant_id -> "
            "'tenant_id_requires_tenant_admin'. Response: {trigger_id, "
            "trigger: {...}}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["cron", "one_off", "event"],
                },
                "agent_definition_id": {
                    "type": "string",
                    "format": "uuid",
                },
                "cron_expr": {
                    "type": ["string", "null"],
                    "maxLength": 128,
                    "description": "5-field cron expression (required when kind=cron).",
                },
                "fire_at": {
                    "type": ["string", "null"],
                    "format": "date-time",
                    "description": "ISO 8601 fire time (required when kind=one_off).",
                },
                "event_filter": {
                    "type": ["object", "null"],
                    "description": "Event-match filter (required when kind=event).",
                },
                "timezone": {
                    "type": "string",
                    "maxLength": 64,
                    "description": "IANA timezone name (default 'UTC').",
                },
                "inputs": {
                    "type": ["object", "null"],
                    "description": "JSON payload forwarded as the agent run's input.",
                },
                "identity_sub": {
                    "type": "string",
                    "maxLength": 256,
                    "description": "Identity sub the scheduler impersonates at fire time.",
                },
                "in_flight_policy": {
                    "type": "string",
                    "enum": ["fail_into_audit", "resume"],
                    "description": "Killed-mid-flight policy (default fail_into_audit).",
                },
                "tenant_id": {
                    "type": ["string", "null"],
                    "format": "uuid",
                    "description": (
                        "Target tenant UUID for cross-tenant admin create "
                        "(tenant_admin only; operator-role callers see "
                        "'tenant_id_requires_tenant_admin'). When omitted "
                        "or null, the trigger is created under the "
                        "caller's tenant."
                    ),
                },
            },
            "required": ["kind", "agent_definition_id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class="write",
    ),
    handler=_create_handler,
)


# ---------------------------------------------------------------------------
# meho.scheduler.cancel
# ---------------------------------------------------------------------------


async def _cancel_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    trigger_id = _require_trigger_id(arguments)
    structlog.contextvars.bind_contextvars(
        audit_op_id=_SCHEDULER_OP_IDS["cancel"],
        audit_op_class="write",
        audit_trigger_id=str(trigger_id),
    )
    service = SchedulerAdminService()
    # Same look-up-then-act shape the REST route uses so we can
    # distinguish 404 (absent / cross-tenant) from 409 (already
    # terminal-fired).
    existing = await service.get(operator.tenant_id, trigger_id)
    if existing is None:
        raise McpInvalidParamsError("trigger_not_found")
    cancelled = await service.cancel(operator.tenant_id, trigger_id)
    if not cancelled:
        raise McpInvalidParamsError("trigger_already_fired")
    return {"trigger_id": str(trigger_id), "cancelled": True}


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.scheduler.cancel",
        description=(
            "Cancel one scheduled trigger by id (Initiative #804). "
            "Tenant_admin only. Transitions status='cancelled'; the row "
            "is retained for audit but never fires again. Idempotent on "
            "an already-cancelled trigger. Cross-tenant / absent id -> "
            "'trigger_not_found' (existence not leaked across tenants). "
            "A terminal-fired one-off -> 'trigger_already_fired'. "
            "Response: {trigger_id, cancelled: true}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "trigger_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "UUID of the trigger to cancel.",
                },
            },
            "required": ["trigger_id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class="write",
    ),
    handler=_cancel_handler,
)
