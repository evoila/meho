# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/service-principals/run-approvals*`` — run approvals (#4126).

A person approves the operation list of one automation run once. The
rules are in :mod:`meho_backplane.operations.run_approvals`.

Route inventory
---------------

* ``POST /api/v1/service-principals/run-approvals`` — the add-on asks for
  a run approval. Only a service principal (403
  ``service_principal_required`` otherwise). 201 with ``status=pending``;
  422 on a destructive entry, a target kind the tenant does not allow, or a
  bad expiry; 409 when this run already has a live request.
* ``GET /api/v1/service-principals/run-approvals`` — list (filters
  ``principal_sub``, ``status``).
* ``GET /api/v1/service-principals/run-approvals/{id}`` — show one. The
  add-on reads the status here while it waits. 404 across tenants.
* ``POST /api/v1/service-principals/run-approvals/{id}/approve`` — a person
  approves the whole list. Human only (403 ``human_principal_required``
  for a machine token) and a fresh approval sign-in (401 with the RFC 9470
  challenge otherwise), the same rules as approving one request. 409 when
  it is not pending; 403 ``role_floor_required`` when an entry needs a
  higher role.
* ``POST /api/v1/service-principals/run-approvals/{id}/revoke`` — a person
  revokes (or turns down) it. Human only. 409 when it already ended.
* ``POST /api/v1/service-principals/run-approvals/{id}/end`` — the
  principal that asked ends its run. 404 for any other caller.

Read, approve and revoke need approvals-plane access (``operator`` role or
the ``approver`` capability), like the approval queue. There is no MCP
tool: approving is a person's decision. Every route binds ``audit_op_id``
so it lands on the audit log.
"""

from __future__ import annotations

from typing import Annotated, Final, NoReturn
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi import status as http_status

from meho_backplane.auth.approval_sign_in import ApprovalAuth
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.rbac import (
    require_approvals_access,
    require_fresh_approval_sign_in,
    require_human_principal,
    require_role,
)
from meho_backplane.operations.run_approval_schemas import (
    RunApprovalCreate,
    RunApprovalListResponse,
    RunApprovalRead,
    RunApprovalRevoke,
    RunApprovalStatus,
)
from meho_backplane.operations.run_approvals import (
    RunApprovalConflictError,
    RunApprovalError,
    RunApprovalGroupError,
    RunApprovalNotFoundError,
    RunApprovalRoleFloorError,
    RunApprovalService,
)

__all__ = ["router"]

router = APIRouter(
    prefix="/api/v1/service-principals/run-approvals",
    tags=["service-principal-run-approvals"],
)

_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_approvals_access = Depends(require_approvals_access)
_require_human = Depends(require_human_principal)
_require_fresh_sign_in = Depends(require_fresh_approval_sign_in)

_OP_IDS: Final[dict[str, str]] = {
    "request": "run_approval.request",
    "list": "run_approval.list",
    "show": "run_approval.show",
    "approve": "run_approval.approve",
    "revoke": "run_approval.revoke",
    "end": "run_approval.end",
}

_RunApprovalId = Annotated[UUID, Path()]


def _raise(exc: RunApprovalError) -> NoReturn:
    if isinstance(exc, RunApprovalNotFoundError):
        code = http_status.HTTP_404_NOT_FOUND
    elif isinstance(exc, RunApprovalConflictError):
        code = http_status.HTTP_409_CONFLICT
    elif isinstance(exc, (RunApprovalRoleFloorError, RunApprovalGroupError)):
        code = http_status.HTTP_403_FORBIDDEN
    else:
        code = http_status.HTTP_422_UNPROCESSABLE_CONTENT
    raise HTTPException(status_code=code, detail=exc.message) from exc


def _bind(op: str, op_class: str, run_approval_id: UUID | None = None) -> None:
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS[op],
        audit_op_class=op_class,
        audit_run_approval_id=str(run_approval_id) if run_approval_id else None,
    )


@router.post("", response_model=RunApprovalRead, status_code=http_status.HTTP_201_CREATED)
async def request_run_approval(
    payload: RunApprovalCreate,
    operator: Operator = _require_operator,
) -> RunApprovalRead:
    """The add-on asks a person to approve the operation list of one run.

    Only a service principal asks: the approval is keyed on its token
    subject and the run reference, and only a service principal's calls
    consult it.
    """
    _bind("request", "write")
    structlog.contextvars.bind_contextvars(audit_work_ref=payload.work_ref)
    if operator.principal_kind is not PrincipalKind.SERVICE:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="service_principal_required",
        )
    try:
        read = await RunApprovalService().request(operator, payload)
    except RunApprovalError as exc:
        _raise(exc)
    structlog.contextvars.bind_contextvars(audit_run_approval_id=str(read.id))
    return read


@router.get("", response_model=RunApprovalListResponse)
async def list_run_approvals(
    operator: Operator = _require_approvals_access,
    principal_sub: str | None = Query(default=None),
    status: RunApprovalStatus | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> RunApprovalListResponse:
    """List the run approvals of the caller's tenant, newest first."""
    _bind("list", "read")
    reads = await RunApprovalService().list_(
        operator.tenant_id,
        principal_sub=principal_sub,
        status=status,
        limit=limit,
        offset=offset,
    )
    return RunApprovalListResponse(run_approvals=reads)


@router.get("/{run_approval_id}", response_model=RunApprovalRead)
async def show_run_approval(
    run_approval_id: _RunApprovalId,
    operator: Operator = _require_approvals_access,
) -> RunApprovalRead:
    """Show one run approval; the add-on reads its status here while it waits."""
    _bind("show", "read", run_approval_id)
    try:
        return await RunApprovalService().get(operator.tenant_id, run_approval_id)
    except RunApprovalError as exc:
        _raise(exc)


@router.post(
    "/{run_approval_id}/approve",
    response_model=RunApprovalRead,
    dependencies=[_require_human],
)
async def approve_run_approval(
    run_approval_id: _RunApprovalId,
    operator: Operator = _require_approvals_access,
    approval_auth: ApprovalAuth = _require_fresh_sign_in,
) -> RunApprovalRead:
    """A person approves the whole operation list of one run, once."""
    _bind("approve", "write", run_approval_id)
    structlog.contextvars.bind_contextvars(
        audit_approval_auth=approval_auth.audit_payload(operator.sub)
    )
    try:
        return await RunApprovalService().approve(operator, run_approval_id)
    except RunApprovalError as exc:
        _raise(exc)


@router.post(
    "/{run_approval_id}/revoke",
    response_model=RunApprovalRead,
    dependencies=[_require_human],
)
async def revoke_run_approval(
    run_approval_id: _RunApprovalId,
    body: RunApprovalRevoke,
    operator: Operator = _require_approvals_access,
) -> RunApprovalRead:
    """A person revokes a run approval; the next listed call waits for approval."""
    _bind("revoke", "write", run_approval_id)
    structlog.contextvars.bind_contextvars(audit_reason=body.reason)
    try:
        return await RunApprovalService().revoke(operator, run_approval_id, body.reason)
    except RunApprovalError as exc:
        _raise(exc)


@router.post("/{run_approval_id}/end", response_model=RunApprovalRead)
async def end_run_approval(
    run_approval_id: _RunApprovalId,
    operator: Operator = _require_operator,
) -> RunApprovalRead:
    """The principal that asked ends its run; the approval covers nothing more."""
    _bind("end", "write", run_approval_id)
    try:
        return await RunApprovalService().end(operator, run_approval_id)
    except RunApprovalError as exc:
        _raise(exc)
