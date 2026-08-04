# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for sensor state-confirmation retries (#2799).

Initiative #2780 (parent goal #221), Task #2799. Coverage matrix mapped
to the issue's acceptance criteria:

* **Commit gate** (``record_sensor_result``, driven directly -- the
  satellite-gateway batch-post shape, no runner involved): a differing
  reading opens a pending soft-state window; ``retry_times`` consecutive
  confirming re-evaluations commit; a revert clears the window without
  committing; an escalation mid-window restarts the count; recovery to
  ``ok`` is confirmed symmetrically; ``unknown`` participates like any
  state; the monotonicity guard rejects an out-of-order result before
  any pending mutation; ``retry_times=0`` commits immediately (the
  pre-#2799 behaviour).
* **Accelerated re-check** (runner path): a pending persist pulls
  ``next_fire_at`` to ``evaluated_at + retry_backoff_seconds``, never
  later than the already-scheduled cadence instant, and a committed
  (non-pending) persist does not touch the schedule.
* **End-to-end suppression** (runner + rollup + transition detector):
  a transient flake commits no state, claims no rollup edge, spawns no
  notification; a genuine confirmed transition claims exactly one edge
  and one notification; a flapping all-clear sends no recovery mail.

``dispatch`` is stubbed on the runner module and the transition
detector's notification / broadcast / investigation seams are replaced
with recorders (patched at the usage location), so no connector, mail
transport, or Valkey is hit.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import meho_backplane.checks.investigate as inv
from meho_backplane.checks.repository import create_sensor, record_sensor_result
from meho_backplane.checks.runner import (
    _IN_FLIGHT,
    reset_sensor_runner_state,
    run_one_sensor_tick,
)
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    CheckDashboard,
    CheckDashboardSensor,
    Sensor,
    SensorCadenceKind,
    Tenant,
)
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")

#: ``count`` <= 10 -> ok, 10 < count <= 100 -> degraded, count > 100 ->
#: critical -- one assertion whose payload drives all three states.
_BANDED_ASSERTION: dict[str, Any] = {
    "select": {"path": "$.count"},
    "compare": {"type": "threshold", "op": "gt", "degraded": 10, "critical": 100},
}

_OK_PAYLOAD = {"count": 3}
_DEGRADED_PAYLOAD = {"count": 42}
_CRITICAL_PAYLOAD = {"count": 500}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env :class:`Settings` requires; reset runner state per test."""
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
            session.add(Tenant(id=tenant_id, slug=str(tenant_id)[:8], name="Tenant D"))
            await session.commit()


async def _create_sensor(
    *,
    retry_times: int,
    retry_backoff_seconds: int = 15,
    interval_seconds: int = 300,
) -> uuid.UUID:
    await _seed_tenant()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await create_sensor(
            session,
            tenant_id=_TENANT,
            name=f"sensor-{uuid.uuid4().hex[:8]}",
            connector_id="vmware-rest-9.0",
            op_id="vmware.vm.list",
            target=None,
            params={},
            assertion=_BANDED_ASSERTION,
            cadence_kind=SensorCadenceKind.INTERVAL,
            interval_seconds=interval_seconds,
            cron_expr=None,
            timezone="UTC",
            severity="critical",
            for_seconds=0,
            retry_times=retry_times,
            retry_backoff_seconds=retry_backoff_seconds,
            identity_sub="__sensor__",
            created_by_sub="op-admin",
        )
        await session.commit()
        return row.id


async def _record(
    sensor_id: uuid.UUID,
    state: str,
    evaluated_at: datetime,
    *,
    value: object = 0,
) -> bool:
    """Drive the shared commit gate directly (the gateway batch-post shape)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        committed = await record_sensor_result(
            session,
            sensor_id=sensor_id,
            state=state,  # type: ignore[arg-type]
            value=value,
            evidence={"observed": value},
            evaluated_at=evaluated_at,
        )
        await session.commit()
        return committed


async def _get_sensor(sensor_id: uuid.UUID) -> Sensor:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(Sensor, sensor_id)
        assert row is not None
        return row


async def _commit_state(
    sensor_id: uuid.UUID,
    state: str,
    *,
    base: datetime,
    retry_times: int,
) -> datetime:
    """Confirm *state* onto the sensor (retry_times + 1 readings); return last ts."""
    at = base
    for i in range(retry_times + 1):
        at = base + timedelta(seconds=i)
        await _record(sensor_id, state, at)
    row = await _get_sensor(sensor_id)
    assert row.last_state == state
    return at


def _aware(dt: datetime | None) -> datetime:
    """Attach UTC to a naive datetime (aiosqlite drops tz on round-trip)."""
    assert dt is not None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def _force_due(sensor_id: uuid.UUID, when: datetime) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(Sensor, sensor_id)
        assert row is not None
        row.next_fire_at = when
        await session.commit()


async def _drain_in_flight(timeout: float = 3.0) -> None:
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while _IN_FLIGHT and loop.time() < deadline:
        tasks = list(_IN_FLIGHT.values())
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)


def _stub_dispatch(monkeypatch: pytest.MonkeyPatch, payload_ref: dict[str, Any]) -> None:
    """Stub the runner's dispatch seam to return ``payload_ref['payload']``."""

    async def _dispatch(**_kwargs: Any) -> OperationResult:
        return OperationResult(
            status="ok",
            op_id="vmware.vm.list",
            result=payload_ref["payload"],
            duration_ms=1.0,
        )

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _dispatch)


async def _tick_with(
    sensor_id: uuid.UUID,
    payload_ref: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    """Force the sensor due, run one tick, and drain the evaluation."""
    payload_ref["payload"] = payload
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))
    await run_one_sensor_tick()
    await _drain_in_flight()


# --------------------------------------------------------------------------- #
# Commit gate (repository level -- the shared, transport-agnostic seam)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_gate_holds_soft_state_then_commits_after_retry_times() -> None:
    """retry_times=3: four consecutive differing readings commit exactly once."""
    sensor_id = await _create_sensor(retry_times=3)
    base = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
    committed_at = await _commit_state(sensor_id, "ok", base=base, retry_times=3)

    outcomes: list[bool] = []
    for i in range(1, 5):
        at = committed_at + timedelta(seconds=i)
        outcomes.append(await _record(sensor_id, "degraded", at, value=42))
        row = await _get_sensor(sensor_id)
        if i <= 3:
            # Soft state: observed but not committed.
            assert row.last_state == "ok"
            assert row.pending_state == "degraded"
            assert row.pending_count == i
            # The observation projection still updates on every reading.
            assert row.last_value == 42
            assert _aware(row.last_evaluated_at) == at
    assert outcomes == [False, False, False, True]

    row = await _get_sensor(sensor_id)
    assert row.last_state == "degraded"
    # state_since is the commit-time evaluation, not the first soft reading.
    assert _aware(row.state_since) == committed_at + timedelta(seconds=4)
    assert row.pending_state is None
    assert row.pending_count == 0


@pytest.mark.asyncio
async def test_gate_transient_flap_never_commits() -> None:
    """ok -> one degraded reading -> ok: last_state never leaves ok."""
    sensor_id = await _create_sensor(retry_times=1)
    base = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
    committed_at = await _commit_state(sensor_id, "ok", base=base, retry_times=1)
    row = await _get_sensor(sensor_id)
    state_since = row.state_since

    assert await _record(sensor_id, "degraded", committed_at + timedelta(seconds=1)) is False
    row = await _get_sensor(sensor_id)
    assert row.last_state == "ok"
    assert row.pending_state == "degraded"  # the window is observable...
    assert row.pending_count == 1

    assert await _record(sensor_id, "ok", committed_at + timedelta(seconds=2)) is False
    row = await _get_sensor(sensor_id)
    assert row.last_state == "ok"
    assert row.pending_state is None  # ...and cleared on revert.
    assert row.pending_count == 0
    assert row.state_since == state_since  # the committed clock never moved


@pytest.mark.asyncio
async def test_gate_escalation_mid_window_restarts_count() -> None:
    """degraded, degraded, critical restarts the count and commits critical."""
    sensor_id = await _create_sensor(retry_times=2)
    base = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
    at = await _commit_state(sensor_id, "ok", base=base, retry_times=2)

    assert await _record(sensor_id, "degraded", at + timedelta(seconds=1)) is False
    assert await _record(sensor_id, "degraded", at + timedelta(seconds=2)) is False
    # Escalation: the candidate changes, the count restarts on critical.
    assert await _record(sensor_id, "critical", at + timedelta(seconds=3)) is False
    row = await _get_sensor(sensor_id)
    assert row.last_state == "ok"  # degraded never committed
    assert row.pending_state == "critical"
    assert row.pending_count == 1

    assert await _record(sensor_id, "critical", at + timedelta(seconds=4)) is False
    assert await _record(sensor_id, "critical", at + timedelta(seconds=5)) is True
    row = await _get_sensor(sensor_id)
    assert row.last_state == "critical"
    assert _aware(row.state_since) == at + timedelta(seconds=5)


@pytest.mark.asyncio
async def test_gate_recovery_is_confirmed_symmetrically() -> None:
    """From committed critical, a lone ok reading commits nothing."""
    sensor_id = await _create_sensor(retry_times=1)
    base = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
    at = await _commit_state(sensor_id, "critical", base=base, retry_times=1)

    assert await _record(sensor_id, "ok", at + timedelta(seconds=1)) is False
    row = await _get_sensor(sensor_id)
    assert row.last_state == "critical"
    assert row.pending_state == "ok"

    assert await _record(sensor_id, "critical", at + timedelta(seconds=2)) is False
    row = await _get_sensor(sensor_id)
    assert row.last_state == "critical"
    assert row.pending_state is None


@pytest.mark.asyncio
async def test_gate_unknown_participates_like_any_state() -> None:
    """A transient dispatch failure (unknown) cannot flip a confirmed sensor."""
    sensor_id = await _create_sensor(retry_times=1)
    base = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
    at = await _commit_state(sensor_id, "ok", base=base, retry_times=1)

    assert await _record(sensor_id, "unknown", at + timedelta(seconds=1)) is False
    row = await _get_sensor(sensor_id)
    assert row.last_state == "ok"
    assert row.pending_state == "unknown"

    assert await _record(sensor_id, "ok", at + timedelta(seconds=2)) is False
    row = await _get_sensor(sensor_id)
    assert row.last_state == "ok"
    assert row.pending_state is None


@pytest.mark.asyncio
async def test_gate_monotonicity_guard_rejects_before_pending_mutation() -> None:
    """An out-of-order gateway result opens no window (rejected up front)."""
    sensor_id = await _create_sensor(retry_times=1)
    base = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
    committed_at = await _commit_state(sensor_id, "ok", base=base, retry_times=1)

    stale = committed_at - timedelta(seconds=30)
    assert await _record(sensor_id, "degraded", stale) is False
    row = await _get_sensor(sensor_id)
    assert row.last_state == "ok"
    assert row.pending_state is None
    assert row.pending_count == 0
    assert _aware(row.last_evaluated_at) == committed_at  # projection intact


@pytest.mark.asyncio
async def test_gate_retry_times_zero_commits_immediately() -> None:
    """The default is bit-for-bit today's behaviour: no window, instant commit."""
    sensor_id = await _create_sensor(retry_times=0)
    at = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)

    assert await _record(sensor_id, "degraded", at, value=42) is True
    row = await _get_sensor(sensor_id)
    assert row.last_state == "degraded"
    assert _aware(row.state_since) == at
    assert row.pending_state is None
    assert row.pending_count == 0


# --------------------------------------------------------------------------- #
# Accelerated re-check (runner path)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pending_result_pulls_next_fire_to_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending persist pulls next_fire_at to evaluated_at + backoff."""
    payload_ref: dict[str, Any] = {"payload": _CRITICAL_PAYLOAD}
    _stub_dispatch(monkeypatch, payload_ref)
    sensor_id = await _create_sensor(retry_times=1, retry_backoff_seconds=15, interval_seconds=300)

    await _tick_with(sensor_id, payload_ref, _CRITICAL_PAYLOAD)

    row = await _get_sensor(sensor_id)
    assert row.pending_state == "critical"  # unknown -> critical is held too
    assert row.pending_count == 1
    assert row.last_state == "unknown"
    # Pulled to exactly evaluated_at + 15 s, far sooner than the 300 s cadence.
    assert _aware(row.next_fire_at) == _aware(row.last_evaluated_at) + timedelta(seconds=15)


@pytest.mark.asyncio
async def test_pending_pull_never_delays_scheduled_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """min() semantics: a backoff beyond the cadence instant is a no-op."""
    payload_ref: dict[str, Any] = {"payload": _CRITICAL_PAYLOAD}
    _stub_dispatch(monkeypatch, payload_ref)
    sensor_id = await _create_sensor(retry_times=1, retry_backoff_seconds=300, interval_seconds=5)

    before = datetime.now(UTC)
    await _tick_with(sensor_id, payload_ref, _CRITICAL_PAYLOAD)

    row = await _get_sensor(sensor_id)
    assert row.pending_state == "critical"
    # The claim advanced next_fire_at to ~before + 5 s; the 300 s backoff
    # must not push it later.
    assert _aware(row.next_fire_at) <= before + timedelta(seconds=7)


@pytest.mark.asyncio
async def test_committed_result_does_not_accelerate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """retry_times=0 commits immediately and leaves the cadence untouched."""
    payload_ref: dict[str, Any] = {"payload": _CRITICAL_PAYLOAD}
    _stub_dispatch(monkeypatch, payload_ref)
    sensor_id = await _create_sensor(retry_times=0, retry_backoff_seconds=15, interval_seconds=300)

    await _tick_with(sensor_id, payload_ref, _CRITICAL_PAYLOAD)

    row = await _get_sensor(sensor_id)
    assert row.last_state == "critical"
    assert row.pending_state is None
    # Still the full cadence away -- no accelerated pull happened.
    delta = _aware(row.next_fire_at) - _aware(row.last_evaluated_at)
    assert delta > timedelta(seconds=250)


# --------------------------------------------------------------------------- #
# End-to-end: rollup edge + notification gating (runner + detector)
# --------------------------------------------------------------------------- #


class _DetectorRecorders:
    """Recording replacements for the transition detector's fan-out seams."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.notifications: list[Any] = []
        self.published: list[dict[str, Any]] = []
        self.investigations: list[Any] = []

        def _notify(notice: Any) -> None:
            self.notifications.append(notice)

        async def _publish(**kwargs: Any) -> None:
            self.published.append(kwargs)

        def _investigate(**kwargs: Any) -> None:
            self.investigations.append(kwargs)

        # Patch at the usage location (the investigate module's globals).
        monkeypatch.setattr(inv, "schedule_dashboard_notification", _notify)
        monkeypatch.setattr(inv, "publish_check_transition_event", _publish)
        monkeypatch.setattr(inv, "_schedule_investigation", _investigate)


async def _seed_dashboard(sensor_id: uuid.UUID, *, memo: str) -> uuid.UUID:
    dashboard_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            CheckDashboard(
                id=dashboard_id,
                tenant_id=_TENANT,
                name=f"dash-{uuid.uuid4().hex[:8]}",
                description=None,
                last_rollup_state=memo,
                created_by_sub="op-admin",
            )
        )
        session.add(CheckDashboardSensor(dashboard_id=dashboard_id, sensor_id=sensor_id))
        await session.commit()
    return dashboard_id


async def _memo(dashboard_id: uuid.UUID) -> str | None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(CheckDashboard, dashboard_id)
        assert row is not None
        return row.last_rollup_state


@pytest.mark.asyncio
async def test_flake_suppressed_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """ok -> one degraded reading -> ok: no edge claimed, no mail spawned."""
    payload_ref: dict[str, Any] = {"payload": _OK_PAYLOAD}
    _stub_dispatch(monkeypatch, payload_ref)
    recorders = _DetectorRecorders(monkeypatch)

    sensor_id = await _create_sensor(retry_times=1)
    base = datetime.now(UTC) - timedelta(minutes=5)
    await _commit_state(sensor_id, "ok", base=base, retry_times=1)
    dashboard_id = await _seed_dashboard(sensor_id, memo="ok")

    await _tick_with(sensor_id, payload_ref, _DEGRADED_PAYLOAD)
    row = await _get_sensor(sensor_id)
    assert row.last_state == "ok"
    assert row.pending_state == "degraded"  # visible while pending...

    await _tick_with(sensor_id, payload_ref, _OK_PAYLOAD)
    row = await _get_sensor(sensor_id)
    assert row.last_state == "ok"
    assert row.pending_state is None  # ...then cleared

    assert await _memo(dashboard_id) == "ok"  # no rollup edge was claimed
    assert recorders.notifications == []  # no notification task spawned
    assert recorders.published == []  # no transition event published
    assert recorders.investigations == []


@pytest.mark.asyncio
async def test_confirmed_transition_claims_one_edge_one_mail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """retry_times=3: four degraded readings commit once -> one edge, one mail."""
    payload_ref: dict[str, Any] = {"payload": _OK_PAYLOAD}
    _stub_dispatch(monkeypatch, payload_ref)
    recorders = _DetectorRecorders(monkeypatch)

    sensor_id = await _create_sensor(retry_times=3, retry_backoff_seconds=15)
    base = datetime.now(UTC) - timedelta(minutes=5)
    await _commit_state(sensor_id, "ok", base=base, retry_times=3)
    dashboard_id = await _seed_dashboard(sensor_id, memo="ok")

    for _ in range(4):
        await _tick_with(sensor_id, payload_ref, _DEGRADED_PAYLOAD)

    row = await _get_sensor(sensor_id)
    assert row.last_state == "degraded"
    assert row.pending_state is None
    assert _aware(row.state_since) == _aware(row.last_evaluated_at)  # commit-time
    assert await _memo(dashboard_id) == "degraded"
    assert len(recorders.notifications) == 1  # exactly one claimed edge
    assert len(recorders.published) == 1
    assert recorders.published[0]["previous_state"] == "ok"
    assert recorders.published[0]["new_state"] == "degraded"


@pytest.mark.asyncio
async def test_recovery_flap_sends_no_all_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """From committed critical: one ok reading then critical -> no recovery mail."""
    payload_ref: dict[str, Any] = {"payload": _CRITICAL_PAYLOAD}
    _stub_dispatch(monkeypatch, payload_ref)
    recorders = _DetectorRecorders(monkeypatch)

    sensor_id = await _create_sensor(retry_times=1)
    base = datetime.now(UTC) - timedelta(minutes=5)
    await _commit_state(sensor_id, "critical", base=base, retry_times=1)
    dashboard_id = await _seed_dashboard(sensor_id, memo="critical")

    await _tick_with(sensor_id, payload_ref, _OK_PAYLOAD)
    await _tick_with(sensor_id, payload_ref, _CRITICAL_PAYLOAD)

    row = await _get_sensor(sensor_id)
    assert row.last_state == "critical"
    assert row.pending_state is None
    assert await _memo(dashboard_id) == "critical"
    assert recorders.notifications == []  # the observed nuisance all-clear
    assert recorders.published == []
