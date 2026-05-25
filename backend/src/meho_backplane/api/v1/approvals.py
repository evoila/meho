# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/approvals/*`` — approval queue REST surface.

Initiative #803 (G11.2 Agent permission model), Task #817 (T4). Three
routes that let operators review, approve, or reject durable
:class:`~meho_backplane.db.models.ApprovalRequest` rows written by the
dispatcher when a ``requires_approval`` op is dispatched.

Route inventory
---------------

* ``GET /api/v1/approvals`` — list pending requests for the operator's
  tenant. Optional ``?status=`` filter (default ``pending``). Role:
  ``operator``.
* ``POST /api/v1/approvals/{request_id}/approve`` — approve a pending
  request and re-dispatch the original call with the original params.
  Body: :class:`ApproveRequestBody` (``params`` dict). Role:
  ``operator``.
* ``POST /api/v1/approvals/{request_id}/reject`` — reject a pending
  request; the original dispatch is not executed. Body:
  :class:`RejectRequestBody` (optional ``reason`` string). Role:
  ``operator``.

Tenant scoping
--------------

Every route derives ``tenant_id`` from the JWT-validated
:class:`~meho_backplane.auth.operator.Operator`; cross-tenant access to
an approval request is indistinguishable from a missing request (404).

Audit trail
-----------

:func:`~meho_backplane.operations.approval_queue.approve_request` and
:func:`~meho_backplane.operations.approval_queue.reject_request` write
their "decision" audit rows synchronously inside the same transaction as
the row update. The routes commit only after the audit row flushes, so
the two always land together.

Resume-then-dispatch
--------------------

After the ``approve`` route commits the decision, it calls
:func:`~meho_backplane.operations.dispatcher.dispatch` with the stored
``connector_id`` / ``op_id`` / ``target_id`` / and the reviewer-supplied
``params``. The re-dispatch result is returned in the response body so
the reviewing operator sees the actual execution outcome.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.operations.approval_queue import (
    ApprovalNotFoundError,
    ApprovalRequestAlreadyDecidedError,
    ParamsMismatchError,
    UnauthorizedApprovalError,
    approve_request,
    reject_request,
)

__all__ = ["router"]

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])

#: Module-level Depends closures -- avoids ruff B008 (mutable call in
#: default-argument position). Same pattern as
#: :mod:`meho_backplane.api.v1.operations`.
_require_operator = Depends(require_role(TenantRole.OPERATOR))


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ApprovalRequestView(BaseModel):
    """Read-only view of an :class:`~meho_backplane.db.models.ApprovalRequest`."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    run_id: uuid.UUID | None
    principal_sub: str
    principal_act: str | None
    op_id: str
    connector_id: str
    target_id: uuid.UUID | None
    params_hash: str
    proposed_effect: dict[str, Any]
    status: ApprovalRequestStatus
    reviewed_by: str | None
    decided_at: str | None
    created_at: str
    expires_at: str | None


class ApproveRequestBody(BaseModel):
    """POST body for ``…/approve``."""

    model_config = ConfigDict(extra="forbid")

    params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The original dispatch params, unchanged. The hash must match "
            "the stored params_hash on the approval request."
        ),
    )


class RejectRequestBody(BaseModel):
    """POST body for ``…/reject``."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        default="",
        description="Optional human-readable rejection reason (recorded in the audit row).",
    )


class ApproveResponseBody(BaseModel):
    """Response for a successful approve + re-dispatch."""

    model_config = ConfigDict(frozen=True)

    approval_request_id: uuid.UUID
    decision: str  # "approved"
    dispatch_status: str
    dispatch_op_id: str
    dispatch_result: dict[str, Any] | list[Any] | None
    dispatch_error: str | None


class RejectResponseBody(BaseModel):
    """Response for a successful rejection."""

    model_config = ConfigDict(frozen=True)

    approval_request_id: uuid.UUID
    decision: str  # "rejected"
    reason: str


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _view(row: ApprovalRequest) -> ApprovalRequestView:
    """Convert an ORM row to a response view."""
    return ApprovalRequestView(
        id=row.id,
        tenant_id=row.tenant_id,
        run_id=row.run_id,
        principal_sub=row.principal_sub,
        principal_act=row.principal_act,
        op_id=row.op_id,
        connector_id=row.connector_id,
        target_id=row.target_id,
        params_hash=row.params_hash,
        proposed_effect=row.proposed_effect,
        status=ApprovalRequestStatus(row.status),
        reviewed_by=row.reviewed_by,
        decided_at=row.decided_at.isoformat() if row.decided_at else None,
        created_at=row.created_at.isoformat(),
        expires_at=row.expires_at.isoformat() if row.expires_at else None,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=list[ApprovalRequestView])
async def list_approvals(
    status: str = Query(
        default="pending",
        description=(
            "Filter by status. One of: pending, approved, rejected, expired. Defaults to 'pending'."
        ),
    ),
    operator: Operator = _require_operator,
) -> list[ApprovalRequestView]:
    """List approval requests for the operator's tenant.

    Defaults to listing only ``pending`` requests. Supports filtering
    by status via ``?status=approved`` / ``?status=rejected`` /
    ``?status=expired``. Returns newest-first (``created_at DESC``).
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id="approval.list",
        audit_op_class="read",
    )
    # Validate status value against the closed enum.
    try:
        status_filter = ApprovalRequestStatus(status)
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown status {status!r}; choose from: pending, approved, rejected, expired",
        ) from exc

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = (
            select(ApprovalRequest)
            .where(ApprovalRequest.tenant_id == operator.tenant_id)
            .where(ApprovalRequest.status == status_filter.value)
            .order_by(ApprovalRequest.created_at.desc())
        )
        result = await session.execute(stmt)
        rows = list(result.scalars().all())

    return [_view(r) for r in rows]


@router.post(
    "/{request_id}/approve",
    response_model=ApproveResponseBody,
)
async def approve_approval_request(
    request_id: Annotated[uuid.UUID, Path()],
    body: ApproveRequestBody,
    operator: Operator = _require_operator,
) -> ApproveResponseBody:
    """Approve a pending request and re-dispatch the original operation.

    The ``params`` body must be the original params unchanged; the service
    re-hashes them and rejects with 422 on a mismatch. On a successful
    approval the original dispatch is re-executed and the result returned.

    HTTP status codes:

    * 200 — approved + re-dispatched successfully.
    * 403 — operator lacks ``operator`` role.
    * 404 — request not found (or belongs to another tenant).
    * 409 — request is already decided.
    * 422 — params hash mismatch.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id="approval.approve",
        audit_op_class="write",
        audit_approval_request_id=str(request_id),
    )
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
            request = await approve_request(
                session,
                request_id,
                operator=operator,
                params=body.params,
            )
            await session.commit()
    except ApprovalNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="approval_request_not_found",
        ) from exc
    except UnauthorizedApprovalError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="insufficient_role",
        ) from exc
    except ApprovalRequestAlreadyDecidedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"approval_request_already_{exc.status}",
        ) from exc
    except ParamsMismatchError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="params_hash_mismatch",
        ) from exc

    # Re-dispatch the original op with the approved params.
    from meho_backplane.operations.dispatcher import dispatch

    dispatch_result = await dispatch(
        operator=operator,
        connector_id=request.connector_id,
        op_id=request.op_id,
        target=None,  # target resolved from connector_id; target_id in params if needed
        params=body.params,
    )

    _log.info(
        "approval_request_redispatched",
        approval_request_id=str(request_id),
        op_id=request.op_id,
        dispatch_status=dispatch_result.status,
        operator_sub=operator.sub,
    )

    return ApproveResponseBody(
        approval_request_id=request_id,
        decision="approved",
        dispatch_status=dispatch_result.status,
        dispatch_op_id=dispatch_result.op_id,
        dispatch_result=dispatch_result.result,
        dispatch_error=dispatch_result.error,
    )


@router.post(
    "/{request_id}/reject",
    response_model=RejectResponseBody,
)
async def reject_approval_request(
    request_id: Annotated[uuid.UUID, Path()],
    body: RejectRequestBody,
    operator: Operator = _require_operator,
) -> RejectResponseBody:
    """Reject a pending request; the original operation is not executed.

    HTTP status codes:

    * 200 — rejected successfully.
    * 403 — operator lacks ``operator`` role.
    * 404 — request not found (or belongs to another tenant).
    * 409 — request is already decided.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id="approval.reject",
        audit_op_class="write",
        audit_approval_request_id=str(request_id),
    )
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
            await reject_request(
                session,
                request_id,
                operator=operator,
                reason=body.reason,
            )
            await session.commit()
    except ApprovalNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="approval_request_not_found",
        ) from exc
    except UnauthorizedApprovalError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="insufficient_role",
        ) from exc
    except ApprovalRequestAlreadyDecidedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"approval_request_already_{exc.status}",
        ) from exc

    return RejectResponseBody(
        approval_request_id=request_id,
        decision="rejected",
        reason=body.reason,
    )
