# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MCP tools for the approval surfacing channel (G11.2-T5 / #818).

Four ``meho.approvals.*`` tools that mirror the REST routes
(:mod:`meho_backplane.api.v1.approvals`) onto the MCP transport:

* ``meho.approvals.list`` — list approval requests, optionally filtered
  by status. Role: ``operator``.
* ``meho.approvals.get`` — inspect one approval request by id. Returns
  the ``proposed_effect`` so an operator can decide before approving.
  Role: ``operator``.
* ``meho.approvals.approve`` — approve a pending request (operator
  decision: status flip + audit + broadcast; **no** params required —
  the agent's REST path retains the params-hash check).
  Role: ``operator``.
* ``meho.approvals.reject`` — reject a pending request. Role:
  ``operator``.

All four tools drive
:mod:`meho_backplane.operations.approval_queue` — the single source of
truth that T4 (#817) shipped — so a decision made over MCP is
reflected in the REST/CLI view immediately and writes the same
synchronous "decision" audit row and ``approval_decided`` broadcast
event as the REST path. RBAC is enforced at two layers: the registry
filter hides write tools from non-admins in ``tools/list``, and the
MCP dispatcher re-checks ``required_role`` at call time.

MCP elicitation URL-mode (forward-looking)
------------------------------------------

When an in-loop agent hits a ``needs-approval`` verdict, the agent
runtime can use the row's ``id`` (returned from ``meho.approvals.get``)
to construct an elicitation URL of the form
``meho://approvals/{request_id}/decide``. MCP-2025-11-25 hosts that
support elicitation URL-mode open this URL in the operator's decision
UI; until that lands the operator approves/rejects via the explicit
``meho.approvals.{approve,reject}`` tools above.

Error mapping
-------------

* :class:`~meho_backplane.operations.approval_queue.ApprovalNotFoundError`
  → :class:`~meho_backplane.mcp.server.McpInvalidParamsError` with code
  ``approval_request_not_found``.
* :class:`~meho_backplane.operations.approval_queue.ApprovalRequestAlreadyDecidedError`
  → ``approval_request_not_pending``.
* :class:`~meho_backplane.operations.approval_queue.UnauthorizedApprovalError`
  → ``approval_unauthorized``.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

import structlog

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.operations.approval_queue import (
    ApprovalNotFoundError,
    ApprovalRequestAlreadyDecidedError,
    UnauthorizedApprovalError,
    approve_request,
    get_request,
    list_pending,
    reject_request,
)

_log = structlog.get_logger(__name__)

#: Canonical op ids — same as the REST routes for transport-independent audit rows.
_OP_IDS: Final[dict[str, str]] = {
    "list": "approval.list",
    "get": "approval.get",
    "approve": "approval.approve",
    "reject": "approval.reject",
}


def _row_to_dict(row: ApprovalRequest) -> dict[str, Any]:
    """Render an :class:`ApprovalRequest` as a JSON-serialisable dict.

    Inlined here (rather than importing the REST route's pydantic view)
    to keep the MCP transport independent of the FastAPI surface.
    """
    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "run_id": str(row.run_id) if row.run_id else None,
        "principal_sub": row.principal_sub,
        "principal_act": row.principal_act,
        "op_id": row.op_id,
        "connector_id": row.connector_id,
        "target_id": str(row.target_id) if row.target_id else None,
        "proposed_effect": row.proposed_effect,
        "status": row.status,
        "reviewed_by": row.reviewed_by,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


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
    status_raw = arguments.get("status", "pending")
    status_filter: str | None
    if status_raw is None or status_raw == "all":
        status_filter = None
    else:
        try:
            status_filter = ApprovalRequestStatus(str(status_raw)).value
        except ValueError as exc:
            raise McpInvalidParamsError(
                f"unknown status {status_raw!r}; valid: pending, approved, rejected, expired, all"
            ) from exc
    limit = int(arguments.get("limit", 50))
    offset = int(arguments.get("offset", 0))

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = await list_pending(
            session,
            tenant_id=operator.tenant_id,
            status=status_filter,
            limit=limit,
            offset=offset,
        )
    return {
        "items": [_row_to_dict(r) for r in rows],
        "limit": limit,
        "offset": offset,
    }


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.approvals.list",
        description=(
            "List approval requests for your tenant (G11.2-T5 / #818). "
            "Operator-level. Use status='pending' (default) for the "
            "common case — requests awaiting a decision. Pass status='all' "
            "for every state. Pagination via limit / offset."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": (
                        "Filter by status: 'pending' (default), 'approved', "
                        "'rejected', 'expired', or 'all'."
                    ),
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
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
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            row = await get_request(
                session,
                tenant_id=operator.tenant_id,
                request_id=request_id,
            )
        except ApprovalNotFoundError as exc:
            raise McpInvalidParamsError("approval_request_not_found") from exc
    return _row_to_dict(row)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.approvals.get",
        description=(
            "Inspect a single approval request by id (G11.2-T5 / #818). "
            "Operator-level. Returns the full detail including "
            "proposed_effect (human-readable description of what the op "
            "would do) so an operator can decide before approving. "
            "Cross-tenant / absent ids return approval_request_not_found."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Approval request UUID."},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
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
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["approve"],
        audit_op_class="write",
        audit_approval_request_id=str(request_id),
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            # Operator-decision path: no params supplied → approve_request
            # skips the hash check and just flips status + writes the
            # decision audit row + publishes the approval_decided event.
            # The agent's REST path (with params) is what re-dispatches.
            row = await approve_request(
                session,
                request_id,
                operator=operator,
                params=None,
            )
            await session.commit()
        except ApprovalNotFoundError as exc:
            raise McpInvalidParamsError("approval_request_not_found") from exc
        except ApprovalRequestAlreadyDecidedError as exc:
            raise McpInvalidParamsError(
                f"approval_request_not_pending: current status is {exc.status!r}"
            ) from exc
        except UnauthorizedApprovalError as exc:
            raise McpInvalidParamsError(
                f"approval_unauthorized: role {exc.role!r} cannot approve"
            ) from exc
    return _row_to_dict(row)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.approvals.approve",
        description=(
            "Approve a pending approval request (G11.2-T5 / #818). "
            "Operator-level. Flips the request to 'approved', writes the "
            "decision audit row, and announces approval_decided on the "
            "broadcast feed. The agent's REST resume path is what "
            "re-dispatches the approved op (with the params it has + the "
            "_approved gate-bypass) — this MCP tool captures the operator "
            "decision durably. Only pending requests may be approved; any "
            "other status returns approval_request_not_pending."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Approval request UUID to approve."},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
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
    reason_str = str(reason) if reason is not None else ""
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["reject"],
        audit_op_class="write",
        audit_approval_request_id=str(request_id),
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            row = await reject_request(
                session,
                request_id,
                operator=operator,
                reason=reason_str,
            )
            await session.commit()
        except ApprovalNotFoundError as exc:
            raise McpInvalidParamsError("approval_request_not_found") from exc
        except ApprovalRequestAlreadyDecidedError as exc:
            raise McpInvalidParamsError(
                f"approval_request_not_pending: current status is {exc.status!r}"
            ) from exc
        except UnauthorizedApprovalError as exc:
            raise McpInvalidParamsError(
                f"approval_unauthorized: role {exc.role!r} cannot reject"
            ) from exc
    return _row_to_dict(row)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.approvals.reject",
        description=(
            "Reject a pending approval request (G11.2-T5 / #818). "
            "Operator-level. Flips the request to 'rejected', writes the "
            "decision audit row, and announces approval_decided on the "
            "broadcast feed. The original op is not executed. Only "
            "pending requests may be rejected."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Approval request UUID to reject."},
                "reason": {
                    "type": "string",
                    "description": "Optional rationale recorded on the decision audit row.",
                },
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
    ),
    handler=_reject_handler,
)
