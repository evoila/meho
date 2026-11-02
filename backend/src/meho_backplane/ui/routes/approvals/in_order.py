# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approve the requests of one change ticket in order, from the console (#3597).

The console face of ``meho approvals approve --work-ref REF --in-order``.
The Pending tab, filtered to a change ticket (``work_ref``) with two or
more pending requests, shows an "Approve group in order" button. It opens
a modal that lists the ticket's requests oldest first.

Routes
------

* ``GET /ui/approvals/in-order?work_ref=REF`` -- the modal with the list
  of steps and a Start button. Mints a CSRF token and re-sets the cookie,
  like the detail modal.
* ``POST /ui/approvals/in-order`` (form: ``work_ref``, ``reason``,
  ``confirm_id``) -- runs the steps on the server, oldest first, and
  renders the modal again with what happened to each step.

How a run goes
--------------

For each request, oldest first:

* approved earlier and ran OK -- skipped;
* approved earlier with a run that failed or never started, or rejected,
  expired or withdrawn -- the run stops (the steps after it may need it);
* approved earlier with a run that has not ended yet -- the run stops,
  and the operator can run it again when that step has ended;
* pending at risk level ``dangerous`` or ``destructive`` -- the run waits.
  The modal shows that step and a button "Approve step k and go on".
  That button posts ``confirm_id`` = the id of that one request, so the
  yes covers only the step the operator saw;
* any other pending request -- approved through the same
  ``_commit_decision`` + ``resume_dispatch_after_approval`` calls the
  single Approve button uses. The run goes on only when the run status is
  ``ok``. ``already_resumed`` means the waiting requester claimed the run,
  not that it ended: the run then polls the row for the stored result
  (:func:`persist_resume_result` writes it when that run ends) and goes
  on only when that result is ``ok``. A failed result, no result within
  ``_RESULT_WAIT_SECONDS``, any other status, or a refusal
  (self-approval, role, already decided, expired) stops it;
* a step whose approval needs a new sign-in -- the run stops there and
  the modal links to the console login that asks for the password again
  and comes back to this change ticket. Every step is an approval, so the
  sign-in behind the console session must be recent and must have asked
  for the password, and a ``destructive`` step needs a sign-in that
  approved nothing else (the same rules as a single approve). After the
  new sign-in the operator starts the run again; the steps that ran OK
  are skipped.

Why a server-side sequencer (v0.1-spec §7)
------------------------------------------

Approval stays one decision per request. Each step is an ordinary single
approval with its own checks and its own ``approval.decision`` audit row,
run by the same service calls as the single Approve button. The server
does not add a way to approve without a human: the operator starts the
run, and every dangerous or destructive step needs its own click. A
server-side loop was picked over re-fetching the detail modal once per
step because the server can read each step's run result before it goes
on, without a round trip per step. Each form the run renders (Start, and
the per-step yes) carries a fresh CSRF token, and the response re-sets
the cookie, so every POST has a matching pair.

When the run stops, the modal names the step it stopped at, why, and the
steps it did not run. Those stay pending. The run is not one transaction:
the steps it already approved stay approved.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.operations.approval_context import resolve_reviewer_context
from meho_backplane.operations.approval_queue import (
    MAX_REASON_LENGTH,
    ApprovalResultState,
    approval_result_state,
    get_request,
    list_pending,
    resume_dispatch_after_approval,
)
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.auth.routes import login_location
from meho_backplane.ui.csrf import mint_csrf_token
from meho_backplane.ui.routes.approvals import routes as _routes
from meho_backplane.ui.routes.approvals.render import (
    RESULT_STATE_PILL,
    STATUS_PILL_CLASS,
    project_request_to_view,
    set_csrf_cookie,
)
from meho_backplane.ui.templating import get_templates

__all__ = ["build_in_order_router", "group_warnings"]

log = structlog.get_logger(__name__)

#: Risk levels at which the run waits for the operator's yes on that step.
_CONFIRM_LEVELS: Final[frozenset[str]] = frozenset({"dangerous", "destructive"})

#: Rows per page when the run reads the change ticket (the REST list cap).
#: The run reads every page, so a group larger than this is complete.
_PAGE: Final[int] = 500

#: How long the run waits for the stored result of a step another caller
#: claimed (``already_resumed``), and how often it looks.
_RESULT_WAIT_SECONDS: Final[float] = 30.0
_RESULT_POLL_SECONDS: Final[float] = 0.5

_MAX_WORK_REF_LENGTH: Final[int] = 512

_ONE_USE: Final[str] = (
    "An approval is used once, so this step cannot run again from here. "
    "Ask the requester to send it again."
)

_NOT_ENDED: Final[str] = (
    "Its run has not ended yet, or no result was stored. "
    "Check the step and run this again when it has ended."
)

_SIGN_IN_AGAIN: Final[str] = (
    "This step needs a new sign-in: an approval needs a recent sign-in that asked "
    "for your password, and a destructive step needs a sign-in of its own. "
    "Sign in again, then start this run again. The steps that ran OK are skipped."
)

#: The machine-readable prefixes of a refusal that a new sign-in fixes.
_SIGN_IN_PREFIXES: Final[tuple[str, ...]] = ("fresh_sign_in_required", "fresh_sign_in_already_used")

_require_session = Depends(require_ui_session)


@dataclass
class _Step:
    """One request of the change ticket and what the run did with it."""

    step: int
    row: ApprovalRequest
    #: ``planned`` (not started), ``skipped``, ``approved``, ``waiting``,
    #: ``stopped``, or ``not_run``.
    outcome: str = "planned"
    detail: str | None = None
    #: The approval was refused because it needs a new sign-in.
    sign_in_needed: bool = False

    @property
    def safety_level(self) -> str | None:
        level = (self.row.proposed_effect or {}).get("safety_level")
        return level if isinstance(level, str) else None


@dataclass
class _Run:
    steps: list[_Step]
    approved_now: int = 0
    #: The step the run waits at for a yes, or the step it stopped at.
    waiting: _Step | None = None
    stopped: _Step | None = None
    #: The run stopped because the step's approval needs a new sign-in.
    sign_in_needed: bool = False
    warnings: list[str] = field(default_factory=list)


def build_in_order_router() -> APIRouter:
    """Build the ``/ui/approvals/in-order`` routes.

    Included ahead of the ``/ui/approvals/{request_id}`` slug route so the
    literal ``in-order`` segment is never read as a request id.
    """
    router = APIRouter(tags=["ui-approvals"])

    @router.get("/ui/approvals/in-order", response_class=HTMLResponse)
    async def approvals_in_order_plan(
        request: Request,
        session: UISessionContext = _require_session,
        work_ref: str = Query(min_length=1, max_length=_MAX_WORK_REF_LENGTH),
    ) -> HTMLResponse:
        """Render the in-order modal with the steps and a Start button."""
        run = _Run(steps=await _load_steps(session.tenant_id, work_ref.strip()))
        run.warnings = group_warnings([s.row for s in run.steps])
        return await _render(request, session, work_ref.strip(), run, reason="", started=False)

    @router.post("/ui/approvals/in-order", response_class=HTMLResponse)
    async def approvals_in_order_run(
        request: Request,
        session: UISessionContext = _require_session,
        work_ref: str = Form(min_length=1, max_length=_MAX_WORK_REF_LENGTH),
        reason: str = Form(default="", max_length=MAX_REASON_LENGTH),
        confirm_id: str = Form(default=""),
    ) -> HTMLResponse:
        """Run the steps in order; render what happened to each."""
        operator = await _routes._resolve_operator(session)
        confirmed = _parse_confirm_id(confirm_id)
        ref = work_ref.strip()
        run = _Run(steps=await _load_steps(session.tenant_id, ref))
        run.warnings = group_warnings([s.row for s in run.steps])
        await _run_steps(operator, run, reason=reason, confirmed=confirmed)
        response = await _render(request, session, ref, run, reason=reason, started=True)
        if run.approved_now:
            # Refresh the bell and the history list, but keep this modal
            # open: ``meho:approval-decided`` would close it.
            response.headers["HX-Trigger"] = "meho:approval-bump"
        return response

    return router


def _parse_confirm_id(raw: str) -> uuid.UUID | None:
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="confirm_id is not a request id") from exc


async def _load_steps(tenant_id: uuid.UUID, work_ref: str) -> list[_Step]:
    """Read every request of *work_ref* in the tenant, oldest first.

    Reads page after page until a short page, so a change ticket with more
    requests than one page holds is still complete.
    """
    rows: list[ApprovalRequest] = []
    async with get_sessionmaker()() as db_session:
        while True:
            page = await list_pending(
                db_session,
                tenant_id=tenant_id,
                status=None,
                work_ref=work_ref,
                limit=_PAGE,
                offset=len(rows),
                order="oldest",
            )
            rows.extend(page)
            if len(page) < _PAGE:
                break
    return [_Step(step=n, row=row) for n, row in enumerate(rows, start=1)]


def group_warnings(rows: list[ApprovalRequest]) -> list[str]:
    """Say when oldest first may not be the order the steps depend on.

    Two cases: the requests come from more than one requester, or two
    requests have the same creation time (the list then breaks the tie on
    the id, which says nothing about the order).
    """
    warnings: list[str] = []
    subs = sorted({row.principal_sub for row in rows})
    if len(subs) > 1:
        warnings.append(
            f"This change ticket has requests from {len(subs)} requesters "
            f"({', '.join(subs)}). Oldest first is the right order only for "
            "one chain sent by one requester."
        )
    for n in range(1, len(rows)):
        if rows[n - 1].created_at >= rows[n].created_at:
            warnings.append(
                f"Steps {n} and {n + 1} were created at the same time, so MEHO "
                "cannot tell which comes first."
            )
    return warnings


async def _run_steps(
    operator: Operator, run: _Run, *, reason: str, confirmed: uuid.UUID | None
) -> None:
    """Go through the steps oldest first until the end, a wait, or a stop."""
    for step in run.steps:
        if run.waiting is not None or run.stopped is not None:
            step.outcome = "not_run"
            continue
        await _run_step(operator, step, reason=reason, confirmed=confirmed)
        if step.outcome == "approved":
            run.approved_now += 1
        elif step.outcome == "waiting":
            run.waiting = step
        elif step.outcome == "stopped":
            run.stopped = step
            run.sign_in_needed = step.sign_in_needed


async def _run_step(
    operator: Operator, step: _Step, *, reason: str, confirmed: uuid.UUID | None
) -> None:
    row = step.row
    if row.status == ApprovalRequestStatus.APPROVED.value:
        state = approval_result_state(row)
        if state == ApprovalResultState.EXECUTED:
            step.outcome, step.detail = "skipped", "Approved earlier and ran OK."
        elif state == ApprovalResultState.STARTED:
            step.outcome, step.detail = "stopped", f"Approved earlier. {_NOT_ENDED}"
        else:
            step.outcome = "stopped"
            step.detail = f"Approved earlier, but its run did not end OK. {_ONE_USE}"
        return
    if row.status != ApprovalRequestStatus.PENDING.value:
        step.outcome = "stopped"
        step.detail = f"This step is {row.status}. The steps after it may need it."
        return
    if step.safety_level in _CONFIRM_LEVELS and row.id != confirmed:
        step.outcome = "waiting"
        return

    try:
        approved = await _routes._commit_decision(
            operator=operator, request_id=row.id, decision="approved", reason=reason
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        if detail.startswith(_SIGN_IN_PREFIXES):
            step.outcome, step.detail, step.sign_in_needed = "stopped", _SIGN_IN_AGAIN, True
        else:
            step.outcome, step.detail = "stopped", f"The approval was refused: {detail}"
        return
    result = await resume_dispatch_after_approval(operator=operator, request=approved, params=None)
    status = getattr(result, "status", None)
    log.info(
        "ui_approval_in_order_step",
        approval_request_id=str(row.id),
        work_ref=row.work_ref,
        step=step.step,
        op_id=row.op_id,
        dispatch_status=status,
        operator_sub=operator.sub,
    )
    step.row = approved
    if status == "ok":
        step.outcome, step.detail = "approved", "Ran OK."
    elif status == "already_resumed":
        await _wait_for_stored_result(step)
    else:
        _stop_on_run_status(step, status, getattr(result, "error", None))


def _stop_on_run_status(step: _Step, status: object, error: object) -> None:
    ended = f"Approved, but the run ended with '{status}'"
    if error:
        ended += f": {error}"
    step.outcome, step.detail = "stopped", f"{ended}. {_ONE_USE}"


async def _wait_for_stored_result(step: _Step) -> None:
    """Wait for the result of a step the waiting requester is running.

    ``already_resumed`` only says another caller claimed the run. The run
    may still be going, and it may fail. So the step counts as done only
    when the stored result on the row says ``ok``. A failed result stops
    the run. No result within ``_RESULT_WAIT_SECONDS`` also stops it: the
    outcome is unknown, and the steps after it may depend on it.
    """
    row = step.row
    deadline = asyncio.get_running_loop().time() + _RESULT_WAIT_SECONDS
    while True:
        async with get_sessionmaker()() as db_session:
            fresh = await get_request(db_session, tenant_id=row.tenant_id, request_id=row.id)
        step.row = fresh
        state = approval_result_state(fresh)
        if state == ApprovalResultState.EXECUTED:
            step.outcome, step.detail = "approved", "The waiting requester ran it. Ran OK."
            return
        if state == ApprovalResultState.FAILED:
            stored = fresh.resume_result or {}
            _stop_on_run_status(step, stored.get("status"), stored.get("error"))
            step.detail = f"The waiting requester ran it. {step.detail}"
            return
        if asyncio.get_running_loop().time() >= deadline:
            step.outcome = "stopped"
            step.detail = f"Approved; the waiting requester is running it. {_NOT_ENDED}"
            return
        await asyncio.sleep(_RESULT_POLL_SECONDS)


async def _render(
    request: Request,
    session: UISessionContext,
    work_ref: str,
    run: _Run,
    *,
    reason: str,
    started: bool,
) -> HTMLResponse:
    waiting_context: Any = None
    if run.waiting is not None:
        async with get_sessionmaker()() as db_session:
            waiting_context = await resolve_reviewer_context(db_session, run.waiting.row)
    csrf_token = mint_csrf_token(str(session.session_id))
    pending = sum(1 for s in run.steps if s.row.status == ApprovalRequestStatus.PENDING.value)
    context: dict[str, object] = {
        "work_ref": work_ref,
        "steps": [_project_step(s) for s in run.steps],
        "total": len(run.steps),
        "pending": pending,
        "started": started,
        "warnings": run.warnings,
        "waiting": _project_step(run.waiting) if run.waiting else None,
        "waiting_context": waiting_context,
        "stopped": _project_step(run.stopped) if run.stopped else None,
        "sign_in_url": (
            login_location(_ticket_page(work_ref), fresh_sign_in=True)
            if run.sign_in_needed
            else None
        ),
        "approved_now": run.approved_now,
        "reason": reason,
        "csrf_token": csrf_token,
        "status_pill_class": STATUS_PILL_CLASS,
        "result_state_pill": RESULT_STATE_PILL,
    }
    response = get_templates().TemplateResponse(request, "approvals/_in_order.html", context)
    set_csrf_cookie(response, csrf_token)
    return response


def _ticket_page(work_ref: str) -> str:
    """The Pending tab filtered to *work_ref*: where the new sign-in comes back to."""
    return f"/ui/approvals?tab=pending&work_ref={quote(work_ref, safe='')}"


def _project_step(step: _Step) -> dict[str, Any]:
    return {
        "step": step.step,
        "request": project_request_to_view(step.row),
        "safety_level": step.safety_level,
        "outcome": step.outcome,
        "detail": step.detail,
    }
