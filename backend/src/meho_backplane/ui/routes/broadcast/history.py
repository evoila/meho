# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/broadcast/history`` -- the Last-24h replay pane.

Initiative #338 (G10.1 Activity broadcast UI), Task #869 (G10.1-T3)
work item #6. The broadcast view's "Last 24h" tab ``hx-get``s this
fragment; it renders the historical events the operator can scroll
back through, with each row opening the same T2 event-detail drawer on
a click.

What it reads (#3758)
=====================

Broadcast carries deliberate agent announcements only. The pane reads
the newest announcements stored in the ``agent_announcement`` archive
within the last :attr:`Settings.broadcast_retention_hours` hours (default
24), capped at :data:`IN_DOM_ROW_CAP` rows, through
:func:`~meho_backplane.broadcast.archive_reader.latest_announcements`.
It is a finite read: no stream, no generator. A database error degrades
the pane to its empty state (logged), not a 500.

People read the pane; only trusted agents publish (#3736). Operations
are not on Broadcast, so the pane has no operation or kind filter
(#3762); ``query_audit`` and the audit console are the record of
operations.

Why reuse the live feed's row + drawer
======================================

The history rows render through the same ``broadcast/_event_row.html``
partial and ``broadcastFeed`` Alpine controller the live feed uses,
seeded from a ``<script type="application/json">`` data island (Jinja
``| tojson``) so quote- and markup-bearing fields cannot break out of
the markup (B1, PR #1044). Announcement text renders as escaped prose.

Tenant scoping
==============

The tenant comes from the validated session, never a query parameter,
so a tenant-A operator's pane reads only tenant A's announcements.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import SQLAlchemyError

from meho_backplane.broadcast.archive_reader import latest_announcements
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_operator
from meho_backplane.ui.routes.broadcast.feed import IN_DOM_ROW_CAP
from meho_backplane.ui.routes.broadcast.stream import session_reader
from meho_backplane.ui.templating import get_templates

__all__ = ["build_history_router"]

_log = structlog.get_logger(__name__)


async def _fetch_history(session_ctx: UISessionContext) -> list[dict[str, Any]]:
    """Return the session tenant's stored announcements of the retention window.

    Newest first, capped at :data:`IN_DOM_ROW_CAP`. Each row is the plain
    model dump (no untrusted-text envelope): this pane's HTML sink escapes
    every field (Alpine ``x-text`` sets ``textContent``). Fail-soft: a
    database error is logged and returns an empty list.
    """
    since = datetime.now(UTC) - timedelta(hours=get_settings().broadcast_retention_hours)
    try:
        latest = await latest_announcements(
            session_reader(session_ctx), limit=IN_DOM_ROW_CAP, since=since
        )
    except (SQLAlchemyError, OSError) as exc:
        _log.warning(
            "ui_broadcast_history_unavailable",
            tenant_id=str(session_ctx.tenant_id),
            error_class=type(exc).__name__,
        )
        return []
    events = [item.event.model_dump(mode="json") for item in latest.events]
    events.reverse()
    return events


#: Module-level :class:`fastapi.Depends` closure -- ruff B008 guard.
#: The ``operator`` floor of the REST broadcast and feed reads: a ``read_only``
#: session gets ``403 operator_required`` before the handler runs.
_require_ui_operator_dep = Depends(require_ui_operator)


def build_history_router() -> APIRouter:
    """Construct the broadcast Last-24h replay :class:`APIRouter`.

    Registers ``GET /ui/broadcast/history`` -- the HTMX-only fragment
    the "Last 24h" tab swaps in. Factory function (not a module-level
    constant) so a test app can construct parallel routers without
    sharing route state -- mirrors the feed / stream / event routers.
    """
    router = APIRouter(tags=["ui-broadcast"])

    async def _handler(
        request: Request,
        session_ctx: UISessionContext = _require_ui_operator_dep,
    ) -> HTMLResponse:
        """``GET /ui/broadcast/history`` -- the Last-24h replay fragment.

        Reads the session tenant's stored announcements of the retention
        window (fail-soft, :func:`_fetch_history`), passes them as a list the template
        serialises with Jinja ``| tojson`` into a
        ``<script type="application/json">`` data island the shared
        ``broadcastFeed`` controller seeds from, and returns the
        ``broadcast/_history.html`` fragment (no ``base.html`` chrome --
        the tab swaps it into the page's history container). The
        tenant comes from the validated session, never a query
        parameter.
        """
        events = await _fetch_history(session_ctx)
        context: dict[str, object] = {
            "in_dom_row_cap": IN_DOM_ROW_CAP,
            "history_events": events,
            "history_count": len(events),
            "retention_hours": get_settings().broadcast_retention_hours,
        }
        return get_templates().TemplateResponse(request, "broadcast/_history.html", context)

    router.add_api_route(
        "/ui/broadcast/history",
        _handler,
        methods=["GET"],
        name="ui_broadcast_history",
        response_class=HTMLResponse,
    )
    return router
