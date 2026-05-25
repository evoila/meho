# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/approvals`` — approval surfacing channel REST routes.

Initiative #803 (G11.2 Agent identity + RBAC + approval), Task #818
(T5). The REST surface operators and automated tooling use to list
pending approval requests, inspect the proposed effect, and post an
approve / reject decision.

Route inventory
---------------

* ``GET  /api/v1/approvals`` — list approval requests (``?status=pending``,
  ``?limit=N``, ``?offset=N``). Role: ``operator``.
* ``GET  /api/v1/approvals/{id}`` — inspect one request. Role: ``operator``.
* ``POST /api/v1/approvals/{id}/approve`` — approve a pending request.
  Body: :class:`~meho_backplane.approvals.schemas.ApprovalDecision`.
  Role: ``operator``.
* ``POST /api/v1/approvals/{id}/reject`` — reject a pending request.
  Body: :class:`~meho_backplane.approvals.schemas.ApprovalDecision`.
  Role: ``operator``.
* ``POST /api/v1/approvals/{id}/decide`` — MCP elicitation URL-mode
  endpoint. Accepts ``{"decision": "approve"|"reject", "reason": "..."}``
  so MCP clients that support elicitation URL-mode can POST a structured
  decision directly. Role: ``operator``.

RBAC + tenant scoping
---------------------

Every route derives ``tenant_id`` from the validated JWT
(:class:`~meho_backplane.auth.operator.Operator`) and requires the
``operator`` role minimum. Read-only operators are blocked on write
routes (approve/reject/decide). Cross-tenant requests 404 — existence
is never leaked across tenants.

MCP elicitation URL-mode
-------------------------

The ``/decide`` endpoint is the MCP elicitation URL-mode wire target.
Per the 2025-11-25 MCP spec (https://workos.com/blog/mcp-elicitation),
an MCP server can surface this URL in its ``elicitation/create`` call
so the host application can open the operator's browser / UI to the
decision form. The endpoint accepts the same structured body an
in-browser form would POST.

Audit rows
----------

Approve/reject/decide bind ``audit_op_id`` / ``audit_op_class``
contextvars before the DB call so the :class:`~meho_backplane.audit.AuditMiddleware`
classifies each decision correctly. The request-row's ``decision_audit_id``
is pre-allocated and injected so the middleware and the service row stay
in sync (the same discipline :mod:`meho_backplane.api.v1.conventions`
uses for history rows).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi import status as http_status

from meho_backplane.approvals.schemas import (
    ApprovalDecision,
    ApprovalListResponse,
    ApprovalRequestDetail,
)
from meho_backplane.approvals.service import (
    ApprovalDecisionError,
    ApprovalNotFoundError,
    ApprovalRequestService,
)
from meho_backplane.audit import bind_preallocated_audit_id
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.models import ApprovalStatus
from meho_backplane.settings import get_settings

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])

_log = structlog.get_logger(__name__)

#: Operator-minimum gate (ruff B008: no call in default-arg position).
_require_operator = Depends(require_role(TenantRole.OPERATOR))

#: Canonical op ids bound into ``audit_op_id`` per route.
_OP_IDS: Final[dict[str, str]] = {
    "list": "approval.list",
    "get": "approval.get",
    "approve": "approval.approve",
    "reject": "approval.reject",
    "decide": "approval.decide",
}


def _get_service(operator: Operator) -> ApprovalRequestService:
    """Build a service instance scoped to the operator's request."""
    settings = get_settings()
    base_url = settings.backplane_url or None
    return ApprovalRequestService(base_url=base_url)


@router.get("", response_model=ApprovalListResponse)
async def list_approvals(
    operator: Annotated[Operator, _require_operator],
    status: Annotated[
        str | None,
        Query(description="Filter by status (pending|approved|rejected|expired)."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ApprovalListResponse:
    """List approval requests for the operator's tenant.

    Returns newest-first, paginated. The ``status=pending`` query is the
    most common operator pattern: "show me what I need to decide on".
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["list"],
        audit_op_class="read",
    )
    svc = _get_service(operator)
    status_filter: ApprovalStatus | None = None
    if status is not None:
        try:
            status_filter = ApprovalStatus(status)
        except ValueError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unknown status {status!r}; valid: pending, approved, rejected, expired",
            ) from exc
    return await svc.list_(
        tenant_id=operator.tenant_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )


@router.get("/{request_id}", response_model=ApprovalRequestDetail)
async def get_approval(
    request_id: Annotated[uuid.UUID, Path()],
    operator: Annotated[Operator, _require_operator],
) -> ApprovalRequestDetail:
    """Inspect a single approval request.

    Returns the full detail including ``proposed_effect`` and the
    ``elicitation_url`` for MCP elicitation URL-mode integration.
    Cross-tenant requests and absent ids both 404.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["get"],
        audit_op_class="read",
        audit_approval_request_id=str(request_id),
    )
    svc = _get_service(operator)
    try:
        return await svc.get(tenant_id=operator.tenant_id, request_id=request_id)
    except ApprovalNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="approval_request_not_found",
        ) from exc


@router.post("/{request_id}/approve", response_model=ApprovalRequestDetail)
async def approve_approval(
    request_id: Annotated[uuid.UUID, Path()],
    body: ApprovalDecision,
    operator: Annotated[Operator, _require_operator],
) -> ApprovalRequestDetail:
    """Approve a pending approval request.

    Flips the request to ``approved``, calls the T4 resume path to
    continue the paused agent run, and publishes a ``approval.approved``
    broadcast event. Only ``pending`` requests may be approved; any other
    status yields 409.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["approve"],
        audit_op_class="write",
        audit_approval_request_id=str(request_id),
    )
    decision_audit_id = uuid.uuid4()
    bind_preallocated_audit_id(decision_audit_id)
    svc = _get_service(operator)
    try:
        return await svc.approve(
            tenant_id=operator.tenant_id,
            request_id=request_id,
            reviewer_sub=operator.sub,
            body=body,
            decision_audit_id=decision_audit_id,
        )
    except ApprovalNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="approval_request_not_found",
        ) from exc
    except ApprovalDecisionError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"approval_request_not_pending: current status is {exc.current_status!r}",
        ) from exc


@router.post("/{request_id}/reject", response_model=ApprovalRequestDetail)
async def reject_approval(
    request_id: Annotated[uuid.UUID, Path()],
    body: ApprovalDecision,
    operator: Annotated[Operator, _require_operator],
) -> ApprovalRequestDetail:
    """Reject a pending approval request.

    Flips the request to ``rejected``, calls the T4 resume path to
    abort the paused agent run, and publishes a ``approval.rejected``
    broadcast event. Only ``pending`` requests may be rejected; any other
    status yields 409.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["reject"],
        audit_op_class="write",
        audit_approval_request_id=str(request_id),
    )
    decision_audit_id = uuid.uuid4()
    bind_preallocated_audit_id(decision_audit_id)
    svc = _get_service(operator)
    try:
        return await svc.reject(
            tenant_id=operator.tenant_id,
            request_id=request_id,
            reviewer_sub=operator.sub,
            body=body,
            decision_audit_id=decision_audit_id,
        )
    except ApprovalNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="approval_request_not_found",
        ) from exc
    except ApprovalDecisionError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"approval_request_not_pending: current status is {exc.current_status!r}",
        ) from exc


class _DecideBody(ApprovalDecision):
    """Extended body for the MCP elicitation URL-mode decide endpoint.

    The MCP elicitation spec requires a ``decision`` field in the submitted
    content. The REST approve/reject verbs encode the decision in the URL
    path; the elicitation URL-mode endpoint uses a single path + a body
    field so MCP clients that construct the elicitation form dynamically
    have one stable URL to point at.
    """

    decision: str  # "approve" | "reject"


@router.post("/{request_id}/decide", response_model=ApprovalRequestDetail)
async def decide_approval(
    request_id: Annotated[uuid.UUID, Path()],
    body: _DecideBody,
    operator: Annotated[Operator, _require_operator],
) -> ApprovalRequestDetail:
    """MCP elicitation URL-mode decision endpoint.

    Accepts ``{"decision": "approve"|"reject", "reason": "..."}`` from
    an MCP elicitation UI. Routes to :func:`approve_approval` or
    :func:`reject_approval` based on the ``decision`` field.

    This endpoint is the ``elicitation_url`` value the ``GET /{id}``
    response carries; an MCP client that received this URL via an
    ``elicitation/create`` response can POST the operator's structured
    answer here.

    See https://workos.com/blog/mcp-elicitation for the MCP elicitation
    URL-mode specification.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["decide"],
        audit_op_class="write",
        audit_approval_request_id=str(request_id),
        audit_decision=body.decision,
    )
    if body.decision not in {"approve", "reject"}:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="decision must be 'approve' or 'reject'",
        )
    decision_audit_id = uuid.uuid4()
    bind_preallocated_audit_id(decision_audit_id)
    svc = _get_service(operator)
    base_body = ApprovalDecision(reason=body.reason)
    try:
        if body.decision == "approve":
            return await svc.approve(
                tenant_id=operator.tenant_id,
                request_id=request_id,
                reviewer_sub=operator.sub,
                body=base_body,
                decision_audit_id=decision_audit_id,
            )
        else:
            return await svc.reject(
                tenant_id=operator.tenant_id,
                request_id=request_id,
                reviewer_sub=operator.sub,
                body=base_body,
                decision_audit_id=decision_audit_id,
            )
    except ApprovalNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="approval_request_not_found",
        ) from exc
    except ApprovalDecisionError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"approval_request_not_pending: current status is {exc.current_status!r}",
        ) from exc
