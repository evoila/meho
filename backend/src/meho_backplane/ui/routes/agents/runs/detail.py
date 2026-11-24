# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/agents/runs/{handle}`` -- the per-run detail + poll surface.

Initiative #1824 (G10.8 Agents console), Task #1830 (T3). A durable view
of one run after the request that started it has returned: status, turn
count, resolved provider + model, the structured ``output`` blob, and the
``error`` reason on a failed run. The page polls *after the fact* (not the
live SSE run console -- that is the sibling run console, #1829): while the
run is non-terminal it re-fetches the status panel on a timer; once the run
reaches a terminal state (``succeeded`` / ``failed`` / ``cancelled``) the
panel renders statically and the poll stops.

Two response shapes from one handler:

* **Full page** (browser navigation) -- the ``agents/runs/detail.html``
  page extending ``base.html``. It embeds the status panel
  (``agents/runs/_status_panel.html``) which self-polls via
  ``hx-trigger="load delay:Ns, every Ns"`` while non-terminal.
* **HTMX fragment** (``HX-Request: true``) -- the status panel partial
  alone, so each poll tick swaps only the panel. The panel template emits
  the ``hx-trigger`` poll directive **only while the run is non-terminal**;
  a terminal render drops the directive so HTMX's load-poll cycle ends
  naturally (the documented "stop returning the polling element" pattern,
  https://htmx.org/docs/#polling) -- no JS, no 286-response plumbing.

The full page also lists the run's saved events (a hosted run saves each
turn, tool call, tool result and its final answer or error while it runs,
:mod:`meho_backplane.session_store.hosted_runs`), read once with an audit
row (``sessions.run_events.read``). A run that stopped without a final
event shows "unfinished" and ends with its last saved event. The status
panel poll does not read the events again. A live run links to the run
console, which follows its events as they are saved.

Read at ``operator`` role via the in-process
:class:`~meho_backplane.agent.invocation.AgentInvoker` (the same ``poll``
the Bearer ``GET /api/v1/agents/runs/{handle}`` route uses). Tenant scoping
is non-overrideable: the invoker's ``poll`` raises
:class:`~meho_backplane.agent.invocation.AgentRunNotFoundError` for a
cross-tenant / absent handle, which this handler maps to 404 -- the same
existence-leak collapse the REST surface relies on, so a run id typed into
the URL bar that belongs to another tenant is indistinguishable from one
that does not exist. The detail never exposes ``system_prompt`` /
``toolset`` / approval params: the poll view carries only the run's
runtime state + result.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from meho_backplane.agent.invocation import (
    AgentRunNotFoundError,
    get_agent_invoker,
)
from meho_backplane.agent.run_events import RunEventsView, read_run_events
from meho_backplane.auth.operator import Operator
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.agents.runs.operator import resolve_run_reader
from meho_backplane.ui.routes.agents.runs.views import project_detail_to_view
from meho_backplane.ui.templating import get_templates

__all__ = ["build_runs_detail_router"]

#: Seconds between status-panel polls while a run is non-terminal. A run
#: progresses on the order of seconds-to-minutes (an LLM tool-use loop), so
#: a 3 s cadence is responsive without hammering the durable row.
_POLL_INTERVAL_SECONDS = 3

_require_session_dep = Depends(require_ui_session)
_run_reader_dep = Depends(resolve_run_reader)


def _is_htmx_request(request: Request) -> bool:
    """Return ``True`` when HTMX issued the request (``HX-Request: true``)."""
    return request.headers.get("hx-request", "").lower() == "true"


def _pretty_json(value: dict[str, object] | None) -> str | None:
    """Render the run ``output`` blob as indented text (``None`` -> ``None``).

    ``output`` is a free-shape JSON object (structured agent output, or a
    ``{"text": ...}`` projection of a free-text answer); rendering it
    sorted + 2-space-indented gives a stable, diff-friendly view.
    ``default=str`` keeps a stray non-JSON-native value (e.g. a datetime)
    from raising mid-render.
    """
    if value is None:
        return None
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _events_context(view: RunEventsView) -> dict[str, object]:
    """Project the saved events onto the template's ``run_events`` shape."""
    return {
        "state": view.state,
        "events": [
            {
                "seq": event.seq,
                "kind": event.kind,
                "at": event.created_at,
                "data_json": _pretty_json(event.data) if event.data else None,
                "withheld": event.withheld,
                "quarantined": event.quarantined and not event.data,
            }
            for event in view.events
        ],
        "last_seq": view.events[-1].seq if view.events else None,
    }


def _build_context(
    detail: dict[str, object],
    *,
    csrf_token: str,
    events: RunEventsView | None = None,
) -> dict[str, object]:
    """Assemble the detail-page / status-panel template context."""
    return {
        "page_title": "Agent run",
        "active_surface": "agents",
        "run": detail,
        "output_json": _pretty_json(detail.get("output")),  # type: ignore[arg-type]
        "poll_interval_seconds": _POLL_INTERVAL_SECONDS,
        "csrf_token": csrf_token,
        "run_events": _events_context(events) if events is not None else None,
    }


def build_runs_detail_router() -> APIRouter:
    """Construct the agent-run-detail :class:`APIRouter`.

    Factory function (chassis convention). The umbrella router includes the
    list router (``/ui/agents/runs``) and this detail router before the
    agents-definition router so the ``runs`` segment is never bound as a
    definition ``{name}``; the ``uuid.UUID`` path type also 422s a non-UUID
    handle, but the include-order discipline is the primary guard.
    """
    router = APIRouter(tags=["ui-agents"])

    @router.get("/ui/agents/runs/{handle}", response_class=HTMLResponse)
    async def run_detail(
        request: Request,
        handle: uuid.UUID,
        session_ctx: UISessionContext = _require_session_dep,
        operator: Operator = _run_reader_dep,
    ) -> HTMLResponse:
        """Render the per-run detail page or poll the status-panel fragment."""
        invoker = get_agent_invoker()
        try:
            view = await invoker.poll(operator, handle)
        except AgentRunNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="agent_run_not_found",
            ) from exc
        detail = project_detail_to_view(view)
        csrf_token = mint_csrf_token(str(session_ctx.session_id))
        if _is_htmx_request(request):
            template_name = "agents/runs/_status_panel.html"
            context = _build_context(detail, csrf_token=csrf_token)
        else:
            template_name = "agents/runs/detail.html"
            events = await read_run_events(operator, handle, audit_path=request.url.path)
            context = _build_context(detail, csrf_token=csrf_token, events=events)
        response = get_templates().TemplateResponse(request, template_name, context)
        response.set_cookie(
            key=CSRF_COOKIE_NAME,
            value=csrf_token,
            httponly=False,
            secure=True,
            samesite="strict",
            path="/ui",
        )
        return response

    return router
