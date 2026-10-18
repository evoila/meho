# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the start-up schedule of the weekly clean-up loops (#4011).

Three loops used to sleep one full interval after every process start, so a
deployment that restarted more often than weekly never ran them. They now
count the interval from their last recorded run
(:mod:`meho_backplane.loop_schedule`).

What is covered:

* **Each loop** (announcement prune, sensor evidence prune, topology history
  prune): a restart before the interval waits only for the rest of it (no
  early run); a restart after it runs one minute after start; no recorded
  run runs one minute after start; a real run is found by the next start.
* **The helper**: only rows the loop writes count as a run, and only within
  the window; the wait is the rest of the interval and a future last run is
  due now; a failed read logs and runs at once.

Tests run against the autouse SQLite engine from ``tests.conftest`` (a fresh
migrated DB per test). ``asyncio.sleep`` is replaced by a fake that records
each wait and stops the loop at the N-th one.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import structlog
from sqlalchemy import select
from structlog.testing import capture_logs

from meho_backplane import loop_schedule
from meho_backplane.broadcast import announcement_retention
from meho_backplane.checks import evidence_retention
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.loop_schedule import (
    FIRST_RUN_DELAY_SECONDS,
    last_recorded_run,
    seconds_until_due,
    wait_until_first_run,
)
from meho_backplane.memory.audit import INTERNAL_METHOD
from meho_backplane.settings import get_settings
from meho_backplane.topology import history_retention

_WEEK_SECONDS = 604800
_DAY_SECONDS = 86400


@dataclass(frozen=True)
class _Loop:
    """One clean-up loop and the values its schedule depends on."""

    name: str
    module: ModuleType
    interval_env: str
    retention_env: str
    retention_days: str
    tenant_id: uuid.UUID
    operator_sub: str
    path: str
    job: str


_LOOPS = [
    _Loop(
        name="announcement",
        module=announcement_retention,
        interval_env="BROADCAST_ANNOUNCEMENT_PRUNE_INTERVAL_SECONDS",
        retention_env="BROADCAST_ANNOUNCEMENT_RETENTION_DAYS",
        retention_days="90",
        tenant_id=announcement_retention.ANNOUNCEMENT_RETENTION_SYSTEM_TENANT_ID,
        operator_sub=announcement_retention.SYSTEM_OPERATOR_SUB,
        path=announcement_retention.ANNOUNCEMENT_RETENTION_PRUNE_PATH,
        job="broadcast_announcement_retention",
    ),
    _Loop(
        name="evidence",
        module=evidence_retention,
        interval_env="CHECKS_EVIDENCE_PRUNE_INTERVAL_SECONDS",
        retention_env="CHECKS_EVIDENCE_RETENTION_DAYS",
        retention_days="7",
        tenant_id=evidence_retention.EVIDENCE_RETENTION_SYSTEM_TENANT_ID,
        operator_sub=evidence_retention.SYSTEM_OPERATOR_SUB,
        path=evidence_retention.EVIDENCE_RETENTION_PRUNE_PATH,
        job="checks_evidence_retention",
    ),
    _Loop(
        name="topology-history",
        module=history_retention,
        interval_env="TOPOLOGY_HISTORY_PRUNE_INTERVAL_SECONDS",
        retention_env="TOPOLOGY_HISTORY_RETENTION_DAYS",
        retention_days="90",
        tenant_id=history_retention.TOPOLOGY_HISTORY_SYSTEM_TENANT_ID,
        operator_sub=history_retention.SYSTEM_OPERATOR_SUB,
        path=history_retention.TOPOLOGY_HISTORY_PRUNE_PATH,
        job="topology_history_retention",
    ),
]


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(params=_LOOPS, ids=[loop.name for loop in _LOOPS])
def loop(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> _Loop:
    """Each clean-up loop in turn, set to a weekly interval."""
    cfg: _Loop = request.param
    monkeypatch.setenv(cfg.interval_env, str(_WEEK_SECONDS))
    monkeypatch.setenv(cfg.retention_env, cfg.retention_days)
    get_settings.cache_clear()
    return cfg


async def _seed_run_row(
    *,
    occurred_at: datetime,
    tenant_id: uuid.UUID,
    operator_sub: str,
    path: str,
    method: str = INTERNAL_METHOD,
) -> None:
    """Insert one audit row, as an earlier run (or something else) wrote it."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            AuditLog(
                id=uuid.uuid4(),
                occurred_at=occurred_at,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                method=method,
                path=path,
                status_code=200,
                duration_ms=Decimal("1.0"),
                payload={"dropped_rows": 0},
            )
        )
        await session.commit()


async def _seed_loop_run(loop: _Loop, *, occurred_at: datetime) -> None:
    await _seed_run_row(
        occurred_at=occurred_at,
        tenant_id=loop.tenant_id,
        operator_sub=loop.operator_sub,
        path=loop.path,
    )


async def _count_run_rows(loop: _Loop) -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == loop.path))
        return len(result.scalars().all())


async def _drive_loop(
    loop: _Loop,
    *,
    cancel_at_sleep: int,
    real_tick: bool = False,
) -> tuple[list[float], int]:
    """Run the loop's ``_prune_loop`` with a fake ``asyncio.sleep``.

    Stops at the N-th sleep. Returns every sleep the loop asked for and how
    many runs happened. With ``real_tick`` the real tick runs (and writes its
    audit row); otherwise a counting stub.
    """
    sleeps: list[float] = []
    tick_calls = 0
    real: Callable[[], Coroutine[Any, Any, None]] = loop.module._run_one_prune_tick

    async def _counting_tick() -> None:
        nonlocal tick_calls
        tick_calls += 1
        if real_tick:
            await real()

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= cancel_at_sleep:
            raise asyncio.CancelledError

    with (
        patch.object(loop.module, "_run_one_prune_tick", new=_counting_tick),
        patch("asyncio.sleep", new=_fake_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await loop.module._prune_loop()
    return sleeps, tick_calls


# ---------------------------------------------------------------------------
# Each loop: the interval counts from the last recorded run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_before_interval_waits_only_the_rest_of_it(
    loop: _Loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Last run 2 days ago, weekly interval: no early run, wait about 5 days."""
    await _seed_loop_run(loop, occurred_at=datetime.now(UTC) - timedelta(days=2))
    monkeypatch.setattr(loop_schedule, "_log", structlog.get_logger(loop_schedule.__name__))

    with capture_logs() as captured:
        sleeps, tick_calls = await _drive_loop(loop, cancel_at_sleep=2)

    assert tick_calls == 0, "a restart before the interval has passed must not run early"
    assert sleeps[0] == FIRST_RUN_DELAY_SECONDS
    rest = _WEEK_SECONDS - 2 * _DAY_SECONDS
    assert rest - 5 <= sleeps[1] <= rest, f"expected the rest of the interval; got {sleeps[1]}"
    scheduled = [e for e in captured if e["event"] == f"{loop.job}_scheduled"]
    assert len(scheduled) == 1
    assert scheduled[0]["last_run_at"] is not None
    assert scheduled[0]["next_run_in_seconds"] == sleeps[1]


@pytest.mark.asyncio
async def test_restart_after_interval_runs_soon_after_start(loop: _Loop) -> None:
    """Last run 8 days ago, weekly interval: run one minute after start."""
    await _seed_loop_run(loop, occurred_at=datetime.now(UTC) - timedelta(days=8))

    sleeps, tick_calls = await _drive_loop(loop, cancel_at_sleep=2)

    assert sleeps == [FIRST_RUN_DELAY_SECONDS, _WEEK_SECONDS]
    assert tick_calls == 1, "a due run must happen right after the start delay"


@pytest.mark.asyncio
async def test_no_recorded_run_runs_soon_after_start(loop: _Loop) -> None:
    """No run recorded (fresh install): run one minute after start."""
    sleeps, tick_calls = await _drive_loop(loop, cancel_at_sleep=2)

    assert sleeps == [FIRST_RUN_DELAY_SECONDS, _WEEK_SECONDS]
    assert tick_calls == 1


@pytest.mark.asyncio
async def test_a_real_run_is_found_by_the_next_start(loop: _Loop) -> None:
    """Start, run, restart: the second start does not run again.

    The first start has no recorded run, so the real tick runs (with nothing
    to delete) and writes its row. A second start right after reads that row
    and waits about one full interval instead.
    """
    _, first_ticks = await _drive_loop(loop, cancel_at_sleep=2, real_tick=True)
    assert first_ticks == 1
    assert await _count_run_rows(loop) == 1, "a run that deleted 0 rows must still write its row"

    sleeps, second_ticks = await _drive_loop(loop, cancel_at_sleep=2, real_tick=True)
    assert second_ticks == 0, "a restart right after a run must not run again"
    assert _WEEK_SECONDS - 60 <= sleeps[1] <= _WEEK_SECONDS
    assert await _count_run_rows(loop) == 1


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000004011")
_SUB = "system:test-cleanup"
_PATH = "test.cleanup.prune"


@pytest.mark.asyncio
async def test_last_recorded_run_reads_only_this_loops_rows() -> None:
    """Only rows this loop writes count, and only within the given window.

    A row counts only with the loop's tenant id, ``method='INTERNAL'``, its
    ``operator_sub`` and its path. The newest matching row wins, and the
    value comes back in UTC with a time zone (SQLite returns it without one).
    """
    now = datetime.now(UTC)
    since = now - timedelta(seconds=_WEEK_SECONDS)

    async def _read() -> datetime | None:
        return await last_recorded_run(
            tenant_id=_TENANT, operator_sub=_SUB, path=_PATH, since=since
        )

    hour = timedelta(hours=1)
    await _seed_run_row(
        occurred_at=now - hour, tenant_id=uuid.uuid4(), operator_sub=_SUB, path=_PATH
    )
    await _seed_run_row(
        occurred_at=now - 2 * hour, tenant_id=_TENANT, operator_sub=_SUB, path="other.path"
    )
    await _seed_run_row(
        occurred_at=now - 3 * hour,
        tenant_id=_TENANT,
        operator_sub="operator-1",
        path=_PATH,
        method="DISPATCH",
    )
    await _seed_run_row(
        occurred_at=now - 4 * hour, tenant_id=_TENANT, operator_sub="system:other", path=_PATH
    )
    assert await _read() is None

    # A real run older than the window is not read (it means "due now").
    await _seed_run_row(
        occurred_at=now - timedelta(days=9), tenant_id=_TENANT, operator_sub=_SUB, path=_PATH
    )
    assert await _read() is None

    newer = now - timedelta(days=3)
    for occurred_at in (now - timedelta(days=5), newer):
        await _seed_run_row(
            occurred_at=occurred_at, tenant_id=_TENANT, operator_sub=_SUB, path=_PATH
        )

    last_run = await _read()
    assert last_run is not None
    assert last_run.tzinfo is not None
    assert last_run == newer


@pytest.mark.parametrize(
    ("last_run_offset_seconds", "expected"),
    [
        (None, 0.0),  # no recorded run: due now
        (-_WEEK_SECONDS - 1, 0.0),  # more than one interval ago: due now
        (-_WEEK_SECONDS, 0.0),  # exactly one interval ago: due now
        (-2 * _DAY_SECONDS, float(_WEEK_SECONDS - 2 * _DAY_SECONDS)),  # the rest
        (0, float(_WEEK_SECONDS)),  # just ran: one full interval
        (1, 0.0),  # one second in the future (fast clock): due now
        (30 * _DAY_SECONDS, 0.0),  # far in the future: due now
    ],
)
def test_seconds_until_due(last_run_offset_seconds: int | None, expected: float) -> None:
    """The wait is the rest of the interval; a future last run is due now."""
    now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    last_run = (
        None
        if last_run_offset_seconds is None
        else now + timedelta(seconds=last_run_offset_seconds)
    )
    assert seconds_until_due(last_run, now=now, interval_seconds=_WEEK_SECONDS) == expected


@pytest.mark.asyncio
async def test_last_run_read_failure_runs_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the last run cannot be read, log it and do not wait any longer."""
    monkeypatch.setattr(loop_schedule, "_log", structlog.get_logger(loop_schedule.__name__))
    monkeypatch.setattr(
        loop_schedule,
        "last_recorded_run",
        AsyncMock(side_effect=RuntimeError("database not reachable")),
    )
    sleep = AsyncMock()

    with capture_logs() as captured, patch("asyncio.sleep", new=sleep):
        await wait_until_first_run(
            job="test_cleanup",
            tenant_id=_TENANT,
            operator_sub=_SUB,
            path=_PATH,
            interval_seconds=_WEEK_SECONDS,
        )

    assert [c.args[0] for c in sleep.await_args_list] == [FIRST_RUN_DELAY_SECONDS]
    failed = [e for e in captured if e["event"] == "test_cleanup_last_run_read_failed"]
    assert len(failed) == 1
    assert failed[0]["error_type"] == "RuntimeError"
    scheduled = [e for e in captured if e["event"] == "test_cleanup_scheduled"]
    assert scheduled[0]["next_run_in_seconds"] == 0.0
