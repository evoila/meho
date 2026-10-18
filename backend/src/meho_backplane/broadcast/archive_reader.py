# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read agent announcements from the archive by commit order (#3748).

The ``agent_announcement`` table is the record; the Valkey stream only
wakes live watchers. Both read tools page by the per-tenant archive
sequence number (``agent_announcement.seq``), which follows commit order
(:mod:`~meho_backplane.broadcast.archive_sequence`). So "the highest
number I have seen" is a safe resume point: nothing can commit later
with a smaller number.

* :func:`read_announcement_history` (``meho_broadcast_recent``) reads one
  fixed view: it takes the tenant's current high-water mark first and
  pages up to it. Each answer has two separate cursors: ``next_cursor``
  continues this history read (``None`` at the end), and
  ``watch_checkpoint`` is where a watcher starts so it misses nothing
  after this view. The checkpoint is the high-water mark, so it moves
  forward even when the filter hides every row.
* :func:`watch_announcements` (``meho_broadcast_watch``) scans the
  archive after the caller's checkpoint, before it waits and again after
  every wake-up or timeout. The stream is only the wake-up signal. When
  Valkey fails, the watch keeps going by scanning every
  :data:`WATCH_POLL_SECONDS` seconds. Event ids are de-duplicated within
  a call. :func:`watch_announcement_rows` is the same wait for the SSE
  feeds; it returns the stored events with their sequence numbers.
* :func:`latest_announcements` reads the newest stored announcements
  (the SSE backlog, the UI history pane, the MCP tenant feed resource,
  #3758).

Retention: the prune deletes rows by ``created_at``, but the tenant
counter keeps counting, and committed numbers have no holes. So every
scan knows how many rows it should have found between its start and its
end, and the difference to the rows it did find is the number of
announcements that are gone. That count comes from the scanned rows
themselves, not from a separate query, so a prune that lands between two
statements is still counted, and so is a row pruned from the middle of
the range (the prune cutoff is by time, and a row's time can be earlier
than its number). The answer says so in ``gap`` -- it never claims to be
complete. A checkpoint above the current high-water mark (for example
after a database restore) gets a ``checkpoint_ahead`` reset and starts
again from the current end.

Rolling upgrade: a pod on the previous release writes rows without a
number. Every scan first gives such rows the next numbers
(:func:`~meho_backplane.broadcast.archive_sequence.number_unnumbered_rows`),
so they show up as new announcements instead of staying invisible.

Broadcast carries deliberate agent announcements only (#3758). Automatic
activity (operations, approvals, checks) is not published to it;
``query_audit`` is the record of that activity.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast.agent_events import AgentAnnouncementEvent
from meho_backplane.broadcast.archive_cursor import (
    HistoryPosition,
    InvalidArchiveCursorError,
    decode_history_cursor,
    decode_watch_checkpoint,
    encode_history_cursor,
    encode_watch_checkpoint,
)
from meho_backplane.broadcast.archive_sequence import (
    archive_watermark,
    number_unnumbered_rows,
    oldest_retained_seq,
)
from meho_backplane.broadcast.client import get_broadcast_blocking_client, get_broadcast_client
from meho_backplane.broadcast.history import (
    DEFAULT_WINDOW_MINUTES,
    dump_event_wire,
    event_matches,
    row_to_announcement,
    stream_key,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentAnnouncement
from meho_backplane.settings import get_settings

__all__ = [
    "WATCH_POLL_SECONDS",
    "WATCH_SCAN_LIMIT",
    "AnnouncementFilter",
    "AnnouncementWatch",
    "StoredAnnouncement",
    "first_seq_since",
    "latest_announcements",
    "read_announcement_history",
    "watch_announcement_rows",
    "watch_announcements",
]

_log = structlog.get_logger(__name__)

#: Scan interval when the Valkey wake-up fails. Bounded so a watcher sees
#: a stored announcement within a few seconds even with no stream.
WATCH_POLL_SECONDS: Final[float] = 5.0

#: Rows scanned per watch scan. A full scan returns at once so the caller
#: pages on with the new checkpoint.
WATCH_SCAN_LIMIT: Final[int] = 100

_HISTORY_PREFIX: Final[str] = "h1."


@dataclass(frozen=True)
class AnnouncementFilter:
    """The ``filter`` object of the recent / watch tools.

    Same exact-match rules as :func:`~meho_backplane.broadcast.history.event_matches`.
    ``op_class`` and ``actor_sub`` are operation-event filters; an
    announcement never matches them.
    """

    op_class: str | None = None
    principal: str | None = None
    target: str | None = None
    actor_sub: str | None = None
    work_ref: str | None = None
    active_only: bool = False

    def matches(self, row: AgentAnnouncement, now: datetime) -> bool:
        return event_matches(
            row_to_announcement(row),
            op_class=self.op_class,
            principal=self.principal,
            target=self.target,
            actor_sub=self.actor_sub,
            work_ref=self.work_ref,
            active_only=self.active_only,
            now=now,
        )


def _gap(reason: str, missed: int | None, detail: str) -> dict[str, Any]:
    return {"reason": reason, "missed_events": missed, "detail": detail}


def _scan_gap(
    after: int, upper: int, rows: list[AgentAnnouncement], limit: int
) -> dict[str, Any] | None:
    """Rows the scan should have found but did not, because retention removed them.

    Committed numbers have no holes, so a scan of ``after < seq <= upper``
    must find one row per number up to the last row of a full page, or up
    to *upper* otherwise. Fewer rows means the prune deleted the rest,
    wherever in the range they were.
    """
    end = cast("int", rows[-1].seq) if len(rows) == limit else upper
    missed = end - after - len(rows)
    if missed <= 0:
        return None
    return _gap(
        "retention_trimmed",
        missed,
        f"{missed} announcement(s) after this cursor were removed by archive retention.",
    )


def _window_gap(since: datetime, oldest: int | None, watermark: int) -> dict[str, Any] | None:
    """Whether a fresh time window may reach into rows the prune deleted."""
    pruned_any = (oldest or watermark + 1) > 1
    days = get_settings().broadcast_announcement_retention_days
    if not pruned_any or days <= 0 or since >= datetime.now(UTC) - timedelta(days=days):
        return None
    return _gap(
        "retention_trimmed",
        None,
        f"announcements older than {days} day(s) were removed; this window starts earlier.",
    )


def _parse_window_start(raw: str) -> datetime:
    """Parse an ISO-8601 window start; refuse old stream cursors with a hint."""
    head, sep, tail = raw.partition("-")
    if sep and head.isdigit() and tail.isdigit():
        raise InvalidArchiveCursorError(
            "cursor: Valkey stream cursors are no longer accepted; omit `cursor`, "
            "pass an ISO-8601 time, or pass `next_cursor` from meho_broadcast_recent"
        )
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00") if raw.endswith("Z") else raw)
    except ValueError as exc:
        raise InvalidArchiveCursorError(
            f"cursor: not a valid ISO-8601 timestamp or history page cursor: {raw!r}"
        ) from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


async def _number_older_writes(session: AsyncSession, tenant_id: UUID) -> None:
    """Number rows an older pod wrote, and commit, before the scan reads."""
    if await number_unnumbered_rows(session, tenant_id):
        await session.commit()


async def _scan(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    after: int,
    upper: int,
    limit: int,
) -> list[AgentAnnouncement]:
    """Archive rows with ``after < seq <= upper``, in sequence order.

    No time filter here: the time window is applied after the read, so
    the row count stays comparable with the number range
    (:func:`_scan_gap`).
    """
    stmt = (
        select(AgentAnnouncement)
        .where(
            AgentAnnouncement.tenant_id == tenant_id,
            AgentAnnouncement.seq > after,
            AgentAnnouncement.seq <= upper,
        )
        .order_by(AgentAnnouncement.seq)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


@dataclass(frozen=True)
class StoredAnnouncement:
    """One archived announcement with its per-tenant sequence number."""

    seq: int
    event: AgentAnnouncementEvent


def _matching(
    rows: list[AgentAnnouncement],
    flt: AnnouncementFilter,
    seen: set[str] | None = None,
    since: datetime | None = None,
) -> list[StoredAnnouncement]:
    """Rows that pass *flt*; skip rows before *since* and event ids in *seen*."""
    now = datetime.now(UTC)
    matched: list[StoredAnnouncement] = []
    for row in rows:
        event_id = str(row.id)
        if seen is not None:
            if event_id in seen:
                continue
            seen.add(event_id)
        announcement = row_to_announcement(row)
        if since is not None and announcement.ts < since:
            continue
        if flt.matches(row, now):
            matched.append(StoredAnnouncement(seq=cast("int", row.seq), event=announcement))
    return matched


def _serialize(
    rows: list[AgentAnnouncement],
    flt: AnnouncementFilter,
    seen: set[str] | None = None,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    """Filter and serialise *rows* for the LLM-facing reads (untrusted text wrapped)."""
    return [dump_event_wire(item.event) for item in _matching(rows, flt, seen, since)]


def _parse_history_cursor(tenant_id: UUID, cursor: str | None) -> HistoryPosition | datetime:
    """Return a page position to continue, or the start of a fresh window."""
    if cursor is None:
        return datetime.now(UTC) - timedelta(minutes=DEFAULT_WINDOW_MINUTES)
    if cursor.startswith(_HISTORY_PREFIX):
        return decode_history_cursor(cursor, tenant_id)
    return _parse_window_start(cursor)


async def _history_start(
    session: AsyncSession, tenant_id: UUID, start: HistoryPosition | datetime
) -> tuple[HistoryPosition, dict[str, Any] | None]:
    """Resolve where a recent read starts, plus the window gap of a fresh read.

    A page cursor continues as it is; its gap comes from the scan. A fresh
    window starts at the first row inside it. Rows below that are older
    than the window or pruned; :func:`_window_gap` says when the second
    case is possible.
    """
    if isinstance(start, HistoryPosition):
        return start, None
    watermark = await archive_watermark(session, tenant_id)
    first = (
        await session.execute(
            select(func.min(AgentAnnouncement.seq)).where(
                AgentAnnouncement.tenant_id == tenant_id,
                AgentAnnouncement.created_at >= start,
            )
        )
    ).scalar_one_or_none()
    after = watermark if first is None else min(int(first) - 1, watermark)
    position = HistoryPosition(watermark=watermark, after_seq=after, since=start)
    oldest = await oldest_retained_seq(session, tenant_id)
    return position, _window_gap(start, oldest, watermark)


async def read_announcement_history(
    operator: Operator,
    *,
    cursor: str | None,
    flt: AnnouncementFilter,
    limit: int,
) -> dict[str, Any]:
    """Read one page of the tenant's archived announcements, oldest first.

    *cursor* is ``None`` (the last :data:`DEFAULT_WINDOW_MINUTES` minutes),
    an ISO-8601 window start, or a ``next_cursor`` from an earlier page.
    Scans at most *limit* rows, then applies the window and the filter,
    so a page can hold fewer than *limit* events and still have a
    ``next_cursor``.

    Returns ``{events, next_cursor, watch_checkpoint, gap}``. Raises
    :class:`InvalidArchiveCursorError` for a bad cursor; DB errors
    propagate (fail-loud).
    """
    tenant_id = operator.tenant_id
    start = _parse_history_cursor(tenant_id, cursor)
    async with get_sessionmaker()() as session:
        await _number_older_writes(session, tenant_id)
        position, window_gap = await _history_start(session, tenant_id, start)
        rows = await _scan(
            session,
            tenant_id,
            after=position.after_seq,
            upper=position.watermark,
            limit=limit,
        )
    gap = _scan_gap(position.after_seq, position.watermark, rows, limit) or window_gap
    next_cursor = None
    if len(rows) == limit:
        last = cast("int", rows[-1].seq)
        next_cursor = encode_history_cursor(
            tenant_id,
            HistoryPosition(watermark=position.watermark, after_seq=last, since=position.since),
        )
    return {
        "events": _serialize(rows, flt, since=position.since),
        "next_cursor": next_cursor,
        "watch_checkpoint": encode_watch_checkpoint(tenant_id, position.watermark),
        "gap": gap,
    }


@dataclass
class _WatchScan:
    matched: list[StoredAnnouncement]
    after: int
    full: bool
    gap: dict[str, Any] | None


@dataclass(frozen=True)
class AnnouncementWatch:
    """What :func:`watch_announcement_rows` found.

    ``after`` is the new resume point (a sequence number); it moves past
    rows the filter hid. ``gap`` is set when retention removed rows or the
    checkpoint was ahead of the archive.
    """

    events: list[StoredAnnouncement]
    after: int
    gap: dict[str, Any] | None


async def _watch_scan(
    tenant_id: UUID, after: int, flt: AnnouncementFilter, seen: set[str]
) -> _WatchScan:
    """One archive scan after *after*; moves the checkpoint even when nothing matches."""
    async with get_sessionmaker()() as session:
        await _number_older_writes(session, tenant_id)
        watermark = await archive_watermark(session, tenant_id)
        if after > watermark:
            return _WatchScan(
                [],
                watermark,
                False,
                _gap(
                    "checkpoint_ahead",
                    None,
                    "the checkpoint is ahead of the archive; watching restarts at its current end.",
                ),
            )
        rows = await _scan(session, tenant_id, after=after, upper=watermark, limit=WATCH_SCAN_LIMIT)
    gap = _scan_gap(after, watermark, rows, WATCH_SCAN_LIMIT)
    full = len(rows) == WATCH_SCAN_LIMIT
    new_after = cast("int", rows[-1].seq) if full else watermark
    return _WatchScan(_matching(rows, flt, seen), new_after, full, gap)


class _StreamWake:
    """The Valkey wake-up for one watch call; falls back to polling on failure."""

    def __init__(self, operator: Operator) -> None:
        self._operator = operator
        self._key = stream_key(operator.tenant_id)
        self._last_id: str | None = None
        self._failed = False

    def _fail(self, exc: Exception) -> None:
        self._failed = True
        _log.warning(
            "broadcast_watch_wake_failed",
            error_class=type(exc).__name__,
            tenant_id=str(self._operator.tenant_id),
        )

    async def mark(self) -> None:
        """Remember the stream's newest entry id, before the archive scan."""
        if self._failed:
            return
        try:
            entries = await get_broadcast_client().xrevrange(self._key, count=1)
        except Exception as exc:
            self._fail(exc)
            return
        # ``decode_responses=True`` on the broadcast client makes the id ``str``.
        self._last_id = str(entries[0][0]) if entries else "0-0"

    async def wait(self, remaining: float) -> None:
        """Block until a newer stream entry, *remaining* seconds pass, or a poll tick."""
        if self._failed or self._last_id is None:
            await asyncio.sleep(min(WATCH_POLL_SECONDS, remaining))
            return
        try:
            await get_broadcast_blocking_client().xread(
                {self._key: self._last_id}, count=1, block=max(1, int(remaining * 1000))
            )
        except asyncio.CancelledError:
            _log.info(
                "broadcast_watch_cancelled",
                stream_key=self._key,
                operator_sub=self._operator.sub,
            )
            raise
        except Exception as exc:
            self._fail(exc)
            await asyncio.sleep(min(WATCH_POLL_SECONDS, remaining))


async def watch_announcement_rows(
    operator: Operator,
    *,
    after: int,
    flt: AnnouncementFilter,
    timeout_ms: int,
) -> AnnouncementWatch:
    """Wait up to *timeout_ms* for announcements stored after sequence *after*.

    Scans the archive first; returns at once when it finds a matching
    event, a full scan page, or a gap. Otherwise waits for a stream
    wake-up (or a poll tick) and scans again, until the time is up. DB
    errors propagate (fail-loud).
    """
    tenant_id = operator.tenant_id
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000
    wake = _StreamWake(operator)
    seen: set[str] = set()
    while True:
        await wake.mark()
        scan = await _watch_scan(tenant_id, after, flt, seen)
        after = scan.after
        remaining = deadline - loop.time()
        if scan.matched or scan.full or scan.gap is not None or remaining <= 0:
            break
        await wake.wait(remaining)
    return AnnouncementWatch(events=scan.matched, after=after, gap=scan.gap)


async def watch_announcements(
    operator: Operator,
    *,
    checkpoint: str,
    flt: AnnouncementFilter,
    timeout_ms: int,
) -> dict[str, Any]:
    """Wait up to *timeout_ms* for announcements after *checkpoint*.

    See :func:`watch_announcement_rows` for the wait. Returns
    ``{events, next_cursor, gap}``; ``next_cursor`` is the new watch
    checkpoint. Raises :class:`InvalidArchiveCursorError` for a bad
    checkpoint; DB errors propagate (fail-loud).
    """
    tenant_id = operator.tenant_id
    after = decode_watch_checkpoint(checkpoint, tenant_id)
    found = await watch_announcement_rows(operator, after=after, flt=flt, timeout_ms=timeout_ms)
    return {
        "events": [dump_event_wire(item.event) for item in found.events],
        "next_cursor": encode_watch_checkpoint(tenant_id, found.after),
        "gap": found.gap,
    }


@dataclass(frozen=True)
class LatestAnnouncements:
    """The newest stored announcements, oldest first, and the tenant's watermark."""

    events: list[StoredAnnouncement]
    watermark: int


async def latest_announcements(
    operator: Operator,
    *,
    limit: int,
    since: datetime | None = None,
    flt: AnnouncementFilter | None = None,
) -> LatestAnnouncements:
    """Read the newest *limit* stored announcements, oldest first.

    *since* (optional) drops rows stored before that time. *flt* is
    applied after the read, so the result can hold fewer than *limit*
    events. ``watermark`` is the tenant's highest sequence number at the
    time of the read: a watcher that starts there misses nothing after
    this read. DB errors propagate (fail-loud).
    """
    tenant_id = operator.tenant_id
    async with get_sessionmaker()() as session:
        await _number_older_writes(session, tenant_id)
        watermark = await archive_watermark(session, tenant_id)
        stmt = (
            select(AgentAnnouncement)
            .where(
                AgentAnnouncement.tenant_id == tenant_id,
                AgentAnnouncement.seq <= watermark,
            )
            .order_by(AgentAnnouncement.seq.desc())
            .limit(limit)
        )
        if since is not None:
            stmt = stmt.where(AgentAnnouncement.created_at >= since)
        rows = list((await session.execute(stmt)).scalars().all())
    rows.reverse()
    return LatestAnnouncements(
        events=_matching(rows, flt or AnnouncementFilter()), watermark=watermark
    )


async def first_seq_since(operator: Operator, since: datetime) -> int:
    """Return the resume point (a sequence number) just before *since*.

    A watcher that starts there sees every announcement stored at or
    after *since*. When none exists it is the current watermark.
    """
    tenant_id = operator.tenant_id
    async with get_sessionmaker()() as session:
        await _number_older_writes(session, tenant_id)
        position, _gap = await _history_start(session, tenant_id, since)
    return position.after_seq
