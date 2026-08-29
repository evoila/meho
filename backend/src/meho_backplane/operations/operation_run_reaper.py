# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""In-flight ``operation_run`` reaper — expired-lease reclaim (#3079).

Async governed dispatch: a governed op submitted in async mode executes on
a background task that keeps a lease on its ``operation_run`` row fresh via
a heartbeat sidecar. If that worker dies mid-flight (pod restart, OOM,
network partition) the heartbeat stops and the lease lapses. This module
owns the *reclaim* half of the "never silently lost" contract: it scans for
``status='running' AND lease_expires_at < now()`` on a fixed cadence and
drives each orphaned run to ``failed`` with a stable interruption reason,
writing an internal audit row in the same transaction.

Why a single fail-into-audit policy (no ``resume``)
---------------------------------------------------

Unlike the agent-run reaper (which offers a ``resume`` policy for
idempotent-friendly LLM loops), a governed operation can wrap a
non-idempotent vendor write. Re-dispatching a half-executed write on pod
death would double-execute it. So an orphaned operation run is **never**
re-dispatched -- it is driven to ``failed`` (an audited terminal state) and
an operator decides whether to re-submit. This is the conservative,
safe half of the #3079 acceptance criterion ("survives pod restart via
lease/reaper semantics **or** terminates into an audited terminal state --
never silently lost").

The loop/lock/failure-isolation discipline mirrors
:mod:`meho_backplane.agent.reaper` verbatim (advisory-lock leader election
per tick on a dedicated pinned connection, ``FOR UPDATE SKIP LOCKED`` claim,
per-row savepoint isolation, single commit per tick, per-tick ``try`` so a
transient blip never stalls the loop). See that module's docstring for the
full rationale; this one is the operation-run-shaped, single-policy trim.
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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.advisory import advisory_lock
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, OperationRun, OperationRunStatus
from meho_backplane.metrics import note_loop_tick
from meho_backplane.operations.operation_run import (
    IllegalOperationRunTransitionError,
    fail_run,
)
from meho_backplane.settings import get_settings

__all__ = [
    "OPERATION_RUN_REAPER_INTERRUPTION_REASON",
    "start_operation_run_reaper",
    "stop_operation_run_reaper",
]


_log = structlog.get_logger(__name__)

#: The fixed advisory-lock key the reaper holds during a tick. A single
#: scalar (no per-row key) because the reaper is a singleton sweep. Distinct
#: from the agent-run reaper's key so the two sweeps never contend.
_REAPER_ADVISORY_LOCK_KEY: int = (
    int.from_bytes(
        hashlib.blake2b(b"operation_run_reaper:v1", digest_size=8).digest(),
        "big",
    )
    & 0x7FFF_FFFF_FFFF_FFFF
)

#: The synthetic operator ``sub`` recorded on reaper-driven audit rows.
_SYSTEM_OPERATOR_SUB = "system:operation-run-reaper"

#: Audit ``method`` for reaper writes. Internal events are not HTTP.
_AUDIT_METHOD = "INTERNAL"

#: Audit ``path`` for reaper writes. Stable identifier for grep / dashboards.
_AUDIT_PATH_FAIL = "internal/operation-run/reaper/fail-into-audit"

#: The human-readable ``error`` recorded on a ``failed`` run reaped here.
#: Stable phrasing so dashboards / alerting can match on it.
OPERATION_RUN_REAPER_INTERRUPTION_REASON = (
    "interrupted: lease expired -- worker died mid-flight "
    "(reaped by operation_run_reaper; not re-dispatched)"
)


def _stage_audit_row(
    *,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    payload: dict[str, object],
) -> None:
    """Stage an :class:`AuditLog` row in *session*; do not commit.

    The reaper's audit row must commit in the *same transaction* as the
    lifecycle transition so a crash between the two cannot leave a reaped run
    without an audit row. ``run_id`` is set to the operation-run id (the
    audit-correlation column, migration ``0034``); ``duration_ms`` is ``0``
    (the relevant duration is the lease expiry, captured in the payload).
    """
    session.add(
        AuditLog(
            id=uuid.uuid4(),
            occurred_at=datetime.now(UTC),
            operator_sub=_SYSTEM_OPERATOR_SUB,
            tenant_id=tenant_id,
            method=_AUDIT_METHOD,
            path=_AUDIT_PATH_FAIL,
            status_code=200,
            duration_ms=Decimal("0"),
            payload=payload,
            run_id=run_id,
        )
    )


async def _reap_one_row(session: AsyncSession, row: OperationRun, *, now: datetime) -> None:
    """Drive one expired-lease run to ``failed`` + stage its audit row.

    ``fail_run`` transitions ``running`` -> ``failed`` (clearing the lease as
    the terminal side effect). A status guard is implicit: the row could
    have transitioned between the claim query and this write (an operator
    cancel landed) -- ``fail_run`` raises
    :class:`IllegalOperationRunTransitionError` on the bad edge; the caller's
    per-row ``try`` catches it.
    """
    payload: dict[str, object] = {
        "run_id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "op_id": row.op_id,
        "connector_id": row.connector_id,
        "prior_lease_owner": row.lease_owner,
        "prior_lease_expires_at": (
            row.lease_expires_at.isoformat() if row.lease_expires_at is not None else None
        ),
        "reaped_at": now.isoformat(),
    }
    await fail_run(session, row, error=OPERATION_RUN_REAPER_INTERRUPTION_REASON)
    _stage_audit_row(
        session=session,
        tenant_id=row.tenant_id,
        run_id=row.id,
        payload=payload,
    )


async def _run_one_tick() -> None:
    """One sweep: claim expired-lease running rows, fail them, audit.

    Acquire the single advisory lock (skip the tick on PG if another replica
    won); select up to ``max_per_tick`` expired-lease ``running`` rows;
    per-row, apply the policy in its own savepoint so a bad row does not
    stall the batch. Commit once at the end so the whole tick lands
    atomically.
    """
    tick_started = time.perf_counter()
    now = datetime.now(UTC)
    settings = get_settings()
    sessionmaker = get_sessionmaker()
    async with advisory_lock(_REAPER_ADVISORY_LOCK_KEY, subsystem="operation_run_reaper") as locked:
        if not locked:
            _log.debug("operation_run_reaper_tick_skipped_lock_held")
            return
        async with sessionmaker() as session:
            stmt = (
                select(OperationRun)
                .where(
                    OperationRun.status == OperationRunStatus.RUNNING.value,
                    OperationRun.lease_expires_at.is_not(None),
                    OperationRun.lease_expires_at < now,
                )
                .order_by(OperationRun.lease_expires_at.asc())
                .limit(settings.operation_run_reaper_max_per_tick)
                .with_for_update(skip_locked=True)
            )
            result = await session.execute(stmt)
            expired_rows = list(result.scalars().all())
            if not expired_rows:
                _log.debug("operation_run_reaper_tick_clean")
                return

            reaped = 0
            for row in expired_rows:
                try:
                    async with session.begin_nested():
                        await _reap_one_row(session, row, now=now)
                    reaped += 1
                except IllegalOperationRunTransitionError:
                    # Raced with an operator cancel between claim and reap;
                    # the run is already terminal. Skip -- the savepoint
                    # rolled back, the outer session stays valid.
                    _log.info(
                        "operation_run_reaper_row_skipped_status_changed",
                        run_id=str(row.id),
                    )
                except Exception:
                    _log.exception(
                        "operation_run_reaper_row_failed",
                        run_id=str(row.id),
                    )

            await session.commit()
            duration_ms = (time.perf_counter() - tick_started) * 1000.0
            _log.info(
                "operation_run_reaper_tick_done",
                reaped_total=len(expired_rows),
                failed=reaped,
                duration_ms=duration_ms,
            )


async def _reaper_loop() -> None:
    """The forever loop: sleep one cadence, sweep, repeat.

    Sleep-then-sweep so the first tick after process start does not race the
    rest of startup. Per-tick ``try`` guards mean a transient DB blip is
    logged and the loop continues; ``CancelledError`` propagates so lifespan
    shutdown stops the task cleanly.
    """
    interval = get_settings().operation_run_reaper_tick_interval_seconds
    _log.info("operation_run_reaper_started", interval_seconds=interval)
    while True:
        await asyncio.sleep(get_settings().operation_run_reaper_tick_interval_seconds)
        try:
            await _run_one_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("operation_run_reaper_tick_failed", exc_info=True)
        note_loop_tick(
            "operation_run_reaper",
            get_settings().operation_run_reaper_tick_interval_seconds,
        )


def start_operation_run_reaper() -> asyncio.Task[None]:
    """Start the background reaper loop and return its task handle.

    Registered in :func:`meho_backplane.main._start_background_tasks` behind
    the ``OPERATION_RUN_REAPER_ENABLED`` setting. The returned task is
    cancelled on lifespan shutdown; returning it (rather than
    fire-and-forgetting) keeps a strong reference so the task is not
    GC'd mid-flight.
    """
    return asyncio.create_task(_reaper_loop(), name="operation-run-reaper")


async def stop_operation_run_reaper(task: asyncio.Task[None]) -> None:
    """Cancel the reaper task and await its unwind (swallowing the cancel)."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
