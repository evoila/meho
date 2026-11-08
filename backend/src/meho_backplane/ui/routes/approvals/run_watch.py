# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Console: approve answers at once, and the page follows the run (#5110).

An approve in the console used to run the whole operation inside the web
request, so the dialog stayed open until the operation ended. Now
:func:`start_approved_run` starts the run in the background with
:meth:`~meho_backplane.operations.operation_run_service.OperationRunService.submit_approval_resume`
(the same run claim and audit as before) and waits only a short time.
A quick run shows its result at once; a long one shows "running".

The card and the approvals list then ask the server again every few
seconds:

* ``GET /ui/approvals/{id}/run`` -- the run part of the outcome card.
* ``GET /ui/approvals/{id}/row`` -- one row of the approvals list.

Both read the stored request and its latest background run
(:func:`live_run`), so they work on every MEHO server process. Both stop
asking at a final state, also when the run ended without a stored result
(for example after a server restart).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, OperationRun, OperationRunStatus
from meho_backplane.operations.approval_outcome import resume_outcome
from meho_backplane.operations.approval_outcome_check import outcome_unknown
from meho_backplane.operations.approval_queue import (
    ApprovalNotFoundError,
    ApprovalResultState,
    approval_result_state,
    get_request,
)
from meho_backplane.operations.operation_run import TERMINAL_STATUSES
from meho_backplane.operations.operation_run_service import get_operation_run_service
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.approvals.render import (
    RESULT_STATE_PILL,
    STATUS_PILL_CLASS,
    project_outcome,
    project_request_to_view,
    project_run_envelope,
)
from meho_backplane.ui.templating import get_templates

__all__ = [
    "ANSWER_WAIT_SECONDS",
    "POLL_SECONDS",
    "build_run_watch_router",
    "live_run",
    "live_runs_for",
    "start_approved_run",
]

log = structlog.get_logger(__name__)

_require_session = Depends(require_ui_session)

#: How long the approve waits for the run before it answers. A quick run
#: shows its result at once; a longer one shows "running".
ANSWER_WAIT_SECONDS: Final[float] = 1.0

#: How often the card and a list row ask the server again.
POLL_SECONDS: Final[int] = 3

_FINAL_ROW_STATES: Final[frozenset[ApprovalResultState]] = frozenset(
    {ApprovalResultState.EXECUTED, ApprovalResultState.FAILED, ApprovalResultState.UNCERTAIN}
)


def _state(result_state: str, run: OperationRun | None, *, polling: bool) -> dict[str, Any]:
    return {
        "result_state": result_state,
        "outcome": None,
        "polling": polling,
        "run_id": str(run.id) if run is not None else None,
    }


def live_run(row: ApprovalRequest, run: OperationRun | None) -> dict[str, Any] | None:
    """Say where the run of an approved request is now.

    *row* is the request as stored now. *run* is its latest background run,
    or ``None``. Returns ``None`` for a request that is not approved. Else a
    dict with ``result_state``, ``outcome`` (the summary of a stored result,
    or ``None``), ``polling`` (ask again: the run is not over) and
    ``run_id``.

    A result stored on the request wins. Else the background run decides:
    still going is ``running``; ended with a result is that result; ended
    without one (a crash, a server restart, a cancel) is
    ``ended_no_result``. When another caller (usually the waiting
    requester) runs the operation, the request is ``started`` until its
    result is stored, and asking stops once the request is on the
    "outcome unknown" list.
    """
    state = approval_result_state(row)
    if state is None:
        return None
    if state in _FINAL_ROW_STATES:
        live = _state(state.value, run, polling=False)
        live["outcome"] = project_outcome(resume_outcome(row))
        return live
    from_run = _from_own_run(row, run) if run is not None else None
    if from_run is not None:
        return from_run
    if state == ApprovalResultState.STARTED:
        return _state("started", run, polling=outcome_unknown(row) is None)
    return _state(state.value, run, polling=False)


def _from_own_run(row: ApprovalRequest, run: OperationRun) -> dict[str, Any] | None:
    """Where the background run *run* is; ``None`` when another caller runs the operation."""
    if OperationRunStatus(run.status) not in TERMINAL_STATUSES:
        return _state("running", run, polling=True)
    envelope = run.result
    if isinstance(envelope, dict) and envelope.get("status") == "already_resumed":
        return None
    if run.status == OperationRunStatus.SUCCEEDED.value and isinstance(envelope, dict):
        projected = project_run_envelope(envelope, parked_op_id=row.op_id)
        live = _state(projected["result_state"], run, polling=False)
        live["outcome"] = projected["outcome"]
        return live
    return _state("ended_no_result", run, polling=False)


async def _latest_runs(
    tenant_id: uuid.UUID, request_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, OperationRun]:
    """The newest background run of each request, in *tenant_id*."""
    ids = list(request_ids)
    if not ids:
        return {}
    stmt = (
        select(OperationRun)
        .where(
            OperationRun.tenant_id == tenant_id,
            OperationRun.approval_request_id.in_(ids),
        )
        .order_by(OperationRun.created_at.asc())
    )
    async with get_sessionmaker()() as db_session:
        runs = (await db_session.execute(stmt)).scalars().all()
    latest: dict[uuid.UUID, OperationRun] = {}
    for run in runs:
        if run.approval_request_id is not None:
            latest[run.approval_request_id] = run
    return latest


async def live_runs_for(
    tenant_id: uuid.UUID, rows: Iterable[ApprovalRequest]
) -> dict[uuid.UUID, dict[str, Any] | None]:
    """:func:`live_run` for each row of the approvals list."""
    rows = list(rows)
    open_ids = [
        row.id
        for row in rows
        if approval_result_state(row)
        in (ApprovalResultState.STARTED, ApprovalResultState.NOT_STARTED)
    ]
    runs = await _latest_runs(tenant_id, open_ids)
    return {row.id: live_run(row, runs.get(row.id)) for row in rows}


async def _load(tenant_id: uuid.UUID, request_id: uuid.UUID) -> ApprovalRequest:
    async with get_sessionmaker()() as db_session:
        try:
            return await get_request(db_session, tenant_id=tenant_id, request_id=request_id)
        except ApprovalNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="approval_request_not_found",
            ) from exc


async def _live_for(tenant_id: uuid.UUID, request_id: uuid.UUID) -> tuple[ApprovalRequest, Any]:
    row = await _load(tenant_id, request_id)
    runs = await _latest_runs(tenant_id, [row.id])
    return row, live_run(row, runs.get(row.id))


async def start_approved_run(operator: Operator, approval: ApprovalRequest) -> dict[str, Any]:
    """Start the run of a just-approved request; wait a short time; say where it is.

    The decision is already saved. The run goes to the background, so the
    console answers within :data:`ANSWER_WAIT_SECONDS` however long the
    operation takes. Closing the dialog does not stop the run.
    """
    service = get_operation_run_service()
    run_id = await service.submit_approval_resume(operator, approval, params=None)
    run_row = await service.poll(operator, run_id)
    run_row = await service.wait_until_done(operator, run_row, wait_seconds=ANSWER_WAIT_SECONDS)
    fresh = await _load(operator.tenant_id, approval.id)
    live = live_run(fresh, run_row)
    log.info(
        "ui_approval_run_started",
        approval_request_id=str(approval.id),
        op_id=approval.op_id,
        operation_run_id=str(run_id),
        run_status=run_row.status,
        operator_sub=operator.sub,
    )
    return live or _state("running", run_row, polling=True)


def build_run_watch_router() -> APIRouter:
    """Build the two routes the card and the list ask again (#5110).

    No tags of its own: :func:`~.routes.build_approvals_router` includes it
    and gives it the ``ui-approvals`` tag.
    """
    router = APIRouter()

    @router.get("/ui/approvals/{request_id}/run", response_class=HTMLResponse)
    async def approval_run_state(
        request: Request,
        request_id: uuid.UUID,
        session: UISessionContext = _require_session,
    ) -> HTMLResponse:
        """The run part of the outcome card, as it is now."""
        row, live = await _live_for(session.tenant_id, request_id)
        context: dict[str, object] = {
            "request": project_request_to_view(row),
            "run": live,
            "poll_seconds": POLL_SECONDS,
        }
        return get_templates().TemplateResponse(request, "approvals/_run_state.html", context)

    @router.get("/ui/approvals/{request_id}/row", response_class=HTMLResponse)
    async def approval_history_row(
        request: Request,
        request_id: uuid.UUID,
        session: UISessionContext = _require_session,
    ) -> HTMLResponse:
        """One row of the approvals list, as it is now."""
        row, live = await _live_for(session.tenant_id, request_id)
        req = project_request_to_view(row)
        req["live"] = live
        context: dict[str, object] = {
            "req": req,
            "status_pill_class": STATUS_PILL_CLASS,
            "result_state_pill": RESULT_STATE_PILL,
            "poll_seconds": POLL_SECONDS,
        }
        return get_templates().TemplateResponse(request, "approvals/_history_row.html", context)

    return router
