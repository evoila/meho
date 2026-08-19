# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Drain loop -- claim + dispatch ``event_outbox`` rows (G11.3-T3 #824).

The lifespan-owned background ``asyncio`` task at the heart of the
event-subscription trigger. On each cadence (default 10s, settable via
``EVENT_DRAIN_TICK_INTERVAL_SECONDS``):

1. **Claim the process-wide advisory lock**
   (``pg_try_advisory_lock``) so only one replica's drain is running
   the tick body at a time. Mirrors the scheduler-loop precedent
   (:mod:`meho_backplane.scheduler.loop`). The lock lives on a
   dedicated pinned connection
   (:func:`meho_backplane.db.advisory.advisory_lock`, #3010) —
   **advisory lock and unlock must run on the same connection**: the
   tick commits mid-lock (the claim stamp, each processed stamp), and a
   lock taken on the work session would strand on the pooled connection
   the first commit releases, silently skipping every later drain tick
   that draws a different connection.

2. **Scan + claim unprocessed rows** via
   ``SELECT ... WHERE processed_at IS NULL ORDER BY event_id LIMIT N
   FOR UPDATE SKIP LOCKED`` on PG so two concurrent claimers never
   receive the same row even with the advisory-lock guard removed.
   Stamp ``claimed_at`` + ``claimed_by`` for observability.

3. **Dispatch each row** through the subscription matcher
   (:mod:`meho_backplane.events.matcher`): for each claimed event it
   fires every active ``kind='event'`` :class:`~meho_backplane.db.models.ScheduledTrigger`
   whose ``event_filter`` is contained by the payload (``payload @>
   event_filter``), then stamps ``processed_at``. An event that matches
   no subscriber is still durably consumed (stamped, no fire, no log
   noise).

4. **Release the advisory lock** on the same pinned connection that
   acquired it (the helper's ``finally``) so a crash mid-tick never
   strands the lock for the rest of the connection's life.

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

At-least-once. A fired subscriber's ``agent_run`` commits in its own
transaction, *before* the drain stamps ``processed_at`` -- so a crash
between the two leaves the event unprocessed and the next tick
re-matches it. Double-firing is prevented by the matcher's
``event:{event_id}:{trigger_id}`` work_ref dedupe
(:mod:`meho_backplane.events.matcher`), not by exactly-once delivery:
the redelivered event finds the first delivery's run and skips it. The
claim stamps are committed *before* the per-event fire so the drain
holds no open write across a subscriber's run-row commit (which lands
in its own session); the drain advisory lock plus the ``processed_at
IS NULL`` conditional stay the single-processing guards.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.advisory import advisory_lock
from meho_backplane.db.engine import get_engine, get_sessionmaker
from meho_backplane.db.models import EVENT_OUTBOX_NOTIFY_CHANNEL, EventOutbox
from meho_backplane.metrics import note_loop_tick
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    # Type-only: the concrete import (and the matcher import in
    # `_dispatch_event`) stay lazy so `events` -> `agent.invocation` ->
    # `operations.agent_run` -> `events.outbox` does not close an import cycle
    # at module-load time. Same lazy-import discipline the scheduler loop uses
    # for its Vault-credential helpers.
    from meho_backplane.agent.invocation import AgentInvoker

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
    invoker: AgentInvoker,
) -> bool:
    """Fire every matching subscription for one event, then mark it processed.

    The subscription matcher (:mod:`meho_backplane.events.matcher`) fires each
    active ``kind='event'`` trigger whose ``event_filter`` the payload contains
    (``payload @> event_filter``) via ``AgentInvoker.run_scheduled``. Firing
    runs first, in the matcher's own sessions (each run row commits
    independently), so this drain session holds no open write across it; the
    fired run's ``event:{event_id}:{trigger_id}`` work_ref makes a redelivered
    event idempotent. Marking processed happens last -- a crash after a fire
    but before the stamp re-delivers the event, and the work_ref dedupe skips
    the already-fired run. An event that matches no subscriber is stamped with
    no fire and no per-event log noise.

    Returns ``True`` when this drainer stamped ``processed_at``; ``False`` when
    another drainer beat it to the row (the conditional stamp matched zero
    rows).
    """
    # Lazy import: keeps `events` -> `events.matcher` -> `scheduler.loop` ->
    # `agent.invocation` off the module-load path (see the top-of-module
    # TYPE_CHECKING note). Cached after the first tick.
    from meho_backplane.events.matcher import fire_matching_triggers

    await fire_matching_triggers(row, invoker)
    return await _mark_processed(session, row, now=now)


async def run_one_drain_tick(invoker: AgentInvoker | None = None) -> int:
    """Execute one drain tick. Returns the number of events processed.

    Public so tests can drive a deterministic single-tick without the
    cadence sleep. The optional *invoker* override lets tests inject a
    deterministic :class:`AgentInvoker` over a ``FunctionModel`` so a
    subscription fire executes end-to-end without a real LLM call (mirrors
    :func:`meho_backplane.scheduler.loop.run_one_tick`).
    """
    if invoker is None:
        # Lazy import (see `_dispatch_event`): the default invoker is only
        # resolved at runtime, off the module-load path.
        from meho_backplane.agent.invocation import get_agent_invoker

        invoker = get_agent_invoker()
    processed = 0
    # The advisory lock lives on its own pinned connection (#3010): the
    # mid-tick commits below (claim stamps, per-row processed stamps)
    # each return the work session's connection to the pool, so a lock
    # taken on that session would migrate off it at the first commit and
    # strand -- silently skipping every later drain tick that draws a
    # different connection.
    async with advisory_lock(_EVENT_DRAIN_ADVISORY_LOCK_KEY, subsystem="event_drain") as locked:
        if not locked:
            _log.debug("event_drain_tick_skipped_lock_held")
            return 0
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
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
            # Commit the claim stamps before dispatching: a matched
            # subscription fires an agent run that commits in its own session,
            # and the drain must hold no open write across that commit (the
            # scheduler-fire precedent -- loop.py commits the trigger-state
            # write before dispatching). The advisory lock (pinned to its own
            # dedicated connection, which a work-session commit cannot touch)
            # plus the ``processed_at IS NULL`` conditional in
            # `_mark_processed` remain the single-processing guards once the
            # claim's row locks release here.
            await session.commit()
            for row in rows:
                try:
                    dispatched = await _dispatch_event(session, row, now=now, invoker=invoker)
                    if dispatched:
                        processed += 1
                    # Commit each event's processed stamp on its own so the
                    # next event's fire again finds no open write held.
                    await session.commit()
                except Exception:
                    # Per-row isolation: one bad row never stalls the tick. Roll
                    # back this row's partial work so the next row starts clean;
                    # the row stays unprocessed and the next tick retries it
                    # (the work_ref dedupe covers any subscriber already fired).
                    await session.rollback()
                    _log.exception(
                        "event_drain_dispatch_failed",
                        event_id=row.event_id,
                    )
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
            note_loop_tick("event_drain", interval)
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
