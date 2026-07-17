# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Scheduled background retention prune for durable announcements (#2547).

Broadcast v2 Initiative #2543, Task #2547 (T2). The
:class:`~meho_backplane.db.models.AgentAnnouncement` table
(migration ``0066``) is the durable archive of every agent-authored
announcement -- one append-only row per ``meho.broadcast.announce`` call.
Without a retention policy it grows unbounded: a fleet of announce-happy
agents adds rows every coordination cycle, indefinitely.

This module ships the physical cleanup: an ``asyncio`` task the FastAPI
lifespan owns that ticks on a fixed cadence (default 7 days / weekly) and
deletes rows where ``created_at < now() - broadcast_announcement_retention_days``.
The table is otherwise append-only by contract (see
:class:`~meho_backplane.db.models.AgentAnnouncement`); this prune task is
the **only** place the application issues a DELETE against it.

Deliberate copy of the topology-history prune mold
--------------------------------------------------

Structure, sentinels, and the audit-row shape mirror
:mod:`meho_backplane.topology.history_retention` verbatim (the mold the
task body names). The rationale carried over unchanged:

* **``asyncio`` loop, not APScheduler.** The chassis's established shape
  for lifespan-owned background loops (topology refresh + retention,
  memory expiry, grant/approval expiry). Zero new dependency.
* **``retention_days == 0`` is the keep-forever opt-out.** A tick still
  runs, observes the no-op shape, logs
  ``broadcast_announcement_retention_disabled``, and returns without a
  DELETE or an audit row -- deliberately distinct from
  ``broadcast_announcement_prune_enabled=false`` (which skips starting
  the loop entirely). The ``0``-day operator still gets a heartbeat log
  that proves the prune surface is alive and the disk-growth tradeoff is
  policy-driven.
* **One audit row per non-no-op tick** (not per tenant): the prune is a
  system-wide op. ``operator_sub='system:broadcast-announcement-retention'``,
  ``method='INTERNAL'``, ``path='broadcast.announcement.prune'``,
  ``payload={"dropped_rows": N, "retention_days": D, "cutoff": <iso>}``,
  attributed to the system-wide sentinel tenant so operators querying
  their own tenant timeline never see the prune row.
* **Fail-closed audit, loop-survival on the tick.** The audit writer
  raises on commit failure but the per-tick ``try`` / ``except`` in
  :func:`_prune_loop` logs and continues -- one bad audit write must not
  kill the prune loop.

Distinct from ``broadcast_retention_hours``
-------------------------------------------

``broadcast_retention_hours`` (default 24) is the *stream* read-window
heuristic -- a different concern on a different substrate (the Valkey
stream). This retention governs the *durable table*; the two windows are
independent by design (hot stream vs. archive).
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
from meho_backplane.db.models import AgentAnnouncement
from meho_backplane.memory.audit import (
    INTERNAL_METHOD,
    write_internal_audit_row,
)
from meho_backplane.settings import get_settings

__all__ = [
    "ANNOUNCEMENT_RETENTION_PRUNE_PATH",
    "ANNOUNCEMENT_RETENTION_SYSTEM_TENANT_ID",
    "SYSTEM_OPERATOR_SUB",
    "start_announcement_retention_sweeper",
    "stop_announcement_retention_sweeper",
]

_log = structlog.get_logger(__name__)

#: Synthetic ``operator_sub`` for retention-prune audit rows. The
#: ``"system:<job>"`` shape lets audit-query filters partition prune rows
#: from other background-job rows by ``operator_sub`` alone, mirroring
#: :data:`meho_backplane.topology.history_retention.SYSTEM_OPERATOR_SUB`.
SYSTEM_OPERATOR_SUB: str = "system:broadcast-announcement-retention"

#: Canonical ``path`` for the prune audit row. Defined here so the audit
#: doc, the prune task, and any audit-query consumer share one symbol.
ANNOUNCEMENT_RETENTION_PRUNE_PATH: str = "broadcast.announcement.prune"

#: Sentinel tenant id for the system-wide ``INTERNAL`` prune audit row.
#: The cutoff predicate spans every tenant's rows in one statement, so
#: attributing the audit row to a real tenant would mislead operators
#: querying their own timeline. A deterministic value (stable across
#: restarts) reserved by convention -- ``audit_log.tenant_id`` is a
#: soft-FK, so a row whose tenant_id has no ``tenant`` row is the
#: supported shape for system ops. Encodes the task number (2547) in the
#: final segment, the same convention the topology prune sentinel uses.
ANNOUNCEMENT_RETENTION_SYSTEM_TENANT_ID: uuid.UUID = uuid.UUID(
    "00000000-0000-0000-0000-000000002547"
)


def _resolve_system_tenant_id() -> uuid.UUID:
    """Return the sentinel tenant id for the prune audit row.

    Centralised behind a function so a future migration to per-tenant
    retention can route the call site through a lookup without rewriting
    the prune task.
    """
    return ANNOUNCEMENT_RETENTION_SYSTEM_TENANT_ID


async def _delete_announcements_older_than(cutoff: datetime) -> int:
    """Run the bounded DELETE; return the dropped-row count.

    The DELETE is bounded by the ``created_at < cutoff`` predicate -- no
    unbounded LIMIT-less statement that could lock the table. The filter
    rides the ``(tenant_id, created_at DESC)`` index (migration ``0066``)
    in reverse, so even a large archive prunes in seconds.

    ``rowcount`` is only typed on the concrete ``CursorResult`` the
    ``session.execute(delete(...))`` call returns at runtime; the abstract
    ``Result`` superclass mypy infers does not declare it (same shape the
    topology prune + ``kb/service.py`` document with
    ``type: ignore[attr-defined]``). ``or 0`` collapses the ``int | None``
    to the ``int`` the audit payload requires.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            delete(AgentAnnouncement).where(AgentAnnouncement.created_at < cutoff)
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

    The DELETE has already committed before this helper runs; an
    audit-write failure must not stall the prune loop. Surface it
    loud-but-non-fatal (same shape as the topology + memory sweepers).
    """
    try:
        await write_internal_audit_row(
            operator_sub=SYSTEM_OPERATOR_SUB,
            tenant_id=_resolve_system_tenant_id(),
            method=INTERNAL_METHOD,
            path=ANNOUNCEMENT_RETENTION_PRUNE_PATH,
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
            "broadcast_announcement_retention_audit_write_failed",
            dropped_rows=dropped_rows,
            retention_days=retention_days,
        )


async def _run_one_prune_tick() -> None:
    """One prune sweep: delete rows older than the retention window, audit.

    Reads ``broadcast_announcement_retention_days`` and computes a cutoff
    of ``now(UTC) - retention_days``. When ``retention_days == 0`` (the
    keep-forever sentinel) the function logs the heartbeat and returns
    without a DELETE or audit row -- the no-op case would otherwise emit
    one ``dropped_rows=0`` row per weekly tick, indistinguishable from a
    real "swept clean" outcome.

    Returns no value; the per-tick ``try`` / ``except`` in
    :func:`_prune_loop` catches any exception so one bad tick cannot kill
    the loop.
    """
    tick_started = time.perf_counter()
    settings = get_settings()
    retention_days = settings.broadcast_announcement_retention_days

    if retention_days == 0:
        _log.info(
            "broadcast_announcement_retention_disabled",
            retention_days=0,
            interval_seconds=settings.broadcast_announcement_prune_interval_seconds,
        )
        return

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    dropped_rows = await _delete_announcements_older_than(cutoff)
    duration_ms = (time.perf_counter() - tick_started) * 1000.0
    _log.info(
        "broadcast_announcement_retention_tick_done",
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

    Sleep-then-prune (not prune-then-sleep) so the first tick after start
    does not race the rest of lifespan boot. Per-tick ``try`` / ``except``
    means a transient DB blip is logged and the loop continues;
    ``asyncio.CancelledError`` propagates so lifespan shutdown stops the
    task cleanly.
    """
    interval = get_settings().broadcast_announcement_prune_interval_seconds
    _log.info(
        "broadcast_announcement_retention_started",
        interval_seconds=interval,
        retention_days=get_settings().broadcast_announcement_retention_days,
    )
    while True:
        await asyncio.sleep(get_settings().broadcast_announcement_prune_interval_seconds)
        try:
            await _run_one_prune_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "broadcast_announcement_retention_tick_failed",
                exc_info=True,
            )


def start_announcement_retention_sweeper() -> asyncio.Task[None]:
    """Start the background retention prune loop and return its task.

    Registered in :func:`meho_backplane.main.lifespan` behind the
    ``BROADCAST_ANNOUNCEMENT_PRUNE_ENABLED`` setting. The returned task is
    cancelled on lifespan shutdown; the caller awaits the cancellation so
    the loop unwinds cleanly. Returning the task (rather than
    fire-and-forgetting) keeps a strong reference alive -- an
    un-referenced task can be GC'd mid-flight, producing the "Task was
    destroyed but it is pending!" warnings pytest-asyncio rejects. Naming
    mirrors the topology / memory-expiry sweepers so the disposal pattern
    stays uniform across the lifespan-owned loops.
    """
    return asyncio.create_task(
        _prune_loop(),
        name="broadcast-announcement-retention-sweeper",
    )


async def stop_announcement_retention_sweeper(task: asyncio.Task[None]) -> None:
    """Cancel the retention prune task and await its unwind.

    Swallows the expected :class:`asyncio.CancelledError`; any other
    exception during unwind propagates so a broken shutdown is visible.
    Mirrors the topology retention sweeper's disposal shape verbatim.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
