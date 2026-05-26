# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/broadcast/history`` -- the Last-24h replay pane.

Initiative #338 (G10.1 Activity broadcast UI), Task #869 (G10.1-T3)
work item #6. The broadcast view's "Last 24h" tab ``hx-get``s this
fragment; it renders the historical events the operator can scroll
back through, with each row opening the same T2 event-detail drawer on
a click.

Live feed vs. history pane
==========================

The live feed (:mod:`~meho_backplane.ui.routes.broadcast.stream`) tails
the per-tenant Valkey stream with a BLOCKing ``XREAD`` -- an open-ended
generator that yields events as they are published. The history pane is
the opposite shape: a **finite** ``XRANGE`` pull of the events already
on the stream, bounded by both a 24h time window and a row cap, rendered
once into the page. There is no streaming, no generator, no
``while True`` -- the endpoint reads a bounded batch and returns, so it
cannot hang a worker the way an unguarded stream loop could.

The 24h window
==============

The ``XRANGE`` lower bound is the bare millisecond timestamp ``now_ms -
broadcast_retention_hours * 3_600_000``; the upper bound is ``"+"``
(latest). The retention setting is :attr:`Settings.broadcast_retention_hours`
(default 24, the locked decision-3 contract -- the same retention the
publisher's ``MAXLEN`` trim targets). ``XRANGE`` returns ascending
(oldest-first); the pane renders newest-first to match the live feed,
so the parsed list is reversed before it reaches the template.

Shared read helper (G6.4-T4 #1103)
==================================

The xrange + redact-aware parse loop lives in
:func:`meho_backplane.broadcast.history.list_recent_events_fail_soft`
(the fail-soft sibling of the MCP ``broadcast.recent`` tool's
fail-loud caller). The wrapper catches :class:`redis.exceptions.RedisError`
and returns the empty result so a Valkey blip degrades this pane to
its empty state, not 500. That guarantee is what keeps the page
useful when the broadcast subchart is unhealthy.

The shared helper handles both :class:`BroadcastEvent` (audit-driven)
and :class:`AgentAnnouncementEvent` (agent-authored). This pane today
renders only :class:`BroadcastEvent` -- the row template binds to
audit-event columns (``op_class`` / ``op_id`` / ``result_status`` /
``payload``) that announcements don't carry. The post-helper filter
in :func:`_fetch_history` drops announcement entries to keep the pane
byte-compatible with the existing live SSE feed (which also drops
announcements today). Adding announcement rendering is a follow-up
that lives on the row template, not this route.

Why reuse the live feed's row + drawer
======================================

The history rows render through the **same** ``broadcast/_event_row.html``
partial and the **same** ``broadcastFeed`` Alpine controller the live
feed uses. The controller is seeded with the historical events from a
``<script type="application/json">`` data island (the template renders
the ``history_events`` list through Jinja ``| tojson``) instead of an
SSE subscription, so the row markup, the op_class badge palette, the 🔒
aggregate-only marker, the timestamp formatting, and the
click-to-open-drawer behaviour are all single-sourced -- a history row
is byte-identical to a live row and opens the identical T2 drawer. The
data-island shape (not an ``x-data`` attribute) keeps quote- and
markup-bearing event fields from breaking out of the markup (B1, PR
#1044).

Tenant scoping
==============

The stream key is ``meho:feed:{session.tenant_id}`` taken from the
validated session, never a query parameter -- the identical guarantee
the live stream bridge makes. A tenant-A operator's history pane reads
only ``meho:feed:{A}``; there is no parameter that could redirect it to
another tenant's stream, so tenant-A events can never surface on
tenant-B's history pane (and vice versa).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import list_recent_events_fail_soft
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.broadcast.feed import (
    IN_DOM_ROW_CAP,
    OP_CLASS_BADGE_CLASSES,
)
from meho_backplane.ui.templating import get_templates

__all__ = ["build_history_router"]

_log = structlog.get_logger(__name__)


def _window_start_iso(*, retention_hours: int, now: datetime) -> str:
    """Build the inclusive ISO-8601 ``since`` for the retention-window read.

    Returns ``now - retention_hours``, ISO-8601 with a ``Z`` suffix --
    the shape :func:`~meho_backplane.broadcast.history.parse_since`
    accepts as an ISO-8601 ``since`` and converts to a bare-ms inclusive
    lower bound. ``retention_hours`` is bounded by the
    :attr:`Settings.broadcast_retention_hours` setting (default 24).

    Returns the epoch when the window would underflow (e.g. a
    misconfigured absurdly-large retention window) so the helper never
    sees a negative timestamp. The epoch maps to bare-ms ``0`` after
    parsing, which Valkey treats as "start from the beginning of time".
    """
    delta = timedelta(hours=retention_hours)
    start = (
        now - delta
        if now - delta > datetime(1970, 1, 1, tzinfo=UTC)
        else datetime(1970, 1, 1, tzinfo=UTC)
    )
    return start.isoformat().replace("+00:00", "Z")


def _is_audit_event(event: dict[str, Any]) -> bool:
    """Return ``True`` iff *event* is a :class:`BroadcastEvent` (audit-driven).

    The shared helper surfaces both :class:`BroadcastEvent` (audit-driven,
    no ``event_kind`` field on the wire JSON) and
    :class:`AgentAnnouncementEvent` (``event_kind == "agent_announcement"``).
    This route today renders only audit events -- the existing row template
    binds to audit columns (``op_class`` / ``op_id`` / ``result_status``
    / ``payload``) that announcements don't carry. Surfacing
    announcement-shape events through the existing template would render
    blank cells. Adding announcement rendering is a follow-up that lives
    on the row template, not this route.
    """
    return event.get("event_kind") != "agent_announcement"


async def _fetch_history(
    tenant_id: UUID,
    operator_sub: str,
) -> list[dict[str, Any]]:
    """Pull the last-24h audit events for *tenant_id*, newest-first, capped.

    Delegates to :func:`~meho_backplane.broadcast.list_recent_events_fail_soft`
    -- a single finite ``XRANGE`` over ``meho:feed:{tenant_id}`` from the
    retention-window start to ``"+"`` (latest), bounded by ``COUNT`` =
    :data:`IN_DOM_ROW_CAP` so the pane stays within the same in-DOM row
    budget as the live feed.

    The helper returns ascending (oldest-first) dict-shaped events with
    a stream-cursor ``id``; this route reverses to newest-first to match
    the live feed's display order, then filters out
    :class:`AgentAnnouncementEvent` entries to preserve the existing
    row template's audit-event contract (see :func:`_is_audit_event`).

    Fail-soft: a Valkey error returns an empty list (the pane renders
    its empty state) rather than 500-ing the fragment. The helper logs
    the structured warning; this route does not re-log or transform the
    failure.

    *operator_sub* is the session's principal subject; the helper takes
    a synthesised :class:`Operator` because its API is operator-scoped
    even though the UI doesn't validate operator role for the read
    (the session middleware already gated the request). The role on the
    synthesised operator is OPERATOR (sufficient for the helper's
    structural tenant check).
    """
    operator = Operator(
        sub=operator_sub,
        raw_jwt="ui-session",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )
    since_iso = _window_start_iso(
        retention_hours=get_settings().broadcast_retention_hours,
        now=datetime.now(UTC),
    )
    result = await list_recent_events_fail_soft(
        operator,
        since=since_iso,
        limit=IN_DOM_ROW_CAP,
    )
    # XRANGE is ascending (oldest-first); the pane renders newest-first
    # to match the live feed's reverse-chronological order.
    audit_events = [e for e in result["events"] if _is_audit_event(e)]
    audit_events.reverse()
    return audit_events


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
        session_ctx: UISessionContext = _require_ui_session_dep,
    ) -> HTMLResponse:
        """``GET /ui/broadcast/history`` -- the Last-24h replay fragment.

        Pulls the session tenant's last-24h events via the shared
        fail-soft read helper, passes them as a list the template
        serialises with Jinja ``| tojson`` into a
        ``<script type="application/json">`` data island the shared
        ``broadcastFeed`` controller seeds from, and returns the
        ``broadcast/_history.html`` fragment (no ``base.html`` chrome --
        the tab swaps it into the page's history container). The
        tenant comes from the validated session, never a query
        parameter.
        """
        events = await _fetch_history(
            session_ctx.tenant_id,
            session_ctx.operator_sub,
        )
        context: dict[str, object] = {
            "in_dom_row_cap": IN_DOM_ROW_CAP,
            "op_class_badge_json": _badge_palette_json(),
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


def _badge_palette_json() -> str:
    """Serialise the op_class → DaisyUI badge palette for the controller.

    The same map the live feed serialises (re-used so the history rows'
    badge colours match the live rows exactly). ``json.dumps`` output is
    HTML-safe inside the ``application/json``-shaped Alpine ``x-data``
    the history fragment renders it into.
    """
    return json.dumps(OP_CLASS_BADGE_CLASSES)
