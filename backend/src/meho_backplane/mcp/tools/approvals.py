# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MCP tools for the approval surfacing channel (G11.2-T5 / #818).

Two **read** ``meho_approvals_*`` tools that mirror the REST routes
(:mod:`meho_backplane.api.v1.approvals`) onto the MCP transport:

* ``meho_approvals_list`` — list approval requests, optionally filtered
  by status. Role: ``operator``.
* ``meho_approvals_get`` — inspect one approval request by id. Returns
  the ``proposed_effect`` so an operator can decide before approving.
  Role: ``operator``.

The decision verbs — approve and reject — have **no MCP path under any
claim set** (#3155, Initiative #3153). Approving or rejecting a parked
operation is a *human* decision (v0.1-spec §7); a model session that
parked an op must not be able to approve it (the self-approval hole
#3143 F4 surfaced). Both stay reachable on the REST / CLI / console
surfaces, which are untouched:
:mod:`meho_backplane.api.v1.approvals`, ``meho approvals
approve|reject``, and the operator console approvals queue. The wire
names are pinned in :mod:`meho_backplane.mcp.human_only` so a
``tools/call`` on them returns a remediation naming those paths.

Both read tools drive
:mod:`meho_backplane.operations.approval_queue` — the single source of
truth that T4 (#817) shipped — so the MCP view matches the REST/CLI
view immediately. RBAC is enforced at two layers: the registry filter
hides tools from non-operators in ``tools/list``, and the MCP
dispatcher re-checks ``required_role`` at call time.

Error mapping
-------------

* :class:`~meho_backplane.operations.approval_queue.ApprovalNotFoundError`
  → :class:`~meho_backplane.mcp.server.McpInvalidParamsError` with code
  ``approval_request_not_found``.
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
    get_request,
    list_pending,
)

#: Canonical op ids — same as the REST routes for transport-independent audit rows.
_OP_IDS: Final[dict[str, str]] = {
    "list": "approval.list",
    "get": "approval.get",
}

#: Allowed ``status`` filter values on ``meho_approvals_list``. Mirrors
#: :class:`~meho_backplane.db.models.ApprovalRequestStatus` plus the
#: ``"all"`` sentinel that means "no filter". Pinning the enum here (and
#: in the inputSchema below) brings ``meho_approvals_list.status`` into
#: parity with ``meho_scheduler_list.status``: both surface the allowed
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
# meho_approvals_list
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
        feature="approvals",
        name="meho_approvals_list",
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
                        "`meho_scheduler_list.status` (both surface the "
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
# meho_approvals_get
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
        feature="approvals",
        name="meho_approvals_get",
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
