# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/broadcast`` + ``GET /ui/broadcast/feed`` -- the live activity feed.

Initiative #338 (G10.1 Activity broadcast UI). Task #867 (G10.1-T1)
shipped the full-page live feed (work items #1, #2, #8, #9); Task #868
(G10.1-T2) adds the **filter bar** that re-renders the feed fragment
with the active filter baked into a new SSE subscription URL (work item
#3).

Two routes, one feed
====================

* ``GET /ui/broadcast`` -- the full page (``broadcast/feed.html``,
  extends ``base.html``). Renders the chrome, the filter bar, the
  drawer slot, and the feed fragment (``broadcast/_feed.html``) inline.
* ``GET /ui/broadcast/feed`` -- the filter-submit target. Returns the
  **fragment only** (``broadcast/_feed.html``): the SSE sink wired to
  ``/ui/broadcast/stream?<filters>`` plus the feed list. The filter bar
  ``hx-get``s here with ``hx-target="#broadcast-feed"
  hx-swap="outerHTML"``; the swapped-in fragment carries a fresh
  ``sse-connect`` URL so the HTMX ``sse`` extension (which auto-processes
  swapped content) tears down the old subscription and opens the
  filtered one.

What it shows (#3762)
=====================

Only announcements that trusted agents publish on purpose (#3736,
#3758). People read them here under their tenant role; the page has no
way to publish. Operations are not on Broadcast: the audit log is their
record. So the page has no operation filters (no op class, no op id).

Filters
=======

Two server-side filters ride into the ``sse-connect`` query string:
``principal`` (exact match on ``principal_sub``) and ``target`` (exact
match on an announcement's ``target`` or any of its ``targets``). The
server drops non-matching announcements before they reach the browser.

Tenant scoping
==============

The target dropdown is populated from the ``targets`` table scoped to
the session's ``tenant_id`` (never a query parameter), so a tenant-A
operator can only filter by tenant-A targets. The live announcements
arrive over the tenant-scoped stream bridge, which reads the session
tenant's archive; there is no tenant query parameter on either route,
so a tenant-A operator's page can never surface tenant-B announcements.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_raw_session
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_operator
from meho_backplane.ui.auth.tenant import PAGE_TENANT_FORM_FIELD
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.templating import get_templates

__all__ = [
    "IN_DOM_ROW_CAP",
    "build_feed_router",
]

#: Hard cap on the number of event rows kept in the DOM at once (work
#: item #9). New events prepend; an Alpine watcher trims the oldest rows
#: past this count so a sustained event stream keeps page memory
#: bounded. Sized to comfortably cover an operator's scroll-back without
#: letting an all-day wall-monitor session grow the DOM unboundedly.
IN_DOM_ROW_CAP: Final[int] = 1000

#: Max targets surfaced in the filter dropdown. The dropdown is an
#: eyeball-scan filter, not a paginated browser (operators with denser
#: target registries reach for the topology surface). Bounds the
#: per-request SELECT so a tenant with thousands of targets does not
#: balloon the page.
_TARGET_OPTIONS_LIMIT: Final[int] = 500

#: The session-gated SSE bridge the live feed subscribes to. NOT
#: ``/api/v1/feed`` -- see the stream module's docstring for the
#: EventSource-cannot-set-Authorization rationale.
_STREAM_ENDPOINT: Final[str] = "/ui/broadcast/stream"


def _stream_url(*, principal: str | None, target: str | None, page_tenant: str) -> str:
    """Build the ``sse-connect`` URL carrying the active server-side filters.

    The two filters (``principal`` / ``target``) ride into the query
    string. Empty / ``None`` filters are omitted so the "All" selection
    streams everything. ``urlencode``
    percent-encodes operator-supplied values (a principal sub or target
    name can contain ``&`` / ``=`` / spaces) so the query string can
    never be corrupted or injected.

    *page_tenant* always rides along as ``meho_tenant`` (#5240): the
    browser's ``EventSource`` can send no header, so the page's tenant
    marker goes in the URL. The form-protection middleware refuses the
    stream with ``409`` when the session works in another tenant by then
    (a switch in another browser tab), on the first connect and on every
    reconnect. The stream route itself never reads the parameter.
    """
    params = {
        key: value
        for key, value in (
            ("principal", principal),
            ("target", target),
        )
        if value
    }
    params[PAGE_TENANT_FORM_FIELD] = page_tenant
    return f"{_STREAM_ENDPOINT}?{urlencode(params)}"


async def _target_names(db_session: AsyncSession, tenant_id: object) -> list[str]:
    """Return the tenant's live target names for the filter dropdown.

    Scoped to ``tenant_id`` at the SQL ``WHERE`` clause (never a query
    parameter), ordered by name for stable rendering, capped at
    :data:`_TARGET_OPTIONS_LIMIT`. The dropdown's values are matched
    against an announcement's ``target`` / ``targets`` by the stream
    filter, so the option set is the target *names*.

    Soft-deleted targets (``deleted_at IS NOT NULL``, G0.14-T4 #1145)
    are excluded so the dropdown stays in parity with
    ``GET /api/v1/targets`` and the MCP ``list_targets`` tool. Without
    this, a tombstoned target name would surface in the dropdown and
    selecting it would produce an empty filtered feed with no UI
    explanation.
    """
    stmt = (
        select(TargetORM.name)
        .where(
            TargetORM.tenant_id == tenant_id,
            TargetORM.deleted_at.is_(None),
        )
        .order_by(TargetORM.name)
        .limit(_TARGET_OPTIONS_LIMIT)
    )
    result = await db_session.execute(stmt)
    return list(result.scalars().all())


def _feed_context(
    *,
    target_names: list[str],
    principal: str,
    target: str,
    page_tenant: str,
) -> dict[str, object]:
    """Build the context shared by the full page and the feed fragment.

    The same shape feeds ``broadcast/feed.html`` and the standalone
    ``broadcast/_feed.html`` fragment so the filter re-render and the
    initial page load render an identical feed. The two filter values
    are echoed back so the swapped fragment's filter bar keeps the
    operator's selection and the embedded ``sse-connect`` URL carries
    them.
    """
    return {
        "stream_endpoint": _stream_url(
            principal=principal or None,
            target=target or None,
            page_tenant=page_tenant,
        ),
        "in_dom_row_cap": IN_DOM_ROW_CAP,
        "target_options": target_names,
        # Echoed filter selections -- keep the <select>/<input> values
        # after an HTMX swap.
        "principal_filter": principal,
        "target_filter": target,
    }


async def _render_page(
    request: Request,
    *,
    principal: str,
    target: str,
    wall: bool,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
) -> HTMLResponse:
    """Render ``GET /ui/broadcast`` -- the full page (or the wall view).

    The page subscribes to ``/ui/broadcast/stream`` (the session-gated
    SSE bridge), so the operator sees their tenant's live events with no
    further auth round-trip. The CSRF cookie is set + echoed via the
    template's ``hx-headers`` so the filter bar's ``hx-get`` (and any
    later state-changing HTMX request from this surface) passes the
    double-submit check -- mirroring the dashboard + topology surfaces.

    ``wall`` selects the wall-monitor template (work item #5): a minimal
    no-chrome layout (no sidebar, no top bar, no filter bar) that
    maximises the feed for a full-screen team-room monitor. It reuses
    the **same** ``broadcast/_feed.html`` fragment -- so the same SSE
    wiring, the same ``EventSource`` reconnect / ``Last-Event-Id``
    replay durability, the same 1000-row cap -- and only swaps the
    surrounding chrome. The wall view subscribes to the live tail with
    no server-side filters (a wall monitor shows everything the tenant
    produces); the filter bar is the in-chrome surface's affordance.
    """
    target_names = await _target_names(db_session, session_ctx.tenant_id)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        "page_title": "Broadcast",
        "active_surface": "broadcast",
        "operator_sub": session_ctx.operator_sub,
        "tenant_id": str(session_ctx.tenant_id),
        "csrf_token": csrf_token,
        **_feed_context(
            target_names=target_names,
            principal=principal,
            target=target,
            page_tenant=str(session_ctx.tenant_id),
        ),
    }
    template = "broadcast/wall.html" if wall else "broadcast/feed.html"
    response = get_templates().TemplateResponse(request, template, context)
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )
    return response


async def _render_fragment(
    request: Request,
    *,
    principal: str,
    target: str,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
) -> HTMLResponse:
    """Render ``GET /ui/broadcast/feed`` -- the filtered feed fragment.

    Returns ``broadcast/_feed.html`` only (no chrome): the SSE sink
    rewired to the filtered ``sse-connect`` URL plus the feed list. The
    filter bar swaps this fragment into ``#broadcast-feed`` via
    ``hx-swap="outerHTML"``; HTMX auto-processes the swapped content so
    the ``sse`` extension closes the prior subscription (the replaced
    node) and opens the new filtered one.
    """
    target_names = await _target_names(db_session, session_ctx.tenant_id)
    context = _feed_context(
        target_names=target_names,
        principal=principal,
        target=target,
        page_tenant=str(session_ctx.tenant_id),
    )
    return get_templates().TemplateResponse(request, "broadcast/_feed.html", context)


#: Module-level :class:`fastapi.Depends` closures -- ruff B008 guard
#: (a function call in a default argument position is disallowed except
#: for the FastAPI-blessed call sites in ``extend-immutable-calls``).
#: The ``operator`` floor of the REST broadcast and feed reads: a ``read_only``
#: session gets ``403 operator_required`` before the handler runs.
_require_ui_operator_dep = Depends(require_ui_operator)
_get_raw_session_dep = Depends(get_raw_session)


def build_feed_router() -> APIRouter:
    """Construct the broadcast feed :class:`APIRouter`.

    Registers ``GET /ui/broadcast`` (full page) and
    ``GET /ui/broadcast/feed`` (filtered fragment). Factory function
    (not a module-level constant) so a test app can construct parallel
    routers without sharing route state -- mirrors the chassis
    convention in :mod:`meho_backplane.ui.routes.topology`.
    """
    router = APIRouter(tags=["ui-broadcast"])

    async def _page_handler(
        request: Request,
        principal: str = Query(default="", max_length=256),
        target: str = Query(default="", max_length=256),
        wall: bool = Query(
            default=False,
            description=(
                "Render the minimal full-screen wall-monitor layout "
                "(no sidebar / top bar / filter bar)."
            ),
        ),
        session_ctx: UISessionContext = _require_ui_operator_dep,
        db_session: AsyncSession = _get_raw_session_dep,
    ) -> HTMLResponse:
        """``GET /ui/broadcast[?principal=&target=&wall=1]``.

        Filters are accepted on the full-page route too so a copy-pasted
        filtered URL reproduces the operator's view (the filter bar sets
        ``hx-push-url`` on the fragment route, mirroring topology).
        ``wall=1`` selects the no-chrome wall-monitor layout (work item
        #5).
        """
        return await _render_page(
            request,
            principal=principal,
            target=target,
            wall=wall,
            session_ctx=session_ctx,
            db_session=db_session,
        )

    async def _fragment_handler(
        request: Request,
        principal: str = Query(default="", max_length=256),
        target: str = Query(default="", max_length=256),
        session_ctx: UISessionContext = _require_ui_operator_dep,
        db_session: AsyncSession = _get_raw_session_dep,
    ) -> HTMLResponse:
        """``GET /ui/broadcast/feed[?principal=&target=]``.

        The filter-bar submit target. Returns the feed fragment with the
        filtered ``sse-connect`` URL embedded.
        """
        return await _render_fragment(
            request,
            principal=principal,
            target=target,
            session_ctx=session_ctx,
            db_session=db_session,
        )

    router.add_api_route(
        "/ui/broadcast",
        _page_handler,
        methods=["GET"],
        name="ui_broadcast_feed",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/broadcast/feed",
        _fragment_handler,
        methods=["GET"],
        name="ui_broadcast_feed_fragment",
        response_class=HTMLResponse,
    )
    return router
