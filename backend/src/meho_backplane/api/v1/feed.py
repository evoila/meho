# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /api/v1/feed`` — Server-Sent Events feed of agent announcements.

Broadcast carries deliberate agent announcements only (#3758). This feed
reads them from the ``agent_announcement`` archive in commit order
(#3748); the Valkey stream only wakes the reader early. Automatic
activity (REST or MCP calls, dispatches, approvals, checks) is not on
the feed. ``query_audit`` is the record of that activity.

Subscribers — ``meho status --watch``, ``meho broadcast watch`` and the
console's ``/ui/broadcast/stream`` bridge (which reuses
:func:`announcement_feed_frames`) — open one HTTP connection and receive
announcements as they are stored.

Transport
=========

Standard SSE per the WHATWG ``EventSource`` spec. Each announcement is
emitted as::

    event: broadcast
    data: <AgentAnnouncementEvent JSON>
    id: <watch checkpoint just after this announcement>

The ``id`` is an opaque archive watch checkpoint (the same kind
``meho_broadcast_watch`` takes). A client that reconnects with
``Last-Event-Id`` resumes right after the last announcement it got.

When archive retention removed announcements the reader would have
returned, or a checkpoint is ahead of the archive, the feed says so in
one ``event: gap`` frame (JSON ``{reason, missed_events, detail}``)
instead of pretending to be complete.

Filtering
=========

``op_class`` / ``principal`` / ``target`` are exact-match filters, the
same rules as :func:`~meho_backplane.broadcast.history.event_matches`.
``op_class`` is an operation filter, so it matches no announcement.

Replay
======

``Last-Event-Id`` wins over the ``since`` query parameter. ``since``
takes a watch checkpoint or an ISO-8601 time (replay every announcement
stored from that time). With neither, a fresh connection first gets
the newest :data:`_BACKLOG_PRELUDE_COUNT` announcements, so a quiet
tenant still paints recent history at once (#1305), then waits for new
ones. A ``Last-Event-Id`` that is not a checkpoint of this tenant — for
example a Valkey stream id a browser kept from before #3758 — starts
fresh instead of failing, so an ``EventSource`` does not get stuck.

Tenant scoping and RBAC
=======================

The tenant comes from the validated JWT; there is no tenant parameter.
Checkpoints are bound to the tenant that issued them. ``operator`` role
minimum (``read_only`` → 403).

Heartbeat and errors
====================

The generator emits ``: heartbeat`` (an SSE comment) after
:data:`_HEARTBEAT_INTERVAL_SECONDS` without an outbound frame, so
proxies do not drop an idle connection. A database error after the
response started becomes one ``event: feed_error`` frame
(``docs/codebase/error-message-shape.md``) and the stream ends. A Valkey
outage is not an error: the archive reader falls back to polling.
Client disconnect propagates as :class:`asyncio.CancelledError`; it is
logged and re-raised.

References
----------

* SSE / EventSource: https://html.spec.whatwg.org/multipage/server-sent-events.html
* FastAPI ``StreamingResponse``:
  https://fastapi.tiangolo.com/advanced/custom-response/#streamingresponse
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.broadcast.archive_cursor import (
    InvalidArchiveCursorError,
    decode_watch_checkpoint,
    encode_watch_checkpoint,
)
from meho_backplane.broadcast.archive_reader import (
    AnnouncementFilter,
    StoredAnnouncement,
    first_seq_since,
    latest_announcements,
    watch_announcement_rows,
)

__all__ = [
    "FeedStart",
    "announcement_feed_frames",
    "feed_response",
    "resolve_feed_start",
    "router",
]

router = APIRouter(prefix="/api/v1", tags=["feed"])

_log = structlog.get_logger(__name__)

#: Operator-minimum gate at module scope (ruff B008).
_REQUIRE_OPERATOR = Depends(require_role(TenantRole.OPERATOR))

#: Longest single archive wait. Also bounds how long a client disconnect
#: takes to land in the generator.
_WAIT_MS: Final[int] = 30_000

#: Idle time after which the generator emits an SSE comment heartbeat.
#: Common proxy idle timeouts are 60 s (nginx ``proxy_read_timeout``,
#: AWS ALB).
_HEARTBEAT_INTERVAL_SECONDS: Final[float] = 30.0

#: Announcements replayed on a fresh connection before the live wait.
#: Matches the MCP ``tenant_feed`` resource snapshot size.
_BACKLOG_PRELUDE_COUNT: Final[int] = 50

_FEED_ERROR_CODE: Final[str] = "broadcast_subsystem_unavailable"
_FEED_ERROR_DOC: Final[str] = "docs/codebase/error-message-shape.md"


@dataclass(frozen=True)
class FeedStart:
    """Where a feed connection starts.

    ``after`` set: resume after that sequence number. ``since`` set:
    replay from that time. Neither: a fresh connection (backlog prelude).
    """

    after: int | None = None
    since: datetime | None = None


def _parse_iso(raw: str) -> datetime | None:
    """Parse an ISO-8601 instant (``T`` required); naive means UTC."""
    if "T" not in raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00") if raw.endswith("Z") else raw)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def resolve_feed_start(
    operator: Operator, last_event_id: str | None, since: str | None
) -> FeedStart:
    """Pick the start of a feed connection; raise HTTP 400 for a bad ``since``.

    ``Last-Event-Id`` is sent by ``EventSource`` on reconnect. One that is
    not a checkpoint of this tenant is ignored (logged), so an old
    browser tab reconnecting after the cut-over starts fresh instead of
    looping on a 400. ``since`` is explicit caller input, so a bad value
    is a 400. The detail never echoes the input.
    """
    if last_event_id:
        try:
            return FeedStart(after=decode_watch_checkpoint(last_event_id, operator.tenant_id))
        except InvalidArchiveCursorError:
            _log.info("feed_last_event_id_ignored", operator_sub=operator.sub)
    if not since:
        return FeedStart()
    try:
        return FeedStart(after=decode_watch_checkpoint(since, operator.tenant_id))
    except InvalidArchiveCursorError:
        pass
    parsed = _parse_iso(since)
    if parsed is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "invalid_cursor: expected a watch checkpoint from this feed or "
                "meho_broadcast_recent, or an ISO-8601 time (e.g. '2026-05-25T10:00:00Z')"
            ),
        )
    return FeedStart(since=parsed)


def _format_event(operator: Operator, item: StoredAnnouncement) -> str:
    """One ``event: broadcast`` frame; the id is the checkpoint after this row."""
    checkpoint = encode_watch_checkpoint(operator.tenant_id, item.seq)
    return f"event: broadcast\ndata: {item.event.model_dump_json()}\nid: {checkpoint}\n\n"


def _format_gap(gap: dict[str, Any]) -> str:
    """One ``event: gap`` frame: the reader could not return every announcement."""
    return f"event: gap\ndata: {json.dumps(gap)}\n\n"


def _format_feed_error(exception_type: str) -> str:
    """The ``event: feed_error`` frame for a read failure after streaming began.

    Same three-clause shape as ``docs/codebase/error-message-shape.md``:
    a stable ``code``, a message naming the component and the exception
    class (never the exception text, which can name infrastructure), and
    a ``doc`` path. No ``id`` line: an error is not a resume point.
    """
    detail = {
        "code": _FEED_ERROR_CODE,
        "message": (
            f"broadcast announcement archive is unavailable (exception: {exception_type}); "
            f"see {_FEED_ERROR_DOC} for the error-shape convention"
        ),
        "doc": _FEED_ERROR_DOC,
    }
    return f"event: feed_error\ndata: {json.dumps(detail)}\n\n"


async def _start_position(
    operator: Operator, start: FeedStart, flt: AnnouncementFilter
) -> tuple[list[str], int]:
    """Return the backlog frames to send first, and the sequence number to resume after."""
    if start.after is not None:
        return [], start.after
    if start.since is not None:
        after, gap = await first_seq_since(operator, start.since)
        return ([_format_gap(gap)] if gap is not None else []), after
    latest = await latest_announcements(operator, limit=_BACKLOG_PRELUDE_COUNT, flt=flt)
    return [_format_event(operator, item) for item in latest.events], latest.watermark


async def announcement_feed_frames(
    operator: Operator,
    *,
    start: FeedStart,
    flt: AnnouncementFilter,
    surface: str,
) -> AsyncIterator[str]:
    """SSE frames of the tenant's stored announcements, then live ones.

    *surface* names the caller (``api`` / ``ui``) on the log lines.
    """
    last_out = time.monotonic()
    try:
        frames, after = await _start_position(operator, start, flt)
        for frame in frames:
            yield frame
        if frames:
            last_out = time.monotonic()
        while True:
            found = await watch_announcement_rows(
                operator, after=after, flt=flt, timeout_ms=_WAIT_MS
            )
            after = found.after
            out = [_format_gap(found.gap)] if found.gap is not None else []
            out.extend(_format_event(operator, item) for item in found.events)
            for frame in out:
                yield frame
            now = time.monotonic()
            if out:
                last_out = now
            elif now - last_out >= _HEARTBEAT_INTERVAL_SECONDS:
                yield ": heartbeat\n\n"
                last_out = now
    except asyncio.CancelledError:
        _log.info("feed_subscriber_disconnected", surface=surface, operator_sub=operator.sub)
        raise
    except (SQLAlchemyError, OSError) as exc:
        _log.warning(
            "feed_broadcast_unavailable",
            surface=surface,
            operator_sub=operator.sub,
            exception_type=type(exc).__name__,
            exc_info=True,
        )
        yield _format_feed_error(type(exc).__name__)


def feed_response(frames: AsyncIterator[str]) -> StreamingResponse:
    """Wrap SSE frames; the headers stop proxies from buffering the stream."""
    return StreamingResponse(
        frames,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/feed")
async def feed_endpoint(
    request: Request,
    op_class: str | None = Query(default=None, description="Filter by event op_class."),
    principal: str | None = Query(default=None, description="Filter by principal_sub."),
    target: str | None = Query(default=None, description="Filter by target_name."),
    since: str | None = Query(
        default=None,
        description="Explicit replay cursor; superseded by Last-Event-Id when present.",
    ),
    operator: Operator = _REQUIRE_OPERATOR,
) -> StreamingResponse:
    """Stream the tenant's agent announcements as Server-Sent Events.

    Headers:
        ``Last-Event-Id``: optional SSE reconnect cursor. Takes
        precedence over the ``since`` query parameter.

    Returns:
        :class:`StreamingResponse` with ``media_type=text/event-stream``.
    """
    start = resolve_feed_start(operator, request.headers.get("Last-Event-Id"), since)
    flt = AnnouncementFilter(op_class=op_class, principal=principal, target=target)
    return feed_response(announcement_feed_frames(operator, start=start, flt=flt, surface="api"))
