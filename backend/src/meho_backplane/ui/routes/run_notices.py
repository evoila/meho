# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Console: the "the run has ended" notice on every page (#5110).

A page that starts a background run (an approve in the console today)
marks it with ``data-meho-watch-run="<run id>"``. The script
``static/src/app/run-notices.js`` keeps those run ids for the browser tab
and asks ``GET /ui/runs/notices?run_id=...`` on the console's 5-second
timer. This route answers with one notice for each run that has ended.
The script adds the notices to the corner of the page and stops asking
for those runs.

Only the person who started a run gets its notice: a run of another
person, of another tenant, or an unknown id gets a hidden marker, so the
script stops asking for it. The notice says how the run ended, not what
it returned, so it shows no result data.

The notice is not tied to approvals. A run that resumes an approved
request links to that request; any other run gets the notice without a
link, until the page that starts it adds one.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import OperationRun, OperationRunOrigin, OperationRunStatus
from meho_backplane.operations import operation_run as run_lifecycle
from meho_backplane.operations.approval_queue import (
    ApprovalNotFoundError,
    get_request,
    result_state_of_envelope,
)
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.approvals.render import RESULT_STATE_PILL
from meho_backplane.ui.routes.approvals.run_watch import live_run
from meho_backplane.ui.templating import get_templates

__all__ = ["MAX_WATCHED_RUNS", "build_run_notices_router"]

_require_session = Depends(require_ui_session)

#: At most this many run ids in one ask. The script keeps no more.
MAX_WATCHED_RUNS: Final[int] = 20

#: DaisyUI alert modifier per end state.
_ALERT_CLASS: Final[dict[str, str]] = {
    "executed": "alert-success",
    "failed": "alert-error",
    "uncertain": "alert-warning",
    "ended_no_result": "alert-warning",
    "started": "alert-warning",
    "cancelled": "alert-info",
}

#: Labels for end states that the approvals pills do not have.
_EXTRA_LABELS: Final[dict[str, str]] = {"cancelled": "cancelled"}


def _plain_run_state(run: OperationRun) -> str:
    """How a run that is not tied to an approval ended."""
    if run.status == OperationRunStatus.SUCCEEDED.value and isinstance(run.result, dict):
        return result_state_of_envelope(run.result).value
    if run.status == OperationRunStatus.UNCERTAIN.value:
        return "uncertain"
    if run.status == OperationRunStatus.CANCELLED.value:
        return "cancelled"
    if run.status == OperationRunStatus.FAILED.value:
        return "failed"
    return "ended_no_result"


async def _approval_run_state(run: OperationRun, approval_id: uuid.UUID) -> str | None:
    """How the run of an approved request ended, or ``None`` while it goes on."""
    async with get_sessionmaker()() as db_session:
        try:
            row = await get_request(db_session, tenant_id=run.tenant_id, request_id=approval_id)
        except ApprovalNotFoundError:
            return _plain_run_state(run)
    live = live_run(row, run)
    if live is None:
        return _plain_run_state(run)
    if live["polling"]:
        return None
    return str(live["result_state"])


async def _notice_for(run: OperationRun) -> dict[str, Any] | None:
    """The notice of an ended run, or ``None`` while it goes on."""
    if OperationRunStatus(run.status) not in run_lifecycle.TERMINAL_STATUSES:
        return None
    approval_id = (
        run.approval_request_id if run.origin == OperationRunOrigin.APPROVAL_RESUME.value else None
    )
    state = await _approval_run_state(run, approval_id) if approval_id else _plain_run_state(run)
    if state is None:
        return None
    label = RESULT_STATE_PILL.get(state, (_EXTRA_LABELS.get(state, state), ""))[0]
    return {
        "run_id": str(run.id),
        "op_id": run.op_id,
        "state": state,
        "label": label,
        "alert_class": _ALERT_CLASS.get(state, "alert-info"),
        "approval_request_id": str(approval_id) if approval_id else None,
    }


def build_run_notices_router() -> APIRouter:
    """Build ``GET /ui/runs/notices``."""
    router = APIRouter(tags=["ui-run-notices"])

    @router.get("/ui/runs/notices", response_class=HTMLResponse)
    async def run_notices(
        request: Request,
        session: UISessionContext = _require_session,
        run_id: list[uuid.UUID] = Query(default=[]),
    ) -> HTMLResponse:
        """One notice per ended run of *run_id*; a hidden marker for an unknown one."""
        if len(run_id) > MAX_WATCHED_RUNS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Ask for at most {MAX_WATCHED_RUNS} runs at once.",
            )
        notices: list[dict[str, Any]] = []
        dropped: list[str] = []
        async with get_sessionmaker()() as db_session:
            runs = {rid: await run_lifecycle.get_run(db_session, rid) for rid in run_id}
        for rid, run in runs.items():
            if (
                run is None
                or run.tenant_id != session.tenant_id
                or run.identity_sub != session.operator_sub
            ):
                dropped.append(str(rid))
                continue
            notice = await _notice_for(run)
            if notice is not None:
                notices.append(notice)
        context: dict[str, object] = {"notices": notices, "dropped": dropped}
        return get_templates().TemplateResponse(request, "runs/_ended_notices.html", context)

    return router
