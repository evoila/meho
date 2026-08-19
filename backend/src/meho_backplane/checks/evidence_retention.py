# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Scheduled background retention prune for per-tick sensor evidence (#2756).

Initiative #2780, Task #2756. The
:class:`~meho_backplane.db.models.SensorResult` table (migration ``0070``) is
the append-only per-tick evidence history the check runner writes on every
non-stale evaluation. Without a retention policy it grows unbounded -- a fleet
of sub-minute sensors adds a row every tick, indefinitely -- which is exactly
the "nobody's DB grows surprisingly" surprise the ops report asked the default
to avoid.

This module ships the physical cleanup: an ``asyncio`` task the FastAPI
lifespan owns that ticks on a fixed cadence (default 7 days / weekly) and
deletes rows where
``evaluated_at < now() - checks_evidence_retention_days``. It is the **only**
place the application issues a DELETE against ``sensor_results``.

Deliberate copy of the announcement-retention mold, with one divergence
--------------------------------------------------------------------------

Structure, sentinels, and the audit-row shape mirror
:mod:`meho_backplane.broadcast.announcement_retention` verbatim (which itself
copied the topology-history mold). Carried over unchanged:

* **``asyncio`` loop, not APScheduler.** The chassis's established shape for
  lifespan-owned background loops. Zero new dependency.
* **One audit row per non-no-op tick** (not per tenant): the prune is a
  system-wide op, attributed to the system sentinel tenant so operators
  querying their own timeline never see the prune row.
* **Fail-closed audit, loop-survival on the tick.** The audit writer swallows
  its own commit failure; the per-tick ``try`` / ``except`` in
  :func:`_prune_loop` logs and continues so one bad tick cannot kill the loop.

The **one deliberate divergence** is the ``retention_days == 0`` meaning. In
the announcement / topology sweepers ``0`` is *keep-forever* (the loop still
runs, no DELETE). Here ``0`` disables the whole feature:
:func:`~meho_backplane.checks.repository.record_sensor_result` writes **no**
history rows when ``checks_evidence_retention_days == 0`` (gated by the runner
on the same setting), so there is nothing to prune -- the tick logs a heartbeat
and returns. "Grows forever" is the surprise #2756 exists to prevent, so ``0``
means "off", not "unbounded". ``checks_evidence_prune_enabled=false`` still
skips starting the loop entirely (an operator running an external sweep).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import SensorResult
from meho_backplane.memory.audit import (
    INTERNAL_METHOD,
    write_internal_audit_row,
)
from meho_backplane.metrics import note_loop_tick
from meho_backplane.settings import get_settings

__all__ = [
    "EVIDENCE_RETENTION_PRUNE_PATH",
    "EVIDENCE_RETENTION_SYSTEM_TENANT_ID",
    "SYSTEM_OPERATOR_SUB",
    "start_evidence_retention_sweeper",
    "stop_evidence_retention_sweeper",
]

_log = structlog.get_logger(__name__)

#: Synthetic ``operator_sub`` for retention-prune audit rows. The
#: ``"system:<job>"`` shape lets audit-query filters partition prune rows from
#: other background-job rows by ``operator_sub`` alone, mirroring
#: :data:`meho_backplane.broadcast.announcement_retention.SYSTEM_OPERATOR_SUB`.
SYSTEM_OPERATOR_SUB: str = "system:checks-evidence-retention"

#: Canonical ``path`` for the prune audit row. Defined here so the audit doc,
#: the prune task, and any audit-query consumer share one symbol.
EVIDENCE_RETENTION_PRUNE_PATH: str = "checks.evidence.prune"

#: Sentinel tenant id for the system-wide ``INTERNAL`` prune audit row. The
#: cutoff predicate spans every tenant's rows in one statement, so attributing
#: the audit row to a real tenant would mislead operators querying their own
#: timeline. Encodes the task number (2756) in the final segment, the same
#: convention the announcement / topology prune sentinels use.
EVIDENCE_RETENTION_SYSTEM_TENANT_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000002756")


async def _delete_sensor_results_older_than(cutoff: datetime) -> int:
    """Run the bounded DELETE; return the dropped-row count.

    The DELETE is bounded by the ``evaluated_at < cutoff`` predicate -- no
    unbounded LIMIT-less statement that could lock the table. The filter rides
    the ``sensor_results_evaluated_at_idx`` btree (migration ``0070``), so even
    a large history prunes in one indexed range-scan.

    ``rowcount`` is only typed on the concrete ``CursorResult`` the
    ``session.execute(delete(...))`` call returns at runtime; ``or 0``
    collapses the ``int | None`` the abstract ``Result`` superclass declares to
    the ``int`` the audit payload requires (same ``type: ignore`` the sibling
    sweepers document).
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            delete(SensorResult).where(SensorResult.evaluated_at < cutoff)
        )
        await session.commit()
        dropped: int = result.rowcount or 0  # type: ignore[attr-defined]
    return dropped


async def _write_prune_audit_row(
    *,
    dropped_rows: int,
    retention_days: int,
    cutoff: datetime,
    duration_ms: float,
) -> None:
    """Write the one summary audit row, swallowing audit-side failures.

    The DELETE has already committed before this helper runs; an audit-write
    failure must not stall the prune loop. Surface it loud-but-non-fatal (same
    shape as the announcement / topology / memory sweepers).
    """
    try:
        await write_internal_audit_row(
            operator_sub=SYSTEM_OPERATOR_SUB,
            tenant_id=EVIDENCE_RETENTION_SYSTEM_TENANT_ID,
            method=INTERNAL_METHOD,
            path=EVIDENCE_RETENTION_PRUNE_PATH,
            status_code=200,
            duration_ms=duration_ms,
            payload={
                "dropped_rows": dropped_rows,
                "retention_days": retention_days,
                "cutoff": cutoff.isoformat(),
            },
        )
    except Exception:
        _log.exception(
            "checks_evidence_retention_audit_write_failed",
            dropped_rows=dropped_rows,
            retention_days=retention_days,
        )


async def _run_one_prune_tick() -> None:
    """One prune sweep: delete rows older than the retention window, audit.

    Reads ``checks_evidence_retention_days`` and computes a cutoff of
    ``now(UTC) - retention_days``. When ``retention_days == 0`` the feature is
    disabled (:func:`record_sensor_result` wrote no rows), so the tick logs the
    heartbeat and returns without a DELETE or audit row -- the no-op case would
    otherwise emit one ``dropped_rows=0`` row per weekly tick, indistinguishable
    from a real "swept clean" outcome.

    Returns no value; the per-tick ``try`` / ``except`` in :func:`_prune_loop`
    catches any exception so one bad tick cannot kill the loop.
    """
    tick_started = time.perf_counter()
    settings = get_settings()
    retention_days = settings.checks_evidence_retention_days

    if retention_days == 0:
        _log.info(
            "checks_evidence_retention_disabled",
            retention_days=0,
            interval_seconds=settings.checks_evidence_prune_interval_seconds,
        )
        return

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    dropped_rows = await _delete_sensor_results_older_than(cutoff)
    duration_ms = (time.perf_counter() - tick_started) * 1000.0
    _log.info(
        "checks_evidence_retention_tick_done",
        dropped_rows=dropped_rows,
        retention_days=retention_days,
        cutoff=cutoff.isoformat(),
        duration_ms=duration_ms,
    )
    await _write_prune_audit_row(
        dropped_rows=dropped_rows,
        retention_days=retention_days,
        cutoff=cutoff,
        duration_ms=duration_ms,
    )


async def _prune_loop() -> None:
    """The forever loop: sleep one cadence, prune, repeat.

    Sleep-then-prune (not prune-then-sleep) so the first tick after start does
    not race the rest of lifespan boot. Per-tick ``try`` / ``except`` means a
    transient DB blip is logged and the loop continues;
    ``asyncio.CancelledError`` propagates so lifespan shutdown stops the task
    cleanly.
    """
    interval = get_settings().checks_evidence_prune_interval_seconds
    _log.info(
        "checks_evidence_retention_started",
        interval_seconds=interval,
        retention_days=get_settings().checks_evidence_retention_days,
    )
    while True:
        await asyncio.sleep(get_settings().checks_evidence_prune_interval_seconds)
        try:
            await _run_one_prune_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "checks_evidence_retention_tick_failed",
                exc_info=True,
            )
        note_loop_tick("evidence_retention", get_settings().checks_evidence_prune_interval_seconds)


def start_evidence_retention_sweeper() -> asyncio.Task[None]:
    """Start the background retention prune loop and return its task.

    Registered in :func:`meho_backplane.main.lifespan` behind the
    ``CHECKS_EVIDENCE_PRUNE_ENABLED`` setting. The returned task is cancelled on
    lifespan shutdown; the caller awaits the cancellation so the loop unwinds
    cleanly. Returning the task (rather than fire-and-forgetting) keeps a strong
    reference alive -- an un-referenced task can be GC'd mid-flight, producing
    the "Task was destroyed but it is pending!" warnings pytest-asyncio rejects.
    Naming mirrors the sibling retention sweepers so the disposal pattern stays
    uniform across the lifespan-owned loops.
    """
    return asyncio.create_task(
        _prune_loop(),
        name="checks-evidence-retention-sweeper",
    )


async def stop_evidence_retention_sweeper(task: asyncio.Task[None]) -> None:
    """Cancel the retention prune task and await its unwind.

    Swallows the expected :class:`asyncio.CancelledError`; any other exception
    during unwind propagates so a broken shutdown is visible. Mirrors the
    announcement retention sweeper's disposal shape verbatim.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
