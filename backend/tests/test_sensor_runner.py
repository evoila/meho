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
import contextlib
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
import structlog

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.checks.assertions import AssertionSpec
from meho_backplane.checks.repository import (
    advance_sensor_next_fire,
    create_sensor,
)
from meho_backplane.checks.runner import (
    _IN_FLIGHT,
    _SENSOR_RUNNER_ADVISORY_LOCK_KEY,
    _sensor_operator,
    _SensorSnapshot,
    reset_sensor_runner_state,
    run_one_sensor_tick,
    start_sensor_runner,
    stop_sensor_runner,
)
from meho_backplane.connectors._shared import credential_backend
from meho_backplane.connectors._shared.gsm_creds import GcpSecretManagerBackend
from meho_backplane.connectors._shared.vault_creds import load_basic_credentials
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Sensor, SensorCadenceKind, SensorStatus, Tenant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
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


# --------------------------------------------------------------------------- #
# Real resolve -> dispatch path (#2595): dispatch is NOT stubbed
# --------------------------------------------------------------------------- #
#
# The blind spot #2595 closes: every test above monkeypatches
# ``checks.runner.dispatch``, so the runner's target-resolution + connector-
# resolution path never ran. Before #2595 the runner forwarded the sensor's
# raw stored ``target`` dict straight to ``dispatch``; the connector resolver
# reads ``product`` / ``version`` off a resolved ``Target`` row (not off a bare
# ``{"name": ...}`` dict), so every target-bound sensor failed ``no_connector``
# while ``POST /api/v1/operations/call`` with the same triple succeeded. These
# tests register a real k8s-mould connector (a ``(product, "", "")`` wildcard
# sibling, the shape that makes a fresh typed target resolve) and drive a real
# runner tick through the actual resolve -> dispatch seam.

_K8S_MOULD_PRODUCT = "k8smould"
_K8S_MOULD_CONNECTOR_ID = f"{_K8S_MOULD_PRODUCT}-1.x"
_K8S_MOULD_OP = f"{_K8S_MOULD_PRODUCT}.pod.count"


async def _k8s_count_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Module-level typed handler returning a payload the evaluator reads.

    ``{"count": 3}`` against ``_OK_ASSERTION`` (``$.count > 10`` critical)
    evaluates ``ok`` -- so a green tick through the real path lands
    ``last_state == "ok"``, not ``unknown``.
    """
    return {"count": 3}


class _K8sMouldConnector(Connector):
    """Connector class the resolver picks for the k8s-mould target."""

    product = _K8S_MOULD_PRODUCT
    version = "1.x"
    impl_id = _K8S_MOULD_PRODUCT

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        raise NotImplementedError


@pytest.fixture
def _reset_registry() -> Iterator[None]:
    """Clear the process-global connector registry + dispatcher caches.

    The real-dispatch tests register a connector + typed op; scrub both around
    each so no registration leaks into a sibling test (mirrors
    ``test_operations_dispatcher``'s autouse ``_reset_module_state``).
    """
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def _embedding_stub() -> AsyncMock:
    """Deterministic embedding stub so ``register_typed_operation`` skips ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
def _captured_broadcast(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Record broadcast events a real dispatch emits instead of hitting the bus."""
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


async def _register_k8s_mould(embedding_service: AsyncMock) -> None:
    register_connector_v2(
        product=_K8S_MOULD_PRODUCT,
        version="",
        impl_id="",
        cls=_K8sMouldConnector,
    )
    await register_typed_operation(
        product=_K8S_MOULD_PRODUCT,
        version="1.x",
        impl_id=_K8S_MOULD_PRODUCT,
        op_id=_K8S_MOULD_OP,
        handler=_k8s_count_handler,
        summary="Count pods on the cluster.",
        description="Return the pod count for the resolved cluster target.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding_service,
    )


async def _seed_target(
    *,
    name: str,
    aliases: list[str] | None = None,
    product: str = _K8S_MOULD_PRODUCT,
    version: str = "1.x",
    tenant_id: uuid.UUID = _TENANT,
    secret_ref: str | None = None,
) -> uuid.UUID:
    await _seed_tenant(tenant_id)
    target_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s, s.begin():
        s.add(
            TargetORM(
                id=target_id,
                tenant_id=tenant_id,
                name=name,
                aliases=aliases or [],
                product=product,
                version=version,
                host="k8s.test",
                port=6443,
                fqdn=None,
                secret_ref=secret_ref,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    return target_id


async def _create_target_bound_sensor(
    *,
    target: dict[str, Any] | None,
    connector_id: str = _K8S_MOULD_CONNECTOR_ID,
    op_id: str = _K8S_MOULD_OP,
    tenant_id: uuid.UUID = _TENANT,
) -> uuid.UUID:
    await _seed_tenant(tenant_id)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await create_sensor(
            session,
            tenant_id=tenant_id,
            name=f"sensor-{uuid.uuid4().hex[:8]}",
            connector_id=connector_id,
            op_id=op_id,
            target=target,
            params={},
            assertion=_OK_ASSERTION,
            cadence_kind=SensorCadenceKind.INTERVAL,
            interval_seconds=300,
            cron_expr=None,
            timezone="UTC",
            severity="critical",
            for_seconds=0,
            identity_sub="__sensor__",
            created_by_sub="op-admin",
        )
        await session.commit()
        return row.id


@pytest.mark.asyncio
async def test_target_bound_sensor_resolves_and_dispatches_real_ok(
    _reset_registry: None,
    _embedding_stub: AsyncMock,
    _captured_broadcast: list[BroadcastEvent],
) -> None:
    """A target-bound sensor evaluates end-to-end through the real resolve->dispatch.

    Regression for #2595 (and the ``test_sensor_runner.py`` stubbed-dispatch
    blind spot): ``dispatch`` is **not** stubbed. Registering a target for the
    typed wildcard-version connector and running a tick lands ``ok``/``3`` --
    parity with ``POST /api/v1/operations/call`` for the same triple.
    """
    await _register_k8s_mould(_embedding_stub)
    await _seed_target(name="k8s-prod")

    sensor_id = await _create_target_bound_sensor(target={"name": "k8s-prod"})
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))

    await run_one_sensor_tick()
    await _drain_in_flight()

    row = await _get_sensor(sensor_id)
    assert row.last_state == "ok", row.last_evidence
    assert row.last_state != "unknown"
    assert row.last_value == 3
    assert row.last_evidence is not None
    assert row.last_evidence["observed"] == 3


@pytest.mark.asyncio
async def test_inline_target_object_resolves_by_name_real_ok(
    _reset_registry: None,
    _embedding_stub: AsyncMock,
    _captured_broadcast: list[BroadcastEvent],
) -> None:
    """A full inline ``{"name": ...}`` target object resolves by name and dispatches ok.

    Resolve-by-name is the primary contract: the stored object is normalised to
    its ``name`` and resolved to the registered row, same as a bare string.
    """
    await _register_k8s_mould(_embedding_stub)
    await _seed_target(name="k8s-prod", aliases=["prod-cluster"])

    # Resolve via an alias carried on the inline object's ``name``.
    sensor_id = await _create_target_bound_sensor(target={"name": "prod-cluster"})
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))

    await run_one_sensor_tick()
    await _drain_in_flight()

    row = await _get_sensor(sensor_id)
    assert row.last_state == "ok", row.last_evidence
    assert row.last_value == 3


@pytest.mark.asyncio
async def test_unresolvable_target_name_yields_no_target_evidence(
    _reset_registry: None,
    _embedding_stub: AsyncMock,
    _captured_broadcast: list[BroadcastEvent],
) -> None:
    """A target name matching no live target reads ``no_target`` -- not ``no_connector``.

    The #2595 legibility fix: resolution failure rides the #136/#2110 target
    vocabulary in the evidence ``reason`` instead of the misleading
    ``no_connector`` the raw-dict dispatch used to yield.
    """
    await _register_k8s_mould(_embedding_stub)
    # No target seeded under this name.

    sensor_id = await _create_target_bound_sensor(target={"name": "ghost-cluster"})
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))

    await run_one_sensor_tick()
    await _drain_in_flight()

    row = await _get_sensor(sensor_id)
    assert row.last_state == "unknown"
    assert row.last_evidence is not None
    assert row.last_evidence["reason"] == "no_target"
    assert "no_connector" not in str(row.last_evidence)


@pytest.mark.asyncio
async def test_alias_collision_yields_ambiguous_target_evidence(
    _reset_registry: None,
    _embedding_stub: AsyncMock,
    _captured_broadcast: list[BroadcastEvent],
) -> None:
    """A target name that collides across two aliases reads ``ambiguous_target``."""
    await _register_k8s_mould(_embedding_stub)
    # Two distinct targets sharing one alias -> the resolver's alias step
    # returns >1 row -> AmbiguousTargetError.
    await _seed_target(name="k8s-a", aliases=["k8s-shared"])
    await _seed_target(name="k8s-b", aliases=["k8s-shared"])

    sensor_id = await _create_target_bound_sensor(target={"name": "k8s-shared"})
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))

    await run_one_sensor_tick()
    await _drain_in_flight()

    row = await _get_sensor(sensor_id)
    assert row.last_state == "unknown"
    assert row.last_evidence is not None
    assert row.last_evidence["reason"] == "ambiguous_target"


# --------------------------------------------------------------------------- #
# Background-dispatch credential identity (#2642)
# --------------------------------------------------------------------------- #
#
# #2595 made target-bound sensors resolve their target; this is the next layer
# down. A Sensor on a target whose credentials live in a credential store still
# could not evaluate: the runner dispatched with ``raw_jwt=""`` and the shared
# loader fail-closed before any backend ran. On a ``credentialBackend=gsm``
# deploy using per-operator WIF that is total -- the WIF exchange needs a
# subject token -- so every credentialed Sensor read ``unknown`` forever while
# ``POST /api/v1/operations/call`` with the same triple succeeded.
#
# These tests drive a real tick through the real resolve -> dispatch ->
# credential-load path with a stubbed STS exchange (the ``wif_credentials_factory``
# seam) and a stubbed Keycloak token endpoint (respx). No network, no GCP.

_CREDS_OP = f"{_K8S_MOULD_PRODUCT}.pod.count.creds"
_RUNNER_CLIENT_ID = "meho-check-runner"
_RUNNER_CLIENT_SECRET = "check-runner-secret"
_RUNNER_TOKEN = "runner.principal.jwt"
_KC_TOKEN_URL = "https://keycloak.test/realms/meho/protocol/openid-connect/token"
_WIF_AUDIENCE = (
    "//iam.googleapis.com/projects/123/locations/global/"
    "workloadIdentityPools/meho-pool/providers/keycloak"
)


async def _k8s_count_with_credentials(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler that resolves the target's stored credentials before answering.

    The shape every REST connector's session builder has: load the target's
    ``secret_ref`` under the caller's identity, then talk to the vendor. A
    credential-read failure propagates and the dispatcher renders it as
    ``connector_error: <class>``.
    """
    await load_basic_credentials(target, operator)
    return {"count": 3}


async def _register_credentialed_op(embedding_service: AsyncMock) -> None:
    """Register the k8s mould plus a second op that needs target credentials."""
    await _register_k8s_mould(embedding_service)
    await register_typed_operation(
        product=_K8S_MOULD_PRODUCT,
        version="1.x",
        impl_id=_K8S_MOULD_PRODUCT,
        op_id=_CREDS_OP,
        handler=_k8s_count_with_credentials,
        summary="Count pods using the cluster's stored credentials.",
        description="Resolve the target's stored credentials, then count pods.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding_service,
    )


@contextlib.contextmanager
def _gsm_backend(backend: Any) -> Iterator[None]:
    """Swap the registered ``gsm`` backend for a test double for the block."""
    original = credential_backend.CREDENTIAL_BACKEND_REGISTRY["gsm"]
    credential_backend.CREDENTIAL_BACKEND_REGISTRY["gsm"] = backend
    try:
        yield
    finally:
        credential_backend.CREDENTIAL_BACKEND_REGISTRY["gsm"] = original


class _StubSecretClient:
    """Stub ``SecretManagerServiceClient`` returning one canned JSON payload."""

    def __init__(self) -> None:
        self.credentials: Any = None

    def access_secret_version(self, *, name: str) -> Any:
        payload = SimpleNamespace(data=b'{"username": "svc", "password": "pw"}')
        return SimpleNamespace(payload=payload, name=name)


def _stub_client_factory(client: _StubSecretClient) -> Any:
    def factory(*, credentials: Any) -> _StubSecretClient:
        client.credentials = credentials
        return client

    return factory


def _capturing_wif_factory() -> Any:
    """A ``wif_credentials_factory`` recording the subject token it was handed."""
    calls: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return object()

    factory.calls = calls  # type: ignore[attr-defined]
    return factory


def _enable_wif(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIAL_BACKEND", "gsm")
    monkeypatch.setenv("GSM_WIF_AUDIENCE", _WIF_AUDIENCE)
    monkeypatch.setenv("GSM_WIF_POOL_ID", "meho-pool")
    monkeypatch.setenv("GSM_WIF_PROVIDER_ID", "keycloak")
    get_settings.cache_clear()


def _enable_runner_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECK_RUNNER_CLIENT_ID", _RUNNER_CLIENT_ID)
    monkeypatch.setenv("CHECK_RUNNER_CLIENT_SECRET", _RUNNER_CLIENT_SECRET)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_runner_presents_service_principal_jwt_as_wif_subject_token(
    monkeypatch: pytest.MonkeyPatch,
    _reset_registry: None,
    _embedding_stub: AsyncMock,
    _captured_broadcast: list[BroadcastEvent],
) -> None:
    """AC1: the STS subject token is the runner principal's JWT, not ``""``.

    The whole point of #2642. With a check-runner principal configured, a
    target-bound Sensor on a per-operator-WIF GSM deploy evaluates ``ok``
    end-to-end, and the token federated at ``sts.googleapis.com`` is the one
    Keycloak issued for the check-runner client.
    """
    _enable_wif(monkeypatch)
    _enable_runner_principal(monkeypatch)
    await _register_credentialed_op(_embedding_stub)
    await _seed_target(name="k8s-prod", secret_ref="gsm:my-project/db-creds")

    sensor_id = await _create_target_bound_sensor(target={"name": "k8s-prod"}, op_id=_CREDS_OP)
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))

    wif_factory = _capturing_wif_factory()
    backend = GcpSecretManagerBackend(
        client_factory=_stub_client_factory(_StubSecretClient()),
        wif_credentials_factory=wif_factory,
    )
    with respx.mock as r:
        r.post(_KC_TOKEN_URL).mock(
            return_value=httpx.Response(
                200, json={"access_token": _RUNNER_TOKEN, "expires_in": 300}
            )
        )
        with _gsm_backend(backend):
            await run_one_sensor_tick()
            await _drain_in_flight()

    row = await _get_sensor(sensor_id)
    assert row.last_state == "ok", row.last_evidence
    assert row.last_value == 3

    assert len(wif_factory.calls) == 1
    subject_token = wif_factory.calls[0]["operator_jwt"]
    assert subject_token == _RUNNER_TOKEN
    assert subject_token != ""
    assert subject_token != "op-admin"


@pytest.mark.asyncio
async def test_ambient_adc_serves_background_dispatch_without_a_principal(
    monkeypatch: pytest.MonkeyPatch,
    _reset_registry: None,
    _embedding_stub: AsyncMock,
    _captured_broadcast: list[BroadcastEvent],
) -> None:
    """AC2: ambient ADC + ``raw_jwt=""`` takes the SA-direct fallback.

    A deployment with pod identity (GKE Workload Identity) needs no runner
    principal: the read runs under MEHO's own ADC and the Sensor evaluates.
    The WIF factory is never reached -- an empty subject token has nothing to
    exchange.
    """
    _enable_wif(monkeypatch)
    await _register_credentialed_op(_embedding_stub)
    await _seed_target(name="k8s-prod", secret_ref="gsm:my-project/db-creds")

    sensor_id = await _create_target_bound_sensor(target={"name": "k8s-prod"}, op_id=_CREDS_OP)
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))

    ambient_adc = object()
    wif_factory = _capturing_wif_factory()
    client = _StubSecretClient()
    backend = GcpSecretManagerBackend(
        adc_loader=lambda **_: (ambient_adc, "adc-project"),
        client_factory=_stub_client_factory(client),
        wif_credentials_factory=wif_factory,
    )
    with _gsm_backend(backend):
        await run_one_sensor_tick()
        await _drain_in_flight()

    row = await _get_sensor(sensor_id)
    assert row.last_state == "ok", row.last_evidence
    assert client.credentials is ambient_adc
    assert wif_factory.calls == []


@pytest.mark.asyncio
async def test_gsm_credential_failure_never_reports_a_vault_error(
    monkeypatch: pytest.MonkeyPatch,
    _reset_registry: None,
    _embedding_stub: AsyncMock,
    _captured_broadcast: list[BroadcastEvent],
) -> None:
    """AC3: on a GSM deploy the sensor's ``dispatch_error`` says nothing about Vault.

    With neither a runner principal nor ambient ADC the read still fails --
    but a deploy running no Vault must not be told to go and look at Vault.
    """
    _enable_wif(monkeypatch)
    await _register_credentialed_op(_embedding_stub)
    await _seed_target(name="k8s-prod", secret_ref="gsm:my-project/db-creds")

    sensor_id = await _create_target_bound_sensor(target={"name": "k8s-prod"}, op_id=_CREDS_OP)
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))

    backend = GcpSecretManagerBackend(adc_loader=lambda **_: (None, "adc-project"))
    with _gsm_backend(backend):
        await run_one_sensor_tick()
        await _drain_in_flight()

    row = await _get_sensor(sensor_id)
    assert row.last_state == "unknown"
    assert row.last_evidence is not None
    dispatch_error = str(row.last_evidence["dispatch_error"])
    assert "Vault" not in dispatch_error
    assert "vault" not in dispatch_error.lower()
    assert dispatch_error == "connector_error: GcpSecretManagerReadError"


@pytest.mark.asyncio
async def test_no_principal_configured_keeps_the_empty_jwt_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt-out: with no principal the synthetic operator still carries no token.

    The pre-#2642 contract every existing deployment relies on -- minting is
    opt-in, and an unconfigured install neither contacts Keycloak nor changes
    what the runner presents.
    """
    monkeypatch.delenv("CHECK_RUNNER_CLIENT_ID", raising=False)
    monkeypatch.delenv("CHECK_RUNNER_CLIENT_SECRET", raising=False)
    get_settings.cache_clear()

    snap = _SensorSnapshot(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        name="s",
        connector_id=_K8S_MOULD_CONNECTOR_ID,
        op_id=_CREDS_OP,
        target=None,
        params={},
        assertion=_OK_ASSERTION,
        identity_sub="__sensor__",
    )
    operator = await _sensor_operator(snap)

    assert operator.raw_jwt == ""
    assert operator.sub == "__sensor__"
    assert operator.principal_kind is PrincipalKind.USER


@pytest.mark.asyncio
async def test_configured_principal_does_not_change_meho_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bearer token is the principal's; the audit ``sub`` stays the sensor's.

    Two different identities on purpose: MEHO attributes the evaluation to the
    Sensor row, the credential store authenticates the runner principal.
    """
    _enable_runner_principal(monkeypatch)

    snap = _SensorSnapshot(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        name="s",
        connector_id=_K8S_MOULD_CONNECTOR_ID,
        op_id=_CREDS_OP,
        target=None,
        params={},
        assertion=_OK_ASSERTION,
        identity_sub="sensor-identity",
    )
    with respx.mock as r:
        r.post(_KC_TOKEN_URL).mock(
            return_value=httpx.Response(
                200, json={"access_token": _RUNNER_TOKEN, "expires_in": 300}
            )
        )
        operator = await _sensor_operator(snap)

    assert operator.raw_jwt == _RUNNER_TOKEN
    assert operator.sub == "sensor-identity"
    assert operator.tenant_role is TenantRole.OPERATOR
    assert operator.principal_kind is PrincipalKind.USER
