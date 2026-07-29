# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/runners`` -- the per-tenant satellite-runner fleet ("who's alive?").

Task #2589, a follow-up to Initiative #2415 (the push-only satellite-runner
gateway). #2415 shipped the whole gateway surface -- runner mode (#2497),
scoped principals (#2502), the long-poll command plane (#2498), and the
dead-man switch (#2501) -- with zero console visibility. This surface adds the
read page: the fleet, each runner's liveness (``last_seen_at``), and the
dead-man state (``stale_at`` -> ``UNKNOWN``).

The route serves two response shapes from one handler (the checks / scheduler
mould):

* **Full page** (normal browser navigation) -- the ``runners/list.html`` page
  extending ``base.html``.
* **HTMX fragment** (``HX-Request: true``) -- the ``runners/_table_rows.html``
  partial. The full page arms an ``hx-trigger="every 30s"`` poll that
  re-fetches this route and swaps only the table body, so a runner going dark
  surfaces without a manual refresh.

Reads at ``operator`` role via the in-process
:class:`~meho_backplane.auth.runner_principals.RunnerPrincipalService` (the
same ``list_`` the Bearer ``GET /api/v1/runner-principals`` route uses) rather
than the REST surface, because a browser carrying only the BFF session cookie
cannot authenticate the Bearer route. The dead-man ``stale_at`` marker lives
on a different table (``runner_assignments``), read through
:func:`meho_backplane.gateway.repository.get_stale_markers` and joined on the
runner name at render. Tenant scoping is non-overrideable -- both reads key on
the session's ``tenant_id``; no query parameter carries a tenant id, so a
foreign tenant's runners never render. Read-only (no register / revoke
affordances -- ``meho runner-principal`` is the single write path, #2502).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from meho_backplane.auth.runner_principals import RunnerPrincipalService
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.gateway import repository
from meho_backplane.gateway.queue import GATEWAY_LONGPOLL_MAX_WAIT_SECONDS
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.runners.views import project_runner_to_row
from meho_backplane.ui.templating import get_templates

__all__ = ["build_list_router"]

#: Hard cap on the runners a single list render considers. The fleet page is a
#: glance surface; a tenant with more than this many runners has an operational
#: scale the list view is not the place to page through.
_LIST_LIMIT = 200

#: Module-level ``Depends`` closure -- ruff B008 idiom (no function calls in
#: default argument positions), matching the checks / scheduler routes.
_require_session_dep = Depends(require_ui_session)


def _is_htmx_request(request: Request) -> bool:
    """Return ``True`` when HTMX issued the request (``HX-Request: true``)."""
    return request.headers.get("hx-request", "").lower() == "true"


def _stale_threshold_seconds() -> int:
    """Central-clock staleness threshold shown as page context (``multiplier x unit``).

    Mirrors :func:`meho_backplane.gateway.deadman._threshold_seconds` (not
    imported -- it is private to the sweeper); a display-only caption, never a
    client-side recomputation of staleness.
    """
    return get_settings().gateway_runner_stale_after_multiplier * GATEWAY_LONGPOLL_MAX_WAIT_SECONDS


async def _render(request: Request, *, session_ctx: UISessionContext) -> HTMLResponse:
    """Render the fleet page or the table-rows fragment.

    Two reads, both tenant-scoped from the session: the runner principals
    (name / liveness / ``last_seen_at`` / revoked / created_at) and the
    ``runner_assignments.stale_at`` dead-man markers joined on the runner
    name. ``include_revoked=True`` so a decommissioned runner still renders
    (with its revoked badge) -- a fleet page shows the whole fleet.
    """
    tenant_id = session_ctx.tenant_id
    principals = await RunnerPrincipalService().list_(
        tenant_id, include_revoked=True, limit=_LIST_LIMIT
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stale_map = await repository.get_stale_markers(session, tenant_id=tenant_id)
    rows = [project_runner_to_row(p, stale_at=stale_map.get(p.name)) for p in principals]
    context: dict[str, object] = {
        "page_title": "Runners",
        "active_surface": "runners",
        "rows": rows,
        "stale_threshold_seconds": _stale_threshold_seconds(),
        # Shared "now" so the relative-time macro stays consistent across
        # rows within one render.
        "now_utc": datetime.now(UTC),
    }
    template_name = "runners/_table_rows.html" if _is_htmx_request(request) else "runners/list.html"
    return get_templates().TemplateResponse(request, template_name, context)


def build_list_router() -> APIRouter:
    """Construct the runners-fleet :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can construct
    parallel routers without sharing route state -- the chassis convention
    every surface router follows. Registers the single ``GET /ui/runners``
    route serving both the full page and the HTMX fragment from one handler.
    """
    router = APIRouter(tags=["ui-runners"])

    async def _handler(
        request: Request,
        session_ctx: UISessionContext = _require_session_dep,
    ) -> HTMLResponse:
        """Serve ``GET /ui/runners``. See module docstring for the contract."""
        return await _render(request, session_ctx=session_ctx)

    router.add_api_route(
        "/ui/runners",
        _handler,
        methods=["GET"],
        name="ui_runners_list",
        response_class=HTMLResponse,
    )
    return router
