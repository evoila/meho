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

The ``?kind=`` filter stays for the page's "All / Operations /
Announcements" control; automatic operations are no longer on
Broadcast, so ``kind=operation`` shows nothing. ``query_audit`` and the
audit console are the record of operations.

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

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import SQLAlchemyError

from meho_backplane.broadcast.archive_reader import latest_announcements
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.broadcast.feed import (
    IN_DOM_ROW_CAP,
    OP_CLASS_BADGE_CLASSES,
)
from meho_backplane.ui.routes.broadcast.stream import session_reader
from meho_backplane.ui.templating import get_templates

__all__ = ["build_history_router"]

_log = structlog.get_logger(__name__)


#: The top-level discriminator value for an audit-driven
#: :class:`~meho_backplane.broadcast.events.BroadcastEvent`. Pre-migration
#: entries omit ``kind`` on the wire and are inferred as this.
_OPERATION_KIND: Final[str] = "operation"

#: The top-level discriminator value for an agent-authored
#: :class:`~meho_backplane.broadcast.agent_events.AgentAnnouncementEvent`.
_ANNOUNCEMENT_KIND: Final[str] = "agent_announcement"

#: The kind values the ``?kind=`` history filter honours. Anything else
#: (blank, garbage) is treated as "no filter" -- the pane renders both
#: kinds rather than 400-ing a browser-supplied query string.
_KIND_FILTER_VALUES: Final[tuple[str, ...]] = (_OPERATION_KIND, _ANNOUNCEMENT_KIND)


def _event_kind(event: dict[str, Any]) -> str:
    """Return the normalised discriminator kind for a wire event dict.

    G0.16-T6 Finding F (#1312). Prefers the top-level ``kind`` field per
    ``docs/codebase/api-shape-conventions.md`` §6, falling back to the
    historical ``event_kind`` alias for v0.8.0 stream entries still in the
    publisher's ``MAXLEN ~`` window, and finally to :data:`_OPERATION_KIND`
    for pre-migration audit rows that carry neither field.
    """
    return event.get("kind") or event.get("event_kind") or _OPERATION_KIND


def _matches_kind_filter(event: dict[str, Any], kind: str | None) -> bool:
    """Return ``True`` iff *event* should render under the ``kind`` filter.

    #2549 turns the former hard drop of announcement-kind events into a
    user-facing kind filter:

    * ``kind is None`` — no filter; both audit-driven operations and
      agent-authored announcements render (the pane is no longer
      write-only for humans).
    * ``kind == "operation"`` — only audit-driven events (announcements
      drop).
    * ``kind == "agent_announcement"`` — only announcements.

    The discriminator is normalised via :func:`_event_kind` so a
    pre-migration audit row (no ``kind`` on the wire) still counts as an
    operation.
    """
    if kind is None:
        return True
    return _event_kind(event) == kind


def _normalise_kind_filter(raw: str | None) -> str | None:
    """Clamp the browser-supplied ``?kind=`` value to a known kind or ``None``.

    Returns the value only when it is one of :data:`_KIND_FILTER_VALUES`;
    an empty string (the "All" sentinel the filter control submits) or any
    unknown value maps to ``None`` (render both kinds). Keeps a hand-edited
    or stale query string from silently emptying the pane.
    """
    return raw if raw in _KIND_FILTER_VALUES else None


async def _fetch_history(
    session_ctx: UISessionContext,
    *,
    kind: str | None = None,
) -> list[dict[str, Any]]:
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
    events = [event for event in events if _matches_kind_filter(event, kind)]
    events.reverse()
    return events


#: Module-level :class:`fastapi.Depends` closure -- ruff B008 guard.
_require_ui_session_dep = Depends(require_ui_session)


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
        kind: str | None = Query(
            default=None,
            max_length=64,
            description=(
                "Optional event-kind filter: 'operation' (audit-driven) or "
                "'agent_announcement'. Absent/blank renders both kinds."
            ),
        ),
        session_ctx: UISessionContext = _require_ui_session_dep,
    ) -> HTMLResponse:
        """``GET /ui/broadcast/history[?kind=]`` -- the Last-24h replay fragment.

        Reads the session tenant's stored announcements of the retention
        window (fail-soft, :func:`_fetch_history`), passes them as a list the template
        serialises with Jinja ``| tojson`` into a
        ``<script type="application/json">`` data island the shared
        ``broadcastFeed`` controller seeds from, and returns the
        ``broadcast/_history.html`` fragment (no ``base.html`` chrome --
        the tab swaps it into the page's history container). The
        tenant comes from the validated session, never a query
        parameter.

        The optional ``kind`` query parameter narrows the pane to a
        single event kind (``operation`` / ``agent_announcement``); it is
        clamped to a known kind by :func:`_normalise_kind_filter`, so a
        blank or unknown value renders both kinds.
        """
        kind_filter = _normalise_kind_filter(kind)
        events = await _fetch_history(session_ctx, kind=kind_filter)
        context: dict[str, object] = {
            "in_dom_row_cap": IN_DOM_ROW_CAP,
            "op_class_badge_json": _badge_palette_json(),
            "history_events": events,
            "history_count": len(events),
            "retention_hours": get_settings().broadcast_retention_hours,
            "kind_filter": kind_filter or "",
            "operation_kind": _OPERATION_KIND,
            "announcement_kind": _ANNOUNCEMENT_KIND,
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


def _badge_palette_json() -> str:
    """Serialise the op_class → DaisyUI badge palette for the controller.

    The same map the live feed serialises (re-used so the history rows'
    badge colours match the live rows exactly). ``json.dumps`` output is
    HTML-safe inside the ``application/json``-shaped Alpine ``x-data``
    the history fragment renders it into.
    """
    return json.dumps(OP_CLASS_BADGE_CLASSES)
