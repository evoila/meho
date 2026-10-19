# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dashboard view: ``GET /ui/`` -- the authenticated landing page.

Initiative #337 (G10.0 Frontend chassis), Task #866 (T5). The dashboard
renders three components per the Initiative work-item #6:

* A 3x2 grid of DaisyUI ``card`` tiles linking to the five surface
  routes G10.1-G10.5 fill in. The sixth tile is a static
  "deploy details" card so the grid layout is balanced without a
  trailing empty slot.
* A recent-operations tray (#3758): the newest 50
  ``operation_execution`` audit rows of the tenant, read through the
  audit reader (:func:`~meho_backplane.audit_query.query_audit`) and
  rendered on the server. ``recent-ops-refresh.js`` re-fetches the tray
  from ``GET /ui/dashboard/recent-operations`` every 5 seconds while the
  page is visible, and the tray shows when it was last updated. It used
  to follow the Broadcast SSE stream; Broadcast now carries deliberate
  agent announcements only. The tray and its fragment need the
  ``operator`` role, the same minimum the audit API applies
  (:func:`~meho_backplane.ui.roles.is_ui_operator`); a ``read_only``
  session gets a notice instead of rows, and the fragment answers 403.
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

References
----------

* Chassis ``base.html`` reference (sidebar links, version footer):
  ``backend/src/meho_backplane/ui/templates/base.html``
* Audit reader: ``backend/src/meho_backplane/audit_query/query.py``
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from meho_backplane.audit_query import AuditQueryFilters, query_audit
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass
from meho_backplane.health import readiness_snapshot
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.roles import is_ui_operator
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


#: Rows in the recent-operations tray. The tray is a glance surface (~12
#: visible rows under its ``max-h-72`` scroll box); 50 gives some
#: scroll-back and keeps the refresh query small.
_RECENT_OPS_LIMIT: Final[int] = 50

#: Path of the tray's refresh fragment (#3758).
_RECENT_OPS_URL: Final[str] = "/ui/dashboard/recent-operations"

_require_ui_session_dep = Depends(require_ui_session)


async def audit_read_allowed(session: UISessionContext = _require_ui_session_dep) -> bool:
    """FastAPI dependency: may this session read audit rows on the console?

    True for ``operator`` and ``tenant_admin``; False for ``read_only``
    and for a role lift that cannot complete. Mirrors the audit API's
    minimum role so the tray never shows rows the API would refuse.
    """
    return await is_ui_operator(session)


_audit_read_allowed_dep = Depends(audit_read_allowed)


def _recent_ops_forbidden_context() -> dict[str, object]:
    """Template context for a session below the ``operator`` role.

    The tray renders a notice and carries no refresh URL, so
    ``recent-ops-refresh.js`` does not poll a fragment that would 403.
    """
    return {
        "recent_ops": [],
        "recent_ops_forbidden": True,
        "recent_ops_limit": _RECENT_OPS_LIMIT,
    }


async def _recent_ops_context(session: UISessionContext) -> dict[str, object]:
    """Read the tenant's newest operation rows through the audit reader.

    Same tenant scope as the audit console: ``tenant_id`` comes from the
    validated session and is a mandatory argument of
    :func:`~meho_backplane.audit_query.query_audit`, so another tenant's
    rows can never appear. Only ``operation_execution`` rows (#3733)
    count: an operation ran.
    """
    filters = AuditQueryFilters(
        event_class=AuditEventClass.OPERATION_EXECUTION,
        limit=_RECENT_OPS_LIMIT,
    )
    async with get_sessionmaker()() as db_session:
        result = await query_audit(filters, tenant_id=session.tenant_id, session=db_session)
    return {
        "recent_ops": result.rows,
        "recent_ops_forbidden": False,
        "recent_ops_url": _RECENT_OPS_URL,
        "recent_ops_limit": _RECENT_OPS_LIMIT,
        "recent_ops_updated_at": datetime.now(UTC).isoformat(),
    }


async def _render_recent_ops(
    request: Request,
    session: UISessionContext = _require_ui_session_dep,
    allowed: bool = _audit_read_allowed_dep,
) -> HTMLResponse:
    """Render ``GET /ui/dashboard/recent-operations`` -- the tray alone."""
    # A session below the ``operator`` role gets the notice tray at 403.
    if not allowed:
        return get_templates().TemplateResponse(
            request,
            "_dashboard_recent_ops.html",
            _recent_ops_forbidden_context(),
            status_code=403,
        )
    return get_templates().TemplateResponse(
        request, "_dashboard_recent_ops.html", await _recent_ops_context(session)
    )


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
    session: UISessionContext = _require_ui_session_dep,
    audit_allowed: bool = _audit_read_allowed_dep,
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
    recent_ops = (
        await _recent_ops_context(session) if audit_allowed else _recent_ops_forbidden_context()
    )
    context = {
        "page_title": "Dashboard",
        "ready": readiness["ready"],
        "readiness_checks": readiness["checks"],
        "surface_tiles": _SURFACE_TILES,
        "operator_sub": session.operator_sub,
        "tenant_id": str(session.tenant_id),
        "csrf_token": csrf_token,
        **recent_ops,
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
        _RECENT_OPS_URL,
        _render_recent_ops,
        methods=["GET"],
        name="ui_dashboard_recent_operations",
        response_class=HTMLResponse,
    )
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
