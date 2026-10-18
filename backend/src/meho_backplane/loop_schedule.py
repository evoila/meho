# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Time the first run of a weekly clean-up loop from its last recorded run.

Three clean-up loops use this: the announcement prune
(:mod:`~meho_backplane.broadcast.announcement_retention`), the sensor
evidence prune (:mod:`~meho_backplane.checks.evidence_retention`) and the
topology history prune (:mod:`~meho_backplane.topology.history_retention`).

The problem it fixes: each loop used to sleep one full interval (7 days by
default) after every process start, and only then run. So a deployment that
restarted more often than once a week (updates, crashes, node drains) never
ran the loop, and the table it cleans grew without limit.

Each of these loops writes one ``INTERNAL`` audit row per run, also when it
deleted 0 rows. So the newest such row is the loop's last real run. After the
process starts, :func:`wait_until_first_run`:

1. waits :data:`FIRST_RUN_DELAY_SECONDS` (one minute), so the first run does
   not race the rest of startup;
2. reads the time of the newest run within the last interval
   (:func:`last_recorded_run`);
3. returns at once if there is no such run (none within the last interval, a
   fresh install, or a loop that is set to keep rows forever and so writes no
   row); otherwise it waits only for the rest of the interval
   (:func:`seconds_until_due`).

The loop then runs, and after that runs once per interval.

A last-run time in the future (a pod with a fast clock wrote it) counts as
"due now", so it can never push the schedule back. If the read fails, the
helper logs ``<job>_last_run_read_failed`` and returns at once. Both are
safe: a run is safe to run twice, and running early is better than running
late. The wait is never longer than one interval.

The raw-payload age-off (:mod:`~meho_backplane.audit_retention`) uses the
same steps with its own copy of the code.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.memory.audit import INTERNAL_METHOD

__all__ = [
    "FIRST_RUN_DELAY_SECONDS",
    "last_recorded_run",
    "seconds_until_due",
    "wait_until_first_run",
]

_log = structlog.get_logger(__name__)

#: How long a loop waits after the process starts before it reads its last
#: recorded run. A run that is due then happens about one minute after start:
#: soon, but not in the middle of the rest of startup. Settings allow no
#: interval below 60 seconds, so this never exceeds the interval.
FIRST_RUN_DELAY_SECONDS: float = 60.0


async def last_recorded_run(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str,
    path: str,
    since: datetime,
) -> datetime | None:
    """Return the newest run of one loop after *since*, or ``None`` if none.

    A row counts only if it has the loop's system tenant id,
    ``method='INTERNAL'``, the loop's ``operator_sub`` and its ``path``. A
    dispatch audit row with the same tenant id and path never counts.

    The caller passes ``since = now - interval``. A run older than that
    means "due now" anyway, and the bound keeps the read cheap.

    SQLite gives back a datetime without a time zone; the writer stores UTC,
    so such a value is read as UTC. PostgreSQL gives back an aware value.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        last_run = await session.scalar(
            select(AuditLog.occurred_at)
            .where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.method == INTERNAL_METHOD,
                AuditLog.operator_sub == operator_sub,
                AuditLog.path == path,
                AuditLog.occurred_at > since,
            )
            .order_by(AuditLog.occurred_at.desc())
            .limit(1)
        )
    if last_run is None:
        return None
    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=UTC)
    return last_run


def seconds_until_due(
    last_run: datetime | None,
    *,
    now: datetime,
    interval_seconds: float,
) -> float:
    """Return how many seconds are left until the next run is due.

    * No recorded run: ``0`` (run now).
    * *last_run* is in the future (a pod with a fast clock wrote it): ``0``
      (run now), so a wrong clock can never push the schedule back.
    * A full interval has passed since *last_run*: ``0`` (run now).
    * Otherwise: what is left of the interval.

    So the result is always between ``0`` and one interval.
    """
    if last_run is None or last_run > now:
        return 0.0
    remaining = interval_seconds - (now - last_run).total_seconds()
    return max(0.0, remaining)


async def wait_until_first_run(
    *,
    job: str,
    tenant_id: uuid.UUID,
    operator_sub: str,
    path: str,
    interval_seconds: float,
) -> None:
    """Wait after process start until the loop's first run is due.

    Waits :data:`FIRST_RUN_DELAY_SECONDS`, reads the last recorded run, and
    then waits only for the rest of the interval (or not at all when a run is
    due). *job* is the loop's log prefix: the helper logs ``<job>_scheduled``
    once per start (with ``last_run_at`` and ``next_run_in_seconds``, so an
    operator can check the schedule after a restart), and
    ``<job>_last_run_read_failed`` if the read fails.

    ``asyncio.CancelledError`` from a sleep propagates, so lifespan shutdown
    can stop the loop while it waits.
    """
    await asyncio.sleep(FIRST_RUN_DELAY_SECONDS)

    now = datetime.now(UTC)
    try:
        last_run = await last_recorded_run(
            tenant_id=tenant_id,
            operator_sub=operator_sub,
            path=path,
            since=now - timedelta(seconds=interval_seconds),
        )
    except Exception as exc:
        # Run at once: a run is safe to run twice, and running early is
        # better than running late.
        _log.warning(
            f"{job}_last_run_read_failed",
            error_type=type(exc).__name__,
            exc_info=True,
        )
        last_run = None

    wait_seconds = seconds_until_due(last_run, now=now, interval_seconds=interval_seconds)
    _log.info(
        f"{job}_scheduled",
        last_run_at=last_run.isoformat() if last_run is not None else None,
        next_run_in_seconds=wait_seconds,
        interval_seconds=interval_seconds,
    )
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)
