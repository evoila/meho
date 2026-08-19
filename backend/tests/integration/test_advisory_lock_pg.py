# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-Postgres regression tests for the #3010 advisory-lock invariant.

The bug: every tick loop took ``pg_try_advisory_lock`` (a session-level,
connection-owned lock) on its work :class:`AsyncSession` and then
committed inside the locked region. Each commit returns the session's
pooled connection; the ``finally`` unlock then ran on a *different*
connection — PG answered ``WARNING: you don't own a lock of type
ExclusiveLock``, returned ``false``, nothing raised, and the lock
stranded on an idle pooled connection. Every later tick drawing any
other connection silently claimed nothing (~35-50 % sensor cadence,
watchdog blind).

These tests run the fixed shape — the lock pinned to a dedicated
connection via :func:`meho_backplane.db.advisory.advisory_lock` — on the
production dialect and assert the issue's acceptance criteria directly:

* mid-lock commits on pool sessions never move or strand the lock, and
  ``pg_locks`` shows **no** advisory holder once the block exits;
* a second would-be holder reads busy while the block is open;
* a sensor-runner tick that claims (and therefore commits) leaves no
  lock behind, and a **subsequent tick claims due rows again**;
* every other converted subsystem tick (scheduler, drain, reaper,
  deadman — the deadman committed mid-lock even on an empty sweep, so
  the old shape stranded its key on every single tick) exits with a
  clean ``pg_locks``.

The ``pg_engine`` fixture from :mod:`tests.integration.conftest` points
the process-wide engine at a ``pgvector/pgvector:pg16`` container and
skips the module when Docker is unavailable (agent sandboxes); CI
provisions containers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from meho_backplane.agent.invocation import AgentInvoker
from meho_backplane.agent.reaper import _run_one_tick as reaper_tick
from meho_backplane.checks.assertions import AssertionOutcome
from meho_backplane.checks.runner import (
    _SENSOR_RUNNER_ADVISORY_LOCK_KEY,
    reset_sensor_runner_state,
    run_one_sensor_tick,
)
from meho_backplane.db.advisory import advisory_lock
from meho_backplane.db.engine import get_engine, get_sessionmaker
from meho_backplane.events.drain import run_one_drain_tick
from meho_backplane.gateway.deadman import _run_one_tick as deadman_tick
from meho_backplane.scheduler.loop import run_one_tick as scheduler_tick
from tests.test_sensor_runner import (
    _create_interval_sensor,
    _drain_in_flight,
    _force_due,
)

_TEST_KEY = 0x4D45_484F_5445_5354  # "MEHOTEST" — collides with no subsystem key


@pytest.fixture(autouse=True)
async def _runner_state() -> AsyncIterator[None]:
    """Reset the sensor runner's per-process state around every test."""
    reset_sensor_runner_state()
    yield
    reset_sensor_runner_state()


async def _advisory_lock_rows() -> list[tuple[int, int, bool]]:
    """Every advisory lock currently held in the container, as pg_locks rows."""
    engine = get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT classid, objid, granted FROM pg_locks WHERE locktype = 'advisory'")
        )
        return [(row.classid, row.objid, row.granted) for row in result]


def _low32(key: int) -> int:
    """The ``objid`` half PG reports for a 64-bit advisory key."""
    return key & 0xFFFF_FFFF


async def test_lock_survives_mid_lock_commits_and_is_released(pg_engine: None) -> None:
    """Pool-session commits inside the block never strand the lock (#3010 AC).

    The exact bug shape: lock, then commit on a pooled session (which
    returns that session's connection), then unlock. With the lock pinned
    to its own connection the unlock lands where the lock lives, and
    ``pg_locks`` reads clean after the block.
    """
    sessionmaker = get_sessionmaker()
    async with advisory_lock(_TEST_KEY, subsystem="test") as locked:
        assert locked is True
        # Two mid-lock commit cycles on pool sessions — each returns its
        # connection to the pool, exactly what used to migrate the unlock.
        for _ in range(2):
            async with sessionmaker() as session:
                await session.execute(text("SELECT 1"))
                await session.commit()
        # While the block is open the lock is visibly granted.
        held = await _advisory_lock_rows()
        assert any(objid == _low32(_TEST_KEY) and granted for _, objid, granted in held)

    # The AC's pg_locks probe: no idle holder left behind.
    assert await _advisory_lock_rows() == []


async def test_second_holder_reads_busy_while_block_open(pg_engine: None) -> None:
    """A concurrent would-be holder gets ``False``; after release it acquires."""
    async with advisory_lock(_TEST_KEY, subsystem="test") as first:
        assert first is True
        async with advisory_lock(_TEST_KEY, subsystem="test") as second:
            assert second is False

    async with advisory_lock(_TEST_KEY, subsystem="test") as again:
        assert again is True
    assert await _advisory_lock_rows() == []


async def test_sensor_tick_claims_commits_and_next_tick_claims_again(
    pg_engine: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner's real tick on PG: claim → advance-commit → unlock → re-claim.

    Encodes the issue's headline acceptance criterion: after the change a
    subsequent tick claims due rows — under the old shape the first
    claiming tick (whose advance committed mid-lock) stranded the key and
    later ticks silently returned 0.
    """
    monkeypatch.setattr(
        "meho_backplane.checks.runner._run_evaluation",
        AsyncMock(return_value=AssertionOutcome(state="ok", value=1, evidence={})),
    )
    sensor_id = await _create_interval_sensor()
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))

    first = await run_one_sensor_tick()
    assert first == 1
    await _drain_in_flight()
    # No idle holder of the runner key (or any advisory key) between ticks.
    locks = await _advisory_lock_rows()
    assert not any(objid == _low32(_SENSOR_RUNNER_ADVISORY_LOCK_KEY) for _, objid, _g in locks)

    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))
    second = await run_one_sensor_tick()
    assert second == 1
    await _drain_in_flight()
    assert await _advisory_lock_rows() == []


async def test_all_converted_subsystem_ticks_leave_pg_locks_clean(pg_engine: None) -> None:
    """Scheduler / drain / reaper / deadman ticks strand no advisory lock.

    The deadman is the sharpest probe: its old shape committed before the
    unlock even on an empty sweep, so *every* tick stranded its key. The
    invoker sentinels are never touched — the tables are empty, so each
    tick runs lock → scan → (commit) → unlock only.
    """
    unused_invoker = cast("AgentInvoker", object())

    assert await scheduler_tick(invoker=unused_invoker) == 0
    assert await _advisory_lock_rows() == []

    assert await run_one_drain_tick(invoker=unused_invoker) == 0
    assert await _advisory_lock_rows() == []

    await reaper_tick()
    assert await _advisory_lock_rows() == []

    await deadman_tick()
    assert await _advisory_lock_rows() == []
