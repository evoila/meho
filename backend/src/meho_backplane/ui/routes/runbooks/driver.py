# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Runbooks UI run driver: current-step render + Advance / Abort / Reassign.

Initiative #1837 (G10.11 Runbook runs UI), Task #1893 (T2). The page a
junior operator uses to drive a *started* runbook step-by-step to
completion or a reasoned abort -- the half that closes the loop on the run
lifecycle (T1 #1884 ships the list + start modal; each row links here).

This is a pure UI/BFF build over the in-process
:class:`~meho_backplane.runbooks.run_service.RunbookRunService` -- the
``require_ui_session`` floor (or the ``require_ui_admin`` hard gate, for
reassign) + a direct service call, never the Bearer ``/api/v1`` surface.
The closest write-surface precedent is
:mod:`meho_backplane.ui.routes.runbooks.lifecycle` (HTMX fragment swap +
typed-error -> inline alert + CSRF re-mint).

This module owns the route wiring + the action orchestration (Advance /
Abort / Reassign -> service call -> outcome-to-fragment mapping); the Jinja
render primitives live in :mod:`.driver_render` (split out so neither file
crosses the code-quality size gate -- the same package-split convention
:mod:`~meho_backplane.ui.routes.runbooks.lifecycle` follows).

The opacity floor (the #1 correctness risk)
-------------------------------------------

This page renders **only the run's current step**, never the full
template body. Re-introducing the full step list would re-open the
skip-ahead leak Initiative #1198 (G12) was built to close. The
load-bearing guard is :meth:`RunbookRunService.get_current_step`, which
returns the SAME single-step :class:`CurrentStepResponse` projection the
advance path returns (one substituted :class:`StepBody`, never
``template.steps``). The handlers here NEVER touch the pinned template body
to re-derive a step -- contrast
:func:`meho_backplane.ui.routes.runbooks.routes._render_steps`, which
renders the FULL ``template.steps`` list (the template-detail surface) and
is the exact pattern this driver must NOT copy.

Route inventory (all registered ahead of ``/ui/runbooks/{slug}`` and
after T1's literal ``/ui/runbooks/runs/start``)
-----------------------------------------------

* ``GET /ui/runbooks/runs/{run_id}`` -- driver page. Operator floor.
  ``RunNotFoundError`` -> 404 page. Renders run coordinates + ``position``
  ("step n of total") + the single current ``StepBody``, with the action
  controls conditional:
  - **Advance** -- shown ONLY when ``session.operator_sub == assigned_to``.
  - **Abort** -- a confirm dialog with a **required** non-empty reason.
  - **Reassign** -- shown ONLY for a ``tenant_admin``.

* ``POST /ui/runbooks/runs/{run_id}/next`` -- operator floor. Builds a
  :class:`NextStepRequest` and calls :meth:`next_step`. Re-renders the step
  fragment from the returned :class:`CurrentStepResponse`; a
  :class:`RunCompletedResponse` renders the completed state.
  :class:`NotRunAssigneeError` -> inline "reassigned away from you" (HTTP
  200, never a 500); :class:`PreviousStepFailedError` -> a dead-end banner
  (Advance hidden, only Abort forward); terminal / verify errors -> inline
  alerts.

* ``POST /ui/runbooks/runs/{run_id}/abort`` -- operator floor; passes
  ``caller_is_admin = probe.is_tenant_admin``. The reason is **required**
  client-side (HTMX ``required``) AND a tampered empty reason is handled
  cleanly (the schema's ``min_length=1`` 422s server-side; the reason is
  persisted to the abort audit row, so the audit guarantee holds even on a
  forged form). On success renders the abandoned state.

* ``POST /ui/runbooks/runs/{run_id}/reassign`` -- **``require_ui_admin``
  hard gate** (an operator gets 403 at the dependency, before the body /
  service is touched). Calls :meth:`reassign_run`; renders the new-ownership
  state. After a reassign, the previous assignee's open page shows
  "reassigned away from you" on their next Advance (the service returns
  :class:`NotRunAssigneeError` for them).

CSRF
----

Double-submit per :mod:`meho_backplane.ui.csrf`. Every fragment render
mints a fresh token, echoes it on the controls via ``hx-headers``, and --
on the same response -- refreshes the ``meho_csrf`` cookie (the cookie/
header desync footgun #1693). The render layer in :mod:`.driver_render`
owns the mint + cookie refresh.

Every state-changing POST here (``/next`` / ``/abort`` / ``/reassign``)
REQUIRES the double-submit token: the ``meho_csrf`` cookie AND an
``X-CSRF-Token`` header (or ``csrf_token`` form field). A request missing
either half is rejected by the middleware with ``403``
``{"detail":"csrf_token_invalid"}`` + an ``x-csrf-rejection-reason``
header -- BY DESIGN, before this handler runs. ``/api/v1/*`` is CSRF-exempt,
so a raw-curl repro sending a Bearer JWT but no ``meho_csrf`` cookie gets a
``403`` on ``/ui/.../abort`` while the same call to
``/api/v1/.../abort`` succeeds: that differential is the CSRF gate, NOT an
RBAC/opacity difference (this abort route is on the SAME operator floor as
REST -- see ``_abort`` below -- and a genuine assignee RBAC denial renders
an inline HTTP-200 fragment, never a ``403``). The run-starter CAN abort
their own run from the browser, which carries the cookie/header. Consumer
note + repro guidance: ``docs/codebase/ui.md`` (#2112).

References
----------

* HTMX 2.0.9 ``hx-post`` / ``hx-target`` / ``hx-swap`` / ``hx-disabled-elt``
  / ``hx-headers``: https://htmx.org/reference/
* DaisyUI 5.5.20 alert / badge / modal: https://daisyui.com/components/alert/
* markdown-it-py 4.2.0 (shared KB renderer): https://markdown-it-py.readthedocs.io/
"""

from __future__ import annotations

import uuid
from typing import Final, Literal

import structlog
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from meho_backplane.runbooks.engine import (
    ConfirmVerifyAnswerNotYesError,
    PreviousStepNotVerifiedError,
    RunAlreadyCompletedError,
    VerifyResponseMismatchError,
    VerifyResponseRequiredError,
)
from meho_backplane.runbooks.run_service import (
    NotRunAssigneeError,
    PreviousStepFailedError,
    RunAlreadyTerminalError,
    RunbookRunService,
    RunNotFoundError,
)
from meho_backplane.runbooks.runs_schemas import (
    AbortRunRequest,
    AbortRunResponse,
    ConfirmVerifyResponse,
    CurrentStepResponse,
    NextStepRequest,
    ReassignRunRequest,
    RunCompletedResponse,
)
from meho_backplane.ui.auth.middleware import (
    UISessionContext,
    require_ui_admin,
    require_ui_session,
)
from meho_backplane.ui.routes.connectors.operator import resolve_role_probe
from meho_backplane.ui.routes.runbooks.driver_render import (
    StepView,
    render_driver_page,
    render_not_found,
    render_step_fragment,
    step_view,
)

__all__ = ["register_driver_routes"]

log = structlog.get_logger(__name__)

#: Module-level ``Depends`` closures (B008 idiom -- no call in a default
#: arg). ``_require_session`` is the operator floor the read page + the
#: next / abort POSTs sit on; ``_require_admin`` is the hard
#: ``tenant_admin`` gate the reassign POST sits on (an operator gets 403
#: at the dependency, before the handler body runs).
_require_session = Depends(require_ui_session)
_require_admin = Depends(require_ui_admin)

#: Cap on the ``answer`` form field (a closed ``yes`` / ``no`` / ``escalate``
#: vocabulary; bounded as defence-in-depth before it reaches the schema).
_MAX_ANSWER_LENGTH: Final[int] = 16

#: Cap on the ``verify_type`` form field (a closed ``confirm`` /
#: ``operation_call`` vocabulary the page echoes back so the handler can
#: shape the :class:`NextStepRequest` without re-loading the template body).
_MAX_VERIFY_TYPE_LENGTH: Final[int] = 32

#: Cap on the abort ``reason`` textarea. A reason is a short human note; this
#: keeps a tampered paste bounded before it reaches the schema's
#: ``min_length=1`` floor (the 422 guard the audit guarantee rests on).
_MAX_REASON_LENGTH: Final[int] = 2_000

#: Cap on the reassign ``new_assignee`` field (an operator subject id).
_MAX_ASSIGNEE_LENGTH: Final[int] = 256

#: The engine's typed verify failures -- a malformed / missing verify
#: response, an already-verified step re-advanced, an advance on a completed
#: run. All are ``ValueError`` subclasses; the driver maps them to an inline
#: alert (HTTP 200) rather than a 500 so the operator sees a recoverable
#: message and can correct + retry.
_VERIFY_VALUE_ERRORS: Final[tuple[type[ValueError], ...]] = (
    VerifyResponseRequiredError,
    VerifyResponseMismatchError,
    PreviousStepNotVerifiedError,
    ConfirmVerifyAnswerNotYesError,
    RunAlreadyCompletedError,
)


def _build_next_request(verify_type: str, answer: str) -> NextStepRequest:
    """Shape the :class:`NextStepRequest` from the form's verify echo.

    The page renders the current step's ``verify.type`` into the Advance
    control so the handler need not re-load the pinned template body to learn
    it (which would risk re-touching the step list). For a ``confirm`` verify
    the operator picked an ``answer`` (``yes`` / ``no`` / ``escalate``) ->
    a :class:`ConfirmVerifyResponse`. For an ``operation_call`` verify the
    engine dispatches + captures the result, so the client sends no captured
    response (``verify_response=None``).

    ``last_verified`` is informational only (the substrate is the oracle); we
    set it ``True`` to record the operator's belief that they performed the
    step, alongside whatever the substrate decides.
    """
    if verify_type == "confirm":
        normalized: Literal["yes", "no", "escalate"]
        if answer == "no":
            normalized = "no"
        elif answer == "escalate":
            normalized = "escalate"
        else:
            normalized = "yes"
        return NextStepRequest(
            last_verified=True,
            verify_response=ConfirmVerifyResponse(type="confirm", answer=normalized),
        )
    return NextStepRequest(last_verified=True, verify_response=None)


async def _advance(
    request: Request,
    session: UISessionContext,
    run_id: uuid.UUID,
    verify_type: str,
    answer: str,
) -> HTMLResponse:
    """Advance the run one step and re-render the step fragment.

    Maps the service / engine outcomes to operator-facing fragments:

    * :class:`CurrentStepResponse` -> the next step's body.
    * :class:`RunCompletedResponse` -> the completed banner.
    * :class:`NotRunAssigneeError` -> an inline "reassigned away from you"
      alert (HTTP 200, never a 500) -- a non-assignee (including a tampered
      POST by a TENANT_ADMIN) cannot advance; the documented path is a
      reassign.
    * :class:`PreviousStepFailedError` -> the dead-end banner: the step
      failed (the operator answered ``no`` / ``escalate``, or an
      operation_call verify did not match), so Advance is hidden and the
      only forward move is Abort.
    * terminal / verify ``ValueError``s -> an inline alert.
    """
    service = RunbookRunService()
    request_model = _build_next_request(verify_type, answer)

    structlog.contextvars.bind_contextvars(
        operator_sub=session.operator_sub,
        tenant_id=str(session.tenant_id),
        audit_op_id="runbook.next_step",
        audit_op_class="write",
    )
    try:
        result = await service.next_step(
            session.tenant_id, session.operator_sub, run_id, request_model
        )
    except NotRunAssigneeError:
        return await _reassigned_away_fragment(request, session, run_id)
    except PreviousStepFailedError:
        return await _failed_step_fragment(request, session, run_id)
    except RunAlreadyTerminalError:
        return await render_step_fragment(
            request,
            session,
            run_id,
            view=None,
            error_message="This run is already finished; no further steps.",
        )
    except _VERIFY_VALUE_ERRORS as exc:
        log.info(
            "ui_runbook_driver_next_rejected",
            tenant_id=str(session.tenant_id),
            run_id=str(run_id),
            reason=type(exc).__name__,
        )
        return await render_step_fragment(
            request, session, run_id, view=None, error_message=str(exc)
        )

    if isinstance(result, RunCompletedResponse):
        log.info(
            "ui_runbook_driver_run_completed",
            tenant_id=str(session.tenant_id),
            run_id=str(run_id),
        )
        return await render_step_fragment(
            request,
            session,
            run_id,
            view=None,
            terminal_state="completed",
            completed_at=result.completed_at,
        )
    return await render_step_fragment(request, session, run_id, view=step_view(result))


async def _failed_step_fragment(
    request: Request,
    session: UISessionContext,
    run_id: uuid.UUID,
) -> HTMLResponse:
    """Render the failed-step dead-end fragment (Advance hidden, Abort only).

    Re-reads the current step (now in ``failed`` state) so the banner still
    shows which step the operator is stuck on. ``get_current_step`` returns
    the step body for the failed step (it is still the run's current step);
    we project it with ``failed=True`` so the template hides Advance and
    renders the dead-end banner. A reassign-race that flipped the run
    terminal in the meantime collapses to the matching terminal banner.
    """
    service = RunbookRunService()
    try:
        current = await service.get_current_step(session.tenant_id, session.operator_sub, run_id)
    except RunNotFoundError:
        return await render_step_fragment(
            request,
            session,
            run_id,
            view=None,
            error_message="This run no longer exists.",
        )
    if isinstance(current, RunCompletedResponse):
        return await render_step_fragment(
            request,
            session,
            run_id,
            view=None,
            terminal_state="completed",
            completed_at=current.completed_at,
        )
    if isinstance(current, AbortRunResponse):
        return await render_step_fragment(
            request,
            session,
            run_id,
            view=None,
            terminal_state="abandoned",
            abandoned_at=current.abandoned_at,
        )
    return await render_step_fragment(
        request, session, run_id, view=step_view(current, failed=True)
    )


async def _reassigned_away_fragment(
    request: Request,
    session: UISessionContext,
    run_id: uuid.UUID,
) -> HTMLResponse:
    """Render the "reassigned away from you" fragment for a non-assignee.

    The run was reassigned to someone else (or the caller was never the
    assignee). Advance is impossible for them now -- the service is the
    authority and already refused the write; this fragment surfaces it as a
    calm inline alert with Advance withheld, not a 500.
    """
    return await render_step_fragment(
        request,
        session,
        run_id,
        view=None,
        error_message=(
            "This run was reassigned away from you. You can no longer advance "
            "it; ask a tenant administrator if you need it back."
        ),
    )


async def _abort(
    request: Request,
    session: UISessionContext,
    run_id: uuid.UUID,
    reason: str,
) -> HTMLResponse:
    """Abort the run with *reason* and render the abandoned state.

    The reason is **required**: the control guards it client-side (HTMX
    ``required``), and a tampered empty / whitespace reason is rejected here
    with an inline alert before the service call -- the schema's
    ``min_length=1`` would 422 anyway (the reason is persisted to the abort
    audit row, so the audit guarantee holds even on a forged form), and
    surfacing it inline keeps the page navigable rather than dropping a raw
    422 body. ``caller_is_admin`` is the soft probe: an admin may abort any
    tenant run; an operator may abort only their own (the service enforces
    the ``{assignee, tenant_admin}`` allowance).
    """
    cleaned = reason.strip()
    if not cleaned:
        return await render_step_fragment(
            request,
            session,
            run_id,
            view=await _current_step_view_or_none(session, run_id),
            error_message="An abort reason is required (it is recorded in the audit log).",
        )

    probe = await resolve_role_probe(request, session)
    service = RunbookRunService()
    structlog.contextvars.bind_contextvars(
        operator_sub=session.operator_sub,
        tenant_id=str(session.tenant_id),
        audit_op_id="runbook.abort",
        audit_op_class="write",
    )
    try:
        result = await service.abort_run(
            session.tenant_id,
            session.operator_sub,
            run_id,
            AbortRunRequest(reason=cleaned),
            caller_is_admin=probe.is_tenant_admin,
        )
    except NotRunAssigneeError:
        return await _reassigned_away_fragment(request, session, run_id)
    except RunAlreadyTerminalError:
        return await render_step_fragment(
            request,
            session,
            run_id,
            view=None,
            error_message="This run is already finished; nothing to abort.",
        )
    except RunNotFoundError:
        return render_not_found(request, run_id)

    log.info(
        "ui_runbook_driver_run_aborted",
        tenant_id=str(session.tenant_id),
        run_id=str(run_id),
    )
    return await render_step_fragment(
        request,
        session,
        run_id,
        view=None,
        terminal_state="abandoned",
        abandoned_at=result.abandoned_at,
    )


async def _current_step_view_or_none(
    session: UISessionContext,
    run_id: uuid.UUID,
) -> StepView | None:
    """Best-effort re-projection of the live step for an in-place error render.

    Used by the empty-abort-reason / blank-assignee guards so the step stays
    on screen with the inline alert. Returns ``None`` for a terminal /
    missing run (the caller's template then renders just the alert).
    """
    try:
        current = await RunbookRunService().get_current_step(
            session.tenant_id, session.operator_sub, run_id
        )
    except RunNotFoundError:
        return None
    if isinstance(current, CurrentStepResponse):
        return step_view(current)
    return None


async def _reassign(
    request: Request,
    session: UISessionContext,
    run_id: uuid.UUID,
    new_assignee: str,
) -> HTMLResponse:
    """Transfer the run to *new_assignee* and render the new-ownership state.

    Reached only past the ``require_ui_admin`` hard gate (an operator is 403'd
    at the dependency, before this body). A blank ``new_assignee`` is rejected
    inline (the schema's ``min_length=1`` would 422). After the flip, the
    fragment re-renders for the admin caller; if the admin reassigned the run
    away from themselves, ``is_assignee`` is now ``False`` so their Advance
    control disappears, and the previous assignee's open page will show the
    "reassigned away" message on their next Advance.
    """
    cleaned = new_assignee.strip()
    if not cleaned:
        return await render_step_fragment(
            request,
            session,
            run_id,
            view=await _current_step_view_or_none(session, run_id),
            error_message="A new assignee is required.",
        )

    service = RunbookRunService()
    structlog.contextvars.bind_contextvars(
        operator_sub=session.operator_sub,
        tenant_id=str(session.tenant_id),
        audit_op_id="runbook.reassign",
        audit_op_class="write",
    )
    try:
        await service.reassign_run(
            session.tenant_id,
            session.operator_sub,
            run_id,
            ReassignRunRequest(new_assignee=cleaned),
        )
    except RunAlreadyTerminalError:
        return await render_step_fragment(
            request,
            session,
            run_id,
            view=None,
            error_message="This run is already finished; it cannot be reassigned.",
        )
    except RunNotFoundError:
        return render_not_found(request, run_id)

    log.info(
        "ui_runbook_driver_run_reassigned",
        tenant_id=str(session.tenant_id),
        run_id=str(run_id),
        to_assignee=cleaned,
    )
    return await render_step_fragment(
        request,
        session,
        run_id,
        view=await _current_step_view_or_none(session, run_id),
    )


def register_driver_routes(router: APIRouter) -> None:
    """Register the run-driver routes onto the runbooks *router*.

    Called by
    :func:`meho_backplane.ui.routes.runbooks.routes.build_runbooks_router`
    **before** the ``/ui/runbooks/{slug}`` catch-all (FastAPI is
    first-match-wins -- the ``{slug}`` route would otherwise bind ``runs`` as
    a slug parameter) and **after** T1's literal ``/ui/runbooks/runs/start``
    (registered in :func:`register_runs_routes`), so the ``{run_id}`` param
    route does not swallow the ``start`` segment. The route-ordering contract
    is grep-proof-tested (the ``start`` literal precedes ``{run_id}`` which
    precedes ``{slug}``).

    The reassign route declares ``_require_admin``: an operator gets 403 at
    the dependency, before the handler body / service runs -- the hidden
    client control is convenience only; the server is the authority.
    """

    @router.get("/ui/runbooks/runs/{run_id}", response_class=HTMLResponse)
    async def runbooks_run_driver(
        run_id: uuid.UUID,
        request: Request,
        session: UISessionContext = _require_session,
    ) -> HTMLResponse:
        return await render_driver_page(request, session, run_id)

    @router.post("/ui/runbooks/runs/{run_id}/next", response_class=HTMLResponse)
    async def runbooks_run_next(
        run_id: uuid.UUID,
        request: Request,
        session: UISessionContext = _require_session,
        verify_type: str = Form(default="", max_length=_MAX_VERIFY_TYPE_LENGTH),
        answer: str = Form(default="", max_length=_MAX_ANSWER_LENGTH),
    ) -> HTMLResponse:
        return await _advance(request, session, run_id, verify_type.strip(), answer.strip())

    @router.post("/ui/runbooks/runs/{run_id}/abort", response_class=HTMLResponse)
    async def runbooks_run_abort(
        run_id: uuid.UUID,
        request: Request,
        session: UISessionContext = _require_session,
        reason: str = Form(default="", max_length=_MAX_REASON_LENGTH),
    ) -> HTMLResponse:
        return await _abort(request, session, run_id, reason)

    @router.post("/ui/runbooks/runs/{run_id}/reassign", response_class=HTMLResponse)
    async def runbooks_run_reassign(
        run_id: uuid.UUID,
        request: Request,
        session: UISessionContext = _require_admin,
        new_assignee: str = Form(default="", max_length=_MAX_ASSIGNEE_LENGTH),
    ) -> HTMLResponse:
        return await _reassign(request, session, run_id, new_assignee)
