# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the sensor evaluation-loop watchdog (#2763).

Coverage mapped to the issue's acceptance criteria:

* **Stall detection, clock-injected** -- no completed tick for more than
  ``SENSOR_RUNNER_STALL_AFTER_TICKS x tick interval`` yields exactly one
  structured ``checks_scheduler_stalled`` log and exactly one
  ``checks.scheduler_stalled`` broadcast event per affected tenant
  (once-per-continuous-stall re-emission policy), classified ``checks``
  by exact ``_CHECK_EVENT_OPS`` membership.
* **Recovery** -- the first completed tick after a detected stall emits
  ``checks_scheduler_recovered`` (log + event) carrying the stall
  duration, to exactly the tenants the stalled event was addressed to.
* **Fail-open** -- an injected broadcast failure during stall/recovery
  emission is swallowed with a warn log and the runner keeps ticking
  (proved through the real ``run_one_sensor_tick`` seam).
* **Liveness derivation** -- the health facet's staleness view is
  derived live from the tick stamp, never from the watchdog's emission
  latch, and reads unknown-not-stalled before the runner has started.

All stall/recovery *logic* is exercised with injected clocks
(python_best_practices §14); the only real sleeps are in the
loop-lifecycle tests, which drive the watchdog loop's own cadence and
poll for completion rather than assert on wall-clock timing. The DB
layer is real against the autouse SQLite engine from :mod:`tests.conftest`;
``publish_event`` is replaced by a recording fake so no Valkey
connection is opened (same seam as ``tests/test_checks_broadcast.py``).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import update
from structlog.testing import capture_logs

import meho_backplane.checks.broadcast as checks_broadcast
import meho_backplane.checks.watchdog as watchdog
from meho_backplane.broadcast.events import (
    _CHECK_EVENT_OPS,
    BroadcastEvent,
    classify_op,
)
from meho_backplane.checks.assertions import AssertionSpec
from meho_backplane.checks.broadcast import (
    SCHEDULER_RECOVERED_OP_ID,
    SCHEDULER_STALLED_OP_ID,
)
from meho_backplane.checks.repository import create_sensor
from meho_backplane.checks.runner import reset_sensor_runner_state, run_one_sensor_tick
from meho_backplane.checks.watchdog import (
    evaluate_stall_watchdog,
    note_tick_completed,
    sensor_runner_liveness,
    start_checks_watchdog,
    stop_checks_watchdog,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Sensor, SensorCadenceKind, SensorStatus, Tenant
from meho_backplane.settings import get_settings

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

_T0 = datetime(2026, 8, 4, 10, 0, 0, tzinfo=UTC)

#: Default threshold under the fixture env: 6 ticks x 10 s.
_THRESHOLD = timedelta(seconds=60)

_ASSERTION: dict[str, Any] = AssertionSpec.model_validate(
    {
        "select": {"path": "$.count"},
        "compare": {"type": "threshold", "op": "gt", "critical": 10},
    }
).model_dump(mode="json")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env :class:`Settings` requires; reset runner + watchdog state."""
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


class _RecordingPublisher:
    """Stands in for ``publish_event``; records every event, optionally raises."""

    def __init__(self, *, exc: Exception | None = None) -> None:
        self.events: list[BroadcastEvent] = []
        self._exc = exc

    async def __call__(self, event: BroadcastEvent) -> None:
        self.events.append(event)
        if self._exc is not None:
            raise self._exc


def _install_publisher(
    monkeypatch: pytest.MonkeyPatch, *, exc: Exception | None = None
) -> _RecordingPublisher:
    """Patch the publisher the checks-broadcast module imported."""
    fake = _RecordingPublisher(exc=exc)
    monkeypatch.setattr(checks_broadcast, "publish_event", fake)
    return fake


async def _seed_active_sensor(tenant_id: uuid.UUID) -> None:
    """One tenant row + one active interval Sensor under it."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if await session.get(Tenant, tenant_id) is None:
            session.add(Tenant(id=tenant_id, slug=str(tenant_id)[:8], name="T"))
            await session.commit()
    async with sessionmaker() as session:
        await create_sensor(
            session,
            tenant_id=tenant_id,
            name=f"sensor-{uuid.uuid4().hex[:8]}",
            connector_id="vmware-rest-9.0",
            op_id="vmware.vm.list",
            target=None,
            params={},
            assertion=_ASSERTION,
            cadence_kind=SensorCadenceKind.INTERVAL,
            interval_seconds=300,
            cron_expr=None,
            timezone="UTC",
            severity="critical",
            for_seconds=0,
            retry_times=0,
            retry_backoff_seconds=15,
            identity_sub="__sensor__",
            created_by_sub="op-test",
        )
        await session.commit()


async def _pause_all_sensors(tenant_id: uuid.UUID) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await session.execute(
            update(Sensor)
            .where(Sensor.tenant_id == tenant_id)
            .values(status=SensorStatus.PAUSED.value)
        )
        await session.commit()


def _events_for(fake: _RecordingPublisher, op_id: str) -> list[BroadcastEvent]:
    return [event for event in fake.events if event.op_id == op_id]


# --------------------------------------------------------------------------- #
# Classifier pins
# --------------------------------------------------------------------------- #


def test_scheduler_op_ids_are_pinned_in_check_event_ops() -> None:
    """The two watchdog op-ids and the classifier allowlist cannot drift."""
    assert SCHEDULER_STALLED_OP_ID in _CHECK_EVENT_OPS
    assert SCHEDULER_RECOVERED_OP_ID in _CHECK_EVENT_OPS
    assert classify_op(SCHEDULER_STALLED_OP_ID) == "checks"
    assert classify_op(SCHEDULER_RECOVERED_OP_ID) == "checks"


# --------------------------------------------------------------------------- #
# Stall detection (clock-injected)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stall_past_threshold_emits_log_and_event_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quiet past N x interval => one stalled log + one event per tenant."""
    fake = _install_publisher(monkeypatch)
    await _seed_active_sensor(_TENANT_A)
    await note_tick_completed(now=_T0)

    check_at = _T0 + _THRESHOLD + timedelta(seconds=1)
    with capture_logs() as logs:
        assert await evaluate_stall_watchdog(now=check_at) is True

    stalled_logs = [entry for entry in logs if entry["event"] == "checks_scheduler_stalled"]
    assert len(stalled_logs) == 1
    assert stalled_logs[0]["log_level"] == "error"
    assert stalled_logs[0]["seconds_since_last_tick"] == pytest.approx(61.0)
    assert stalled_logs[0]["stall_threshold_seconds"] == pytest.approx(60.0)

    events = _events_for(fake, SCHEDULER_STALLED_OP_ID)
    assert len(events) == 1
    event = events[0]
    assert event.tenant_id == _TENANT_A
    assert event.op_class == "checks"
    assert event.principal_sub == "__checks__"
    assert event.audit_id == uuid.UUID(int=0)
    assert event.payload["seconds_since_last_tick"] == pytest.approx(61.0)
    assert event.payload["stall_threshold_seconds"] == pytest.approx(60.0)
    assert event.payload["tick_interval_seconds"] == 10


@pytest.mark.asyncio
async def test_continuing_stall_does_not_re_emit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once per continuous stall: later checks return True but stay silent."""
    fake = _install_publisher(monkeypatch)
    await _seed_active_sensor(_TENANT_A)
    await note_tick_completed(now=_T0)

    first = _T0 + _THRESHOLD + timedelta(seconds=1)
    assert await evaluate_stall_watchdog(now=first) is True
    with capture_logs() as logs:
        assert await evaluate_stall_watchdog(now=first + timedelta(minutes=10)) is True

    assert [entry for entry in logs if entry["event"] == "checks_scheduler_stalled"] == []
    assert len(_events_for(fake, SCHEDULER_STALLED_OP_ID)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("quiet_seconds", [59.0, 60.0])
async def test_quiet_at_or_below_threshold_is_not_a_stall(
    monkeypatch: pytest.MonkeyPatch, quiet_seconds: float
) -> None:
    """The threshold is exclusive: exactly N x interval is still healthy."""
    fake = _install_publisher(monkeypatch)
    await note_tick_completed(now=_T0)

    check_at = _T0 + timedelta(seconds=quiet_seconds)
    with capture_logs() as logs:
        assert await evaluate_stall_watchdog(now=check_at) is False

    assert [entry for entry in logs if entry["event"] == "checks_scheduler_stalled"] == []
    assert fake.events == []


@pytest.mark.asyncio
async def test_no_reference_at_all_is_not_a_stall(monkeypatch: pytest.MonkeyPatch) -> None:
    """No tick and no watchdog start => nothing to measure from, no stall."""
    fake = _install_publisher(monkeypatch)
    assert await evaluate_stall_watchdog(now=_T0) is False
    assert fake.events == []


@pytest.mark.asyncio
async def test_never_ticked_runner_trips_from_the_start_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loop that never completes a single tick still trips the detector."""
    fake = _install_publisher(monkeypatch)
    await _seed_active_sensor(_TENANT_A)
    task = start_checks_watchdog()
    await stop_checks_watchdog(task)
    baseline = watchdog._BASELINE
    assert baseline is not None

    assert await evaluate_stall_watchdog(now=baseline + timedelta(seconds=59)) is False
    assert await evaluate_stall_watchdog(now=baseline + _THRESHOLD + timedelta(seconds=1)) is True
    assert len(_events_for(fake, SCHEDULER_STALLED_OP_ID)) == 1


@pytest.mark.asyncio
async def test_stall_threshold_scales_with_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """SENSOR_RUNNER_STALL_AFTER_TICKS x SENSOR_RUNNER_TICK_INTERVAL_SECONDS."""
    monkeypatch.setenv("SENSOR_RUNNER_TICK_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("SENSOR_RUNNER_STALL_AFTER_TICKS", "4")
    get_settings.cache_clear()
    fake = _install_publisher(monkeypatch)
    await _seed_active_sensor(_TENANT_A)
    await note_tick_completed(now=_T0)

    assert await evaluate_stall_watchdog(now=_T0 + timedelta(seconds=120)) is False
    assert await evaluate_stall_watchdog(now=_T0 + timedelta(seconds=121)) is True
    assert sensor_runner_liveness(now=_T0).stall_threshold_seconds == pytest.approx(120.0)
    assert fake.events  # the 121 s check emitted


# --------------------------------------------------------------------------- #
# Tenant fan-out
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fan_out_targets_active_sensor_tenants_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stalled events go to tenants with >=1 active Sensor; paused-only stays dark."""
    fake = _install_publisher(monkeypatch)
    await _seed_active_sensor(_TENANT_A)
    await _seed_active_sensor(_TENANT_B)
    await _pause_all_sensors(_TENANT_B)
    await note_tick_completed(now=_T0)

    assert await evaluate_stall_watchdog(now=_T0 + _THRESHOLD + timedelta(seconds=5)) is True

    stalled = _events_for(fake, SCHEDULER_STALLED_OP_ID)
    assert [event.tenant_id for event in stalled] == [_TENANT_A]


@pytest.mark.asyncio
async def test_recovery_goes_to_the_tenants_the_stall_went_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stalled/recovered pair matches even when tenants change mid-stall."""
    fake = _install_publisher(monkeypatch)
    await _seed_active_sensor(_TENANT_A)
    await note_tick_completed(now=_T0)
    assert await evaluate_stall_watchdog(now=_T0 + _THRESHOLD + timedelta(seconds=5)) is True

    # A tenant that gains its first sensor mid-stall was never told about
    # the stall -- it must not receive a recovery either.
    await _seed_active_sensor(_TENANT_B)
    await note_tick_completed(now=_T0 + timedelta(minutes=10))

    recovered = _events_for(fake, SCHEDULER_RECOVERED_OP_ID)
    assert [event.tenant_id for event in recovered] == [_TENANT_A]


# --------------------------------------------------------------------------- #
# Recovery
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_first_tick_after_stall_emits_recovered_with_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery log + event carry the stall duration; the latch re-arms."""
    fake = _install_publisher(monkeypatch)
    await _seed_active_sensor(_TENANT_A)
    await note_tick_completed(now=_T0)
    assert await evaluate_stall_watchdog(now=_T0 + _THRESHOLD + timedelta(seconds=1)) is True

    recover_at = _T0 + timedelta(minutes=37)
    with capture_logs() as logs:
        await note_tick_completed(now=recover_at)

    recovered_logs = [entry for entry in logs if entry["event"] == "checks_scheduler_recovered"]
    assert len(recovered_logs) == 1
    assert recovered_logs[0]["log_level"] == "warning"
    assert recovered_logs[0]["stalled_for_seconds"] == pytest.approx(37 * 60.0)

    events = _events_for(fake, SCHEDULER_RECOVERED_OP_ID)
    assert len(events) == 1
    assert events[0].tenant_id == _TENANT_A
    assert events[0].op_class == "checks"
    assert events[0].payload["stalled_for_seconds"] == pytest.approx(37 * 60.0)

    # Latch cleared: the next quiet window is a fresh stall with fresh events.
    assert await evaluate_stall_watchdog(now=recover_at + timedelta(seconds=30)) is False
    assert await evaluate_stall_watchdog(now=recover_at + _THRESHOLD + timedelta(seconds=1)) is True
    assert len(_events_for(fake, SCHEDULER_STALLED_OP_ID)) == 2


@pytest.mark.asyncio
async def test_healthy_tick_emits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The no-stall hot path is silent: stamp only, no events, no logs."""
    fake = _install_publisher(monkeypatch)
    with capture_logs() as logs:
        await note_tick_completed(now=_T0)
        await note_tick_completed(now=_T0 + timedelta(seconds=10))
    assert fake.events == []
    assert [e for e in logs if e["event"].startswith("checks_scheduler")] == []


# --------------------------------------------------------------------------- #
# Fail-open (through the runner seam)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_broadcast_failure_during_stall_and_recovery_never_breaks_the_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising publisher warn-logs and the runner keeps ticking (#2763 AC)."""
    fake = _install_publisher(monkeypatch, exc=RuntimeError("valkey down"))
    await _seed_active_sensor(_TENANT_A)
    await note_tick_completed(now=_T0)

    with capture_logs() as logs:
        assert await evaluate_stall_watchdog(now=_T0 + _THRESHOLD + timedelta(seconds=1)) is True
    assert [e for e in logs if e["event"] == "checks_scheduler_broadcast_failed"]

    # The real tick seam: recovery emission fails too, the tick still
    # completes, stamps, and clears the latch.
    with capture_logs() as logs:
        dispatched = await run_one_sensor_tick()
    assert dispatched == 0
    assert [e for e in logs if e["event"] == "checks_scheduler_broadcast_failed"]
    assert [e for e in logs if e["event"] == "checks_scheduler_recovered"]
    liveness = sensor_runner_liveness()
    assert liveness.seconds_since_last_tick is not None
    assert liveness.seconds_since_last_tick < 5.0
    assert watchdog._STALLED_SINCE is None
    # Both emission attempts reached the publisher before it raised.
    assert _events_for(fake, SCHEDULER_STALLED_OP_ID)
    assert _events_for(fake, SCHEDULER_RECOVERED_OP_ID)


@pytest.mark.asyncio
async def test_tenant_query_failure_is_swallowed_and_still_latches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing fan-out query warn-logs; the latch still arms the recovery pair."""
    fake = _install_publisher(monkeypatch)
    await note_tick_completed(now=_T0)

    async def _boom() -> list[uuid.UUID]:
        raise RuntimeError("db down")

    monkeypatch.setattr(watchdog, "_active_sensor_tenant_ids", _boom)
    with capture_logs() as logs:
        assert await evaluate_stall_watchdog(now=_T0 + _THRESHOLD + timedelta(seconds=1)) is True

    assert [e for e in logs if e["event"] == "checks_watchdog_emit_failed"]
    assert watchdog._STALLED_SINCE is not None
    assert fake.events == []


# --------------------------------------------------------------------------- #
# Liveness view (health facet substrate)
# --------------------------------------------------------------------------- #


def test_liveness_before_any_reference_reads_unknown_not_stalled() -> None:
    liveness = sensor_runner_liveness(now=_T0)
    assert liveness.seconds_since_last_tick is None
    assert liveness.stalled is False
    assert liveness.stall_threshold_seconds == pytest.approx(60.0)


@pytest.mark.asyncio
async def test_liveness_derives_stalled_live_without_the_watchdog_flag() -> None:
    """The facet trips on the stamp alone -- a dead watchdog task cannot blind it."""
    await note_tick_completed(now=_T0)
    healthy = sensor_runner_liveness(now=_T0 + timedelta(seconds=30))
    assert healthy.stalled is False
    assert healthy.seconds_since_last_tick == pytest.approx(30.0)

    stalled = sensor_runner_liveness(now=_T0 + _THRESHOLD + timedelta(seconds=1))
    assert stalled.stalled is True
    assert stalled.seconds_since_last_tick == pytest.approx(61.0)
    # No evaluate_stall_watchdog ran: the emission latch is untouched.
    assert watchdog._STALLED_SINCE is None


@pytest.mark.asyncio
async def test_run_one_sensor_tick_stamps_the_liveness_view() -> None:
    assert sensor_runner_liveness().seconds_since_last_tick is None
    await run_one_sensor_tick()
    liveness = sensor_runner_liveness()
    assert liveness.seconds_since_last_tick is not None
    assert liveness.seconds_since_last_tick < 5.0


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_start_and_stop_watchdog_clean() -> None:
    """Start/stop unwinds without pending-task warnings; baseline is set."""
    task = start_checks_watchdog()
    assert watchdog._BASELINE is not None
    assert not task.done()
    await stop_checks_watchdog(task)
    assert task.cancelled()


@pytest.mark.asyncio
async def test_watchdog_loop_survives_a_failing_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising check is warn-logged and the loop keeps running."""
    monkeypatch.setenv("SENSOR_RUNNER_TICK_INTERVAL_SECONDS", "1")
    get_settings.cache_clear()

    calls = 0

    async def _boom(now: datetime | None = None) -> bool:
        nonlocal calls
        calls += 1
        raise RuntimeError("check exploded")

    monkeypatch.setattr(watchdog, "evaluate_stall_watchdog", _boom)
    task = start_checks_watchdog()
    try:
        with capture_logs() as logs:
            for _ in range(400):
                await asyncio.sleep(0.01)
                if calls >= 2:
                    break
    finally:
        await stop_checks_watchdog(task)

    assert calls >= 2
    assert [e for e in logs if e["event"] == "checks_watchdog_check_failed"]
