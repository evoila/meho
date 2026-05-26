# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G9.1-T3 scheduled topology-refresh loop.

Coverage matrix (Task #450 acceptance criteria):

* **Sweep iterates every tenant's targets** — a sweep with 2 targets
  across 2 tenants calls :func:`refresh_target_topology` once per
  target with a tenant-scoped system operator.
* **Failure of one target doesn't stall the rest** — one target's
  refresh raises; the other targets are still refreshed and the bad
  one goes on backoff.
* **Backoff window is honoured** — a target still inside its backoff
  window is skipped on the next sweep.
* **Advisory lock no-ops on SQLite** — the dialect gate returns
  ``True`` (proceed) on the non-PG test path so the unit suite
  exercises the real loop without a Postgres container.
* **Lock-contention path** — when ``pg_try_advisory_lock`` reports the
  lock is held elsewhere the target is skipped (no refresh call).
* **Cadence is the configured setting** — the loop sleeps
  ``TOPOLOGY_REFRESH_INTERVAL_SECONDS`` between sweeps.
* **start/stop lifecycle** — the lifespan helpers create and cleanly
  cancel the background task.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target, Tenant
from meho_backplane.settings import get_settings
from meho_backplane.topology import scheduler as sched
from meho_backplane.topology.scheduler import (
    _advisory_lock_key,
    _refresh_one_target,
    _run_one_sweep,
    _SchedulerState,
    start_topology_refresh_scheduler,
    stop_topology_refresh_scheduler,
)

_REFRESH = "meho_backplane.topology.scheduler.refresh_target_topology"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_target(slug: str, name: str) -> tuple[uuid.UUID, Target]:
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()
    target = Target(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=name,
        aliases=[],
        product="faketopo",
        host="h.example.test",
    )
    async with sessionmaker() as session:
        session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
        session.add(target)
        await session.commit()
        await session.refresh(target)
    return tenant_id, target


# ---------------------------------------------------------------------------
# Advisory lock key
# ---------------------------------------------------------------------------


def test_advisory_lock_key_is_deterministic_and_in_range() -> None:
    a = uuid.uuid4()
    b = uuid.uuid4()
    k1 = _advisory_lock_key(a, b)
    k2 = _advisory_lock_key(a, b)
    assert k1 == k2
    assert 0 <= k1 <= 0x7FFF_FFFF_FFFF_FFFF
    # Different pair → (almost surely) different key.
    assert _advisory_lock_key(b, a) != k1


# ---------------------------------------------------------------------------
# Sweep iterates every tenant's targets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_refreshes_every_target_across_tenants() -> None:
    _, target_a = await _seed_target("tenant-a", "vc-a")
    _, target_b = await _seed_target("tenant-b", "vc-b")

    refreshed: list[uuid.UUID] = []

    async def _fake_refresh(target: Target, operator: object) -> object:
        refreshed.append(target.id)
        return object()

    with patch(_REFRESH, new=AsyncMock(side_effect=_fake_refresh)):
        await _run_one_sweep(_SchedulerState())

    assert sorted(refreshed) == sorted([target_a.id, target_b.id])


@pytest.mark.asyncio
async def test_sweep_excludes_soft_deleted_targets() -> None:
    """Soft-deleted targets are skipped by the background refresh sweep.

    Regression test for G0.14-T4 #1145: without the ``deleted_at IS NULL``
    filter in :func:`_run_one_sweep`, the scheduler keeps probing a
    tombstoned target every cadence — generating connector calls,
    audit rows, broadcast events, and graph_node reconciliation against
    a retired target. The soft-delete must apply to the scheduler the
    same way it applies to the resolver, the REST list, the MCP
    ``list_targets`` tool, and the broadcast feed dropdown.

    Seeds one live + one soft-deleted target on the same tenant; the
    sweep must invoke :func:`refresh_target_topology` for the live one
    only. The deletion is stamped on the same ``deleted_at`` column the
    DELETE handler in ``api/v1/targets.py`` writes, so the test exercises
    the production contract end-to-end.
    """
    # Seed one tenant with two targets directly so both share the
    # tenant (``_seed_target`` creates a fresh tenant per call and
    # would collide on the unique ``slug``).
    tenant_id = uuid.uuid4()
    live = Target(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="live-target",
        aliases=[],
        product="faketopo",
        host="live.example.test",
    )
    dead = Target(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="dead-target",
        aliases=[],
        product="faketopo",
        host="dead.example.test",
        deleted_at=datetime.now(UTC),
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(Tenant(id=tenant_id, slug="tenant-soft-delete", name="Tenant"))
        session.add(live)
        session.add(dead)
        await session.commit()

    refreshed: list[uuid.UUID] = []

    async def _fake_refresh(target: Target, operator: object) -> object:
        refreshed.append(target.id)
        return object()

    with patch(_REFRESH, new=AsyncMock(side_effect=_fake_refresh)):
        await _run_one_sweep(_SchedulerState())

    assert refreshed == [live.id]
    assert dead.id not in refreshed


@pytest.mark.asyncio
async def test_sweep_passes_tenant_scoped_system_operator() -> None:
    tenant_id, _target = await _seed_target("tenant-a", "vc-a")
    seen: list[object] = []

    async def _fake_refresh(t: Target, operator: object) -> object:
        seen.append(operator)
        return object()

    with patch(_REFRESH, new=AsyncMock(side_effect=_fake_refresh)):
        await _run_one_sweep(_SchedulerState())

    assert len(seen) == 1
    op = seen[0]
    assert op.tenant_id == tenant_id  # type: ignore[attr-defined]
    assert op.sub == "system:topology-scheduler"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Failure isolation + backoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_failing_target_does_not_stall_others() -> None:
    _, good = await _seed_target("tenant-a", "good")
    _, bad = await _seed_target("tenant-b", "bad")

    refreshed: list[uuid.UUID] = []

    async def _fake_refresh(target: Target, operator: object) -> object:
        refreshed.append(target.id)
        if target.id == bad.id:
            raise RuntimeError("connector down")
        return object()

    state = _SchedulerState()
    with patch(_REFRESH, new=AsyncMock(side_effect=_fake_refresh)):
        await _run_one_sweep(state)

    assert good.id in refreshed
    assert bad.id in refreshed
    # The bad target is now on backoff.
    assert bad.id in state.backoff
    assert state.backoff[bad.id].consecutive_failures == 1
    assert state.backoff[bad.id].skip_until > 0


@pytest.mark.asyncio
async def test_target_inside_backoff_window_is_skipped() -> None:
    _, target = await _seed_target("tenant-a", "vc-a")
    state = _SchedulerState()

    calls: list[uuid.UUID] = []

    async def _fake_refresh(t: Target, operator: object) -> object:
        calls.append(t.id)
        return object()

    # Pre-load a backoff window that has not elapsed.
    bo = state.backoff.setdefault(target.id, sched._TargetBackoff())
    bo.consecutive_failures = 2
    bo.skip_until = asyncio.get_event_loop().time() + 9999

    with patch(_REFRESH, new=AsyncMock(side_effect=_fake_refresh)):
        await _refresh_one_target(target, state)

    assert calls == []


@pytest.mark.asyncio
async def test_success_clears_backoff() -> None:
    _, target = await _seed_target("tenant-a", "vc-a")
    state = _SchedulerState()
    bo = state.backoff.setdefault(target.id, sched._TargetBackoff())
    bo.consecutive_failures = 1
    bo.skip_until = 0.0  # already elapsed

    with patch(_REFRESH, new=AsyncMock(return_value=object())):
        await _refresh_one_target(target, state)

    assert target.id not in state.backoff


# ---------------------------------------------------------------------------
# Advisory lock behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advisory_lock_noop_on_sqlite_lets_refresh_proceed() -> None:
    _, target = await _seed_target("tenant-a", "vc-a")
    state = _SchedulerState()
    calls: list[uuid.UUID] = []

    async def _fake_refresh(t: Target, operator: object) -> object:
        calls.append(t.id)
        return object()

    with patch(_REFRESH, new=AsyncMock(side_effect=_fake_refresh)):
        await _refresh_one_target(target, state)

    assert calls == [target.id]


@pytest.mark.asyncio
async def test_locked_target_is_skipped() -> None:
    _, target = await _seed_target("tenant-a", "vc-a")
    state = _SchedulerState()
    calls: list[uuid.UUID] = []

    async def _fake_refresh(t: Target, operator: object) -> object:
        calls.append(t.id)
        return object()

    with (
        patch(
            "meho_backplane.topology.scheduler._try_advisory_lock",
            new=AsyncMock(return_value=False),
        ),
        patch(_REFRESH, new=AsyncMock(side_effect=_fake_refresh)),
    ):
        await _refresh_one_target(target, state)

    assert calls == []


# ---------------------------------------------------------------------------
# Cadence + lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_sleeps_configured_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOPOLOGY_REFRESH_INTERVAL_SECONDS", "1234")
    get_settings.cache_clear()

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        # Stop the loop after the first sleep.
        raise asyncio.CancelledError

    with (
        patch(
            "meho_backplane.topology.scheduler._run_one_sweep",
            new=AsyncMock(),
        ),
        patch("asyncio.sleep", new=_fake_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await sched._scheduler_loop()

    assert sleeps == [1234]


@pytest.mark.asyncio
async def test_start_and_stop_scheduler_lifecycle() -> None:
    with patch(
        "meho_backplane.topology.scheduler._run_one_sweep",
        new=AsyncMock(),
    ):
        task = start_topology_refresh_scheduler()
        assert not task.done()
        await stop_topology_refresh_scheduler(task)
        assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_loop_survives_sweep_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOPOLOGY_REFRESH_INTERVAL_SECONDS", "5")
    get_settings.cache_clear()

    sweep_calls = 0

    async def _flaky_sweep(state: object) -> None:
        nonlocal sweep_calls
        sweep_calls += 1
        raise RuntimeError("transient enumerate failure")

    async def _fake_sleep(seconds: float) -> None:
        # Allow exactly one sweep + sleep, then stop the loop.
        raise asyncio.CancelledError

    with (
        patch(
            "meho_backplane.topology.scheduler._run_one_sweep",
            new=_flaky_sweep,
        ),
        patch("asyncio.sleep", new=_fake_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await sched._scheduler_loop()

    # The sweep raised but the loop reached the sleep (didn't die).
    assert sweep_calls == 1
