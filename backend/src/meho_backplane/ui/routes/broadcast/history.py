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

Valkey stream entry ids are ``<ms-timestamp>-<sequence>``. A bare
millisecond timestamp as the ``XRANGE`` start auto-completes the
sequence to ``0`` (Valkey ``XRANGE`` semantics), so
``XRANGE key <now_ms - 24h> + COUNT <cap>`` returns every entry from the
last 24 hours, oldest-first, capped. The window width is
:attr:`Settings.broadcast_retention_hours` (default 24, the locked
decision-3 contract -- the same retention the publisher's ``MAXLEN``
trim targets). ``XRANGE`` returns ascending (oldest-first); the pane
renders newest-first to match the live feed, so the parsed list is
reversed before it reaches the template.

Why reuse the live feed's row + drawer
======================================

The history rows render through the **same** ``broadcast/_event_row.html``
partial and the **same** ``broadcastFeed`` Alpine controller the live
feed uses. The controller is seeded with the historical events as JSON
(``history_events_json``) instead of an SSE subscription, so the row
markup, the op_class badge palette, the 🔒 aggregate-only marker, the
timestamp formatting, and the click-to-open-drawer behaviour are all
single-sourced -- a history row is byte-identical to a live row and
opens the identical T2 drawer.

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
import time
from typing import Final

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from meho_backplane.broadcast import BroadcastEvent, get_broadcast_client
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.broadcast.feed import (
    IN_DOM_ROW_CAP,
    OP_CLASS_BADGE_CLASSES,
)
from meho_backplane.ui.templating import get_templates

__all__ = ["build_history_router"]

_log = structlog.get_logger(__name__)

#: Milliseconds per hour -- the 24h window start id is computed as
#: ``now_ms - broadcast_retention_hours * _MS_PER_HOUR``.
_MS_PER_HOUR: Final[int] = 3_600_000

#: ``XRANGE`` end anchor -- ``"+"`` is Valkey's "latest available entry"
#: sentinel, so the pull spans from the window start to the live tail.
_XRANGE_END: Final[str] = "+"


def _stream_key(tenant_id: object) -> str:
    """Build the per-tenant Valkey stream key.

    Mirrors :func:`meho_backplane.ui.routes.broadcast.stream._stream_key`
    and the publisher exactly so the history pane reads the same key the
    publisher writes and the live stream tails.
    """
    return f"meho:feed:{tenant_id}"


def _window_start_id(*, retention_hours: int, now_ms: int) -> str:
    """Build the ``XRANGE`` start id for the replay window.

    The start id is a bare millisecond timestamp ``now_ms -
    retention_hours * 3_600_000``. Valkey ``XRANGE`` auto-completes a
    bare timestamp's sequence to ``0``, so the range is inclusive of
    every entry from that millisecond onward. Clamped at ``0`` so a
    misconfigured (absurdly large) retention window never produces a
    negative start id that Valkey would reject.
    """
    start_ms = max(now_ms - retention_hours * _MS_PER_HOUR, 0)
    return str(start_ms)


def _parse_entries(
    entries: list[tuple[str, dict[str, str]]],
    *,
    stream_key: str,
) -> list[BroadcastEvent]:
    """Parse ``XRANGE`` entries into :class:`BroadcastEvent` objects.

    Mirrors the skip discipline in
    :func:`meho_backplane.api.v1.feed._process_entries`: an entry with
    no ``event`` field, or a malformed JSON ``event`` field, is logged
    and skipped rather than failing the whole pane -- the same
    belt-and-suspenders guard against a future foreign writer on the
    shared stream key.
    """
    events: list[BroadcastEvent] = []
    for entry_id, fields in entries:
        raw_event_json = fields.get("event")
        if not isinstance(raw_event_json, str):
            _log.warning(
                "broadcast_history_skipped_unknown_field_shape",
                stream_key=stream_key,
                entry_id=entry_id,
                fields=list(fields.keys()),
            )
            continue
        try:
            events.append(BroadcastEvent.model_validate_json(raw_event_json))
        except ValidationError:
            _log.warning(
                "broadcast_history_skipped_malformed_event",
                stream_key=stream_key,
                entry_id=entry_id,
            )
    return events


async def _fetch_history(tenant_id: object) -> list[BroadcastEvent]:
    """Pull the last-24h events for *tenant_id*, newest-first, capped.

    A single finite ``XRANGE`` over ``meho:feed:{tenant_id}`` from the
    window start to ``"+"`` (latest), bounded by ``COUNT`` =
    :data:`IN_DOM_ROW_CAP` so the pane stays within the same in-DOM row
    budget as the live feed. ``XRANGE`` returns oldest-first; the list
    is reversed so the pane renders newest-first, matching the live
    feed's reverse-chronological order.

    Fail-soft: any Valkey error returns an empty list (the pane renders
    its empty state) rather than 500-ing the fragment -- a transient
    broadcast-subchart blip should degrade the replay pane, not the
    page. The error is logged with the exception class only (redis-py
    exceptions can embed the endpoint URL).
    """
    client = get_broadcast_client()
    stream_key = _stream_key(tenant_id)
    retention_hours = get_settings().broadcast_retention_hours
    start_id = _window_start_id(
        retention_hours=retention_hours,
        now_ms=int(time.time() * 1000),
    )
    try:
        entries = await client.xrange(
            stream_key,
            min=start_id,
            max=_XRANGE_END,
            count=IN_DOM_ROW_CAP,
        )
    except Exception as exc:
        _log.warning(
            "broadcast_history_fetch_failed",
            error_class=type(exc).__name__,
            stream_key=stream_key,
        )
        return []
    events = _parse_entries(entries, stream_key=stream_key)
    # XRANGE is ascending (oldest-first); the pane renders newest-first
    # to match the live feed's reverse-chronological order.
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
        session_ctx: UISessionContext = _require_ui_session_dep,
    ) -> HTMLResponse:
        """``GET /ui/broadcast/history`` -- the Last-24h replay fragment.

        Pulls the session tenant's last-24h events via a finite
        ``XRANGE``, serialises them as JSON the shared ``broadcastFeed``
        controller renders through ``broadcast/_event_row.html``, and
        returns the ``broadcast/_history.html`` fragment (no
        ``base.html`` chrome -- the tab swaps it into the page's history
        container). The tenant comes from the validated session, never a
        query parameter.
        """
        events = await _fetch_history(session_ctx.tenant_id)
        context: dict[str, object] = {
            "in_dom_row_cap": IN_DOM_ROW_CAP,
            "op_class_badge_json": _badge_palette_json(),
            "history_events_json": _events_json(events),
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


def _events_json(events: list[BroadcastEvent]) -> str:
    """Serialise the historical events as a JSON array for the controller.

    Each event is dumped via :meth:`BroadcastEvent.model_dump_json` so
    the client-side shape is byte-identical to a live SSE frame's
    ``data:`` payload -- the ``broadcastFeed`` controller seeds these
    into its ``events`` array and the rows render through the same
    ``_event_row.html`` partial. The result is a JSON array string the
    template embeds via ``| safe`` inside the Alpine ``x-data``.
    """
    return "[" + ",".join(event.model_dump_json() for event in events) + "]"
