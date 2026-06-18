# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/agents/runs`` -- the cross-agent run-history list.

Initiative #1824 (G10.8 Agents console), Task #1830 (T3). A scannable
index of the tenant's agent runs, newest-first, filterable by ``status``
and ``work_ref`` -- the console face of ``meho agent runs`` / ``GET
/api/v1/agents/runs``. One handler serves two response shapes (mirroring
:mod:`meho_backplane.ui.routes.connectors.list_view` /
:mod:`meho_backplane.ui.routes.scheduler.list_view`):

* **Full page** (browser navigation) -- the ``agents/runs/list.html`` page
  extending ``base.html``, carrying the "Agents" surface chrome + a
  Definitions/Runs tab strip.
* **HTMX fragment** (``HX-Request: true``) -- the
  ``agents/runs/_table_rows.html`` partial so a filter change re-renders
  only the table body.

The list reads at ``operator`` role via the in-process
:class:`~meho_backplane.agent.invocation.AgentInvoker` (the same
``list_runs`` the Bearer ``GET /api/v1/agents/runs`` route calls) rather
than the REST surface, because a browser carrying only the BFF session
cookie cannot authenticate the Bearer route. Tenant scoping is
non-overrideable -- the invoker's ``list_runs`` keys every query on the
synthesised operator's ``tenant_id``; no query parameter carries a tenant
id (cross-tenant runs are invisible).

URL contract::

    GET /ui/agents/runs
        [?status=pending|running|awaiting_approval|succeeded|failed|cancelled
         &work_ref=<exact-match string>]

An out-of-enum ``status`` -> 422 (the ``StrEnum`` query validator at the
HTTP boundary). The list never exposes ``system_prompt`` / ``toolset`` /
approval params -- the summary projection the invoker returns carries none
of them.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from meho_backplane.agent.invocation import get_agent_invoker
from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import AgentRunStatus
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.agents.runs.operator import resolve_run_reader
from meho_backplane.ui.routes.agents.runs.views import project_run_to_view
from meho_backplane.ui.templating import get_templates

__all__ = ["build_runs_list_router"]

#: Hard cap on the runs a single list render considers. The runs list is a
#: glance surface; an operator chasing a deep history pages via the CLI /
#: REST ``offset``. Matches the invoker's ``list_runs`` ``le=500`` clamp
#: ceiling while keeping the default render bounded.
_LIST_LIMIT = 200

_require_session_dep = Depends(require_ui_session)
_run_reader_dep = Depends(resolve_run_reader)


def _is_htmx_request(request: Request) -> bool:
    """Return ``True`` when HTMX issued the request (``HX-Request: true``).

    HTMX 2 sets ``HX-Request: true`` on every fetch its directives drive
    (https://htmx.org/reference/#request_headers). The handler branches on
    this header to decide between the full page (browser nav) and the
    table-rows fragment (filter swap). Case-insensitive per the HTTP spec.
    """
    return request.headers.get("hx-request", "").lower() == "true"


async def _render(
    request: Request,
    *,
    status_filter: AgentRunStatus | None,
    work_ref: str | None,
    session_ctx: UISessionContext,
    operator: Operator,
) -> HTMLResponse:
    """Render the runs list page or the table-rows fragment.

    Both branches receive the same context shape so the fragment template
    and the full-page template stay interchangeable.
    """
    invoker = get_agent_invoker()
    summaries = await invoker.list_runs(
        operator,
        work_ref=work_ref or None,
        status=status_filter,
        limit=_LIST_LIMIT,
    )
    rows = [project_run_to_view(summary) for summary in summaries]

    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        "page_title": "Agent runs",
        "active_surface": "agents",
        "rows": rows,
        # Shared "now" so the relative-time macro stays consistent across
        # rows within one render (per-row datetime.now would drift).
        "now_utc": datetime.now(UTC),
        "status_options": [s.value for s in AgentRunStatus],
        "status_filter": status_filter.value if status_filter is not None else "",
        "work_ref_filter": work_ref or "",
        "csrf_token": csrf_token,
    }
    template_name = (
        "agents/runs/_table_rows.html" if _is_htmx_request(request) else "agents/runs/list.html"
    )
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


def build_runs_list_router() -> APIRouter:
    """Construct the agent-runs-list :class:`APIRouter`.

    Factory function (chassis convention) so a test app can construct
    parallel routers without sharing route state. Registers the single
    ``GET /ui/agents/runs`` route serving both the full page and the HTMX
    fragment from one handler. The umbrella router includes this **before**
    the agents-definition router so the literal ``/ui/agents/runs`` segment
    is matched before the definition surface's ``/ui/agents/{name}`` would
    bind ``"runs"`` as a name.
    """
    router = APIRouter(tags=["ui-agents"])

    async def _handler(
        request: Request,
        status: AgentRunStatus | None = Query(default=None),
        work_ref: str | None = Query(default=None, max_length=256),
        session_ctx: UISessionContext = _require_session_dep,
        operator: Operator = _run_reader_dep,
    ) -> HTMLResponse:
        """Serve ``GET /ui/agents/runs``. See module docstring for the URL contract."""
        return await _render(
            request,
            status_filter=status,
            work_ref=work_ref,
            session_ctx=session_ctx,
            operator=operator,
        )

    router.add_api_route(
        "/ui/agents/runs",
        _handler,
        methods=["GET"],
        name="ui_agents_runs_list",
        response_class=HTMLResponse,
    )
    return router
