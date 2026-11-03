# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Let running operations finish when the pod stops (#5024).

When Kubernetes stops a MEHO pod, it sends the stop signal (``SIGTERM``).
From that moment the pod drains:

* ``/ready`` answers 503, so no new traffic is sent to the pod.
* A new operation call is refused before anything is sent to a target
  (:func:`shutting_down_result`). The caller can try again; the next pod
  takes it. A step of a multi-step operation that already runs, and an
  approved request whose run was already claimed, are not new work and go
  on.
* Calls that already run may finish, up to ``SHUTDOWN_DRAIN_SECONDS``
  after the stop signal. uvicorn waits for the open HTTP requests (its own
  limit, ``UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN``, is set to the same value by
  the chart). The lifespan then waits for operations that run in the
  background (async runs, sensor checks) until the same deadline.
* An operation that still runs at the limit is recorded as unfinished:
  one ``operation_unfinished_at_shutdown`` log line and one audit row
  (``result_status='unfinished'``, ``execution_outcome='uncertain'``,
  ``retry=False``). It may have reached the target, so MEHO never runs it
  again by itself. The lasting records that the start-up recovery reads
  stay as they are: an approved request keeps its started claim with no
  result, and an async run stays ``running`` until its lease ends.

The process-wide state lives in one object. Only the event loop thread
changes the list of running operations; the signal handler only sets the
draining flag and the time of the stop signal.
"""

from __future__ import annotations

import asyncio
import signal
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from itertools import count
from types import FrameType
from typing import Any

import structlog

from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog
from meho_backplane.request_usage.collector import current_request_id

__all__ = [
    "SHUTTING_DOWN_ERROR_CODE",
    "RunningOperation",
    "begin_drain",
    "drain_deadline",
    "install_stop_signal_hook",
    "is_draining",
    "record_unfinished_operations",
    "reset_drain_state",
    "running_operations",
    "shutting_down_result",
    "track_operation",
    "wait_for_running_operations",
]

_log = structlog.get_logger(__name__)

#: ``extras["error_code"]`` of a call refused because the pod stops.
SHUTTING_DOWN_ERROR_CODE = "meho_shutting_down"

#: The audit ``result_status`` of an operation that was still running when
#: the pod stopped.
_UNFINISHED_STATUS = "unfinished"

#: Upper bound for writing the audit rows of unfinished operations, so a
#: database that does not answer cannot use up the pod's grace period.
_RECORD_TIMEOUT_SECONDS = 5.0

#: How often the lifespan looks whether every running operation finished.
_POLL_SECONDS = 0.1

_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


@dataclass
class RunningOperation:
    """One top-level operation call that runs in this process."""

    op_id: str
    connector_id: str
    operator_sub: str
    tenant_id: uuid.UUID | None
    request_id: uuid.UUID | None
    started_at: datetime
    approved_run: bool
    recorded: bool = False


@dataclass
class _DrainState:
    draining: bool = False
    stop_requested_at: float | None = None
    running: dict[int, RunningOperation] = field(default_factory=dict)
    cut_off: list[RunningOperation] = field(default_factory=list)


_state = _DrainState()
_keys = count()


def reset_drain_state() -> None:
    """Forget every drain state. For tests only."""
    global _state
    _state = _DrainState()


def begin_drain() -> None:
    """Start the drain: refuse new calls from now on. Safe to call twice.

    Called from the stop signal handler, so it only sets two plain values.
    """
    if not _state.draining:
        _state.stop_requested_at = time.monotonic()
        _state.draining = True


def is_draining() -> bool:
    """Return ``True`` once the pod got its stop signal."""
    return _state.draining


def drain_deadline(drain_seconds: float) -> float:
    """Return the ``time.monotonic()`` value at which the drain ends.

    Counted from the stop signal. When no signal was seen (the server
    stopped in another way), counted from now.
    """
    started = _state.stop_requested_at
    if started is None:
        started = time.monotonic()
    return started + drain_seconds


def running_operations() -> list[RunningOperation]:
    """Return the top-level operation calls that run now."""
    return list(_state.running.values())


def shutting_down_result(op_id: str) -> OperationResult:
    """The answer to a call that came in after the stop signal.

    Nothing was sent to a target, so the caller may simply try again.
    """
    return OperationResult(
        status="error",
        op_id=op_id,
        error=(
            "meho_shutting_down: this MEHO instance is stopping and takes no "
            "new calls. Nothing was sent to the target. Try again in a minute."
        ),
        duration_ms=0.0,
        extras={
            "error_code": SHUTTING_DOWN_ERROR_CODE,
            "execution_outcome": "not_started",
            "retry": True,
        },
    )


@asynccontextmanager
async def track_operation(
    *,
    op_id: str,
    connector_id: str,
    operator_sub: str,
    tenant_id: uuid.UUID | None,
    approved_run: bool,
) -> AsyncIterator[None]:
    """Keep one top-level operation call in the list of running calls.

    A call cancelled while the pod drains (uvicorn cancels the open
    requests at its limit) moves to the list of cut-off calls, which
    :func:`record_unfinished_operations` records.
    """
    key = next(_keys)
    entry = RunningOperation(
        op_id=op_id,
        connector_id=connector_id,
        operator_sub=operator_sub,
        tenant_id=tenant_id,
        request_id=current_request_id(),
        started_at=datetime.now(UTC),
        approved_run=approved_run,
    )
    state = _state
    state.running[key] = entry
    try:
        yield
    except asyncio.CancelledError:
        if state.draining and not entry.recorded:
            state.cut_off.append(entry)
        raise
    finally:
        state.running.pop(key, None)


async def wait_for_running_operations(deadline: float) -> list[RunningOperation]:
    """Wait until no operation call runs, or until *deadline* (monotonic).

    Returns the calls that still run at the deadline.
    """
    while _state.running and time.monotonic() < deadline:
        await asyncio.sleep(min(_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
    return running_operations()


async def record_unfinished_operations(still_running: list[RunningOperation]) -> int:
    """Record every call cut off by the stop, and every call in *still_running*.

    Writes one log line and one audit row per call, and returns how many
    calls it recorded. Never raises: a failed audit write is logged, and the
    log lines are written first.
    """
    entries = [e for e in [*_state.cut_off, *still_running] if not e.recorded]
    _state.cut_off.clear()
    if not entries:
        return 0
    stopped_at = datetime.now(UTC)
    for entry in entries:
        entry.recorded = True
        _log.warning(
            "operation_unfinished_at_shutdown",
            op_id=entry.op_id,
            connector_id=entry.connector_id,
            operator_sub=entry.operator_sub,
            tenant_id=str(entry.tenant_id) if entry.tenant_id else None,
            request_id=str(entry.request_id) if entry.request_id else None,
            started_at=entry.started_at.isoformat(),
            approved_run=entry.approved_run,
        )
    try:
        async with asyncio.timeout(_RECORD_TIMEOUT_SECONDS):
            await _write_audit_rows(entries, stopped_at)
    except Exception:
        _log.exception("operation_unfinished_audit_failed", count=len(entries))
    return len(entries)


async def _write_audit_rows(entries: list[RunningOperation], stopped_at: datetime) -> None:
    async with get_sessionmaker()() as session:
        for entry in entries:
            session.add(_audit_row(entry, stopped_at))
        await session.commit()


def _audit_row(entry: RunningOperation, stopped_at: datetime) -> AuditLog:
    payload: dict[str, Any] = {
        "op_id": entry.op_id,
        "connector_id": entry.connector_id,
        "result_status": _UNFINISHED_STATUS,
        "execution_outcome": "uncertain",
        "retry": False,
        "approved_run": entry.approved_run,
        "started_at": entry.started_at.isoformat(),
        "stopped_at": stopped_at.isoformat(),
        "reason": (
            "MEHO stopped before this operation finished. It may have reached "
            "the target. Check the target before you run it again."
        ),
    }
    duration_ms = (stopped_at - entry.started_at).total_seconds() * 1000
    return AuditLog(
        id=uuid.uuid4(),
        event_class=AuditEventClass.OPERATION_EXECUTION,
        occurred_at=stopped_at,
        operator_sub=entry.operator_sub,
        tenant_id=entry.tenant_id,
        method="DISPATCH",
        path=entry.op_id,
        status_code=503,
        request_id=entry.request_id,
        duration_ms=Decimal(str(round(duration_ms, 2))),
        payload=payload,
    )


def install_stop_signal_hook() -> Callable[[], None]:
    """Start the drain as soon as the stop signal arrives.

    uvicorn handles ``SIGTERM`` and ``SIGINT`` itself. This puts a small
    handler in front of uvicorn's: it calls :func:`begin_drain`, then the
    handler that was there before. Only a callable handler is wrapped, and
    only in the main thread (Python allows signal handlers only there).
    Returns a function that puts the previous handlers back.
    """
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    installed: dict[int, tuple[Callable[[int, FrameType | None], None], Any]] = {}
    for sig in _STOP_SIGNALS:
        previous = signal.getsignal(sig)
        if not callable(previous):
            continue
        handler = _chained_handler(previous)
        signal.signal(sig, handler)
        installed[sig] = (handler, previous)

    def undo() -> None:
        for sig, (handler, previous) in installed.items():
            if signal.getsignal(sig) is handler:
                signal.signal(sig, previous)

    return undo


def _chained_handler(
    previous: Callable[[int, FrameType | None], Any],
) -> Callable[[int, FrameType | None], None]:
    def on_stop(signum: int, frame: FrameType | None) -> None:
        begin_drain()
        previous(signum, frame)

    return on_stop
