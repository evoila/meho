# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/operations/runs/*`` — async governed dispatch handle surface.

Async governed dispatch (#3079). The poll / list / cancel side of the
durable run handle a ``POST /api/v1/operations/call`` (with ``async=true``)
or an async approval resume hands back. The submit side lives on the
existing ``POST /api/v1/operations/call`` route
(:mod:`meho_backplane.api.v1.operations`), which returns HTTP 202 + the run
handle when async mode is requested; this module lets the caller read that
run's durable state (and, on completion, its full
:class:`~meho_backplane.connectors.schemas.OperationResult` envelope) back
after the submitting connection is gone.

Route inventory
---------------

* ``GET /api/v1/operations/runs`` — list the tenant's runs, newest first;
  optional ``?status=`` filter. Role: ``operator``.
* ``GET /api/v1/operations/runs/{handle}`` — poll one run's durable state;
  the persisted result envelope is present once ``status='succeeded'``. 404
  for an unknown / cross-tenant handle. Role: ``operator``.
* ``POST /api/v1/operations/runs/{handle}/cancel`` — cancel a non-terminal
  run. 404 unknown / cross-tenant; 409 already-terminal. Role: ``operator``.

This router is registered **before** the ``operations`` router in
:func:`meho_backplane.main` so the literal ``/runs`` list route wins over
that router's ``/{descriptor_id}`` catch-all (the same ordering the
agent-runs router uses against the agents definition-CRUD routes).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.models import OperationRun, OperationRunStatus
from meho_backplane.operations.operation_run import (
    IllegalOperationRunTransitionError,
    OperationRunNotFoundError,
    UnauthorizedOperationRunCancellationError,
)
from meho_backplane.operations.operation_run_service import get_operation_run_service

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/operations", tags=["operations"])

_log = structlog.get_logger(__name__)

#: Operator-minimum gate, module-scoped to satisfy ruff B008.
_require_operator = Depends(require_role(TenantRole.OPERATOR))

#: Canonical op ids bound into ``audit_op_id`` per route.
_RUN_OP_IDS: Final[dict[str, str]] = {
    "status": "operation.run_status",
    "list": "operation.list_runs",
    "cancel": "operation.cancel_run",
}


class OperationRunStatusResponse(BaseModel):
    """Poll response for ``GET /operations/runs/{handle}``.

    ``result`` carries the full persisted ``OperationResult`` envelope once
    the run reaches ``succeeded`` (the dropped-response-class fix); ``error``
    carries the run-crash / reaper reason on a ``failed`` run. Both are
    ``None`` while the run is still ``pending`` / ``running``.
    """

    model_config = ConfigDict(frozen=True)

    run_id: uuid.UUID
    status: OperationRunStatus
    origin: str
    connector_id: str
    op_id: str
    target_name: str | None
    approval_request_id: uuid.UUID | None
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None


class OperationRunSummaryResponse(BaseModel):
    """One row of the operation-run list (``GET /operations/runs``).

    A scannable index row: identity, lifecycle state, dispatch coordinates,
    timestamps. The full ``result`` envelope is omitted — a caller wanting a
    run's result polls ``GET /operations/runs/{handle}``.
    """

    model_config = ConfigDict(frozen=True)

    run_id: uuid.UUID
    status: OperationRunStatus
    origin: str
    connector_id: str
    op_id: str
    target_name: str | None
    approval_request_id: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None


def _status_response(row: OperationRun) -> OperationRunStatusResponse:
    """Project an :class:`OperationRun` onto the full poll wire model."""
    return OperationRunStatusResponse(
        run_id=row.id,
        status=OperationRunStatus(row.status),
        origin=row.origin,
        connector_id=row.connector_id,
        op_id=row.op_id,
        target_name=row.target_name,
        approval_request_id=row.approval_request_id,
        result=row.result,
        error=row.error,
        created_at=row.created_at,
        started_at=row.started_at,
        ended_at=row.ended_at,
    )


def _summary_response(row: OperationRun) -> OperationRunSummaryResponse:
    """Project an :class:`OperationRun` onto the list-row wire model."""
    return OperationRunSummaryResponse(
        run_id=row.id,
        status=OperationRunStatus(row.status),
        origin=row.origin,
        connector_id=row.connector_id,
        op_id=row.op_id,
        target_name=row.target_name,
        approval_request_id=row.approval_request_id,
        created_at=row.created_at,
        started_at=row.started_at,
        ended_at=row.ended_at,
    )


@router.get("/runs", response_model=list[OperationRunSummaryResponse])
async def list_operation_runs(
    status: OperationRunStatus | None = Query(
        default=None,
        description=(
            "Filter by lifecycle status (pending / running / succeeded / "
            "failed / cancelled). Omit for every state."
        ),
    ),
    limit: int = Query(default=100, ge=1, le=500, description="Max runs per page (1..500)."),
    offset: int = Query(default=0, ge=0, description="Rows to skip for paging."),
    operator: Operator = _require_operator,
) -> list[OperationRunSummaryResponse]:
    """List the operator's tenant's async operation runs, newest first.

    Tenant-isolated server-side via the JWT — cross-tenant runs are
    invisible. ``?status=running`` narrows to one lifecycle state. Returns
    ``created_at DESC``.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUN_OP_IDS["list"],
        audit_op_class="read",
    )
    service = get_operation_run_service()
    rows = await service.list(operator, status=status, limit=limit, offset=offset)
    return [_summary_response(row) for row in rows]


@router.get("/runs/{handle}", response_model=OperationRunStatusResponse)
async def get_operation_run_status(
    handle: uuid.UUID,
    operator: Operator = _require_operator,
) -> OperationRunStatusResponse:
    """Poll an async operation run's durable state by handle (the run id).

    Reads the durable ``operation_run`` row, so it works after the request
    that submitted the run has returned — the dropped-response-class fix: the
    full result envelope is retrievable here even if the submit response was
    lost in transit. An unknown / cross-tenant handle is 404.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUN_OP_IDS["status"],
        audit_op_class="read",
    )
    service = get_operation_run_service()
    try:
        row = await service.poll(operator, handle)
    except OperationRunNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="operation_run_not_found",
        ) from exc
    return _status_response(row)


@router.post("/runs/{handle}/cancel", response_model=OperationRunSummaryResponse)
async def cancel_operation_run(
    handle: uuid.UUID,
    operator: Operator = _require_operator,
) -> OperationRunSummaryResponse:
    """Cancel a non-terminal async operation run by handle (the run id).

    Records the durable cancel intent through the shared
    :func:`~meho_backplane.operations.operation_run.cancel_run` service path.
    The in-flight task is not torn down synchronously: it loses its lease and
    its result is discarded when it finalises against the now-terminal row
    (best-effort cancel — a governed op already dispatched to the vendor may
    complete, but its own synchronous audit row is the durable record).

    An unknown / cross-tenant handle is 404 (existence is not leaked across
    tenants). An already-terminal run is 409, not a 500. A ``read_only``
    operator is rejected by the route's role gate; the service's own role
    check is a defence-in-depth backstop mapped to 403.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_RUN_OP_IDS["cancel"],
        audit_op_class="write",
    )
    service = get_operation_run_service()
    try:
        row = await service.cancel(operator, handle)
    except OperationRunNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="operation_run_not_found",
        ) from exc
    except UnauthorizedOperationRunCancellationError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="operation_run_cancel_forbidden",
        ) from exc
    except IllegalOperationRunTransitionError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="operation_run_not_cancellable",
        ) from exc
    return _summary_response(row)
