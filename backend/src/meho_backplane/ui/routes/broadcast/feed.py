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

Server-side vs client-side filters
==================================

The stream bridge (:mod:`broadcast.stream`) and the canonical
``/api/v1/feed`` accept exactly three filters -- ``op_class``,
``principal`` (exact-match on ``principal_sub``), ``target`` (exact-match
on ``target_name``). Those three ride into the ``sse-connect`` query
string so the *server* drops non-matching events before they reach the
browser. The fourth control, **op_id search**, is a free-text substring
match the stream does not expose; layering it onto the shared stream
filter would diverge the bridge from ``/api/v1/feed`` (out of scope).
It is therefore applied **client-side** by the ``broadcastFeed`` Alpine
controller against the already-streamed events -- the row count the
operator sees narrows live as they type, and the server stream stays
byte-compatible with the API feed.

op_class colour-coding
======================

The event-row badge colour is keyed on the event's ``op_class`` via
:data:`OP_CLASS_BADGE_CLASSES` -- a fixed map from the closed op-class
vocabulary (:func:`meho_backplane.broadcast.classify_op`) to DaisyUI
badge variants, serialised into the page as JSON the Alpine row-builder
reads. :data:`OP_CLASS_FILTER_OPTIONS` is the same vocabulary surfaced
as the filter dropdown's options (``All`` plus the six classes).

Tenant scoping
==============

The target dropdown is populated from the ``targets`` table scoped to
the session's ``tenant_id`` (never a query parameter), so a tenant-A
operator can only filter by tenant-A targets. The live events arrive
over the tenant-scoped stream bridge whose key is
``meho:feed:{session.tenant_id}``; there is no tenant query parameter on
either route, so a tenant-A operator's page can never surface tenant-B
events.
"""

from __future__ import annotations

import json
from typing import Final
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_raw_session
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.templating import get_templates

__all__ = [
    "IN_DOM_ROW_CAP",
    "OP_CLASS_BADGE_CLASSES",
    "OP_CLASS_FILTER_OPTIONS",
    "build_feed_router",
]

#: Hard cap on the number of event rows kept in the DOM at once (work
#: item #9). New events prepend; an Alpine watcher trims the oldest rows
#: past this count so a sustained event stream keeps page memory
#: bounded. Sized to comfortably cover an operator's scroll-back without
#: letting an all-day wall-monitor session grow the DOM unboundedly.
IN_DOM_ROW_CAP: Final[int] = 1000

#: Map from the closed ``op_class`` vocabulary
#: (:func:`meho_backplane.broadcast.classify_op`) to DaisyUI badge
#: variant classes. ``credential_read`` / ``credential_mint`` /
#: ``audit_query`` -- the sensitive, aggregate-only classes per decision
#: #3 -- get the warning palette so an operator scanning the feed reads
#: the sensitivity at a glance; ``write`` is accent (mutation), ``read``
#: is the neutral ghost, ``other`` falls back to ghost too. A class not
#: in this map falls back to ``badge-ghost`` in the row builder.
OP_CLASS_BADGE_CLASSES: Final[dict[str, str]] = {
    "read": "badge-ghost",
    "write": "badge-accent",
    "credential_read": "badge-warning",
    "credential_mint": "badge-warning",
    "audit_query": "badge-info",
    "other": "badge-ghost",
}

#: The op_class filter dropdown options (work item #3). The empty string
#: is the "All" sentinel -- it maps to *no* ``op_class`` query parameter
#: on the stream so every class streams. The rest are the closed
#: vocabulary keys of :data:`OP_CLASS_BADGE_CLASSES`, surfaced in the
#: order the Initiative #338 work-item #3 lists them (read / write /
#: credential_read / audit_query / other) plus ``credential_mint``,
#: which shares the same sensitive (aggregate-only) treatment and would
#: otherwise be unfilterable.
OP_CLASS_FILTER_OPTIONS: Final[tuple[str, ...]] = (
    "read",
    "write",
    "credential_read",
    "credential_mint",
    "audit_query",
    "other",
)

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


def _stream_url(*, op_class: str | None, principal: str | None, target: str | None) -> str:
    """Build the ``sse-connect`` URL carrying the active server-side filters.

    Only the three stream-supported filters (``op_class`` / ``principal``
    / ``target``) ride into the query string; the op_id search is
    client-side (see the module docstring). Empty / ``None`` filters are
    omitted so the "All" selection streams everything. ``urlencode``
    percent-encodes operator-supplied values (a principal sub or target
    name can contain ``&`` / ``=`` / spaces) so the query string can
    never be corrupted or injected.
    """
    params = {
        key: value
        for key, value in (
            ("op_class", op_class),
            ("principal", principal),
            ("target", target),
        )
        if value
    }
    if not params:
        return _STREAM_ENDPOINT
    return f"{_STREAM_ENDPOINT}?{urlencode(params)}"


async def _target_names(db_session: AsyncSession, tenant_id: object) -> list[str]:
    """Return the tenant's target names for the filter dropdown.

    Scoped to ``tenant_id`` at the SQL ``WHERE`` clause (never a query
    parameter), ordered by name for stable rendering, capped at
    :data:`_TARGET_OPTIONS_LIMIT`. The dropdown's values are matched
    against :attr:`BroadcastEvent.target_name` by the stream filter, so
    the option set is the target *names* -- the same string the
    publisher stamps onto each event.
    """
    stmt = (
        select(TargetORM.name)
        .where(TargetORM.tenant_id == tenant_id)
        .order_by(TargetORM.name)
        .limit(_TARGET_OPTIONS_LIMIT)
    )
    result = await db_session.execute(stmt)
    return list(result.scalars().all())


def _feed_context(
    *,
    target_names: list[str],
    op_class: str,
    principal: str,
    target: str,
    op_id: str,
) -> dict[str, object]:
    """Build the context shared by the full page and the feed fragment.

    The same shape feeds ``broadcast/feed.html`` and the standalone
    ``broadcast/_feed.html`` fragment so the filter re-render and the
    initial page load render an identical feed. The four filter values
    are echoed back so the swapped fragment's filter bar keeps the
    operator's selection and the embedded ``sse-connect`` URL carries
    the three server-side filters.
    """
    # Normalise the "All" sentinel: an empty op_class means no server
    # filter (stream everything). Same for blank principal / target.
    server_op_class = op_class or None
    server_principal = principal or None
    server_target = target or None
    return {
        "stream_endpoint": _stream_url(
            op_class=server_op_class,
            principal=server_principal,
            target=server_target,
        ),
        "in_dom_row_cap": IN_DOM_ROW_CAP,
        # Serialised once server-side so the Alpine row-builder reads a
        # single authoritative colour table rather than duplicating the
        # mapping in JS. ``json.dumps`` output is HTML-safe inside the
        # ``application/json`` script block the template renders it into.
        "op_class_badge_json": json.dumps(OP_CLASS_BADGE_CLASSES),
        "op_class_options": OP_CLASS_FILTER_OPTIONS,
        "target_options": target_names,
        # Echoed filter selections -- keep the <select>/<input> values
        # after an HTMX swap.
        "op_class_filter": op_class,
        "principal_filter": principal,
        "target_filter": target,
        "op_id_filter": op_id,
    }


async def _render_page(
    request: Request,
    *,
    op_class: str,
    principal: str,
    target: str,
    op_id: str,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
) -> HTMLResponse:
    """Render ``GET /ui/broadcast`` -- the full page.

    The page subscribes to ``/ui/broadcast/stream`` (the session-gated
    SSE bridge), so the operator sees their tenant's live events with no
    further auth round-trip. The CSRF cookie is set + echoed via the
    template's ``hx-headers`` so the filter bar's ``hx-get`` (and any
    later state-changing HTMX request from this surface) passes the
    double-submit check -- mirroring the dashboard + topology surfaces.
    """
    target_names = await _target_names(db_session, session_ctx.tenant_id)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        "page_title": "Broadcast",
        "active_surface": "broadcast",
        "operator_sub": session_ctx.operator_sub,
        "tenant_id": str(session_ctx.tenant_id),
        "csrf_token": csrf_token,
        # ``base.html``'s footer reads ``ready`` to colour the readiness
        # pill; the broadcast surface does not poll readiness (the
        # dashboard owns that), so ship ``False`` so ``StrictUndefined``
        # does not raise on the read.
        "ready": False,
        **_feed_context(
            target_names=target_names,
            op_class=op_class,
            principal=principal,
            target=target,
            op_id=op_id,
        ),
    }
    response = get_templates().TemplateResponse(request, "broadcast/feed.html", context)
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
    op_class: str,
    principal: str,
    target: str,
    op_id: str,
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
        op_class=op_class,
        principal=principal,
        target=target,
        op_id=op_id,
    )
    return get_templates().TemplateResponse(request, "broadcast/_feed.html", context)


#: Module-level :class:`fastapi.Depends` closures -- ruff B008 guard
#: (a function call in a default argument position is disallowed except
#: for the FastAPI-blessed call sites in ``extend-immutable-calls``).
_require_ui_session_dep = Depends(require_ui_session)
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
        op_class: str = Query(default="", max_length=64),
        principal: str = Query(default="", max_length=256),
        target: str = Query(default="", max_length=256),
        op_id: str = Query(default="", max_length=256),
        session_ctx: UISessionContext = _require_ui_session_dep,
        db_session: AsyncSession = _get_raw_session_dep,
    ) -> HTMLResponse:
        """``GET /ui/broadcast[?op_class=&principal=&target=&op_id=]``.

        Filters are accepted on the full-page route too so a copy-pasted
        filtered URL reproduces the operator's view (the filter bar sets
        ``hx-push-url`` on the fragment route, mirroring topology).
        """
        return await _render_page(
            request,
            op_class=op_class,
            principal=principal,
            target=target,
            op_id=op_id,
            session_ctx=session_ctx,
            db_session=db_session,
        )

    async def _fragment_handler(
        request: Request,
        op_class: str = Query(default="", max_length=64),
        principal: str = Query(default="", max_length=256),
        target: str = Query(default="", max_length=256),
        op_id: str = Query(default="", max_length=256),
        session_ctx: UISessionContext = _require_ui_session_dep,
        db_session: AsyncSession = _get_raw_session_dep,
    ) -> HTMLResponse:
        """``GET /ui/broadcast/feed[?op_class=&principal=&target=&op_id=]``.

        The filter-bar submit target. Returns the feed fragment with the
        filtered ``sse-connect`` URL embedded.
        """
        return await _render_fragment(
            request,
            op_class=op_class,
            principal=principal,
            target=target,
            op_id=op_id,
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
