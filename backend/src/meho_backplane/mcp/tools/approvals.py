# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MCP tools for the approval surfacing channel (G11.2-T5 / #818).

Four ``meho.approvals.*`` tools that mirror the REST routes
(:mod:`meho_backplane.api.v1.approvals`) onto the MCP transport:

* ``meho.approvals.list`` — list approval requests, optionally filtered
  by status. Role: ``operator``.
* ``meho.approvals.get`` — inspect one approval request by id. Returns
  the ``proposed_effect`` and the ``elicitation_url`` forward wire
  address for MCP elicitation URL-mode. Role: ``operator``.
* ``meho.approvals.approve`` — approve a pending request. Role:
  ``operator``.
* ``meho.approvals.reject`` — reject a pending request. Role:
  ``operator``.

All tools drive the same :class:`~meho_backplane.approvals.service.ApprovalRequestService`
the REST routes use, so a decision made over MCP is reflected in the
REST/CLI view immediately — the durable ``approval_request`` row is the
shared state.

MCP elicitation URL-mode
------------------------

The ``elicitation_url`` field on ``meho.approvals.get`` results is the
MCP elicitation URL-mode address for the request. When an in-loop agent
hits a ``needs-approval`` verdict, the agent runtime can call
``elicitation/create`` with this URL as the ``url`` field and the
structured ``requestedSchema`` described in
:mod:`meho_backplane.approvals.schemas` so the MCP host application can
open the operator's decision UI directly.

Error mapping
-------------

:class:`~meho_backplane.approvals.service.ApprovalNotFoundError` maps to
:class:`~meho_backplane.mcp.server.McpInvalidParamsError` (JSON-RPC
``-32602``). :class:`~meho_backplane.approvals.service.ApprovalDecisionError`
maps to a typed error result (``approval_request_not_pending``).
"""

from __future__ import annotations

import uuid
from typing import Any, Final

import structlog

from meho_backplane.approvals.schemas import ApprovalDecision
from meho_backplane.approvals.service import (
    ApprovalDecisionError,
    ApprovalNotFoundError,
    ApprovalRequestService,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.models import ApprovalStatus
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.settings import get_settings

_log = structlog.get_logger(__name__)

#: Canonical op ids — same as the REST routes for transport-independent audit rows.
_OP_IDS: Final[dict[str, str]] = {
    "list": "approval.list",
    "get": "approval.get",
    "approve": "approval.approve",
    "reject": "approval.reject",
}


def _get_service() -> ApprovalRequestService:
    settings = get_settings()
    return ApprovalRequestService(base_url=settings.backplane_url or None)


def _require_id(arguments: dict[str, Any]) -> uuid.UUID:
    raw = arguments.get("id")
    if not isinstance(raw, str) or not raw:
        raise McpInvalidParamsError("id is required and must be a non-empty UUID string")
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise McpInvalidParamsError("id must be a valid UUID") from exc


# ---------------------------------------------------------------------------
# meho.approvals.list
# ---------------------------------------------------------------------------


async def _list_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["list"],
        audit_op_class="read",
    )
    status_raw = arguments.get("status")
    status_filter: ApprovalStatus | None = None
    if status_raw is not None:
        try:
            status_filter = ApprovalStatus(str(status_raw))
        except ValueError as exc:
            raise McpInvalidParamsError(
                f"unknown status {status_raw!r}; valid: pending, approved, rejected, expired"
            ) from exc
    limit = int(arguments.get("limit", 50))
    offset = int(arguments.get("offset", 0))
    svc = _get_service()
    result = await svc.list_(
        tenant_id=operator.tenant_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [item.model_dump(mode="json") for item in result.items],
        "total": result.total,
        "limit": result.limit,
        "offset": result.offset,
    }


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.approvals.list",
        description=(
            "List pending (or all) approval requests for your tenant "
            "(G11.2-T5 / #818). Operator-level. Use status='pending' for the "
            "most common operator query: requests awaiting a decision. "
            "Pagination via limit / offset. Returns {items, total, limit, offset}; "
            "each item carries id, connector_id, op_id, principal_sub, status, "
            "created_at."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "approved", "rejected", "expired"],
                    "description": "Filter by status (omit for all).",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": "Max items per page (default 50).",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Pagination offset (default 0).",
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
# meho.approvals.get
# ---------------------------------------------------------------------------


async def _get_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    request_id = _require_id(arguments)
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["get"],
        audit_op_class="read",
        audit_approval_request_id=str(request_id),
    )
    svc = _get_service()
    try:
        detail = await svc.get(tenant_id=operator.tenant_id, request_id=request_id)
    except ApprovalNotFoundError as exc:
        raise McpInvalidParamsError("approval_request_not_found") from exc
    return detail.model_dump(mode="json")


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.approvals.get",
        description=(
            "Inspect a single approval request by id (G11.2-T5 / #818). "
            "Operator-level. Returns the full detail including proposed_effect "
            "(human-readable description of what the op would do) and "
            "elicitation_url — the MCP elicitation URL-mode address operators "
            "or host applications can use to post a structured decision. "
            "Cross-tenant / absent ids return approval_request_not_found."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Approval request UUID.",
                },
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class="read",
    ),
    handler=_get_handler,
)


# ---------------------------------------------------------------------------
# meho.approvals.approve
# ---------------------------------------------------------------------------


async def _approve_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    request_id = _require_id(arguments)
    reason = arguments.get("reason")
    reason_str = str(reason) if reason is not None else None
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["approve"],
        audit_op_class="write",
        audit_approval_request_id=str(request_id),
    )
    svc = _get_service()
    try:
        detail = await svc.approve(
            tenant_id=operator.tenant_id,
            request_id=request_id,
            reviewer_sub=operator.sub,
            body=ApprovalDecision(reason=reason_str),
        )
    except ApprovalNotFoundError as exc:
        raise McpInvalidParamsError("approval_request_not_found") from exc
    except ApprovalDecisionError as exc:
        raise McpInvalidParamsError(
            f"approval_request_not_pending: current status is {exc.current_status!r}"
        ) from exc
    return detail.model_dump(mode="json")


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.approvals.approve",
        description=(
            "Approve a pending approval request (G11.2-T5 / #818). "
            "Operator-level. Flips the request to approved, resumes the paused "
            "agent run (T4 path), and announces the decision on the broadcast feed. "
            "Only pending requests may be approved — any other status returns "
            "approval_request_not_pending. Absent / cross-tenant ids return "
            "approval_request_not_found."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Approval request UUID to approve.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional rationale for the approval.",
                },
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class="write",
    ),
    handler=_approve_handler,
)


# ---------------------------------------------------------------------------
# meho.approvals.reject
# ---------------------------------------------------------------------------


async def _reject_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    request_id = _require_id(arguments)
    reason = arguments.get("reason")
    reason_str = str(reason) if reason is not None else None
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["reject"],
        audit_op_class="write",
        audit_approval_request_id=str(request_id),
    )
    svc = _get_service()
    try:
        detail = await svc.reject(
            tenant_id=operator.tenant_id,
            request_id=request_id,
            reviewer_sub=operator.sub,
            body=ApprovalDecision(reason=reason_str),
        )
    except ApprovalNotFoundError as exc:
        raise McpInvalidParamsError("approval_request_not_found") from exc
    except ApprovalDecisionError as exc:
        raise McpInvalidParamsError(
            f"approval_request_not_pending: current status is {exc.current_status!r}"
        ) from exc
    return detail.model_dump(mode="json")


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.approvals.reject",
        description=(
            "Reject a pending approval request (G11.2-T5 / #818). "
            "Operator-level. Flips the request to rejected, aborts the paused "
            "agent run (T4 path), and announces the decision on the broadcast feed. "
            "Only pending requests may be rejected — any other status returns "
            "approval_request_not_pending. Absent / cross-tenant ids return "
            "approval_request_not_found."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Approval request UUID to reject.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional rationale for the rejection.",
                },
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class="write",
    ),
    handler=_reject_handler,
)
