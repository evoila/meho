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
    SelfApprovalForbiddenError,
    UnauthorizedApprovalError,
    approve_request,
    get_request,
    list_pending,
    publish_approval_event,
    reject_request,
    resume_dispatch_after_approval,
)

_log = structlog.get_logger(__name__)

#: Canonical op ids — same as the REST routes for transport-independent audit rows.
_OP_IDS: Final[dict[str, str]] = {
    "list": "approval.list",
    "get": "approval.get",
    "approve": "approval.approve",
    "reject": "approval.reject",
}

#: Allowed ``status`` filter values on ``meho.approvals.list``. Mirrors
#: :class:`~meho_backplane.db.models.ApprovalRequestStatus` plus the
#: ``"all"`` sentinel that means "no filter". Pinning the enum here (and
#: in the inputSchema below) brings ``meho.approvals.list.status`` into
#: parity with ``meho.scheduler.list.status``: both surface the allowed
#: vocabulary as a JSON-Schema ``enum`` rather than prose, so a schema-
#: driven MCP client renders the same dropdown shape for sibling list
#: filters (RDC #789 N4 / G0.18-T5 #1358).
_LIST_STATUS_VALUES: Final[tuple[str, ...]] = (
    "pending",
    "approved",
    "rejected",
    "expired",
    "all",
)

#: Shared ``approval_request_id`` schema fragment with the deprecated
#: ``id`` alias. ``additionalProperties: false`` plus the explicit
#: alias declaration keeps the wire surface honest: a future schema
#: tweak adds a new field by name, never by silent passthrough.
_APPROVAL_REQUEST_ID_PROPERTY: Final[dict[str, Any]] = {
    "type": "string",
    "format": "uuid",
    "description": (
        "Approval request UUID. Canonical name "
        "(G0.18-T5 #1358); matches the `<noun>_id` convention used "
        "by every other MCP tool that names a resource UUID."
    ),
}

#: Deprecated ``id`` alias kept for backward compat with v0.8.0 callers.
_APPROVAL_LEGACY_ID_PROPERTY: Final[dict[str, Any]] = {
    "type": "string",
    "format": "uuid",
    "description": (
        "DEPRECATED alias for `approval_request_id` (v0.8.0 wire "
        "shape). Accepted for backward compatibility; new callers "
        "SHOULD use `approval_request_id`. Mutually exclusive with "
        "`approval_request_id`; passing both rejects with -32602."
    ),
    "deprecated": True,
}

#: Either alias satisfies the "id required" constraint; the handler
#: enforces the XOR. Shared across get / approve / reject.
_APPROVAL_ID_ANYOF: Final[list[dict[str, Any]]] = [
    {"required": ["approval_request_id"]},
    {"required": ["id"]},
]


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
        "work_ref": row.work_ref,
    }


def _require_id(arguments: dict[str, Any]) -> uuid.UUID:
    """Resolve the approval-request UUID from the wire arguments.

    Accepts the canonical ``approval_request_id`` (G0.18-T5 #1358) and
    the deprecated ``id`` (v0.8.0 wire shape) as aliases — exactly one
    must be supplied. Passing both rejects with -32602. The
    ``<noun>_id`` rename aligns with every other MCP tool that names a
    resource UUID (``trigger_id`` / ``audit_id`` / ``agent_session_id``);
    ``id`` is retained for one cycle so v0.8.0 callers continue to work.
    """
    canonical = arguments.get("approval_request_id")
    legacy = arguments.get("id")
    if canonical is not None and legacy is not None:
        raise McpInvalidParamsError(
            "pass either `approval_request_id` (canonical) or `id` (deprecated alias), not both",
        )
    raw = canonical if canonical is not None else legacy
    if not isinstance(raw, str) or not raw:
        raise McpInvalidParamsError(
            "approval_request_id is required and must be a non-empty UUID string",
        )
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise McpInvalidParamsError("approval_request_id must be a valid UUID") from exc


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
    work_ref_raw = arguments.get("work_ref")
    work_ref_filter: str | None = None
    if work_ref_raw is not None:
        if not isinstance(work_ref_raw, str) or not work_ref_raw:
            raise McpInvalidParamsError("work_ref must be a non-empty string when supplied")
        work_ref_filter = work_ref_raw

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = await list_pending(
            session,
            tenant_id=operator.tenant_id,
            status=status_filter,
            work_ref=work_ref_filter,
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
                    "enum": list(_LIST_STATUS_VALUES),
                    "default": "pending",
                    "description": (
                        "Filter by status. 'all' is the sentinel meaning "
                        "'no filter'. Vocabulary mirrors "
                        "`meho.scheduler.list.status` (both surface the "
                        "allowed values as a JSON enum, not prose) — "
                        "RDC #789 N4 / G0.18-T5."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 50,
                    "description": "Page size. Default 50; max 200.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "Rows to skip before the first returned row. Default 0.",
                },
                "work_ref": {
                    "type": "string",
                    "description": (
                        "Filter by external change-ticket reference (exact "
                        "match), e.g. 'gh:evoila/meho#1' — the requests "
                        "authorised by change ticket X (work_ref I2-T1 "
                        "#1659). Omit for no work_ref filter."
                    ),
                },
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
            "Cross-tenant / absent ids return approval_request_not_found. "
            "Pass either `approval_request_id` (canonical name; "
            "G0.18-T5 #1358) or the deprecated `id` alias."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "approval_request_id": _APPROVAL_REQUEST_ID_PROPERTY,
                "id": _APPROVAL_LEGACY_ID_PROPERTY,
            },
            "anyOf": _APPROVAL_ID_ANYOF,
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
    reason = arguments.get("reason")
    reason_str = str(reason) if reason is not None else ""
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
            # decision audit row. The params for the re-dispatch are read
            # back from the row (stored at park time, #1503).
            row = await approve_request(
                session,
                request_id,
                operator=operator,
                params=None,
                reason=reason_str,
            )
            await session.commit()
        except ApprovalNotFoundError as exc:
            raise McpInvalidParamsError("approval_request_not_found") from exc
        except ApprovalRequestAlreadyDecidedError as exc:
            raise McpInvalidParamsError(
                f"approval_request_not_pending: current status is {exc.status!r}"
            ) from exc
        except SelfApprovalForbiddenError as exc:
            # G11.7-T1 #1401: requester != approver. Surfaced as an
            # invalid-params error so the operator sees the refusal
            # reason rather than a generic role failure. Append
            # ``str(exc)`` so the message also carries the
            # ``APPROVAL_ALLOW_SELF_APPROVAL=true`` break-glass hint the
            # exception already constructs (#1483).
            raise McpInvalidParamsError(f"self_approval_forbidden: {exc}") from exc
        except UnauthorizedApprovalError as exc:
            raise McpInvalidParamsError(
                f"approval_unauthorized: role {exc.role!r} cannot approve"
            ) from exc
    # Publish AFTER commit (fail-open).
    await publish_approval_event(
        tenant_id=operator.tenant_id,
        request=row,
        decision="approved",
        principal_sub=operator.sub,
        audit_id=row._audit_id,  # type: ignore[attr-defined]
    )

    result = _row_to_dict(row)

    # Every approval drives the execute (#1503, #2293): the committed
    # approval is the authorization; the stored params re-hydrate the
    # dispatch. The exactly-one-resumer claim inside
    # resume_dispatch_after_approval arbitrates against the in-process
    # agent waiter for a run-bound request — this MCP path executes only
    # when the claim is free (the waiter had died: wait-timeout, pod
    # restart, run cancelled), else it no-ops with status
    # "already_resumed". The old run_id-is-not-None skip was the source of
    # the silent-non-execution seam when the waiter was gone.
    dispatch_result = await resume_dispatch_after_approval(
        operator=operator, request=row, params=None
    )
    _log.info(
        "approval_request_redispatched",
        approval_request_id=str(request_id),
        op_id=row.op_id,
        dispatch_status=dispatch_result.status,
        operator_sub=operator.sub,
        via="mcp",
    )
    result["dispatch"] = {
        "status": dispatch_result.status,
        "op_id": dispatch_result.op_id,
        "result": dispatch_result.result,
        "error": dispatch_result.error,
    }

    return result


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.approvals.approve",
        description=(
            "Approve a pending approval request (G11.2-T5 / #818; #1503; "
            "#2293). Operator-level. Flips the request to 'approved', writes "
            "the decision audit row, and announces approval_decided on the "
            "broadcast feed. It then re-dispatches the op using the params "
            "stored at park time and returns the outcome under `dispatch`. "
            "The exactly-one-resumer claim makes this safe for an agent-run "
            "request: `dispatch.status` is 'ok' when this approve executed "
            "it (a direct op, or the fallback when the in-process agent "
            "waiter was gone), or 'already_resumed' when the live waiter "
            "executed it first — so the approved write lands exactly once. "
            "Only pending requests may be approved; any other status "
            "returns approval_request_not_pending. Pass either "
            "`approval_request_id` (canonical name; G0.18-T5 #1358) or the "
            "deprecated `id` alias."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "approval_request_id": _APPROVAL_REQUEST_ID_PROPERTY,
                "id": _APPROVAL_LEGACY_ID_PROPERTY,
                "reason": {
                    "type": "string",
                    "description": "Optional rationale recorded on the decision audit row.",
                },
            },
            "anyOf": _APPROVAL_ID_ANYOF,
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
    # Publish AFTER commit (fail-open).
    await publish_approval_event(
        tenant_id=operator.tenant_id,
        request=row,
        decision="rejected",
        principal_sub=operator.sub,
        audit_id=row._audit_id,  # type: ignore[attr-defined]
    )
    return _row_to_dict(row)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.approvals.reject",
        description=(
            "Reject a pending approval request (G11.2-T5 / #818). "
            "Operator-level. Flips the request to 'rejected', writes the "
            "decision audit row, and announces approval_decided on the "
            "broadcast feed. The original op is not executed. Only "
            "pending requests may be rejected. Pass either "
            "`approval_request_id` (canonical name; G0.18-T5 #1358) or "
            "the deprecated `id` alias."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "approval_request_id": _APPROVAL_REQUEST_ID_PROPERTY,
                "id": _APPROVAL_LEGACY_ID_PROPERTY,
                "reason": {
                    "type": "string",
                    "description": "Optional rationale recorded on the decision audit row.",
                },
            },
            "anyOf": _APPROVAL_ID_ANYOF,
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
    ),
    handler=_reject_handler,
)
