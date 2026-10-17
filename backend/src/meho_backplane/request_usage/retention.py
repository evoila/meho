# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Daily retention prune for request usage events (#3734).

An ``asyncio`` task owned by the FastAPI lifespan. Once a day it deletes
``request_usage_event`` rows older than ``REQUEST_USAGE_RETENTION_DAYS``.
The lifespan starts it only when that setting is set; with the setting
unset, collection is off and there is nothing to prune.

Same shape as the sibling retention loops (for example
:mod:`meho_backplane.checks.evidence_retention`):

* sleep first, then prune, so the first tick does not race startup;
* one bounded ``DELETE ... WHERE occurred_at < cutoff`` on the
  ``request_usage_event_occurred_at_idx`` index;
* one summary ``INTERNAL`` audit row per tick, on a system sentinel
  tenant, so a deletion of stored data is on record;
* a failed tick is logged and the loop goes on.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

import structlog
from sqlalchemy import delete

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, RequestUsageEvent
from meho_backplane.memory.audit import INTERNAL_METHOD, write_internal_audit_row
from meho_backplane.metrics import note_loop_tick
from meho_backplane.settings import get_settings

__all__ = [
    "PRUNE_INTERVAL_SECONDS",
    "REQUEST_USAGE_PRUNE_PATH",
    "REQUEST_USAGE_RETENTION_SYSTEM_TENANT_ID",
    "SYSTEM_OPERATOR_SUB",
    "prune_request_usage_once",
    "start_request_usage_retention_sweeper",
    "stop_request_usage_retention_sweeper",
]

_log = structlog.get_logger(__name__)

#: How often the loop runs: once a day.
PRUNE_INTERVAL_SECONDS: Final[int] = 86400

#: ``operator_sub`` and ``path`` of the prune audit row.
SYSTEM_OPERATOR_SUB: Final[str] = "system:request-usage-retention"
REQUEST_USAGE_PRUNE_PATH: Final[str] = "request_usage.prune"

#: Sentinel tenant for the system-wide prune audit row (task number in
#: the last part, the same convention the sibling sweepers use).
REQUEST_USAGE_RETENTION_SYSTEM_TENANT_ID: Final[uuid.UUID] = uuid.UUID(
    "00000000-0000-0000-0000-000000003734",
)


async def _delete_events_older_than(cutoff: datetime) -> int:
    """Delete events older than *cutoff*; return how many rows went."""
    async with get_sessionmaker()() as session:
        result = await session.execute(
            delete(RequestUsageEvent).where(RequestUsageEvent.occurred_at < cutoff),
        )
        await session.commit()
        dropped: int = result.rowcount or 0  # type: ignore[attr-defined]
    return dropped


async def prune_request_usage_once(*, now: datetime | None = None) -> int | None:
    """Run one prune pass. Returns the deleted row count.

    Returns ``None`` and deletes nothing when retention is unset. *now*
    is injectable for tests.
    """
    retention_days = get_settings().request_usage_retention_days
    if retention_days is None:
        return None
    started = time.perf_counter()
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    dropped = await _delete_events_older_than(cutoff)
    duration_ms = (time.perf_counter() - started) * 1000.0
    _log.info(
        "request_usage_retention_tick_done",
        dropped_rows=dropped,
        retention_days=retention_days,
    )
    try:
        await write_internal_audit_row(
            event_class=AuditEventClass.OPERATION_EXECUTION,
            operator_sub=SYSTEM_OPERATOR_SUB,
            tenant_id=REQUEST_USAGE_RETENTION_SYSTEM_TENANT_ID,
            method=INTERNAL_METHOD,
            path=REQUEST_USAGE_PRUNE_PATH,
            status_code=200,
            duration_ms=duration_ms,
            payload={
                "dropped_rows": dropped,
                "retention_days": retention_days,
                "cutoff": cutoff.isoformat(),
            },
        )
    except Exception:
        # The DELETE already committed; a missing summary row must not stop
        # the loop.
        _log.exception("request_usage_retention_audit_write_failed", dropped_rows=dropped)
    return dropped


async def _prune_loop() -> None:
    """Sleep one interval, prune, repeat. Cancellation stops the loop."""
    while True:
        await asyncio.sleep(PRUNE_INTERVAL_SECONDS)
        try:
            await prune_request_usage_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("request_usage_retention_tick_failed", exc_info=True)
        note_loop_tick("request_usage_retention", PRUNE_INTERVAL_SECONDS)


def start_request_usage_retention_sweeper() -> asyncio.Task[None]:
    """Start the prune loop and return its task (the caller keeps a reference)."""
    return asyncio.create_task(_prune_loop(), name="request-usage-retention-sweeper")


async def stop_request_usage_retention_sweeper(task: asyncio.Task[None]) -> None:
    """Cancel the prune loop and wait for it to stop."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
