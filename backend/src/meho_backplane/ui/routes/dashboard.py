# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dashboard view: ``GET /ui/`` -- the authenticated landing page.

Initiative #337 (G10.0 Frontend chassis), Task #866 (T5). The dashboard
renders three components per the Initiative work-item #6:

* A 3x2 grid of DaisyUI ``card`` tiles linking to the five surface
  routes G10.1-G10.5 fill in. The sixth tile is a static
  "deploy details" card so the grid layout is balanced without a
  trailing empty slot.
* A live recent-activity snippet wired to the session-gated SSE
  bridge ``/ui/broadcast/stream`` (G0.25 #1696). The chassis
  originally pointed ``sse-connect`` at the Bearer-authenticated
  ``/api/v1/feed``, but the browser's ``EventSource`` cannot send an
  ``Authorization`` header (the WHATWG constructor exposes only
  ``withCredentials``), so that wiring 401-looped and the tray never
  left its "Connecting..." placeholder. The bridge lives under
  ``/ui/`` where the operator's session cookie -- the same boundary
  that gated this page render -- authenticates the stream. Frames are
  consumed through the hidden-sink + Alpine pattern the broadcast and
  connectors surfaces established (``dashboardFeedTray`` in
  ``static/src/app/dashboard-feed.js``): the controller cancels the
  extension's raw swap and renders each ``BroadcastEvent`` JSON frame
  through ``x-text`` bindings, which keeps markup-bearing event
  fields inert instead of letting the swap parse them into live DOM.
* A version + readiness card sourced from the deployed-build label the
  chassis Jinja env binds as the ``app_version`` global -- the same
  ``CHART_VERSION`` / ``GIT_SHA`` env metadata ``GET /version`` reads
  (#1698; the handler must NOT pass its own ``app_version`` context
  key: render context shadows env globals, which is exactly how the
  static package ``__version__`` used to leak into this page) -- and
  the chassis readiness probe registry
  (``meho_backplane.health.run_probes_async``) -- the same data
  ``/ready`` returns, surfaced as a single ready/starting pill.

The handler also sets the ``meho_csrf`` cookie on the response. Per
the OWASP signed double-submit cookie pattern, the cookie is
JS-readable (no ``HttpOnly``) so the HTMX ``hx-headers`` directive on
the page can echo it back to the server on subsequent state-changing
requests. The cookie's ``SameSite=Strict`` + ``Secure`` attributes
mirror the session cookie's posture.

Why one combined handler instead of split fragments
---------------------------------------------------

The Initiative explicitly scopes "dashboard view" to a single page; the
HTMX SSE snippet swaps fragments into the same page rather than
navigating to a sub-route. A future surface Initiative may add HTMX
``hx-get`` partials (e.g. tenant selector dropdown), but those are
G10.1+ scope. v0.2.0 ships the dashboard as one Jinja template.

References
----------

* HTMX 2 SSE extension -- ``sse-connect`` / ``sse-swap``:
  https://htmx.org/extensions/sse/
* Chassis ``base.html`` reference (sidebar links, version footer):
  ``backend/src/meho_backplane/ui/templates/base.html``
* Session-gated SSE bridge (G10.1-T1, #867) the tray subscribes to:
  ``backend/src/meho_backplane/ui/routes/broadcast/stream.py``
* Canonical per-tenant SSE feed the bridge stays byte-compatible
  with (G6.1-T4, #310):
  ``backend/src/meho_backplane/api/v1/feed.py``
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from meho_backplane.health import readiness_snapshot
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.templating import get_templates

__all__ = ["build_dashboard_router"]


#: Surface-card grid descriptors. Each tile renders a branded card
#: with an icon, a title, a one-line summary, and an ``href`` to the
#: surface stub T5 also lands. ``icon`` is a name resolved by the
#: ``icon()`` macro in ``templates/_icons.html`` (inline Lucide SVG) —
#: the rebrand replaced the chassis emoji. The surface Initiatives
#: (G10.1-G10.5) re-style this when their dashboards ship richer
#: per-surface widgets.
_SURFACE_TILES: Final[tuple[dict[str, str], ...]] = (
    {
        "title": "Broadcast",
        "summary": "Live activity feed across the tenant.",
        "href": "/ui/broadcast",
        "icon": "radio-tower",
    },
    {
        "title": "Knowledge",
        "summary": "Search + browse the team's distilled knowledge base.",
        "href": "/ui/kb",
        "icon": "book-open",
    },
    {
        "title": "Docs Corpus",
        "summary": "Ask the attached vendor-document corpus; read cited chunks.",
        "href": "/ui/corpus",
        "icon": "library",
    },
    {
        "title": "Retrieval",
        "summary": "Diagnose retrieval: per-signal RRF score & rank breakdown.",
        "href": "/ui/retrieval",
        "icon": "search",
    },
    {
        "title": "Topology",
        "summary": "Targets, clusters, and dependencies.",
        "href": "/ui/topology",
        "icon": "waypoints",
    },
    {
        "title": "Connectors",
        "summary": "Manage tenant targets + per-target credentials.",
        "href": "/ui/connectors",
        "icon": "plug",
    },
    {
        "title": "Memory",
        "summary": "Operator + tenant + target memories across 5 scopes.",
        "href": "/ui/memory",
        "icon": "brain",
    },
    {
        "title": "Agents",
        "summary": "Define + manage the tenant's LLM agents.",
        "href": "/ui/agents",
        "icon": "bot",
    },
    {
        "title": "Runbooks",
        "summary": "Browse runbook templates + lifecycle state.",
        "href": "/ui/runbooks",
        "icon": "scroll",
    },
    {
        "title": "Approvals",
        "summary": "Review + approve or deny pending agent actions.",
        "href": "/ui/approvals",
        "icon": "bell",
    },
)


#: The session-gated SSE bridge the recent-activity tray subscribes to
#: (G0.25 #1696). NOT ``/api/v1/feed`` -- the browser ``EventSource``
#: cannot send the Bearer header that endpoint requires, so the
#: original wiring 401-looped forever; see the rationale in
#: :mod:`meho_backplane.ui.routes.broadcast.stream` and the matching
#: ``_STREAM_ENDPOINT`` constant in
#: :mod:`meho_backplane.ui.routes.broadcast.feed`. The dashboard
#: subscribes to the unfiltered live tail (no query parameters) --
#: filtering is the broadcast surface's affordance.
_FEED_STREAM_ENDPOINT: Final[str] = "/ui/broadcast/stream"

#: Hard cap on the number of event rows the tray keeps in the DOM at
#: once, passed to the ``dashboardFeedTray`` Alpine controller. The
#: tray is a glance surface (~12 visible rows under its ``max-h-72``
#: scroll box), so the connectors recent-ops default (50) is the right
#: order of magnitude -- enough scroll-back to be useful, bounded so an
#: all-day dashboard tab can't grow the DOM unboundedly. The richer
#: bounded-tray UX (row counters, trim affordances) is G10.1 (#338)
#: surface work per #1696's out-of-scope.
_FEED_TRAY_DOM_CAP: Final[int] = 50


async def _readiness_snapshot() -> dict[str, object]:
    """Project the chassis readiness-probe results into the dashboard shape.

    Returns ``{"ready": bool, "checks": [{"name": ..., "ok": ..., "detail": ...}, ...]}``.
    The shape mirrors the ``/ready`` endpoint payload so the dashboard's
    detailed readiness card and the chassis footer pill read one
    contract.

    Delegates to :func:`~meho_backplane.health.readiness_snapshot` with
    ``max_age_s=0`` so the dashboard always runs a *fresh* probe sweep
    (its pre-#1776 behaviour), rather than reading the short-TTL cache
    the other surfaces share via the session middleware. The dashboard
    owns the live readiness card, so freshness is the right trade here.
    """
    return await readiness_snapshot(max_age_s=0)


async def _render_dashboard(
    request: Request,
    session: UISessionContext = Depends(require_ui_session),
) -> HTMLResponse:
    """Render ``GET /ui/`` for the authenticated operator.

    Per acceptance criterion 1 on #866: returns 200 with
    ``<title>MEHO Operator Console`` plus the 3x2 grid + sidebar
    links to all five surfaces (the sidebar is inherited from
    ``base.html``).

    The handler also mints + sets the CSRF cookie so any subsequent
    HTMX ``hx-post`` / ``hx-delete`` from this page passes the
    double-submit check.
    """
    readiness = await _readiness_snapshot()
    # The chassis context processor injects ``ready`` into every render
    # from ``request.state.ui_ready`` (the middleware's short-TTL-cached
    # verdict) and -- because Starlette runs context processors after
    # the route context -- it wins over the ``ready`` in ``context``
    # below. Write the dashboard's own *fresh* verdict back to
    # ``request.state.ui_ready`` so the processor re-injects that exact
    # value: the footer pill and the dashboard's readiness card stay in
    # lock-step, and the dashboard's behaviour is unchanged (#1776).
    request.state.ui_ready = bool(readiness["ready"])
    csrf_token = mint_csrf_token(str(session.session_id))
    context = {
        "page_title": "Dashboard",
        "ready": readiness["ready"],
        "readiness_checks": readiness["checks"],
        "surface_tiles": _SURFACE_TILES,
        "operator_sub": session.operator_sub,
        "tenant_id": str(session.tenant_id),
        "csrf_token": csrf_token,
        # Endpoint the HTMX SSE sink subscribes to. Lifted out so a
        # future deploy can swap to a CDN-hosted edge proxy without
        # editing the template. The session-gated bridge streams the
        # tenant's live tail scoped by the validated session -- no
        # tenant or limit query parameters exist on the route, so the
        # tray subscribes bare and bounds itself client-side via
        # ``feed_tray_cap``.
        "feed_endpoint": _FEED_STREAM_ENDPOINT,
        "feed_tray_cap": _FEED_TRAY_DOM_CAP,
    }
    response = get_templates().TemplateResponse(request, "dashboard.html", context)
    # Mirror the SameSite + Secure posture of the session cookie. The
    # CSRF cookie is intentionally NOT HttpOnly -- HTMX needs to read
    # it to populate ``X-CSRF-Token`` on outbound state-changing
    # requests; the HMAC binding to the session_id defeats the
    # cookie-injection attack JS-read would otherwise enable.
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )
    return response


def build_dashboard_router() -> APIRouter:
    """Construct the dashboard :class:`APIRouter`.

    Factory function (not module-level constant) so the umbrella
    :func:`meho_backplane.ui.routes.build_router` can mount a fresh
    instance per app under test without sharing route state.
    """
    router = APIRouter(tags=["ui"])
    router.add_api_route(
        "/ui/",
        _render_dashboard,
        methods=["GET"],
        name="ui_dashboard",
        # Returning HTML rather than JSON; reflect that in the
        # OpenAPI surface (the dashboard route is not API-documented,
        # but the response class hints aid tooling that walks the
        # routing tree).
        response_class=HTMLResponse,
    )
    return router
