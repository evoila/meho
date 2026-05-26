# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""In-flight ``agent_run`` reaper — expired-lease policy enforcement.

Initiative #804 (G11.3 Scheduler), Task #825 (T4). A scheduled or
event-fired agent run that gets killed mid-flight (pod restart, OOM,
network partition) must end in a terminal audited state -- never
silently lost. This module owns the *reclaim* half of that contract:
the trigger-firing path (T2 #823 / T3 #824) writes a lease + heartbeat
on the run row as it executes; the healthy worker bumps the lease
forward via :func:`meho_backplane.operations.agent_run.heartbeat`; the
reaper here scans for ``status='running' AND lease_expires_at < now()``
on a fixed cadence and applies :attr:`AgentRun.in_flight_policy`:

* **``fail_into_audit``** -- transition the row to ``failed`` with an
  interruption reason; write an internal audit row so operators can
  see which run was reaped. The next trigger tick fires a fresh run
  (the consumer doc ``agent-runtime-for-ops-spec.md`` §P2 explicitly
  accepts this outcome as the default policy).
* **``resume``** -- clear the lease columns (``lease_owner`` /
  ``lease_expires_at`` to NULL) so the dispatcher's next sweep
  re-claims the row and a fresh worker resumes from its recorded
  state. Write an internal audit row noting the resume so the
  interruption is durably visible. At-least-once semantics
  documented; the underlying agent runtime should be
  idempotent-friendly.

Why ``asyncio.create_task`` and not APScheduler 4.x
---------------------------------------------------

Same reasoning as :mod:`meho_backplane.topology.scheduler` and
:mod:`meho_backplane.memory.expiry`: APScheduler 4.x ships alpha
releases the maintainer documents as "should NOT be used in
production". The chassis follows a "no new substrate / minimal
dependencies" discipline (``CLAUDE.md``); a stdlib ``asyncio`` loop in
the lifespan is zero-dependency and is the same shape every other
lifespan-owned background sweeper uses.

Per-replica leader election (advisory lock)
-------------------------------------------

Two backplane replicas share one Postgres. Without coordination both
would reap the same expired-lease row in the same tick, racing to mark
it ``failed`` (idempotent under :func:`transition` -- the second move
hits the terminal-state guard -- but still wasteful). A single
session-level advisory lock on a fixed key, acquired non-blocking,
elects one replica per tick. The replica that loses the race skips
the sweep entirely; the next cadence is its chance to re-elect.

On SQLite (the dev / test path) the lock is a no-op: the test process
is single-replica so there is nothing to elect.

Failure isolation
-----------------

Each tick runs inside its own ``try`` / ``except`` so one bad row
(connector down mid-audit, transient DB blip, malformed payload)
never stalls the loop. The exception is logged with ``structlog.warning``
under ``agent_run_reaper_tick_failed`` and the loop waits for the next
cadence. A single missed tick is recoverable on the next cadence; a
crashed loop silently stops the reclaim contract until the next
process restart -- which is exactly what we are protecting against.
Loop survival dominates.

Per-row reclaim is similarly isolated: one row failing to reap
(audit-write error, transaction conflict) does not block the rest of
the batch. The next tick picks up whatever is still expired.

Reap batch size + per-tick LIMIT
--------------------------------

The reaper claims at most
:attr:`Settings.agent_run_reaper_max_per_tick` rows per tick so a
backlog (e.g. after a long outage where many leases expired
simultaneously) does not pin a Postgres backend for an arbitrary
amount of time. A large backlog is drained across several ticks; the
LIMIT is purely a fairness / latency knob, not a correctness one.

At-least-once semantics
-----------------------

The ``resume`` policy is at-least-once by construction: the original
worker may still be alive (network-partitioned, garbage-collected,
slow GC pause) and resume its own work after the reaper has cleared
the lease. The reaper's first defense is the
:func:`heartbeat`-conditional ``UPDATE`` (which raises
:class:`LeaseLostError` so a partitioned worker stops on its next
heartbeat); the agent runtime's second defense should be
idempotent-friendly tool calls (G11.1's design constraint). T4 does
not enforce idempotency at the substrate; it is the agent author's
contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentRun,
    AgentRunStatus,
    AuditLog,
    ScheduledTriggerInFlightPolicy,
)
from meho_backplane.operations.agent_run import (
    IllegalTransitionError,
    release_lease,
    transition,
)
from meho_backplane.settings import get_settings

__all__ = [
    "AGENT_RUN_REAPER_INTERRUPTION_REASON",
    "start_agent_run_reaper",
    "stop_agent_run_reaper",
]


_log = structlog.get_logger(__name__)

#: The fixed advisory-lock key the reaper holds during a tick. Same
#: hashing shape :mod:`meho_backplane.topology.scheduler` uses; a single
#: scalar (no per-row key) because the reaper is a singleton sweep, not
#: a per-row claimer. Computed once at module import for cheap reuse.
_REAPER_ADVISORY_LOCK_KEY: int = (
    int.from_bytes(
        hashlib.blake2b(b"agent_run_reaper:v1", digest_size=8).digest(),
        "big",
    )
    & 0x7FFF_FFFF_FFFF_FFFF
)

#: The synthetic operator ``sub`` recorded on reaper-driven audit rows.
#: A stable prefix makes reaper events filterable in audit queries and
#: distinct from operator-driven ones; mirrors the convention
#: :mod:`meho_backplane.topology.scheduler` (``system:topology-scheduler``)
#: and :mod:`meho_backplane.memory.expiry` (the internal-audit helper)
#: use.
_SYSTEM_OPERATOR_SUB = "system:agent-run-reaper"

#: Audit ``method`` for reaper writes. Internal events are not HTTP;
#: the chassis-wide convention is ``INTERNAL`` so audit filters can
#: easily exclude them from operator activity.
_AUDIT_METHOD = "INTERNAL"

#: Audit ``path`` for reaper writes. Stable identifier so operators
#: can grep / dashboard on it; distinct per policy outcome so the
#: filter is precise.
_AUDIT_PATH_FAIL = "internal/agent-run/reaper/fail-into-audit"
_AUDIT_PATH_RESUME = "internal/agent-run/reaper/clear-for-resume"

#: The human-readable ``error`` recorded on a ``failed`` run reaped by
#: the ``fail_into_audit`` policy. Stable phrasing so dashboards and
#: alerting can match on it; the audit row carries the lease metadata.
AGENT_RUN_REAPER_INTERRUPTION_REASON = (
    "interrupted: lease expired -- worker died mid-flight (reaped by agent_run_reaper)"
)


async def _try_advisory_lock(session: AsyncSession, key: int) -> bool:
    """Acquire a session-level PG advisory lock; ``True`` on non-PG.

    Returns ``True`` when the lock is held (or the dialect has no
    advisory locks, i.e. the single-replica SQLite test path) and the
    caller should proceed with the sweep; ``False`` when another
    replica holds it and this tick should be skipped.

    Same shape as
    :func:`meho_backplane.topology.scheduler._try_advisory_lock` --
    duplicated rather than imported so the reaper does not pull in
    the topology package (different feature surface, different
    lifespan ordering).
    """
    conn = await session.connection()
    if conn.dialect.name != "postgresql":
        return True
    locked = await session.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
    return bool(locked)


async def _release_advisory_lock(session: AsyncSession, key: int) -> None:
    """Release the session-level PG advisory lock; no-op on non-PG.

    The advisory lock is released explicitly at the end of every tick
    so the connection can be returned to the pool clean. PG would
    release it on session close anyway, but the asyncpg pool may
    reuse the connection before close.
    """
    conn = await session.connection()
    if conn.dialect.name != "postgresql":
        return
    await session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})


def _stage_audit_row(
    *,
    session: AsyncSession,
    operator_sub: str,
    tenant_id: uuid.UUID,
    method: str,
    path: str,
    payload: dict[str, object],
    agent_session_id: uuid.UUID,
) -> None:
    """Stage an :class:`AuditLog` row in *session*; do not commit.

    The reaper's audit row must commit in the *same transaction* as the
    lifecycle transition (or the lease release) so a crash between the
    two cannot leave a reaped run without an audit row. The chassis's
    shared writers (:func:`meho_backplane.audit._write_audit_row`,
    :func:`meho_backplane.memory.audit.write_internal_audit_row`) both
    open their own session and commit -- correct for the chassis HTTP
    path and for the memory sweeper (which has already committed its
    deletes by the time it audits), but wrong for the reaper, where
    the lifecycle transition + the audit row must commit atomically.

    This staging helper sits next to its one caller and writes the row
    via :meth:`AsyncSession.add` (no autoflush -- the outer
    :func:`session.commit` in :func:`_run_one_tick` is the single
    flush + commit boundary).

    The ``duration_ms`` column is :class:`Numeric`; the reaper has no
    per-row wall-clock to record (the relevant duration is the *lease*
    expiry, captured in the payload), so we record ``0`` -- a stable
    value the audit-query layer can recognise as "internal
    instantaneous event". Mirrors the convention the memory-expiry
    sweeper uses for its per-tenant audit rows.
    """
    session.add(
        AuditLog(
            id=uuid.uuid4(),
            occurred_at=datetime.now(UTC),
            operator_sub=operator_sub,
            tenant_id=tenant_id,
            method=method,
            path=path,
            status_code=200,
            duration_ms=Decimal("0"),
            payload=payload,
            agent_session_id=agent_session_id,
        )
    )


async def _reap_one_row(
    session: AsyncSession,
    row: AgentRun,
    *,
    now: datetime,
) -> tuple[str, str]:
    """Apply the row's in-flight policy. Return ``(policy, outcome)``.

    Called inside the tick's per-row ``try`` block so a single bad row
    cannot stall the rest of the batch. The session commit is the
    caller's responsibility -- this function only stages the
    transition + audit so the caller can group multiple rows in one
    transaction if it wants.

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        row: The expired-lease :class:`AgentRun` being reaped.
        now: The tick's "now" -- threaded in so all rows in one tick
            share a single timestamp (cleaner audit + easier test
            assertions).

    Returns:
        ``(policy_value, outcome_label)`` -- ``outcome_label`` is
        ``"failed"`` for ``fail_into_audit`` and ``"cleared"`` for
        ``resume``. Used for the structured log line.
    """
    policy_value = row.in_flight_policy
    run_id = row.id
    tenant_id = row.tenant_id
    prior_owner = row.lease_owner
    prior_expires_at = row.lease_expires_at

    # Payload shape mirrors the audit writer's convention: stable
    # field names so dashboards can index on them; the prior lease
    # metadata is captured for forensics (which worker died, when
    # the lease should have ended).
    payload: dict[str, object] = {
        "run_id": str(run_id),
        "tenant_id": str(tenant_id),
        "policy": policy_value,
        "prior_lease_owner": prior_owner,
        "prior_lease_expires_at": (
            prior_expires_at.isoformat() if prior_expires_at is not None else None
        ),
        "reaped_at": now.isoformat(),
    }

    if policy_value == ScheduledTriggerInFlightPolicy.RESUME.value:
        # Clear the lease so the next dispatcher sweep can re-claim
        # the row. Status stays ``running`` -- the dispatcher's claim
        # query already filters on ``lease_owner IS NULL`` for
        # resume-eligible work (T2 / T3 will wire that filter when
        # they fire runs). We do NOT transition status here; the
        # row's current state IS the resume point.
        await release_lease(session, row)
        _stage_audit_row(
            session=session,
            operator_sub=_SYSTEM_OPERATOR_SUB,
            tenant_id=tenant_id,
            method=_AUDIT_METHOD,
            path=_AUDIT_PATH_RESUME,
            payload=payload,
            agent_session_id=run_id,
        )
        return policy_value, "cleared"

    # fail_into_audit -- the conservative default. Mark the row
    # ``failed`` with a stable interruption reason; ``transition()``
    # clears the lease as part of the terminal-state side effect.
    # Status guard: the row could have transitioned between the
    # claim query and this write (e.g. an operator cancel landed) --
    # ``transition()`` raises :class:`IllegalTransitionError` on a
    # bad edge; the outer per-row ``try`` catches and logs.
    row.error = AGENT_RUN_REAPER_INTERRUPTION_REASON
    await transition(session, row, AgentRunStatus.FAILED)
    _stage_audit_row(
        session=session,
        operator_sub=_SYSTEM_OPERATOR_SUB,
        tenant_id=tenant_id,
        method=_AUDIT_METHOD,
        path=_AUDIT_PATH_FAIL,
        payload=payload,
        agent_session_id=run_id,
    )
    return policy_value, "failed"


async def _run_one_tick() -> None:
    """One sweep: claim expired-lease rows, apply policy, audit.

    Two-phase shape:

    1. Acquire the single advisory lock (skip the tick on PG if
       another replica won).
    2. Select up to ``max_per_tick`` expired-lease ``running`` rows;
       per-row, apply the policy in its own ``try`` so a bad row
       does not stall the batch.

    Commits once at the end so the entire tick's transitions + audit
    rows land atomically. A per-row commit would force a per-row
    transaction, which on a 100-row backlog is 100 round-trips; a
    single commit per tick is the same correctness with N=1.
    """
    tick_started = time.perf_counter()
    now = datetime.now(UTC)
    settings = get_settings()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if not await _try_advisory_lock(session, _REAPER_ADVISORY_LOCK_KEY):
            _log.debug("agent_run_reaper_tick_skipped_lock_held")
            return
        try:
            # ``select ... where status='running' AND lease_expires_at < now``
            # -- the partial index drives this on PG. The LIMIT bounds
            # the per-tick work; a backlog is drained across multiple
            # ticks rather than pinning one backend.
            stmt = (
                select(AgentRun)
                .where(
                    AgentRun.status == AgentRunStatus.RUNNING.value,
                    AgentRun.lease_expires_at.is_not(None),
                    AgentRun.lease_expires_at < now,
                )
                .order_by(AgentRun.lease_expires_at.asc())
                .limit(settings.agent_run_reaper_max_per_tick)
            )
            result = await session.execute(stmt)
            expired_rows = list(result.scalars().all())
            if not expired_rows:
                _log.debug("agent_run_reaper_tick_clean")
                return

            outcomes: dict[str, int] = {"failed": 0, "cleared": 0}
            for row in expired_rows:
                try:
                    _policy, outcome = await _reap_one_row(session, row, now=now)
                    outcomes[outcome] = outcomes.get(outcome, 0) + 1
                except IllegalTransitionError:
                    # The row's status changed between the claim query
                    # and the reap (operator cancel, race with another
                    # reaper instance that bypassed the advisory lock,
                    # etc.). Skip this row -- the new status is
                    # already terminal and another writer owns the
                    # audit row for it.
                    _log.info(
                        "agent_run_reaper_row_skipped_status_changed",
                        run_id=str(row.id),
                    )
                except Exception:
                    # Per-row failure isolation -- log loud, continue.
                    _log.exception(
                        "agent_run_reaper_row_failed",
                        run_id=str(row.id),
                    )

            await session.commit()
            duration_ms = (time.perf_counter() - tick_started) * 1000.0
            _log.info(
                "agent_run_reaper_tick_done",
                reaped_total=len(expired_rows),
                failed=outcomes.get("failed", 0),
                cleared_for_resume=outcomes.get("cleared", 0),
                duration_ms=duration_ms,
            )
        finally:
            await _release_advisory_lock(session, _REAPER_ADVISORY_LOCK_KEY)


async def _reaper_loop() -> None:
    """The forever loop: sleep one cadence, sweep, repeat.

    Sleep-then-sweep (rather than sweep-then-sleep) so the first tick
    after process start does not race the rest of the startup work
    (eager engine init, embedding preload, typed-op registration). A
    single cadence delay after startup is the cleanest signal that all
    eager init has completed.

    Per-tick ``try`` / ``except`` guards mean a transient DB blip
    (connection lost, advisory-lock query failed) is logged and the
    loop continues to the next cadence. ``CancelledError`` propagates
    so lifespan shutdown can stop the task cleanly.
    """
    interval = get_settings().agent_run_reaper_tick_interval_seconds
    _log.info(
        "agent_run_reaper_started",
        interval_seconds=interval,
    )
    while True:
        await asyncio.sleep(get_settings().agent_run_reaper_tick_interval_seconds)
        try:
            await _run_one_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "agent_run_reaper_tick_failed",
                exc_info=True,
            )


def start_agent_run_reaper() -> asyncio.Task[None]:
    """Start the background reaper loop and return its task handle.

    Registered in :func:`meho_backplane.main.lifespan` behind the
    ``AGENT_RUN_REAPER_ENABLED`` setting. The returned task is
    cancelled on lifespan shutdown; the caller awaits the cancellation
    so the loop unwinds cleanly. Returning the task (rather than
    fire-and-forgetting) keeps a strong reference alive -- an
    un-referenced :class:`asyncio.Task` can be garbage-collected
    mid-flight, producing the "Task was destroyed but it is pending!"
    warnings the chassis bars under pytest-asyncio shutdown.
    """
    return asyncio.create_task(_reaper_loop(), name="agent-run-reaper")


async def stop_agent_run_reaper(task: asyncio.Task[None]) -> None:
    """Cancel the reaper task and await its unwind.

    Swallows the expected :class:`asyncio.CancelledError`; any other
    exception surfaced during unwind propagates so a broken shutdown
    is visible rather than silently swallowed. The shape mirrors
    :func:`meho_backplane.memory.expiry.stop_memory_expiry_sweeper`
    verbatim so future contributors find one disposal pattern across
    the lifespan-owned tasks.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
