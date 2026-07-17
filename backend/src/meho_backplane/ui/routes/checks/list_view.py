# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/checks`` -- the per-tenant Dashboard list ("is everything OK?").

Task #2506 under Initiative #2416 (parent goal #221). The anchor console
page: one glance answers whether every composed check is healthy. The route
serves two response shapes from one handler (the scheduler / connectors
mould):

* **Full page** (normal browser navigation) -- the ``checks/list.html`` page
  extending ``base.html``.
* **HTMX fragment** (``HX-Request: true``) -- the ``checks/_table_rows.html``
  partial. The full page arms an ``hx-trigger="every 30s"`` poll that
  re-fetches this route and swaps only the table body, so the rolled-up
  states stay live without a manual refresh.

Read at ``operator`` role via the in-process
:class:`~meho_backplane.checks.dashboard_service.CheckDashboardAdminService`
(the same service the Bearer ``GET /api/v1/checks/dashboards`` route uses)
rather than the REST surface, because a browser carrying only the BFF session
cookie cannot authenticate the Bearer route. Tenant scoping is
non-overrideable -- the service's first WHERE clause is the session's
``tenant_id``; no query parameter carries a tenant id. The surface is
read-only in v1 (no create / delete affordances -- REST is the single write
path), so there is no role probe or CSRF token.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from meho_backplane.checks.dashboard_service import CheckDashboardAdminService
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.checks.views import project_dashboard_to_row
from meho_backplane.ui.templating import get_templates

__all__ = ["build_list_router"]

#: Hard cap on the Dashboards a single list render considers. The checks list
#: is a glance surface; a tenant with more than this many Dashboards has a
#: composition-sprawl problem the list view is not the place to page through.
_LIST_LIMIT = 200

#: Module-level ``Depends`` closure -- ruff B008 idiom (no function calls in
#: default argument positions), matching the scheduler / connectors routes.
_require_session_dep = Depends(require_ui_session)


def _is_htmx_request(request: Request) -> bool:
    """Return ``True`` when HTMX issued the request (``HX-Request: true``)."""
    return request.headers.get("hx-request", "").lower() == "true"


async def _render(request: Request, *, session_ctx: UISessionContext) -> HTMLResponse:
    """Render the list page or the table-rows fragment.

    Both branches receive the same context shape so the fragment template and
    the full-page template stay interchangeable.
    """
    service = CheckDashboardAdminService()
    dashboards = await service.list_(session_ctx.tenant_id, limit=_LIST_LIMIT)
    rows = [project_dashboard_to_row(d) for d in dashboards]
    context: dict[str, object] = {
        "page_title": "Checks",
        "active_surface": "checks",
        "rows": rows,
        # Shared "now" so the relative-time macro stays consistent across
        # rows within one render.
        "now_utc": datetime.now(UTC),
    }
    template_name = "checks/_table_rows.html" if _is_htmx_request(request) else "checks/list.html"
    return get_templates().TemplateResponse(request, template_name, context)


def build_list_router() -> APIRouter:
    """Construct the checks-list :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can construct
    parallel routers without sharing route state -- the chassis convention
    every surface router follows. Registers the single ``GET /ui/checks``
    route serving both the full page and the HTMX fragment from one handler.
    """
    router = APIRouter(tags=["ui-checks"])

    async def _handler(
        request: Request,
        session_ctx: UISessionContext = _require_session_dep,
    ) -> HTMLResponse:
        """Serve ``GET /ui/checks``. See module docstring for the contract."""
        return await _render(request, session_ctx=session_ctx)

    router.add_api_route(
        "/ui/checks",
        _handler,
        methods=["GET"],
        name="ui_checks_list",
        response_class=HTMLResponse,
    )
    return router
