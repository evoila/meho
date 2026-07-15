# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Central dead-man's-switch sweeper for satellite runners (#2415, #2501).

A satellite runner that dies, wedges, or loses its network path must not
leave its workloads silently reporting last-known-good forever. This
module owns the *enforcement* half of the runner dead-man switch: an
interval-tick loop the FastAPI lifespan owns that flips a lapsed runner's
:class:`~meho_backplane.db.models.RunnerAssignmentRow` to a stale/unknown
marker (``stale_at``) once the runner's central-clock ``last_seen_at``
(stamped on every runner-plane request by
:func:`~meho_backplane.auth.runner_guard.assert_runner_scope`) falls
behind ``N x GATEWAY_LONGPOLL_MAX_WAIT_SECONDS``.

The stamp half lives in the runner guard (piggybacked on the existing
request cycle — no dedicated heartbeat endpoint, per the #1501 lesson: a
dedicated heartbeat loop can stay alive while the work loops are wedged).

Design moulds
-------------

* Interval-tick loop + start/stop pair: copied in shape from
  :mod:`meho_backplane.memory.expiry` (``_sweeper_loop`` /
  ``start_*`` / ``stop_*``) — sleep-then-tick, per-tick ``try`` /
  ``except`` so one bad tick never stalls the loop, ``CancelledError``
  propagates for a clean lifespan shutdown. Binding per #2415: the
  in-process interval-tick sweeper, **not** the DB-session-bound
  scheduler trigger loop.
* Lapse-then-flip tick body + advisory lock: moulded on the agent-run
  reaper (:mod:`meho_backplane.agent.reaper`) — a fixed non-blocking PG
  advisory lock elects one replica per tick (no-op on SQLite), the flip
  is per-row isolated, one internal audit row per flipped runner.
* Internal audit row: :func:`~meho_backplane.memory.audit.write_internal_audit_row`
  (opens its own session, commits) — mould parity with the memory sweeper.

Central-clock discipline
------------------------

The flip cutoff and the reported lapse are computed **only** from
:func:`datetime.now` in UTC and the central-stamped ``last_seen_at``.
No runner-reported timestamp participates in the staleness decision — a
runner cannot keep itself alive by lying about its clock.

Idempotency + multi-replica safety
-----------------------------------

Each candidate is flipped by a conditional ``UPDATE ... WHERE stale_at IS
NULL`` whose ``rowcount`` gates the audit write: the tick that wins the
flip (``rowcount == 1``) writes the audit row; a racing tick sees
``rowcount == 0`` and writes nothing. This makes an immediate second tick
a natural no-op and keeps "exactly one audit row per flip" true even when
the advisory lock is a no-op (the SQLite test path) or two replicas race.

Recovery
--------

Recovery is data-driven, never sweeper-driven. The sweeper only ever
*sets* ``stale_at``; :func:`clear_runner_stale` (called from the result-
ingest paths) is the only clear path. Runner-level derived staleness
clears the instant the runner's next request re-stamps ``last_seen_at``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
import structlog
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunnerAssignmentRow, RunnerPrincipal
from meho_backplane.gateway.queue import GATEWAY_LONGPOLL_MAX_WAIT_SECONDS
from meho_backplane.memory.audit import (
    INTERNAL_METHOD,
    SYSTEM_OPERATOR_SUB,
    write_internal_audit_row,
)
from meho_backplane.settings import get_settings

__all__ = [
    "GATEWAY_RUNNER_STALE_PATH",
    "clear_runner_stale",
    "start_gateway_deadman_sweeper",
    "stop_gateway_deadman_sweeper",
]

_log = structlog.get_logger(__name__)

#: Canonical internal-audit ``path`` for a dead-man flip. Defined here (the
#: module that owns the ``stale_at`` lifecycle) next to the sweeper, mould
#: parity with the reaper's local ``_AUDIT_PATH_*`` literals. Documented in
#: ``docs/codebase/satellite-runner.md`` as the forward reference for the
#: G8 audit-query consumers; ``stale_at IS NOT NULL`` maps to ``UNKNOWN`` in
#: #2416's five-state rollup (#2506).
GATEWAY_RUNNER_STALE_PATH: str = "gateway.runner.stale"

#: The fixed advisory-lock key the sweeper holds during a tick. Same
#: hashing shape the reaper / topology scheduler use; a single scalar (no
#: per-row key) because this is a singleton sweep, not a per-row claimer.
_DEADMAN_ADVISORY_LOCK_KEY: int = (
    int.from_bytes(
        hashlib.blake2b(b"gateway_deadman:v1", digest_size=8).digest(),
        "big",
    )
    & 0x7FFF_FFFF_FFFF_FFFF
)


def _threshold_seconds() -> int:
    """Central-clock staleness threshold: ``multiplier x unit``.

    The threshold is ``gateway_runner_stale_after_multiplier`` times
    :data:`~meho_backplane.gateway.queue.GATEWAY_LONGPOLL_MAX_WAIT_SECONDS`
    (30 s) — 3 x 30 s = 90 s by default. What it must clear is the runner's
    real idle cadence: the satellite runner is a sweep-then-sleep
    interval-tick loop (:func:`meho_backplane.runner.loop.run_one_tick`)
    that fetches its assignment every ``tick_interval_seconds`` (#2499's
    ``GET /checks/assignment``, default 60 s) — an authenticated
    runner-plane request that re-stamps ``last_seen_at`` — even when idle.
    There is no long-poll client on the runner, so the 30 s unit is a
    convenient multiplicand, not the runner's cadence. Invariant: keep
    ``multiplier x GATEWAY_LONGPOLL_MAX_WAIT_SECONDS >= runner
    tick_interval_seconds`` (90 s >= 60 s) or a healthy idle runner
    false-trips. Never re-hardcode the number here.
    """
    return get_settings().gateway_runner_stale_after_multiplier * GATEWAY_LONGPOLL_MAX_WAIT_SECONDS


def _as_utc(value: datetime) -> datetime:
    """Coerce a possibly-naive ``timestamptz`` read to UTC-aware.

    aiosqlite reads ``DateTime(timezone=True)`` columns back as naive
    (the PG driver returns aware); coerce so the lapse subtraction is
    well-defined on both dialects.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _try_advisory_lock(session: AsyncSession, key: int) -> bool:
    """Acquire a session-level PG advisory lock; ``True`` on non-PG.

    Returns ``True`` when the lock is held (or the dialect has no advisory
    locks, i.e. the single-replica SQLite test path) and the caller should
    proceed; ``False`` when another replica holds it and this tick should
    be skipped. Mould: :func:`meho_backplane.agent.reaper._try_advisory_lock`.
    """
    conn = await session.connection()
    if conn.dialect.name != "postgresql":
        return True
    locked = await session.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
    return bool(locked)


async def _release_advisory_lock(session: AsyncSession, key: int) -> None:
    """Release the session-level PG advisory lock; no-op on non-PG."""
    conn = await session.connection()
    if conn.dialect.name != "postgresql":
        return
    await session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})


async def _run_one_tick() -> None:
    """One sweep: flip lapsed runners' assignment rows, audit each flip.

    Two-phase shape (reaper mould):

    1. Acquire the single advisory lock (skip the tick on PG if another
       replica won).
    2. Select the assignment rows whose runner's ``last_seen_at`` is behind
       the central-clock cutoff and which are not already flipped, then
       flip each with a conditional ``UPDATE ... WHERE stale_at IS NULL``
       and collect the ones this tick actually won (``rowcount == 1``).

    Commits the flips once, then writes one internal audit row per won flip
    (the memory-sweeper ordering: mutate + commit, then audit). An audit-
    write failure is logged loud but never rolls back a flip.
    """
    tick_started = time.perf_counter()
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=_threshold_seconds())
    sessionmaker = get_sessionmaker()
    # (tenant_id, runner_name, lapse_seconds) for each flip this tick won.
    flipped: list[tuple[uuid.UUID, str, float]] = []
    async with sessionmaker() as session:
        if not await _try_advisory_lock(session, _DEADMAN_ADVISORY_LOCK_KEY):
            _log.debug("gateway_deadman_tick_skipped_lock_held")
            return
        try:
            candidate_stmt = (
                select(
                    RunnerAssignmentRow.id,
                    RunnerAssignmentRow.tenant_id,
                    RunnerAssignmentRow.runner_name,
                    RunnerPrincipal.last_seen_at,
                )
                .join(
                    RunnerPrincipal,
                    sa.and_(
                        RunnerPrincipal.tenant_id == RunnerAssignmentRow.tenant_id,
                        RunnerPrincipal.name == RunnerAssignmentRow.runner_name,
                    ),
                )
                .where(
                    RunnerPrincipal.last_seen_at < cutoff,
                    RunnerAssignmentRow.stale_at.is_(None),
                )
                .order_by(RunnerPrincipal.last_seen_at.asc())
            )
            candidates = (await session.execute(candidate_stmt)).all()
            for assignment_id, tenant_id, runner_name, last_seen_at in candidates:
                # Conditional flip: the ``stale_at IS NULL`` predicate makes
                # the audit gate exact even when the advisory lock is a
                # no-op (SQLite) or two replicas race -- only the tick whose
                # UPDATE matches a still-fresh row (rowcount == 1) audits.
                result = await session.execute(
                    update(RunnerAssignmentRow)
                    .where(
                        RunnerAssignmentRow.id == assignment_id,
                        RunnerAssignmentRow.stale_at.is_(None),
                    )
                    .values(stale_at=now)
                )
                # ``rowcount`` is only typed on the concrete ``CursorResult``
                # subclass an UPDATE produces at runtime, not on the
                # ``Result`` the ``AsyncSession.execute`` stub advertises --
                # the ignore mirrors the sibling ``gateway/queue.py``.
                won_flip: int = result.rowcount  # type: ignore[attr-defined]
                if won_flip == 1:
                    lapse_seconds = (now - _as_utc(last_seen_at)).total_seconds()
                    flipped.append((tenant_id, runner_name, lapse_seconds))
            await session.commit()
        finally:
            await _release_advisory_lock(session, _DEADMAN_ADVISORY_LOCK_KEY)

    if not flipped:
        _log.debug("gateway_deadman_tick_clean")
        return

    duration_ms = (time.perf_counter() - tick_started) * 1000.0
    for tenant_id, runner_name, lapse_seconds in flipped:
        try:
            await write_internal_audit_row(
                operator_sub=SYSTEM_OPERATOR_SUB,
                tenant_id=tenant_id,
                method=INTERNAL_METHOD,
                path=GATEWAY_RUNNER_STALE_PATH,
                status_code=200,
                duration_ms=duration_ms,
                payload={"runner": runner_name, "lapse_seconds": lapse_seconds},
            )
        except Exception:
            # An audit-write failure must not stall the tick (the flip is
            # already committed; we cannot roll it back from here). Surface
            # it loud so operators see it in logs.
            _log.exception(
                "gateway_deadman_audit_write_failed",
                tenant_id=str(tenant_id),
                runner=runner_name,
            )
    _log.info(
        "gateway_deadman_tick_done",
        flipped=len(flipped),
        threshold_seconds=_threshold_seconds(),
        duration_ms=duration_ms,
    )


async def clear_runner_stale(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    runner_name: str,
) -> None:
    """Clear a runner's dead-man flip marker on an accepted result ingestion.

    Recovery is data-driven, never sweeper-driven (#2501): an accepted
    result batch proves the runner is alive and reporting, so its
    ``runner_assignments.stale_at`` is reset to ``NULL``. The sweeper only
    ever *sets* ``stale_at``; this is the only clear path.

    Operates on the caller's session and does **not** commit — the caller's
    result-ingest transaction owns the commit. Idempotent: the
    ``stale_at IS NOT NULL`` predicate makes it a no-op ``UPDATE`` when the
    runner was never flipped, so the fresh-runner hot path pays nothing.
    """
    await session.execute(
        update(RunnerAssignmentRow)
        .where(
            RunnerAssignmentRow.tenant_id == tenant_id,
            RunnerAssignmentRow.runner_name == runner_name,
            RunnerAssignmentRow.stale_at.is_not(None),
        )
        .values(stale_at=None)
    )


async def _sweeper_loop() -> None:
    """The forever loop: sleep one cadence, sweep, repeat.

    Sleep-then-sweep (memory-expiry / reaper mould) so the first tick after
    process start does not race the rest of the startup work. Per-tick
    ``try`` / ``except`` guards mean a transient DB blip is logged and the
    loop continues; ``CancelledError`` propagates so lifespan shutdown can
    stop the task cleanly.
    """
    interval = get_settings().gateway_deadman_tick_interval_seconds
    _log.info(
        "gateway_deadman_sweeper_started",
        interval_seconds=interval,
        threshold_seconds=_threshold_seconds(),
    )
    while True:
        await asyncio.sleep(get_settings().gateway_deadman_tick_interval_seconds)
        try:
            await _run_one_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "gateway_deadman_tick_failed",
                exc_info=True,
            )


def start_gateway_deadman_sweeper() -> asyncio.Task[None]:
    """Start the background sweeper loop and return its task handle.

    Registered in :func:`meho_backplane.main.lifespan` behind
    ``GATEWAY_DEADMAN_ENABLED`` (default on — that is what "mandatory"
    means: central enforcement is on by default and a runner cannot opt
    out of heartbeating because the stamp is a request side effect). The
    returned task is cancelled on lifespan shutdown; the caller awaits the
    cancellation so the loop unwinds cleanly. Returning the task (rather
    than fire-and-forgetting) keeps a strong reference alive — an
    un-referenced :class:`asyncio.Task` can be garbage-collected mid-flight,
    producing the "Task was destroyed but it is pending!" warnings the
    chassis bars under pytest-asyncio shutdown.
    """
    return asyncio.create_task(_sweeper_loop(), name="gateway-deadman-sweeper")


async def stop_gateway_deadman_sweeper(task: asyncio.Task[None]) -> None:
    """Cancel the sweeper task and await its unwind.

    Swallows the expected :class:`asyncio.CancelledError`; any other
    exception surfaced during unwind propagates so a broken shutdown is
    visible. Shape mirrors
    :func:`meho_backplane.memory.expiry.stop_memory_expiry_sweeper` verbatim.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
