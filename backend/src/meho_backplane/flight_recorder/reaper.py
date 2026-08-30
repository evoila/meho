# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Flight-recorder trace retention reaper (#3212, F4).

A lifespan-owned ``asyncio`` loop that deletes expired flight-recorder traces
on a fixed cadence. Traces are debugging exhaust, not records of account
(F4 of ``docs/decisions/dispatch-flight-recorder.md``): the ``audit_log`` row
is the permanent record and is **never** touched by this reaper -- only the
``dispatch_trace`` header and its ``dispatch_trace_span`` children are deleted.

The per-tenant retention window (14 days lab-class / 7 days default,
per-tenant configurable) is resolved and stamped onto
:attr:`~meho_backplane.db.models.DispatchTrace.expires_at` at **write** time
(:func:`meho_backplane.flight_recorder.store.record_trace`). So this reaper is
a plain bounded ``WHERE expires_at < now()`` sweep -- the window math lives in
:mod:`meho_backplane.flight_recorder.config`, and this loop never reads a
per-tenant policy or does interval arithmetic in SQL (which does not port
cleanly across the PG / SQLite dialects the codebase targets).

Delete order: spans first, then headers. The FK is ``ON DELETE CASCADE``, but
the SQLite ``foreign_keys`` pragma is opt-in in this codebase
(:mod:`meho_backplane.db.engine`), so an explicit span-first delete keeps the
sweep correct on every dialect rather than depending on the pragma.

Structure mirrors :mod:`meho_backplane.topology.history_retention` (the closest
retention-prune precedent) and the reaper mould of
:mod:`meho_backplane.operations.operation_run_reaper`: a bounded per-tick
delete, a per-tick ``try`` / ``except`` so one bad tick never stalls the loop,
and a single summary ``INTERNAL`` audit row per non-empty sweep (empty sweeps
write no row -- hourly ``dropped=0`` rows would flood ``audit_log``).

Per-pod leader election is deliberately omitted (same calculus as the topology
prune): under N replicas the worst case is N identical bounded DELETEs in the
same second targeting rows already gone from the previous winner -- idempotent
and below the noise floor. The ``max_per_tick`` LIMIT bounds each statement.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import delete, select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import DispatchTrace, DispatchTraceSpan
from meho_backplane.memory.audit import INTERNAL_METHOD, write_internal_audit_row
from meho_backplane.metrics import note_loop_tick
from meho_backplane.settings import get_settings

__all__ = [
    "FLIGHT_RECORDER_RETENTION_PATH",
    "FLIGHT_RECORDER_SYSTEM_TENANT_ID",
    "SYSTEM_OPERATOR_SUB",
    "start_flight_recorder_reaper",
    "stop_flight_recorder_reaper",
]

_log = structlog.get_logger(__name__)

#: Synthetic ``operator_sub`` on retention-reaper audit rows. The
#: ``"system:<job>"`` convention keeps background-job rows partitionable from
#: operator rows without parsing ``path``.
SYSTEM_OPERATOR_SUB: str = "system:flight-recorder-retention"

#: Canonical ``INTERNAL`` ``path`` for the retention-reaper audit row. Shared
#: symbol for the task, the audit doc registry, and future audit-query
#: consumers. Registered in ``docs/architecture/audit.md``.
FLIGHT_RECORDER_RETENTION_PATH: str = "flight_recorder.trace.prune"

#: Sentinel tenant id for the system-wide reaper audit row. The sweep spans
#: every tenant's expired traces in one statement, so attributing the row to a
#: real tenant would mislead per-tenant audit replays. A stable deterministic
#: value (last segment encodes issue #3212) -- reserved by convention, exploits
#: the ``audit_log.tenant_id`` soft-FK so it writes without a matching
#: ``tenant`` row (same shape as the topology-history retention sentinel).
FLIGHT_RECORDER_SYSTEM_TENANT_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000003212")


async def _reap_expired(now: datetime, limit: int) -> tuple[int, int]:
    """Delete up to *limit* expired traces (spans first, then headers).

    Selects the oldest-expired header ids up to ``limit``, deletes their spans,
    then the headers, in one transaction. Returns ``(dropped_headers,
    dropped_spans)``. The ``audit_log`` row is never touched.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        expired_ids = list(
            (
                await session.execute(
                    select(DispatchTrace.id)
                    .where(DispatchTrace.expires_at < now)
                    .order_by(DispatchTrace.expires_at.asc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        if not expired_ids:
            return 0, 0
        span_result = await session.execute(
            delete(DispatchTraceSpan).where(DispatchTraceSpan.trace_id.in_(expired_ids))
        )
        header_result = await session.execute(
            delete(DispatchTrace).where(DispatchTrace.id.in_(expired_ids))
        )
        await session.commit()
        # ``rowcount`` is typed only on the concrete CursorResult; the abstract
        # Result superclass mypy infers omits it (same shape the topology prune
        # documents). Bounded single-statement DELETEs never executemany, so the
        # value is a real int; ``or 0`` collapses the ``int | None`` type.
        dropped_spans: int = span_result.rowcount or 0  # type: ignore[attr-defined]
        dropped_headers: int = header_result.rowcount or 0  # type: ignore[attr-defined]
    return dropped_headers, dropped_spans


async def _write_prune_audit_row(
    *,
    dropped_headers: int,
    dropped_spans: int,
    cutoff: datetime,
    duration_ms: float,
) -> None:
    """Write the one summary ``INTERNAL`` audit row, swallowing audit failures.

    Called only on a non-empty sweep. The DELETE has already committed; an
    audit-write failure must not stall the reaper loop, so it is logged
    loud-but-non-fatal (the topology-prune / memory-expiry discipline).
    """
    try:
        await write_internal_audit_row(
            operator_sub=SYSTEM_OPERATOR_SUB,
            tenant_id=FLIGHT_RECORDER_SYSTEM_TENANT_ID,
            method=INTERNAL_METHOD,
            path=FLIGHT_RECORDER_RETENTION_PATH,
            status_code=200,
            duration_ms=duration_ms,
            payload={
                "dropped_trace_headers": dropped_headers,
                "dropped_trace_spans": dropped_spans,
                "cutoff": cutoff.isoformat(),
            },
        )
    except Exception:
        _log.exception(
            "flight_recorder_retention_audit_write_failed",
            dropped_trace_headers=dropped_headers,
            dropped_trace_spans=dropped_spans,
        )


async def _run_one_reap_tick() -> None:
    """One retention sweep: delete expired traces + audit a non-empty sweep.

    ``cutoff`` is the current wall-clock time; every trace whose stamped
    ``expires_at`` is in the past is expired. On an empty sweep the function
    logs a heartbeat and returns without an audit row (hourly ``dropped=0``
    rows would flood ``audit_log``). The per-tick ``try`` / ``except`` in
    :func:`_reap_loop` catches any exception so one bad tick cannot kill the
    loop.
    """
    tick_started = time.perf_counter()
    cutoff = datetime.now(UTC)
    limit = get_settings().flight_recorder_reaper_max_per_tick
    dropped_headers, dropped_spans = await _reap_expired(cutoff, limit)
    if dropped_headers == 0:
        _log.debug("flight_recorder_retention_tick_clean")
        return
    duration_ms = (time.perf_counter() - tick_started) * 1000.0
    _log.info(
        "flight_recorder_retention_tick_done",
        dropped_trace_headers=dropped_headers,
        dropped_trace_spans=dropped_spans,
        cutoff=cutoff.isoformat(),
        duration_ms=duration_ms,
    )
    await _write_prune_audit_row(
        dropped_headers=dropped_headers,
        dropped_spans=dropped_spans,
        cutoff=cutoff,
        duration_ms=duration_ms,
    )


async def _reap_loop() -> None:
    """The forever loop: sleep one cadence, sweep, repeat.

    Sleep-then-sweep so the first tick does not race the rest of startup.
    Per-tick ``try`` / ``except`` guards mean a transient DB blip is logged and
    the loop continues; ``CancelledError`` propagates so lifespan shutdown can
    stop the task cleanly.
    """
    interval = get_settings().flight_recorder_reaper_tick_interval_seconds
    _log.info("flight_recorder_retention_started", interval_seconds=interval)
    while True:
        await asyncio.sleep(get_settings().flight_recorder_reaper_tick_interval_seconds)
        try:
            await _run_one_reap_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("flight_recorder_retention_tick_failed", exc_info=True)
        note_loop_tick(
            "flight_recorder_retention",
            get_settings().flight_recorder_reaper_tick_interval_seconds,
        )


def start_flight_recorder_reaper() -> asyncio.Task[None]:
    """Start the background retention reaper loop and return its task handle.

    Registered in :func:`meho_backplane.main._start_background_tasks` behind the
    ``FLIGHT_RECORDER_REAPER_ENABLED`` setting. The returned task is cancelled
    on lifespan shutdown; returning it keeps a strong reference so the task is
    not GC'd mid-flight.
    """
    return asyncio.create_task(_reap_loop(), name="flight-recorder-retention-reaper")


async def stop_flight_recorder_reaper(task: asyncio.Task[None]) -> None:
    """Cancel the reaper task and await its unwind (swallowing the cancel)."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
