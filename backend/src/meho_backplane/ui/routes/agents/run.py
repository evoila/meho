# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Agent run console -- start a run, then follow its saved events live.

Initiative #1824 (G10.8 Agents console), Task #1829 (T2). The highest-
value surface in G10.8: invoke an agent and watch it reason live, frame
by frame (``turn`` / ``tool_call`` / ``tool_result`` / ``final`` /
``error``) streaming into a transcript pane.

Why a UI-owned GET SSE bridge
=============================

The canonical streaming run path is ``POST /api/v1/agents/{name}/run/events``
(:mod:`meho_backplane.api.v1.agent_runs`) -- a ``POST`` authenticated by
the ``Authorization: Bearer <jwt>`` header. The browser's ``EventSource``
(the only browser primitive that speaks Server-Sent Events) issues a
**GET** with **no custom headers**: it cannot send a JWT header and it
cannot POST a body. So the console needs a cookie-authed GET bridge under
``/ui/agents`` -- the shape :mod:`meho_backplane.ui.routes.broadcast.stream`
established for the activity feed (see its docstring for the
``EventSource``-header limitation in full).

The bridge starts the run in the background
(:meth:`~meho_backplane.agent.invocation.AgentInvoker.run` with
``async_mode=True``) and then streams the run's saved events from the
session store (:func:`~meho_backplane.ui.routes.agents.runs.events.tail_frames`).
So the run goes on when the page closes. The console puts the run id in
its address (``?run=<id>``); a reload then follows the same run through
``GET /ui/agents/runs/{handle}/events``: first the events so far, then
the live ones.

Tenant isolation
================

The broadcast bridge keys its Valkey stream by the session's tenant
(``meho:feed:{tenant_id}``); a run stream has no Valkey stream -- it
starts a *fresh* run. The tenant-isolation lever here is the
**lifted operator**: the invoker's ``run`` is tenant-scoped through the
:class:`~meho_backplane.auth.operator.Operator` (it loads only the
operator's own tenant's definition and records the run under that
tenant), identical to the REST surface. The operator is lifted from the
validated BFF session via
:func:`~meho_backplane.ui.routes.agents.operator.resolve_run_operator_or_403`,
never from a request parameter -- so a crafted request cannot redirect
the run to another tenant's agent, the same guarantee the broadcast
bridge holds with its session-derived stream key.

CSRF + the run-handoff token
============================

Starting a run executes real tool-calls against live targets and incurs
provider cost (the #1829 risk note). The console therefore splits the
flow so the **state-changing** half stays behind the chassis CSRF
double-submit gate (which exempts safe-method GETs -- and ``EventSource``
can only GET):

* ``POST /ui/agents/{name}/run`` -- CSRF-gated, operator-role. Validates
  the prompt, confirms the agent is runnable (404 / 409 / 429 surface
  here, before any stream opens, mirroring the REST route's
  ``ensure_runnable`` pre-check), then mints a short-lived encrypted run
  token (:mod:`~meho_backplane.ui.routes.agents.run_token`) and returns
  the transcript fragment whose ``sse-connect`` carries it.
* ``GET /ui/agents/{name}/run/stream?token=...`` -- the cookie-authed
  bridge. It verifies the token against the cookie session (so a forged /
  cross-session token streams nothing), records the token's first use
  (a second GET with the same token gets ``204`` and starts no run), lifts
  the operator, starts the run and streams its saved events with
  ``X-Accel-Buffering: no`` so each frame flushes immediately (SSE
  buffering #1389 is fixed, so the console paints turn-by-turn rather
  than only at completion).

Stop button
===========

The console carries a **Stop** control (T9 #1833) over the operator
run-cancel path (T8 #1828). The browser holds a cookie session, not a
JWT, so it cannot call the Bearer-authed REST cancel route
(``POST /api/v1/agents/runs/{handle}/cancel``) directly; instead the
console posts to a cookie-authed BFF proxy,
``POST /ui/agents/{name}/run/{handle}/cancel``, which lifts the operator
from the session (operator floor, the same gate the run POST uses) and
drives the *same* in-process
:meth:`~meho_backplane.agent.invocation.AgentInvoker.cancel` the REST
route does -- so the durable ``cancelled`` transition runs through one
service path regardless of caller. The proxy is CSRF-double-submit-gated
(a state-changing ``/ui/*`` POST) and maps the cancel outcomes to the
same statuses the REST route returns: 404 (unknown / cross-tenant
handle), 409 (already-terminal -- ``agent_run_not_cancellable``), 403
(role below the cancel floor). The Alpine controller turns those into
inline feedback rather than a torn surface (a 409 means the run already
reached a terminal state; the console refreshes the status pill instead
of erroring).

The ``run_id`` the Stop control cancels is the one the SSE stream
surfaces (the controller learns it from the first frame's ``run_id``),
so the affordance only appears once a run is live and disappears the
moment the run reaches a terminal frame -- a terminal run is not
cancellable and the proxy would 409 it.

The sensitive ``system_prompt`` / ``toolset`` bodies are never logged or
streamed by this surface -- the transcript carries only the runtime's
progress frames (turn boundaries, tool names + args, tool results, final
output, errors), the same vocabulary the REST SSE route emits.
"""

from __future__ import annotations

import uuid
from typing import Final
from urllib.parse import quote

import structlog
from fastapi import HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from redis.exceptions import RedisError

from meho_backplane.agent.invocation import (
    AgentDisabledError,
    AgentNotFoundError,
    AgentRunNotFoundError,
    BudgetExceededError,
    get_agent_invoker,
)
from meho_backplane.api.v1.agent_runs import AgentRunRequest, _bound_work_ref
from meho_backplane.auth.operator import Operator
from meho_backplane.operations.agent_run import (
    IllegalTransitionError,
    UnauthorizedCancellationError,
)
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.agents.run_token import (
    claim_run_token,
    mint_run_token,
    verify_run_token,
)
from meho_backplane.ui.routes.agents.runs.events import (
    events_response,
    first_read,
    tail_frames,
)
from meho_backplane.ui.routes.agents.views import (
    _fetch_agent_or_404,
    is_htmx_request,
)
from meho_backplane.ui.routes.work_ref_form import form_work_ref
from meho_backplane.ui.templating import get_templates

__all__ = [
    "cancel_run",
    "render_run_console",
    "stream_run_events",
    "submit_run",
]

_log = structlog.get_logger(__name__)

#: Max length of the prompt textarea. Generous (a run prompt can be a
#: multi-paragraph instruction) but bounded so a pathological submit
#: cannot blow the form-body parse or the signed token.
INPUT_MAX: Final[int] = 16 * 1024


def _set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Mirror the chassis CSRF cookie posture for the console renders."""
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


def _console_context(
    session_ctx: UISessionContext,
    csrf_token: str,
    *,
    agent_name: str,
    turn_budget: int,
    enabled: bool,
    run_id: uuid.UUID | None,
) -> dict[str, object]:
    """Build the run-console template context.

    With a *run_id*, the console follows that run's saved events
    (``GET /ui/agents/runs/{handle}/events``) instead of waiting for a new run.
    """
    context: dict[str, object] = {
        "page_title": "Agents",
        "active_surface": "agents",
        "operator_sub": session_ctx.operator_sub,
        "csrf_token": csrf_token,
        "agent_name": agent_name,
        "turn_budget": turn_budget,
        "enabled": enabled,
    }
    if run_id is not None:
        context["stream_url"] = f"/ui/agents/runs/{run_id}/events"
        context["remember_run"] = False
    return context


async def render_run_console(
    request: Request,
    session_ctx: UISessionContext,
    *,
    name: str,
    run_id: uuid.UUID | None = None,
) -> HTMLResponse:
    """Render the run console page for one agent (operator-or-above).

    404 for an absent / cross-tenant name (the read fetch collapses both
    to "not found", the existence-leak posture the rest of the surface
    holds). The page carries the agent's ``turn_budget`` so the operator
    sees the cost ceiling before pressing Run, and disables the Run
    control for a disabled definition (a run would 409 anyway).

    *run_id* (the ``?run=`` query the console sets once a run starts)
    shows that run's transcript: its saved events, then the live ones. The
    events stream checks the run's tenant, so a run id of another tenant
    shows nothing.
    """
    agent = await _fetch_agent_or_404(session_ctx, name)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context = _console_context(
        session_ctx,
        csrf_token,
        agent_name=agent.name,
        turn_budget=agent.turn_budget,
        enabled=agent.enabled,
        run_id=run_id,
    )
    template = (
        "agents/_run_console_body.html" if is_htmx_request(request) else "agents/run_console.html"
    )
    response = get_templates().TemplateResponse(request, template, context)
    _set_csrf_cookie(response, csrf_token)
    return response


def _render_run_error(
    request: Request,
    session_ctx: UISessionContext,
    *,
    agent_name: str,
    message: str,
    status_code: int,
) -> HTMLResponse:
    """Re-render the run form fragment carrying an actionable error.

    The form's ``hx-post`` targets the transcript region with
    ``hx-swap="innerHTML"``, so a 404 / 409 / 429 from the runnable
    pre-check swaps an inline alert in place of the transcript -- the
    operator reads *why* the run was refused (no such agent / disabled /
    budget exhausted) instead of a torn stream or a generic error page.
    """
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        "page_title": "Agents",
        "active_surface": "agents",
        "csrf_token": csrf_token,
        "agent_name": agent_name,
        "error_message": message,
    }
    response = get_templates().TemplateResponse(
        request,
        "agents/_run_error.html",
        context,
        status_code=status_code,
    )
    _set_csrf_cookie(response, csrf_token)
    return response


async def submit_run(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    *,
    name: str,
    input_: str,
    work_ref: str | None,
) -> HTMLResponse:
    """Authorise a run and hand back the transcript fragment (CSRF-gated).

    The state-changing half of the run flow. Validates the prompt via the
    same :class:`~meho_backplane.api.v1.agent_runs.AgentRunRequest` model
    the REST surface uses (an empty prompt re-renders the form with an
    inline error), confirms the agent is runnable -- surfacing 404 /
    409 / 429-budget as actionable inline alerts before any stream opens
    -- then mints a run token binding ``(session, name, input, work_ref)``
    and returns the transcript fragment whose ``sse-connect`` carries it.
    The actual run executes inside the GET bridge the fragment subscribes
    to (one ``EventSource`` connection = one run's lifetime).
    """
    cleaned_work_ref = form_work_ref(work_ref)
    # Reject a blank / whitespace-only prompt up front. The REST
    # ``AgentRunRequest`` model only enforces ``min_length=1`` (so a
    # single space would pass and burn a turn on an empty instruction);
    # the console strips first and surfaces the inline 422 the operator
    # can act on rather than silently running on whitespace.
    cleaned_input = input_.strip()
    try:
        validated = AgentRunRequest(input=cleaned_input)
    except ValidationError:
        return _render_run_error(
            request,
            session_ctx,
            agent_name=name,
            message="Enter a prompt to run the agent.",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    invoker = get_agent_invoker()
    try:
        await invoker.ensure_runnable(operator, name)
    except AgentNotFoundError:
        return _render_run_error(
            request,
            session_ctx,
            agent_name=name,
            message="No such agent in this tenant. It may have been deleted.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    except AgentDisabledError:
        return _render_run_error(
            request,
            session_ctx,
            agent_name=name,
            message="This agent is disabled. Enable it from the agent detail page before running.",
            status_code=status.HTTP_409_CONFLICT,
        )
    except BudgetExceededError as exc:
        return _render_run_error(
            request,
            session_ctx,
            agent_name=name,
            message=f"Run budget exceeded ({exc.reason}). Try again later or raise the budget.",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    token = mint_run_token(
        session_id=str(session_ctx.session_id),
        name=name,
        input_=validated.input,
        work_ref=cleaned_work_ref,
    )
    _log.info(
        "ui_agent_run_authorised",
        tenant_id=str(session_ctx.tenant_id),
        operator_sub=session_ctx.operator_sub,
        name=name,
        has_work_ref=cleaned_work_ref is not None,
    )
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        "page_title": "Agents",
        "active_surface": "agents",
        "csrf_token": csrf_token,
        "agent_name": name,
        "stream_url": f"/ui/agents/{quote(name, safe='')}/run/stream?token={quote(token, safe='')}",
        "remember_run": True,
    }
    response = get_templates().TemplateResponse(request, "agents/_run_transcript.html", context)
    _set_csrf_cookie(response, csrf_token)
    return response


async def _start_run(
    operator: Operator, *, name: str, input_: str, work_ref: str | None
) -> uuid.UUID:
    """Start the run in the background and return its id.

    Re-checks that the agent is runnable: the POST already ran the check,
    but the definition could have been disabled or deleted in the window
    between authorising the run and the EventSource connecting. A refusal
    is a clean HTTP status, not a torn text/event-stream the EventSource
    would reconnect into a hot loop -- the same posture the REST route
    holds.
    """
    invoker = get_agent_invoker()
    try:
        await invoker.ensure_runnable(operator, name)
        with _bound_work_ref(work_ref):
            outcome = await invoker.run(operator, name, input_, async_mode=True)
    except AgentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent_not_found",
        ) from exc
    except AgentDisabledError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="agent_disabled",
        ) from exc
    except BudgetExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "budget_exceeded", "reason": exc.reason},
        ) from exc
    return outcome.run_id


async def stream_run_events(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    *,
    name: str,
    token: str,
) -> Response:
    """``GET /ui/agents/{name}/run/stream`` -- the cookie-authed SSE bridge.

    Verifies the run token against the cookie session (a forged / expired
    / cross-session token is 403 -- never an opened stream that runs an
    agent), confirms the token's bound agent name matches the path
    segment, then starts the run in the background for the lifted
    operator and streams its saved events. The run prompt comes from the
    *token* (authorised by the CSRF-gated POST), not the query string, so
    a tampered query cannot change what runs.

    One token starts one run. The first accepted GET records the token's
    id (:func:`claim_run_token`). Every later GET with the same token gets
    ``204 No Content`` and starts no run; ``204`` also tells the browser's
    ``EventSource`` to stop reconnecting. When Valkey cannot record the
    use, the bridge answers ``503`` and starts no run.

    The run does not stop when the stream closes. Following it again is
    ``GET /ui/agents/runs/{handle}/events``. Opening this stream writes one
    audit row for the read of the run's events.

    Sets ``X-Accel-Buffering: no`` so the per-frame flush is not held by
    an intermediary (nginx) buffer -- the live console paints turn-by-turn
    (SSE buffering #1389 is fixed).
    """
    decoded = verify_run_token(session_id=str(session_ctx.session_id), token=token)
    if decoded is None or decoded.name != name:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="agent_run_token_invalid",
        )
    try:
        first_use = await claim_run_token(decoded.token_id)
    except RedisError as exc:
        # No exc_info: a traceback with locals would print the token.
        _log.warning("ui_agent_run_token_claim_failed", name=name, error_type=type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="agent_run_token_store_unavailable",
        ) from exc
    if not first_use:
        _log.info("ui_agent_run_token_reused", name=name)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    structlog.contextvars.bind_contextvars(
        audit_op_id="agent.run_events",
        audit_op_class="write",
        audit_agent_name=name,
    )
    run_id = await _start_run(operator, name=name, input_=decoded.input, work_ref=decoded.work_ref)
    first = await first_read(operator, run_id, after=0, path=request.url.path)
    return events_response(tail_frames(operator, first))


async def cancel_run(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    *,
    name: str,
    handle: uuid.UUID,
) -> Response:
    """``POST /ui/agents/{name}/run/{handle}/cancel`` -- cookie-authed cancel.

    The Stop button's BFF proxy. ``EventSource`` cannot carry a JWT, so
    the browser holds a cookie session, not the Bearer token the REST
    cancel route (``POST /api/v1/agents/runs/{handle}/cancel``) requires.
    This route lifts the operator from the session (operator floor, the
    same :func:`resolve_run_operator_or_403` gate the run POST uses) and
    drives the *same* in-process
    :meth:`~meho_backplane.agent.invocation.AgentInvoker.cancel` the REST
    route does, so the durable ``cancelled`` transition runs through one
    service path. The CSRF double-submit middleware gates this POST; the
    Alpine controller echoes the token in the ``X-CSRF-Token`` header.

    The ``name`` path segment is the console's, not a cancel input -- the
    invoker scopes the cancel by ``(tenant, run_id)``, so the run's
    parent agent is irrelevant to the transition. It rides the path only
    so the route nests under the per-agent console URL.

    Outcomes mirror the REST route exactly:

    * 204 on a successful cancel (no body -- the Alpine controller flips
      to the ``cancelled`` pill and closes the stream client-side).
    * 404 (``agent_run_not_found``) for an unknown / cross-tenant handle;
      existence is not leaked across tenants.
    * 409 (``agent_run_not_cancellable``) for an already-terminal run --
      including one that raced to a terminal state between the click and
      the write. The controller treats this as "already done", not an
      error.
    * 403 (``agent_run_cancel_forbidden``) if the operator's role ranks
      below the cancel floor (defence-in-depth; the route gate already
      rejects ``read_only``).
    """
    del request  # tenant + identity come from the lifted operator.
    structlog.contextvars.bind_contextvars(
        audit_op_id="agent.cancel_run",
        audit_op_class="write",
        audit_agent_name=name,
    )
    invoker = get_agent_invoker()
    try:
        await invoker.cancel(operator, handle)
    except AgentRunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent_run_not_found",
        ) from exc
    except UnauthorizedCancellationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="agent_run_cancel_forbidden",
        ) from exc
    except IllegalTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="agent_run_not_cancellable",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
