# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/broadcast`` -- the live activity feed view.

Initiative #338 (G10.1 Activity broadcast UI), Task #867 (G10.1-T1)
work items #1, #2, #8, #9. Renders the full broadcast surface page that
streams the operator's tenant feed in reverse-chronological order.

What this route renders
=======================

A single full-page template (``broadcast/feed.html``, extends
``base.html``) containing:

* The HTMX ``sse``-extension wrapper subscribing to the session-gated
  ``/ui/broadcast/stream`` bridge (see
  :mod:`~meho_backplane.ui.routes.broadcast.stream` for why the UI does
  not subscribe to ``/api/v1/feed`` directly -- the browser
  ``EventSource`` cannot send the Bearer header that route requires).
* The empty state shown before any event arrives / when none match.
* A server-rendered ``<template>`` carrying the Jinja2 event-row markup
  the client clones per incoming event (work item #2). The markup, the
  DaisyUI badge palette, and the column structure are all authored
  server-side in :mod:`broadcast/_event_row.html`; only the per-event
  data binding happens client-side, because the shared ``/api/v1/feed``
  substrate streams JSON (consumed identically by ``meho status
  --watch`` and the MCP resource) and is out of scope to reshape into
  HTML frames.
* An Alpine.js controller that prepends each event and trims the in-DOM
  list to :data:`IN_DOM_ROW_CAP` rows (work item #9).

op_class colour-coding
======================

The event-row badge colour is keyed on the event's ``op_class`` via
:data:`OP_CLASS_BADGE_CLASSES` -- a fixed map from the closed op-class
vocabulary (:func:`meho_backplane.broadcast.classify_op`) to DaisyUI
badge variants. The map is rendered into the page as a JSON object the
Alpine row-builder reads, so the colour decision is authored server-side
(one auditable table) even though the row is materialised client-side.

Tenant scoping
==============

The page itself carries no tenant data beyond the operator's own
identity; the live events arrive over the tenant-scoped stream bridge.
There is no tenant query parameter on this route or on the stream it
subscribes to, so a tenant-A operator's page can never surface tenant-B
events -- the boundary is the session's ``tenant_id``.
"""

from __future__ import annotations

import json
from typing import Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.templating import get_templates

__all__ = [
    "IN_DOM_ROW_CAP",
    "OP_CLASS_BADGE_CLASSES",
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


async def _render_feed(
    request: Request,
    session: UISessionContext = Depends(require_ui_session),
) -> HTMLResponse:
    """Render ``GET /ui/broadcast`` for the authenticated operator.

    The page subscribes to ``/ui/broadcast/stream`` (the session-gated
    SSE bridge), so the operator sees their tenant's live events with
    no further auth round-trip. The CSRF cookie is set + echoed via the
    template's ``hx-headers`` so any future state-changing HTMX request
    from this surface (T2's filters submit via ``hx-get``, which is
    safe, but the chain is in place for any later mutation) passes the
    double-submit check -- mirroring the dashboard + topology surfaces.
    """
    csrf_token = mint_csrf_token(str(session.session_id))
    context = {
        "page_title": "Broadcast",
        "active_surface": "broadcast",
        "operator_sub": session.operator_sub,
        "tenant_id": str(session.tenant_id),
        "csrf_token": csrf_token,
        # The session-gated SSE bridge the live feed subscribes to.
        # NOT ``/api/v1/feed`` -- see the stream module's docstring for
        # the EventSource-cannot-set-Authorization rationale.
        "stream_endpoint": "/ui/broadcast/stream",
        "in_dom_row_cap": IN_DOM_ROW_CAP,
        # Serialised once server-side so the Alpine row-builder reads a
        # single authoritative colour table rather than duplicating the
        # mapping in JS. ``json.dumps`` output is HTML-safe inside the
        # ``application/json`` script block the template renders it into.
        "op_class_badge_json": json.dumps(OP_CLASS_BADGE_CLASSES),
        # ``base.html``'s footer reads ``ready`` to colour the readiness
        # pill; the broadcast surface does not poll readiness (the
        # dashboard owns that), so ship ``False`` so ``StrictUndefined``
        # does not raise on the read.
        "ready": False,
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


def build_feed_router() -> APIRouter:
    """Construct the broadcast feed-page :class:`APIRouter`.

    Registers ``GET /ui/broadcast``. Factory function (not a
    module-level constant) so a test app can construct parallel routers
    without sharing route state -- mirrors the chassis convention in
    :mod:`meho_backplane.ui.routes.dashboard`.
    """
    router = APIRouter(tags=["ui-broadcast"])
    router.add_api_route(
        "/ui/broadcast",
        _render_feed,
        methods=["GET"],
        name="ui_broadcast_feed",
        response_class=HTMLResponse,
    )
    return router
