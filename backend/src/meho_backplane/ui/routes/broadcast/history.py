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
and :class:`AgentAnnouncementEvent` (agent-authored). #2549 makes the
pane render both: the shared row partial (``broadcast/_event_row.html``)
branches on ``ev.kind`` so an operation renders the audit columns
(``op_class`` / ``op_id`` / ``result_status`` / ``payload``) and an
announcement renders its agent-authored variant (principal, phase chip,
enveloped activity text shown as escaped quoted prose, plus the #2544
structured claim fields -- targets / work_ref / TTL). The former hard
drop in :func:`_fetch_history` becomes the optional ``?kind=`` filter
(see :func:`_matches_kind_filter`), so an operator can still narrow the
pane to one kind. This mirrors the SSE feed, which #2549 also union-
validates so announcements flow as first-class frames.

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
from typing import Any, Final
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query, Request
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
    tenant_id: UUID,
    operator_sub: str,
    *,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """Pull the last-24h events for *tenant_id*, newest-first, capped.

    Delegates to :func:`~meho_backplane.broadcast.list_recent_events_fail_soft`
    -- a single finite ``XRANGE`` over ``meho:feed:{tenant_id}`` from the
    retention-window start to ``"+"`` (latest), bounded by ``COUNT`` =
    :data:`IN_DOM_ROW_CAP` so the pane stays within the same in-DOM row
    budget as the live feed.

    The helper returns ascending (oldest-first) dict-shaped events with a
    stream-cursor ``id``. On the fail-soft path it serialises via
    ``_dump_event_plain`` (no untrusted-text envelope) because this pane's
    HTML sink escapes every field separately (Alpine ``x-text`` sets
    ``textContent``); the announcement free text renders as escaped quoted
    prose, never interpreted. This route reverses to newest-first to match
    the live feed's display order and applies the optional ``kind`` filter
    (see :func:`_matches_kind_filter`). Both event kinds render -- the
    shared row partial branches on ``ev.kind`` so an announcement renders
    its agent-authored variant and an operation renders the audit columns.

    Fail-soft: a Valkey error returns an empty list (the pane renders its
    empty state) rather than 500-ing the fragment. The helper logs the
    structured warning; this route does not re-log or transform the failure.

    *operator_sub* is the session's principal subject; the helper takes a
    synthesised :class:`Operator` because its API is operator-scoped even
    though the UI doesn't validate operator role for the read (the session
    middleware already gated the request). The role on the synthesised
    operator is OPERATOR (sufficient for the helper's structural tenant
    check).
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
    events = [e for e in result["events"] if _matches_kind_filter(e, kind)]
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

        Pulls the session tenant's last-24h events via the shared
        fail-soft read helper, passes them as a list the template
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
        events = await _fetch_history(
            session_ctx.tenant_id,
            session_ctx.operator_sub,
            kind=kind_filter,
        )
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
