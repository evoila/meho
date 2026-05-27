# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Drain loop -- claim + dispatch ``event_outbox`` rows (G11.3-T3 #824).

The lifespan-owned background ``asyncio`` task at the heart of the
event-subscription trigger. On each cadence (default 10s, settable via
``EVENT_DRAIN_TICK_INTERVAL_SECONDS``):

1. **Claim the process-wide advisory lock**
   (``pg_try_advisory_lock``) so only one replica's drain is running
   the tick body at a time. Mirrors the scheduler-loop precedent
   (:mod:`meho_backplane.scheduler.loop`).

2. **Scan + claim unprocessed rows** via
   ``SELECT ... WHERE processed_at IS NULL ORDER BY event_id LIMIT N
   FOR UPDATE SKIP LOCKED`` on PG so two concurrent claimers never
   receive the same row even with the advisory-lock guard removed.
   Stamp ``claimed_at`` + ``claimed_by`` for observability.

3. **Dispatch each row.** In v0.2 the subscription matcher
   (``scheduled_trigger`` rows of ``kind='event'`` whose
   ``event_filter`` matches the payload) is not yet built -- T5 #826's
   admin surface ships the trigger-creation path that populates such
   rows. The drain therefore stamps ``processed_at`` directly: the
   event is durably consumed even though no subscriber fires. When T5
   lands the matcher is folded in here (one ``SELECT`` against
   ``scheduled_trigger`` per drained event) without a migration.

4. **Release the advisory lock** in a ``finally`` so a crash mid-tick
   never strands the lock for the rest of the connection's life.

LISTEN/NOTIFY wake hint
=======================

Alongside the polled cadence, a parallel ``LISTEN`` task subscribes to
:data:`~meho_backplane.db.models.EVENT_OUTBOX_NOTIFY_CHANNEL`. A producer's
post-commit ``NOTIFY`` (:mod:`meho_backplane.events.outbox`) sets an
``asyncio.Event``; the drain's sleep races the cadence sleep against
that event so a fresh write wakes the loop in sub-second time. The
notification is **not durable** -- a notification sent while no
listener is connected is lost -- but that's fine because the drain
polls anyway. The hint trims tail latency from "next 10s tick" to
"sub-second" under normal operation.

Replica-safety
==============

Two replicas running this loop against the same Postgres see exactly
one of them holding the advisory lock at any instant. Even if the
advisory-lock claim were removed, ``SELECT FOR UPDATE SKIP LOCKED``
plus the conditional ``UPDATE`` claim (``WHERE processed_at IS NULL
AND event_id = :id``) guarantees single-processing across all
in-flight claimers.

Restart durability
==================

The outbox row carries the durable state. On restart:

* Unprocessed rows (``processed_at IS NULL``) are picked up by the
  next tick, ordered by ``event_id`` so the oldest-pending events
  drain first.
* An in-flight claim that crashed mid-dispatch (``claimed_at`` stamped
  but ``processed_at`` still NULL) is re-claimed by the next tick --
  the SKIP LOCKED predicate keys on the row lock (released on
  rollback), not on ``claimed_at``. ``claimed_by`` is overwritten
  with the new claimer's identity; the prior claim is visible in
  audit logs only.

Delivery semantics
==================

At-least-once. A dispatch that crashes between "marked processed" and
"side-effect committed" is acceptable; the v1 dispatch (mark
processed, no subscriber) is idempotent by construction. When the
T5 matcher lands the subscriber's dispatch will need to be idempotent
or the matcher will need to record a per-subscriber dedupe key in the
trigger's audit row. Documented as a follow-up.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_engine, get_sessionmaker
from meho_backplane.db.models import EVENT_OUTBOX_NOTIFY_CHANNEL, EventOutbox
from meho_backplane.settings import get_settings

__all__ = [
    "run_one_drain_tick",
    "start_event_drain",
    "stop_event_drain",
]

_log = structlog.get_logger(__name__)

#: 63-bit signed-int key for ``pg_try_advisory_lock``. Distinct from
#: the scheduler-loop key (:data:`~meho_backplane.scheduler.loop._SCHEDULER_ADVISORY_LOCK_KEY`)
#: so the two loops can run concurrently without one starving the
#: other -- both lifespan-owned tasks, one PG advisory-lock slot each.
_EVENT_DRAIN_ADVISORY_LOCK_KEY: int = 0x4D45_484F_4556_5442  # "MEHOEVTB"

#: Maximum rows the drain claims per tick. Bounds per-tick work under
#: a burst (a connector that emits 1000 alerts in one second still
#: drains over 10 ticks, ~100ms each, rather than blocking the loop
#: for a full minute). 100 is generous for the typical "dozens per
#: minute" outbox volume the consumer doc anticipates.
_DRAIN_BATCH_LIMIT: int = 100


def _claimer_identity() -> str:
    """Compute a stable per-process identifier for ``claimed_by``.

    ``"<hostname>:<pid>"`` -- visible from PG diagnostics, no PII, no
    secret material. Operators chasing a stuck claim can map back to
    the offending pod / process from this stamp.
    """
    return f"{socket.gethostname()}:{os.getpid()}"


async def _try_advisory_lock(session: AsyncSession, key: int) -> bool:
    """Acquire the process-wide PG advisory lock; ``True`` on non-PG."""
    conn = await session.connection()
    if conn.dialect.name != "postgresql":
        return True
    locked = await session.scalar(
        text("SELECT pg_try_advisory_lock(:k)"),
        {"k": key},
    )
    return bool(locked)


async def _advisory_unlock(session: AsyncSession, key: int) -> None:
    """Release the advisory lock; no-op on non-PG dialects."""
    conn = await session.connection()
    if conn.dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_unlock(:k)"),
        {"k": key},
    )


async def _claim_unprocessed(
    session: AsyncSession,
    *,
    limit: int,
) -> list[EventOutbox]:
    """Return up to *limit* unprocessed events, locked for this tx.

    PG path: ``SELECT ... WHERE processed_at IS NULL ORDER BY event_id
    LIMIT N FOR UPDATE SKIP LOCKED``. The row locks are released on
    the caller's commit / rollback. The ordering is by ``event_id``
    (the BIGSERIAL primary key) so the oldest-pending events drain
    first -- a backlog after an outage drains in age order.

    SQLite path: the locking clauses no-op; the test path relies on
    the conditional UPDATE in :func:`_mark_processed` for single-
    processing across two in-process drain instances sharing the
    same connection pool.
    """
    conn = await session.connection()
    stmt = (
        select(EventOutbox)
        .where(EventOutbox.processed_at.is_(None))
        .order_by(EventOutbox.event_id.asc())
        .limit(limit)
    )
    if conn.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _stamp_claim(
    session: AsyncSession,
    rows: list[EventOutbox],
    *,
    now: datetime,
    claimed_by: str,
) -> None:
    """Stamp ``claimed_at`` / ``claimed_by`` on each claimed row.

    Bulk UPDATE rather than per-row so the round-trip cost stays
    linear in the batch size. The claim stamp is purely observational
    -- the SKIP LOCKED row lock is what guarantees exclusivity --
    so a partial stamp (some rows stamped, the tick crashed before
    flushing) is benign.
    """
    if not rows:
        return
    event_ids = [r.event_id for r in rows]
    await session.execute(
        update(EventOutbox)
        .where(EventOutbox.event_id.in_(event_ids))
        .values(claimed_at=now, claimed_by=claimed_by)
    )
    # Refresh local rows so callers / logs see the new fields without
    # a re-read.
    for r in rows:
        r.claimed_at = now
        r.claimed_by = claimed_by


async def _mark_processed(
    session: AsyncSession,
    row: EventOutbox,
    *,
    now: datetime,
) -> bool:
    """Conditional UPDATE that marks one row processed exactly once.

    ``WHERE event_id = :id AND processed_at IS NULL`` so a parallel
    drain that somehow claimed the same row (advisory-lock bypassed,
    SKIP LOCKED race) finds zero rows on its own attempt. The
    conditional shape is the single-processing enforcement on the
    SQLite test path.

    Returns ``True`` when this caller's UPDATE landed the stamp,
    ``False`` when another drainer beat it to the row.
    """
    result = await session.execute(
        update(EventOutbox)
        .where(
            EventOutbox.event_id == row.event_id,
            EventOutbox.processed_at.is_(None),
        )
        .values(processed_at=now)
    )
    rowcount: int = result.rowcount  # type: ignore[attr-defined]
    if rowcount == 0:
        return False
    row.processed_at = now
    return True


async def _dispatch_event(
    session: AsyncSession,
    row: EventOutbox,
    *,
    now: datetime,
) -> bool:
    """Dispatch one event -- v0.2 stamps processed; matcher TBD (T5).

    The v0.2 dispatch path is a no-op subscriber match: the event is
    consumed (``processed_at`` stamped) but no agent run fires
    because the subscription junction (``scheduled_trigger`` rows of
    ``kind='event'`` whose ``event_filter`` matches the payload) has
    no admin-surface path to populate it until T5 #826 lands.

    When T5 ships, this function gains a ``SELECT`` against
    ``scheduled_trigger`` (``WHERE kind='event' AND status='active'
    AND tenant_id = row.tenant_id``) and a JSONB containment match
    against ``event_filter``. For each matching trigger, the function
    fires the agent via the same :class:`AgentInvoker.run_scheduled`
    path the scheduler loop uses (Hard rule: subscribers are
    idempotent or carry a dedupe key in their audit row).

    Returns ``True`` when the event was successfully dispatched and
    marked processed; ``False`` when another drainer beat us to it.
    """
    # v0.2 no-op match (T5 follow-up wires the junction here).
    return await _mark_processed(session, row, now=now)


async def run_one_drain_tick() -> int:
    """Execute one drain tick. Returns the number of events processed.

    Public so tests can drive a deterministic single-tick without the
    cadence sleep.
    """
    sessionmaker = get_sessionmaker()
    processed = 0
    async with sessionmaker() as session:
        locked = await _try_advisory_lock(
            session,
            _EVENT_DRAIN_ADVISORY_LOCK_KEY,
        )
        if not locked:
            return 0
        try:
            now = datetime.now(UTC)
            rows = await _claim_unprocessed(session, limit=_DRAIN_BATCH_LIMIT)
            if not rows:
                return 0
            await _stamp_claim(
                session,
                rows,
                now=now,
                claimed_by=_claimer_identity(),
            )
            for row in rows:
                try:
                    dispatched = await _dispatch_event(session, row, now=now)
                    if dispatched:
                        processed += 1
                except Exception:
                    # Per-row isolation: one bad row never stalls the
                    # tick. The row stays unprocessed (next tick
                    # retries); the SKIP LOCKED row lock is released
                    # on session commit.
                    _log.exception(
                        "event_drain_dispatch_failed",
                        event_id=row.event_id,
                    )
        finally:
            await _advisory_unlock(session, _EVENT_DRAIN_ADVISORY_LOCK_KEY)
            await session.commit()
    return processed


# ---------------------------------------------------------------------------
# LISTEN/NOTIFY wake hint
# ---------------------------------------------------------------------------


async def _listen_for_notify(wake: asyncio.Event) -> None:
    """Listen for ``NOTIFY`` and set *wake* when one arrives.

    Subscribes a long-lived asyncpg connection to
    :data:`~meho_backplane.db.models.EVENT_OUTBOX_NOTIFY_CHANNEL` and
    sets the *wake* event on every notification. The drain's
    cadence-sleep races against ``wake.wait()`` so a fresh notify
    short-circuits the sleep and runs the tick immediately.

    The connection is borrowed from the SQLAlchemy engine pool's raw
    asyncpg side. On a non-PG dialect (the SQLite unit-test path)
    there is no NOTIFY mechanism, so this task is a no-op: it
    immediately returns and the drain falls back to pure polling.

    The listener never raises out of the task body -- a failed
    listener degrades the drain to polling-only (which is still
    durable), not a crashed background task.
    """
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        # No NOTIFY on SQLite; the drain falls back to polling-only.
        return
    try:
        # Borrow a raw asyncpg connection from the engine pool for
        # the lifetime of the lifespan task. ``run_sync`` exposes the
        # sync DBAPI connection, but asyncpg specifically needs the
        # async API; the SQLAlchemy AsyncAdaptedConnection wraps it
        # and exposes ``driver_connection`` for direct access.
        async with engine.connect() as conn:
            raw = await conn.get_raw_connection()
            asyncpg_conn = raw.driver_connection
            if asyncpg_conn is None:
                # The driver connection is unreachable (a non-asyncpg
                # adapter, or a pooler that disallows raw access);
                # degrade to polling-only.
                return

            def _on_notify(
                _connection: object,
                _pid: int,
                _channel: str,
                _payload: str,
            ) -> None:
                # The callback runs on asyncpg's event loop; set the
                # event from the same loop so the drain (running on
                # the lifespan loop, which is the *same* loop) sees
                # the wake immediately.
                wake.set()

            await asyncpg_conn.add_listener(
                EVENT_OUTBOX_NOTIFY_CHANNEL,
                _on_notify,
            )
            # Park forever; the lifespan cancellation surfaces as
            # CancelledError, which the outer task handler swallows.
            try:
                await asyncio.Event().wait()
            finally:
                with contextlib.suppress(Exception):
                    await asyncpg_conn.remove_listener(
                        EVENT_OUTBOX_NOTIFY_CHANNEL,
                        _on_notify,
                    )
    except asyncio.CancelledError:
        raise
    except Exception:
        # NOTIFY is a latency hint, not durability. A failed listener
        # degrades to polling-only; the drain still drains.
        _log.warning("event_drain_listener_failed", exc_info=True)


# ---------------------------------------------------------------------------
# Lifespan task entry points
# ---------------------------------------------------------------------------


async def _drain_loop() -> None:
    """The forever loop: sleep one cadence (or wake on NOTIFY), tick, repeat.

    Sleep-first so the first tick after process start is delayed by
    one cadence -- letting the rest of the lifespan eager-init
    complete before the loop touches the DB. Each tick races the
    cadence-sleep against the NOTIFY ``wake`` event so a fresh
    publish wakes the loop in sub-second time.
    """
    interval = get_settings().event_drain_tick_interval_seconds
    wake = asyncio.Event()
    listener_task = asyncio.create_task(
        _listen_for_notify(wake),
        name="event-drain-listener",
    )
    _log.info("event_drain_started", interval_seconds=interval)
    try:
        while True:
            # Race the cadence sleep against the NOTIFY wake event.
            # The first to fire short-circuits the sleep; clear the
            # event so the next tick is paced by the cadence again
            # (a single NOTIFY drains the wake, not every NOTIFY
            # afterwards burning ticks).
            sleep_task = asyncio.create_task(asyncio.sleep(interval))
            wake_task = asyncio.create_task(wake.wait())
            try:
                done, _pending = await asyncio.wait(
                    {sleep_task, wake_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                # Cancel whichever task didn't win the race so we
                # don't leak it across ticks.
                for t in (sleep_task, wake_task):
                    if not t.done():
                        t.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await t
            if wake_task in done:
                wake.clear()
            try:
                await run_one_drain_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.warning("event_drain_tick_failed", exc_info=True)
    finally:
        listener_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await listener_task


def start_event_drain() -> asyncio.Task[None]:
    """Start the background drain loop; return its task handle.

    Registered in :func:`meho_backplane.main.lifespan` behind the
    ``EVENT_DRAIN_ENABLED`` setting. The returned task is cancelled on
    lifespan shutdown; the caller awaits the cancellation so the loop
    unwinds cleanly. Returning the task (rather than fire-and-forget)
    keeps a strong reference alive -- an un-referenced
    :class:`asyncio.Task` can be GC'd mid-flight, producing the "Task
    was destroyed but it is pending!" warnings pytest-asyncio shutdown
    fails on.
    """
    return asyncio.create_task(_drain_loop(), name="event-drain-loop")


async def stop_event_drain(task: asyncio.Task[None]) -> None:
    """Cancel the drain task and await its unwind.

    Swallows the expected :class:`asyncio.CancelledError`; any other
    exception during unwind propagates so a broken shutdown is visible
    rather than silently swallowed. Mirrors
    :func:`~meho_backplane.scheduler.loop.stop_scheduler` verbatim so
    future contributors find one disposal pattern across every
    lifespan-owned task.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
