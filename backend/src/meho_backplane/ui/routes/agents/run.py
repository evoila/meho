# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Agent run console -- the live SSE bridge over ``invoker.stream_events``.

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
``/ui/agents`` that proxies the same
:meth:`~meho_backplane.agent.invocation.AgentInvoker.stream_events`
generator -- exactly the shape
:mod:`meho_backplane.ui.routes.broadcast.stream` established for the
activity feed (see its docstring for the ``EventSource``-header
limitation in full).

Tenant isolation
================

The broadcast bridge keys its Valkey stream by the session's tenant
(``meho:feed:{tenant_id}``); a run stream has no Valkey stream -- it
drives a *fresh* run inline. The tenant-isolation lever here is the
**lifted operator**: :func:`stream_events` is tenant-scoped through the
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
  ``ensure_runnable`` pre-check), then mints a short-lived signed run
  token (:mod:`~meho_backplane.ui.routes.agents.run_token`) and returns
  the transcript fragment whose ``sse-connect`` carries it.
* ``GET /ui/agents/{name}/run/stream?token=...`` -- the cookie-authed
  bridge. It verifies the token against the cookie session (so a forged /
  replayed / cross-session token streams nothing), lifts the operator,
  and proxies ``stream_events`` with ``X-Accel-Buffering: no`` so each
  frame flushes immediately (SSE buffering #1389 is fixed, so the console
  paints turn-by-turn rather than only at completion).

No Stop button
==============

This console ships **without** a Stop affordance. "Stop watching" merely
closes the ``EventSource`` -- it does not cancel the run. The operator
run-cancel REST endpoint (T8 #1828) and its Stop button (T9 #1833) are
separate Tasks; wiring a Stop control here would be a no-op cancel.

The sensitive ``system_prompt`` / ``toolset`` bodies are never logged or
streamed by this surface -- the transcript carries only the runtime's
progress frames (turn boundaries, tool names + args, tool results, final
output, errors), the same vocabulary the REST SSE route emits.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Final

import structlog
from fastapi import HTTPException, Request, status
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import ValidationError

from meho_backplane.agent.invocation import (
    AgentDisabledError,
    AgentNotFoundError,
    BudgetExceededError,
    get_agent_invoker,
)
from meho_backplane.api.v1.agent_runs import AgentRunRequest, _events_generator
from meho_backplane.auth.operator import Operator
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.agents.run_token import (
    mint_run_token,
    verify_run_token,
)
from meho_backplane.ui.routes.agents.views import (
    _fetch_agent_or_404,
    is_htmx_request,
)
from meho_backplane.ui.templating import get_templates

__all__ = [
    "WORK_REF_MAX",
    "render_run_console",
    "stream_run_events",
    "submit_run",
]

_log = structlog.get_logger(__name__)

#: Max length of the optional ``work_ref`` form field. Mirrors the
#: change-ticket reference the run row stamps (work_ref I3-T2 #1662); a
#: generous cap that bounds the form-body parse before it reaches the
#: token mint.
WORK_REF_MAX: Final[int] = 256

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
) -> dict[str, object]:
    """Build the run-console template context."""
    return {
        "page_title": "Agents",
        "active_surface": "agents",
        "operator_sub": session_ctx.operator_sub,
        "csrf_token": csrf_token,
        "agent_name": agent_name,
        "turn_budget": turn_budget,
        "enabled": enabled,
    }


async def render_run_console(
    request: Request,
    session_ctx: UISessionContext,
    *,
    name: str,
) -> HTMLResponse:
    """Render the run console page for one agent (operator-or-above).

    404 for an absent / cross-tenant name (the read fetch collapses both
    to "not found", the existence-leak posture the rest of the surface
    holds). The page carries the agent's ``turn_budget`` so the operator
    sees the cost ceiling before pressing Run, and disables the Run
    control for a disabled definition (a run would 409 anyway).
    """
    agent = await _fetch_agent_or_404(session_ctx, name)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context = _console_context(
        session_ctx,
        csrf_token,
        agent_name=agent.name,
        turn_budget=agent.turn_budget,
        enabled=agent.enabled,
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
    cleaned_work_ref = work_ref.strip() if work_ref else None
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
        "run_token": token,
    }
    response = get_templates().TemplateResponse(request, "agents/_run_transcript.html", context)
    _set_csrf_cookie(response, csrf_token)
    return response


async def _bridge_generator(
    operator: Operator,
    *,
    name: str,
    input_: str,
    work_ref: str | None,
) -> AsyncIterator[str]:
    """Proxy ``invoker.stream_events`` for the cookie-authed GET bridge.

    Delegates to the REST surface's
    :func:`~meho_backplane.api.v1.agent_runs._events_generator` so the
    UI bridge and the REST SSE route emit byte-identical frames
    (``event: <kind>`` / ``data: {run_id, ...}``) and bind the
    ``Meho-Work-Ref`` the same way. A client disconnect propagates as
    :class:`asyncio.CancelledError` into the pending iteration; the
    underlying loop's cleanup cancels the in-flight run and re-raises per
    asyncio's contract.
    """
    async for frame in _events_generator(operator, name, input_, work_ref):
        yield frame


async def stream_run_events(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    *,
    name: str,
    token: str,
) -> StreamingResponse:
    """``GET /ui/agents/{name}/run/stream`` -- the cookie-authed SSE bridge.

    Verifies the run token against the cookie session (a forged / expired
    / cross-session token is 403 -- never an opened stream that runs an
    agent), confirms the token's bound agent name matches the path
    segment, then proxies ``stream_events`` for the lifted operator. The
    run prompt comes from the *token* (authorised by the CSRF-gated POST),
    not the query string, so a tampered query cannot change what runs.

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
    structlog.contextvars.bind_contextvars(
        audit_op_id="agent.run_events",
        audit_op_class="write",
        audit_agent_name=name,
    )
    # Belt-and-suspenders runnable pre-check: the POST already ran it, but
    # the definition could have been disabled / deleted in the window
    # between authorising the run and the EventSource connecting. Resolve
    # it before opening the stream so the failure is a clean HTTP status,
    # not a torn text/event-stream the EventSource would auto-reconnect
    # into a hot loop -- the same posture the REST route holds.
    invoker = get_agent_invoker()
    try:
        await invoker.ensure_runnable(operator, name)
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
    del request  # tenant + identity come from the session, not the request.
    return StreamingResponse(
        _bridge_generator(
            operator,
            name=name,
            input_=decoded.input,
            work_ref=decoded.work_ref,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",
        },
    )
