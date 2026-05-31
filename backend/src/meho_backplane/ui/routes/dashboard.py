# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dashboard view: ``GET /ui/`` -- the authenticated landing page.

Initiative #337 (G10.0 Frontend chassis), Task #866 (T5). The dashboard
renders three components per the Initiative work-item #6:

* A 3x2 grid of DaisyUI ``card`` tiles linking to the five surface
  routes G10.1-G10.5 fill in. The sixth tile is a static
  "deploy details" card so the grid layout is balanced without a
  trailing empty slot.
* A live recent-activity snippet wired to ``/api/v1/feed`` via the
  HTMX 2 SSE extension (``hx-ext="sse"`` + ``sse-connect="..."`` +
  ``sse-swap="broadcast"``). The feed endpoint validates the JWT via
  the Bearer header on ``/api/v1/feed``; the dashboard surface only
  renders the HTMX wiring -- the actual subscription happens
  browser-side once the page loads (and the operator's session cookie
  is the auth boundary that gates ``/ui/``). Trimming the rendered
  tray to the last N events is G10.1 (#338) client-side surface work;
  the underlying feed endpoint streams the live tail unbounded.
* A version + readiness card sourced from
  :data:`meho_backplane.__version__` (always rendered) and the chassis
  readiness probe registry (``meho_backplane.health.run_probes_async``)
  -- the same data ``/ready`` returns, surfaced as a single
  ready/starting pill.

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
* Per-tenant SSE feed endpoint (G6.1-T4, #310):
  ``backend/src/meho_backplane/api/v1/feed.py``
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from meho_backplane import __version__
from meho_backplane.health import run_probes_async
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.templating import get_templates

__all__ = ["build_dashboard_router"]


#: 3x2 surface-card grid descriptors. Each tile renders a DaisyUI
#: ``card`` with an icon, a title, a one-line summary, and an
#: ``href`` to the surface stub T5 also lands. The 6th tile is the
#: "deploy info" cell so the grid stays balanced; the surface
#: Initiatives (G10.1-G10.5) re-style this when their dashboards
#: ship richer per-surface widgets.
_SURFACE_TILES: Final[tuple[dict[str, str], ...]] = (
    {
        "title": "Broadcast",
        "summary": "Live activity feed across the tenant.",
        "href": "/ui/broadcast",
        "icon": "\U0001f4e1",  # satellite antenna
    },
    {
        "title": "Knowledge",
        "summary": "Search + browse the team's distilled knowledge base.",
        "href": "/ui/kb",
        "icon": "\U0001f4da",  # books
    },
    {
        "title": "Topology",
        "summary": "Targets, clusters, and dependencies.",
        "href": "/ui/topology",
        "icon": "\U0001f578",  # spider web
    },
    {
        "title": "Connectors",
        "summary": "Manage tenant targets + per-target credentials.",
        "href": "/ui/connectors",
        "icon": "\U0001f50c",  # electric plug
    },
    {
        "title": "Memory",
        "summary": "Operator + tenant + target memories across 5 scopes.",
        "href": "/ui/memory",
        "icon": "\U0001f9e0",  # brain
    },
    {
        "title": "Runbooks",
        "summary": "Browse runbook templates + lifecycle state.",
        "href": "/ui/runbooks",
        "icon": "\U0001f4d8",  # blue book
    },
)


async def _readiness_snapshot() -> dict[str, object]:
    """Project the chassis readiness-probe results into the dashboard shape.

    Returns ``{"ready": bool, "checks": [{"name": ..., "ok": ..., "detail": ...}, ...]}``.
    The shape mirrors the ``/ready`` endpoint payload so a future
    "expanded readiness card" surface Initiative reuses the same
    contract.
    """
    results = await run_probes_async()
    ready_ok = bool(results) and all(r.ok for r in results)
    return {
        "ready": ready_ok,
        "checks": [{"name": r.name, "ok": r.ok, "detail": r.detail or ""} for r in results],
    }


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
    csrf_token = mint_csrf_token(str(session.session_id))
    context = {
        "page_title": "Dashboard",
        "app_version": __version__,
        "ready": readiness["ready"],
        "readiness_checks": readiness["checks"],
        "surface_tiles": _SURFACE_TILES,
        "operator_sub": session.operator_sub,
        "tenant_id": str(session.tenant_id),
        "csrf_token": csrf_token,
        # Endpoint the HTMX SSE snippet subscribes to. Lifted out so a
        # future deploy can swap to a CDN-hosted edge proxy without
        # editing the template. ``/api/v1/feed`` is the canonical
        # tenant-scoped SSE stream from G6.1-T4 (#310); the route does
        # not accept a ``limit`` query parameter (FastAPI silently
        # ignores unknown query params, so a hardcoded ``?limit=5``
        # would be a no-op surface promise), so the dashboard subscribes
        # to the full live stream. Trimming the tray to the last N
        # events client-side is G10.1 (#338) surface work.
        "feed_endpoint": "/api/v1/feed",
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
