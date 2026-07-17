# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the deterministic sensor check-runner (#2505).

Initiative #2416 (parent goal #221), Task #2505. Coverage matrix mapped to the
issue's acceptance criteria:

* **Disabled via SENSOR_RUNNER_ENABLED=false** -- the setting resolves false so
  the lifespan gate skips starting the task.
* **Distinct advisory-lock key** -- the runner's key differs from the
  scheduler's ``_SCHEDULER_ADVISORY_LOCK_KEY`` so the two loops never contend.
* **At-most-once** -- a dispatch that raises still leaves ``next_fire_at``
  advanced; an immediate second tick dispatches nothing more.
* **Replica-safety** -- two concurrent ticks over one due sensor dispatch once.
* **Both cadences advance** -- interval by exactly its interval; cron via
  ``next_fire_after``.
* **Corrupt cadence parks** -- an unparseable persisted cron parks the row and
  is never re-claimed.
* **Overlap guard + no lock-wedge** -- a still-running evaluation makes the next
  tick skip dispatch (logging ``sensor_evaluation_overlap_skipped``), and the
  tick returns while the evaluation is still pending.
* **Identity** -- the dispatch runs as a synthetic per-tenant USER operator
  whose ``sub`` is the sensor's ``identity_sub``.
* **Failure mapping** -- non-``ok`` dispatch statuses and an evaluation timeout
  persist ``unknown``; ``ok`` routes the payload into #2504's evaluator.
* **Paused sensors** -- never claimed.
* **Lifecycle** -- start/stop is clean and cancels outstanding evaluations.

The tests run on the autouse SQLite-backed engine from :mod:`tests.conftest`.
``dispatch`` is stubbed via monkeypatch on the runner module so no connector or
network is hit (python_best_practices §14 -- no network in unit tests).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog

from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.checks.assertions import AssertionSpec
from meho_backplane.checks.repository import (
    advance_sensor_next_fire,
    create_sensor,
)
from meho_backplane.checks.runner import (
    _IN_FLIGHT,
    _SENSOR_RUNNER_ADVISORY_LOCK_KEY,
    reset_sensor_runner_state,
    run_one_sensor_tick,
    start_sensor_runner,
    stop_sensor_runner,
)
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Sensor, SensorCadenceKind, SensorStatus, Tenant
from meho_backplane.scheduler.cron import next_fire_after
from meho_backplane.scheduler.loop import _SCHEDULER_ADVISORY_LOCK_KEY
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

# A threshold assertion that reads ``ok`` for a payload ``{"count": 3}``:
# ``3 > 10`` is False, so no critical violation.
_OK_ASSERTION: dict[str, Any] = AssertionSpec.model_validate(
    {
        "select": {"path": "$.count"},
        "compare": {"type": "threshold", "op": "gt", "critical": 10},
    }
).model_dump(mode="json")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires; reset runner state."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SENSOR_RUNNER_ENABLED", "false")
    get_settings.cache_clear()
    reset_sensor_runner_state()
    yield
    reset_sensor_runner_state()
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _seed_tenant(tenant_id: uuid.UUID = _TENANT) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if await session.get(Tenant, tenant_id) is None:
            session.add(Tenant(id=tenant_id, slug=str(tenant_id)[:8], name="Tenant C"))
            await session.commit()


async def _create_interval_sensor(
    *,
    interval_seconds: int = 300,
    identity_sub: str = "__sensor__",
    assertion: dict[str, Any] | None = None,
    tenant_id: uuid.UUID = _TENANT,
    base: datetime | None = None,
) -> uuid.UUID:
    await _seed_tenant(tenant_id)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await create_sensor(
            session,
            tenant_id=tenant_id,
            name=f"sensor-{uuid.uuid4().hex[:8]}",
            connector_id="vmware-rest-9.0",
            op_id="vmware.vm.list",
            target=None,
            params={},
            assertion=assertion if assertion is not None else _OK_ASSERTION,
            cadence_kind=SensorCadenceKind.INTERVAL,
            interval_seconds=interval_seconds,
            cron_expr=None,
            timezone="UTC",
            severity="critical",
            for_seconds=0,
            identity_sub=identity_sub,
            created_by_sub="op-admin",
            base=base,
        )
        await session.commit()
        return row.id


async def _create_cron_sensor(
    *,
    cron_expr: str = "*/5 * * * *",
    timezone: str = "UTC",
    tenant_id: uuid.UUID = _TENANT,
    base: datetime,
) -> uuid.UUID:
    await _seed_tenant(tenant_id)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await create_sensor(
            session,
            tenant_id=tenant_id,
            name=f"sensor-{uuid.uuid4().hex[:8]}",
            connector_id="vmware-rest-9.0",
            op_id="vmware.vm.list",
            target=None,
            params={},
            assertion=_OK_ASSERTION,
            cadence_kind=SensorCadenceKind.CRON,
            interval_seconds=None,
            cron_expr=cron_expr,
            timezone=timezone,
            severity="critical",
            for_seconds=0,
            identity_sub="__sensor__",
            created_by_sub="op-admin",
            base=base,
        )
        await session.commit()
        return row.id


async def _force_due(sensor_id: uuid.UUID, when: datetime) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(Sensor, sensor_id)
        assert row is not None
        row.next_fire_at = when
        await session.commit()


async def _set_status(sensor_id: uuid.UUID, status: SensorStatus) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(Sensor, sensor_id)
        assert row is not None
        row.status = status.value
        await session.commit()


async def _get_sensor(sensor_id: uuid.UUID) -> Sensor:
    """Load a sensor and return it (no commit -- attrs stay accessible detached)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(Sensor, sensor_id)
        assert row is not None
        return row


def _aware(dt: datetime | None) -> datetime | None:
    """Attach UTC to a naive datetime (aiosqlite drops tz on round-trip)."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def _drain_in_flight(timeout: float = 3.0) -> None:
    """Await every outstanding evaluation task until the registry drains."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while _IN_FLIGHT and loop.time() < deadline:
        tasks = list(_IN_FLIGHT.values())
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)


async def _wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate() and loop.time() < deadline:
        await asyncio.sleep(0)
    assert predicate(), "condition not met within timeout"


def _ok_result(payload: dict[str, Any]) -> OperationResult:
    return OperationResult(status="ok", op_id="vmware.vm.list", result=payload, duration_ms=1.0)


# --------------------------------------------------------------------------- #
# Settings gate + advisory-lock key
# --------------------------------------------------------------------------- #


def test_sensor_runner_disabled_setting_resolves_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SENSOR_RUNNER_ENABLED=false reads through to the settings flag.

    The lifespan gate is ``if settings.sensor_runner_enabled:
    start_sensor_runner()``; a false flag means no task is created.
    """
    monkeypatch.setenv("SENSOR_RUNNER_ENABLED", "false")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    get_settings.cache_clear()
    assert get_settings().sensor_runner_enabled is False
    get_settings.cache_clear()


def test_sensor_runner_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner is on by default (the shipped in-process evaluator)."""
    monkeypatch.delenv("SENSOR_RUNNER_ENABLED", raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.sensor_runner_enabled is True
    assert settings.sensor_runner_tick_interval_seconds == 10
    get_settings.cache_clear()


def test_advisory_lock_key_differs_from_scheduler() -> None:
    """The runner's advisory-lock key must not collide with the scheduler's."""
    assert _SENSOR_RUNNER_ADVISORY_LOCK_KEY != _SCHEDULER_ADVISORY_LOCK_KEY
    # Non-negative so it round-trips through asyncpg's signed bigint binding.
    assert 0 <= _SENSOR_RUNNER_ADVISORY_LOCK_KEY <= 0x7FFF_FFFF_FFFF_FFFF


# --------------------------------------------------------------------------- #
# Cadence advance (value-assert, deterministic fire_instant)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_interval_cadence_advances_by_exact_interval() -> None:
    """An interval sensor advances ``next_fire_at`` by exactly its interval."""
    sensor_id = await _create_interval_sensor(interval_seconds=45)
    fire_instant = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(Sensor, sensor_id)
        assert row is not None
        advanced = await advance_sensor_next_fire(session, row, fire_instant=fire_instant)
        assert advanced is not None
        assert _aware(advanced.next_fire_at) == fire_instant + timedelta(seconds=45)


@pytest.mark.asyncio
async def test_cron_cadence_advances_via_next_fire_after() -> None:
    """A cron sensor's advanced ``next_fire_at`` equals ``next_fire_after``."""
    sensor_id = await _create_cron_sensor(
        cron_expr="*/5 * * * *",
        base=datetime(2026, 5, 25, 11, 0, 0, tzinfo=UTC),
    )
    fire_instant = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(Sensor, sensor_id)
        assert row is not None
        advanced = await advance_sensor_next_fire(session, row, fire_instant=fire_instant)
        assert advanced is not None
        assert _aware(advanced.next_fire_at) == next_fire_after("*/5 * * * *", fire_instant, "UTC")
        assert _aware(advanced.next_fire_at) == datetime(2026, 5, 25, 12, 5, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_lost_advance_race_returns_none() -> None:
    """A conditional advance loses to a claimer that already advanced the row.

    The conditional ``WHERE next_fire_at=:previous`` is the single-fire guard on
    the SKIP-LOCKED-less dialect: a second claimer holding a stale
    ``next_fire_at`` matches zero rows and returns ``None`` (the other claimer
    owns this tick).
    """
    sensor_id = await _create_interval_sensor(interval_seconds=60)
    fire_instant = datetime(2026, 5, 25, tzinfo=UTC)
    sessionmaker = get_sessionmaker()
    # Session A loads the row at its current next_fire_at.
    async with sessionmaker() as session_a:
        row_a = await session_a.get(Sensor, sensor_id)
        assert row_a is not None

        # A concurrent claimer (session B) advances the same row first.
        async with sessionmaker() as session_b:
            row_b = await session_b.get(Sensor, sensor_id)
            assert row_b is not None
            advanced_b = await advance_sensor_next_fire(session_b, row_b, fire_instant=fire_instant)
            assert advanced_b is not None
            await session_b.commit()

        # Session A still holds the stale next_fire_at; its conditional advance
        # matches zero rows.
        advanced_a = await advance_sensor_next_fire(session_a, row_a, fire_instant=fire_instant)
        assert advanced_a is None


# --------------------------------------------------------------------------- #
# At-most-once + replica safety
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_at_most_once_dispatch_that_raises_advances_and_does_not_refire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dispatch that raises still advances ``next_fire_at``; a second tick is a no-op."""
    calls = 0

    async def _raising_dispatch(**_kwargs: Any) -> OperationResult:
        nonlocal calls
        calls += 1
        raise RuntimeError("connector exploded")

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _raising_dispatch)

    sensor_id = await _create_interval_sensor(interval_seconds=300)
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))

    dispatched = await run_one_sensor_tick()
    assert dispatched == 1
    await _drain_in_flight()
    assert calls == 1

    # ``next_fire_at`` advanced past the claimed instant (into the future).
    advanced = await _get_sensor(sensor_id)
    next_fire = _aware(advanced.next_fire_at)
    assert next_fire is not None
    assert next_fire > datetime.now(UTC) - timedelta(seconds=10)

    # An immediate second tick finds nothing due -> zero further dispatch.
    dispatched_again = await run_one_sensor_tick()
    await _drain_in_flight()
    assert dispatched_again == 0
    assert calls == 1


@pytest.mark.asyncio
async def test_raising_dispatch_persists_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dispatch() that raises a non-timeout error persists ``unknown`` instead of
    stranding the projection -- the ``_run_evaluation`` never-raises contract."""

    async def _raising_dispatch(**_kwargs: Any) -> OperationResult:
        raise RuntimeError("connector blew up")

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _raising_dispatch)

    sensor_id = await _create_interval_sensor(interval_seconds=300)
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))

    await run_one_sensor_tick()
    await _drain_in_flight()

    row = await _get_sensor(sensor_id)
    assert row.last_state == "unknown"
    assert row.last_evidence is not None
    assert row.last_evidence["reason"] == "dispatch_error"
    assert "connector blew up" in row.last_evidence["error"]


@pytest.mark.asyncio
async def test_two_concurrent_sensor_ticks_never_double_evaluate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent ticks over one due sensor dispatch exactly once."""
    calls = 0

    async def _counting_dispatch(**_kwargs: Any) -> OperationResult:
        nonlocal calls
        calls += 1
        return _ok_result({"count": 3})

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _counting_dispatch)

    sensor_id = await _create_interval_sensor(interval_seconds=300)
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))

    results = await asyncio.gather(run_one_sensor_tick(), run_one_sensor_tick())
    assert sum(results) == 1, f"expected exactly one dispatch, got {results}"
    await _drain_in_flight()
    assert calls == 1


# --------------------------------------------------------------------------- #
# Corrupt cadence parks
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_corrupt_cron_cadence_parks_and_is_not_reclaimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unparseable persisted cron parks the row; it is never re-claimed."""
    calls = 0

    async def _counting_dispatch(**_kwargs: Any) -> OperationResult:
        nonlocal calls
        calls += 1
        return _ok_result({"count": 3})

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _counting_dispatch)

    sensor_id = await _create_cron_sensor(
        cron_expr="*/5 * * * *",
        base=datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC),
    )
    # Corrupt the persisted cron expression (bypassing the create validator)
    # and force it due.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(Sensor, sensor_id)
        assert row is not None
        row.cron_expr = "not a cron expr"
        row.next_fire_at = datetime(2026, 1, 1, tzinfo=UTC)
        await session.commit()

    dispatched = await run_one_sensor_tick()
    await _drain_in_flight()
    assert dispatched == 0
    assert calls == 0

    parked = await _get_sensor(sensor_id)
    assert parked.status == SensorStatus.PAUSED.value
    assert parked.status_reason
    assert "invalid_cadence" in parked.status_reason

    # Paused row is never re-claimed on the next tick.
    dispatched_again = await run_one_sensor_tick()
    await _drain_in_flight()
    assert dispatched_again == 0
    assert calls == 0


@pytest.mark.asyncio
async def test_paused_sensor_due_in_past_is_never_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paused sensor overdue in the past yields zero dispatches."""
    calls = 0

    async def _counting_dispatch(**_kwargs: Any) -> OperationResult:
        nonlocal calls
        calls += 1
        return _ok_result({"count": 3})

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _counting_dispatch)

    sensor_id = await _create_interval_sensor(interval_seconds=60)
    await _force_due(sensor_id, datetime(2026, 1, 1, tzinfo=UTC))
    await _set_status(sensor_id, SensorStatus.PAUSED)

    dispatched = await run_one_sensor_tick()
    await _drain_in_flight()
    assert dispatched == 0
    assert calls == 0


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dispatch_runs_as_synthetic_tenant_user_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatch operator carries the sensor's identity_sub / tenant / USER kind."""
    captured: dict[str, Any] = {}

    async def _capturing_dispatch(*, operator: Operator, **_kwargs: Any) -> OperationResult:
        captured["operator"] = operator
        return _ok_result({"count": 3})

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _capturing_dispatch)

    sensor_id = await _create_interval_sensor(interval_seconds=300)
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))

    await run_one_sensor_tick()
    await _drain_in_flight()

    operator = captured["operator"]
    assert isinstance(operator, Operator)
    assert operator.sub == "__sensor__"
    assert operator.tenant_id == _TENANT
    assert operator.principal_kind is PrincipalKind.USER
    assert operator.raw_jwt == ""


@pytest.mark.asyncio
async def test_dispatch_uses_custom_identity_sub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sensor created with an explicit identity_sub dispatches under it."""
    captured: dict[str, Any] = {}

    async def _capturing_dispatch(*, operator: Operator, **_kwargs: Any) -> OperationResult:
        captured["operator"] = operator
        return _ok_result({"count": 3})

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _capturing_dispatch)

    sensor_id = await _create_interval_sensor(interval_seconds=300, identity_sub="svc:sensor-x")
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))

    await run_one_sensor_tick()
    await _drain_in_flight()
    assert captured["operator"].sub == "svc:sensor-x"


# --------------------------------------------------------------------------- #
# Failure mapping + ok routing
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ok_dispatch_routes_payload_into_evaluator_and_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``status=='ok'`` feeds the payload to the evaluator and persists its outcome."""

    async def _ok_dispatch(**_kwargs: Any) -> OperationResult:
        return _ok_result({"count": 3})

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _ok_dispatch)

    sensor_id = await _create_interval_sensor(interval_seconds=300)
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))

    await run_one_sensor_tick()
    await _drain_in_flight()

    row = await _get_sensor(sensor_id)
    assert row.last_state == "ok"
    assert row.last_value == 3
    assert row.last_evidence is not None
    assert row.last_evidence["observed"] == 3
    assert row.last_evaluated_at is not None
    assert row.state_since is not None


@pytest.mark.asyncio
async def test_ok_dispatch_critical_state_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A payload violating the threshold persists ``critical`` (evaluator verdict)."""

    async def _ok_dispatch(**_kwargs: Any) -> OperationResult:
        return _ok_result({"count": 42})  # 42 > 10 -> critical

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _ok_dispatch)

    sensor_id = await _create_interval_sensor(interval_seconds=300)
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))

    await run_one_sensor_tick()
    await _drain_in_flight()

    row = await _get_sensor(sensor_id)
    assert row.last_state == "critical"
    assert row.last_value == 42


@pytest.mark.parametrize("status", ["error", "denied", "awaiting_approval", "pending"])
@pytest.mark.asyncio
async def test_non_ok_dispatch_persists_unknown(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    """Any non-``ok`` dispatch status persists ``unknown`` with the status in evidence."""

    async def _non_ok_dispatch(**_kwargs: Any) -> OperationResult:
        return OperationResult(
            status=status,
            op_id="vmware.vm.list",
            error="boom" if status in ("error", "denied") else None,
            duration_ms=1.0,
        )

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _non_ok_dispatch)

    sensor_id = await _create_interval_sensor(interval_seconds=300)
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))

    await run_one_sensor_tick()
    await _drain_in_flight()

    row = await _get_sensor(sensor_id)
    assert row.last_state == "unknown"
    assert row.last_evidence is not None
    assert row.last_evidence["dispatch_status"] == status
    assert row.last_evidence["reason"] == "dispatch_not_ok"


@pytest.mark.asyncio
async def test_evaluation_timeout_persists_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dispatch that outlasts the per-evaluation timeout persists ``unknown``."""
    monkeypatch.setattr("meho_backplane.checks.runner._EVAL_TIMEOUT_SECONDS", 0.05)

    async def _slow_dispatch(**_kwargs: Any) -> OperationResult:
        await asyncio.sleep(5.0)
        return _ok_result({"count": 3})

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _slow_dispatch)

    sensor_id = await _create_interval_sensor(interval_seconds=300)
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))

    await run_one_sensor_tick()
    await _drain_in_flight()

    row = await _get_sensor(sensor_id)
    assert row.last_state == "unknown"
    assert row.last_evidence is not None
    assert row.last_evidence["reason"] == "evaluation_timeout"


# --------------------------------------------------------------------------- #
# Overlap guard + no lock-wedge (blocked dispatch)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tick_returns_while_evaluation_pending_no_lock_wedge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_one_sensor_tick`` returns while the evaluation task is still pending."""
    gate = asyncio.Event()

    async def _blocked_dispatch(**_kwargs: Any) -> OperationResult:
        await gate.wait()
        return _ok_result({"count": 3})

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _blocked_dispatch)

    sensor_id = await _create_interval_sensor(interval_seconds=300)
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))

    try:
        dispatched = await run_one_sensor_tick()
        assert dispatched == 1
        task = _IN_FLIGHT[sensor_id]
        assert task.done() is False
    finally:
        gate.set()
        await _drain_in_flight()


@pytest.mark.asyncio
async def test_overlap_guard_skips_second_dispatch_and_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A still-running evaluation makes the next due tick skip dispatch + log it."""
    gate = asyncio.Event()
    calls = 0

    async def _blocked_dispatch(**_kwargs: Any) -> OperationResult:
        nonlocal calls
        calls += 1
        await gate.wait()
        return _ok_result({"count": 3})

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _blocked_dispatch)

    sensor_id = await _create_interval_sensor(interval_seconds=300)
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))

    try:
        # Tick 1 spawns the evaluation; wait until it is blocked in dispatch.
        await run_one_sensor_tick()
        await _wait_until(lambda: calls == 1)

        # Force it due again; the second tick must skip the still-in-flight
        # sensor and log the overlap.
        await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))
        with structlog.testing.capture_logs() as logs:
            dispatched = await run_one_sensor_tick()

        assert dispatched == 0
        assert calls == 1
        events = [entry.get("event") for entry in logs]
        assert "sensor_evaluation_overlap_skipped" in events
    finally:
        gate.set()
        await _drain_in_flight()


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_start_and_stop_sensor_runner_lifecycle_clean() -> None:
    """The lifespan helpers create + cancel the loop task without GC warnings."""
    task = start_sensor_runner()
    assert not task.done()
    await stop_sensor_runner(task)
    assert task.done()
    assert task.cancelled() or task.exception() is None


@pytest.mark.asyncio
async def test_stop_cancels_outstanding_evaluations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stop_sensor_runner`` cancels + drains in-flight evaluation tasks."""
    gate = asyncio.Event()

    async def _blocked_dispatch(**_kwargs: Any) -> OperationResult:
        await gate.wait()
        return _ok_result({"count": 3})

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _blocked_dispatch)

    sensor_id = await _create_interval_sensor(interval_seconds=300)
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))

    await run_one_sensor_tick()
    evaluation = _IN_FLIGHT[sensor_id]
    assert not evaluation.done()

    loop_task = start_sensor_runner()
    try:
        await stop_sensor_runner(loop_task)
    finally:
        gate.set()

    assert evaluation.done()
    assert not _IN_FLIGHT
