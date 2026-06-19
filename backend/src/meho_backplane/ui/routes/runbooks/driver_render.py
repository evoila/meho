# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Runbooks UI run-driver rendering layer (T2 #1893).

Initiative #1837 (G10.11 Runbook runs UI), Task #1893. The render
primitives the driver's route handlers (:mod:`.driver`) reuse: the
single-step projection :class:`StepView`, the full-page render
:func:`render_driver_page`, the 404 render :func:`render_not_found`, and
the HTMX fragment render :func:`render_step_fragment`.

Split out of :mod:`.driver` so neither file crosses the code-quality size
gate -- the same package-split convention
:mod:`meho_backplane.ui.routes.runbooks.lifecycle` /
:mod:`~meho_backplane.ui.routes.runbooks.runs` follow. This module owns the
Jinja context assembly + CSRF cookie refresh; ``driver`` owns the action
orchestration (Advance / Abort / Reassign) + the route wiring.

OPACITY: every render here surfaces only the ONE current step
(:attr:`StepView.step`) -- the Markdown body is pre-rendered once
(:func:`render_markdown`) and the templates carry no full-step-list loop.
This is the read-side mirror of the single-step
:class:`~meho_backplane.runbooks.runs_schemas.CurrentStepResponse`
projection; re-introducing the template's step list here would re-open the
#1198 skip-ahead leak.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import HTMLResponse

from meho_backplane.runbooks.run_service import RunbookRunService, RunNotFoundError
from meho_backplane.runbooks.runs_schemas import (
    AbortRunResponse,
    CurrentStepResponse,
    RunCompletedResponse,
    StepBody,
    StepPosition,
)
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import mint_csrf_token
from meho_backplane.ui.routes.connectors.operator import resolve_role_probe
from meho_backplane.ui.routes.kb.render import render_markdown
from meho_backplane.ui.routes.runbooks.editor import set_csrf_cookie
from meho_backplane.ui.templating import get_templates

__all__ = [
    "StepView",
    "render_driver_page",
    "render_not_found",
    "render_step_fragment",
    "step_view",
]


@dataclass(frozen=True)
class StepView:
    """Render-ready projection of the driver's single current step.

    Carries the substituted :class:`StepBody` (for type / op_id / params /
    verify access in the template) plus the pre-rendered Markdown
    ``body_html`` so the Jinja layer never calls the renderer itself --
    mirroring the ``{step, body_html}`` shape
    :func:`meho_backplane.ui.routes.runbooks.routes._render_steps` produces,
    but for exactly ONE step (the opacity property). ``failed`` flags the
    dead-end state: the step verify failed (operator answered ``no`` /
    ``escalate``, or an operation_call verify did not match), so the only
    forward move is Abort -- Advance is hidden and a banner explains why.
    """

    step: StepBody
    body_html: object
    position: StepPosition
    failed: bool = False


def step_view(response: CurrentStepResponse, *, failed: bool = False) -> StepView:
    """Project a :class:`CurrentStepResponse` into the render-ready single step.

    The Markdown render happens here (once) so the template renders raw
    ``body_html``. Only the one ``current_step`` is touched -- the response
    carries no other step by construction, so there is nothing to leak.
    """
    return StepView(
        step=response.current_step,
        body_html=render_markdown(response.current_step.body),
        position=response.position,
        failed=failed,
    )


def _base_context(
    *,
    run_id: uuid.UUID,
    csrf_token: str,
) -> dict[str, object]:
    """Shared template context keys for every driver render.

    Seeds every optional key the ``_run_step.html`` fragment reads so both
    render paths (the full page include + the POST fragment swap) always
    supply them -- the templating env runs with strict ``Undefined``, so a
    missing key is an ``UndefinedError``, not a silent empty string. The
    callers overwrite the keys they own.
    """
    return {
        "run_id": str(run_id),
        "csrf_token": csrf_token,
        "active_surface": "runbooks",
        "active_tab": "runs",
        # Fragment optionals (defaults; callers override as needed).
        "step_view": None,
        "terminal_state": None,
        "error_message": None,
        "completed_at": None,
        "abandoned_at": None,
        "is_assignee": False,
        "is_admin": False,
    }


async def render_driver_page(
    request: Request,
    session: UISessionContext,
    run_id: uuid.UUID,
) -> HTMLResponse:
    """Render the full driver page for *run_id*, or a 404 page.

    Reads the single current step (opacity-safe) + the run's assignee + the
    caller's admin probe, then renders the page with the action controls
    gated:

    * **Advance** only when ``session.operator_sub == assigned_to``.
    * **Reassign** only for a ``tenant_admin`` (the soft probe hides the
      control; the POST re-gates hard via ``require_ui_admin``).

    A terminal run (``completed`` / ``abandoned``) renders the terminal
    banner with no step body and no Advance. ``RunNotFoundError`` (unknown
    or cross-tenant run id) renders the 404 page rather than leaking
    existence via a status-code differential.
    """
    service = RunbookRunService()
    try:
        current = await service.get_current_step(session.tenant_id, session.operator_sub, run_id)
    except RunNotFoundError:
        return render_not_found(request, run_id)

    probe = await resolve_role_probe(request, session)
    csrf_token = mint_csrf_token(str(session.session_id))
    context = _base_context(run_id=run_id, csrf_token=csrf_token)
    context["is_admin"] = probe.is_tenant_admin
    context["page_title"] = "Runbook run"

    if isinstance(current, RunCompletedResponse):
        context["terminal_state"] = "completed"
        context["completed_at"] = current.completed_at
    elif isinstance(current, AbortRunResponse):
        context["terminal_state"] = "abandoned"
        context["abandoned_at"] = current.abandoned_at
    else:
        assignee = await service.get_run_assignee(session.tenant_id, run_id)
        context["is_assignee"] = session.operator_sub == assignee
        context["template_slug"] = current.template_slug
        context["template_version"] = current.template_version
        context["step_view"] = step_view(current)

    response = get_templates().TemplateResponse(request, "runbooks/run_driver.html", context)
    set_csrf_cookie(response, csrf_token)
    return response


def render_not_found(request: Request, run_id: uuid.UUID) -> HTMLResponse:
    """Render the driver 404 page for an unresolvable / cross-tenant run id."""
    context = {
        "run_id": str(run_id),
        "active_surface": "runbooks",
        "active_tab": "runs",
        "page_title": "Run not found",
    }
    return get_templates().TemplateResponse(
        request, "runbooks/run_not_found.html", context, status_code=404
    )


async def render_step_fragment(
    request: Request,
    session: UISessionContext,
    run_id: uuid.UUID,
    *,
    view: StepView | None,
    terminal_state: str | None = None,
    error_message: str | None = None,
    completed_at: object | None = None,
    abandoned_at: object | None = None,
) -> HTMLResponse:
    """Render the ``_run_step.html`` fragment for an HTMX swap.

    The single OOB-free fragment the next / abort POSTs swap into
    ``#runbook-run-step``. Carries the (re-projected) current step, or the
    terminal banner, plus an optional inline alert. The CSRF token is
    re-minted + the cookie refreshed so the next control carries a live
    double-submit pair (the prior token was consumed by this POST).

    Advance is shown only for a live (non-terminal, non-failed) step the
    caller is assigned: the handlers re-resolve the assignee from the
    service result they just produced, so a reassign race is reflected
    immediately; the failed / terminal branches force the control off.
    """
    probe = await resolve_role_probe(request, session)
    csrf_token = mint_csrf_token(str(session.session_id))
    context = _base_context(run_id=run_id, csrf_token=csrf_token)
    context["is_admin"] = probe.is_tenant_admin
    context["step_view"] = view
    context["terminal_state"] = terminal_state
    context["error_message"] = error_message
    context["completed_at"] = completed_at
    context["abandoned_at"] = abandoned_at
    if view is not None and terminal_state is None and not view.failed:
        assignee = await RunbookRunService().get_run_assignee(session.tenant_id, run_id)
        context["is_assignee"] = session.operator_sub == assignee
    response = get_templates().TemplateResponse(request, "runbooks/_run_step.html", context)
    set_csrf_cookie(response, csrf_token)
    return response
