# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Sensor and Dashboard changes become events behind CHECKS_AGENT_EVENTS (#6180).

With the switch off, no ``check.*`` row is written. With it on, a confirmed
Sensor change writes one ``check.sensor.changed`` row in the transaction of
``last_state``, and a Dashboard change writes ``check.dashboard.changed`` rows.
Runs on the SQLite engine from :mod:`tests.conftest`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

import meho_backplane.checks.investigate as inv
import meho_backplane.checks.notify as notify
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.checks.assertions import AssertionOutcome
from meho_backplane.checks.events import (
    CHECK_DASHBOARD_CHANGED,
    CHECK_SENSOR_CHANGED,
    publish_dashboard_changed,
)
from meho_backplane.checks.runner import _persist_outcome, _SensorSnapshot
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    CheckDashboard,
    CheckDashboardSensor,
    EventOutbox,
    Sensor,
    Tenant,
)
from meho_backplane.events.matcher import _payload_contains
from meho_backplane.events.normalizers import synthesize_event_prompt
from meho_backplane.memory.schemas import MemoryScope
from meho_backplane.memory.service import MemoryService
from meho_backplane.settings import get_settings
from meho_backplane.untrusted_text import BLOCK_START

_TENANT = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

_ASSERTION: dict[str, Any] = {
    "select": {"path": "$.count"},
    "compare": {"type": "threshold", "op": "lt", "critical": 10},
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://kc.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SENSOR_RUNNER_ENABLED", "false")
    monkeypatch.delenv("CHECKS_AGENT_EVENTS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    inv._INVESTIGATIONS.clear()
    notify._NOTIFICATIONS.clear()


def _switch(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CHECKS_AGENT_EVENTS", value)
    get_settings.cache_clear()


async def _seed_tenant() -> None:
    async with get_sessionmaker()() as session:
        if await session.get(Tenant, _TENANT) is None:
            session.add(Tenant(id=_TENANT, slug="tenant-b", name="Tenant B"))
            await session.commit()


async def _seed_sensor(
    *,
    name: str,
    last_state: str = "ok",
    retry_times: int = 0,
    target: dict[str, object] | None = None,
) -> UUID:
    now = datetime.now(UTC)
    sensor_id = uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            Sensor(
                id=sensor_id,
                tenant_id=_TENANT,
                name=name,
                connector_id="vmware-rest-9.0",
                op_id="vmware.vm.list",
                target=target,
                params={},
                assertion=_ASSERTION,
                status="active",
                cadence_kind="interval",
                interval_seconds=60,
                cron_expr=None,
                timezone="UTC",
                next_fire_at=now + timedelta(seconds=60),
                severity="critical",
                for_seconds=0,
                retry_times=retry_times,
                last_state=last_state,
                last_value=None,
                last_evidence=None,
                last_evaluated_at=now - timedelta(seconds=30),
                state_since=now - timedelta(hours=1),
                identity_sub="__sensor__",
                created_by_sub="op-admin",
            )
        )
        await session.commit()
    return sensor_id


async def _seed_dashboard(sensor_ids: list[UUID], *, memo: str | None) -> UUID:
    dashboard_id = uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            CheckDashboard(
                id=dashboard_id,
                tenant_id=_TENANT,
                name="prod",
                description=None,
                last_rollup_state=memo,
                created_by_sub="op-admin",
            )
        )
        for sid in sensor_ids:
            session.add(CheckDashboardSensor(dashboard_id=dashboard_id, sensor_id=sid))
        await session.commit()
    return dashboard_id


async def _rows(kind: str | None = None) -> list[EventOutbox]:
    async with get_sessionmaker()() as session:
        stmt = select(EventOutbox).order_by(EventOutbox.event_id)
        if kind is not None:
            stmt = stmt.where(EventOutbox.event_kind == kind)
        return list((await session.execute(stmt)).scalars().all())


def _snap(
    sensor_id: UUID, *, name: str, target: dict[str, object] | None = None
) -> _SensorSnapshot:
    return _SensorSnapshot(
        id=sensor_id,
        tenant_id=_TENANT,
        name=name,
        connector_id="vmware-rest-9.0",
        op_id="vmware.vm.list",
        target=target,
        params={},
        assertion=_ASSERTION,
        identity_sub="__sensor__",
    )


def _outcome(state: str) -> AssertionOutcome:
    return AssertionOutcome(state=state, value=1, evidence={"secret-looking": "raw text"})


def _member(name: str, *, target: dict[str, object] | None = None) -> inv._MemberSnapshot:
    return inv._MemberSnapshot(
        sensor_id=uuid4(),
        name=name,
        connector_id="vmware-rest-9.0",
        op_id="vmware.vm.list",
        target=target,
        severity="critical",
        raw_state="critical",
        effective_state="critical",
        last_value=None,
        last_evidence=None,
    )


def _dash(*, prev: str = "ok", cur: str = "critical") -> inv._DashboardSnapshot:
    return inv._DashboardSnapshot(
        dashboard_id=uuid4(),
        name="prod",
        slug="prod",
        previous_state=prev,
        current_state=cur,
        investigator_prompt=None,
        notify_email=None,
    )


async def _drain_investigations() -> None:
    while inv._INVESTIGATIONS:
        await asyncio.gather(*list(inv._INVESTIGATIONS))


# ---------------------------------------------------------------------------
# Sensor events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_off_writes_no_check_rows() -> None:
    await _seed_tenant()
    sid = await _seed_sensor(name="s", last_state="ok")
    dash_member = await _seed_sensor(name="m", last_state="critical")
    await _seed_dashboard([dash_member], memo="ok")

    await _persist_outcome(_snap(sid, name="s"), _outcome("critical"))
    await inv.investigate_on_transition(sensor_id=dash_member, tenant_id=_TENANT)
    await _drain_investigations()

    assert await _rows() == []


@pytest.mark.asyncio
async def test_sensor_change_writes_one_event_with_last_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, "true")
    await _seed_tenant()
    sid = await _seed_sensor(name="disk", last_state="ok", target={"name": "esx-1", "id": "x"})

    await _persist_outcome(_snap(sid, name="disk", target={"name": "esx-1"}), _outcome("critical"))

    rows = await _rows(CHECK_SENSOR_CHANGED)
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["event"] == CHECK_SENSOR_CHANGED
    assert payload["source"] == {"kind": "sensor", "id": str(sid), "name": "disk"}
    assert payload["previous_state"] == "ok"
    assert payload["state"] == "critical"
    assert payload["worsening"] is True
    assert payload["target"] == "esx-1"
    assert payload["coalesce_key"] == "sensor:esx-1"
    assert "evidence" not in payload
    assert "raw text" not in str(payload)
    async with get_sessionmaker()() as session:
        assert (await session.get(Sensor, sid)).last_state == "critical"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_sensor_without_target_keys_by_sensor_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, "true")
    await _seed_tenant()
    sid = await _seed_sensor(name="disk", last_state="critical", target=None)

    await _persist_outcome(_snap(sid, name="disk"), _outcome("ok"))

    (row,) = await _rows(CHECK_SENSOR_CHANGED)
    assert row.payload["coalesce_key"] == f"sensor:{sid}"
    assert row.payload["target"] is None
    assert row.payload["worsening"] is False


@pytest.mark.asyncio
async def test_unchanged_and_held_readings_write_no_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, "true")
    await _seed_tenant()
    same = await _seed_sensor(name="same", last_state="ok")
    held = await _seed_sensor(name="held", last_state="ok", retry_times=2)

    await _persist_outcome(_snap(same, name="same"), _outcome("ok"))
    await _persist_outcome(_snap(held, name="held"), _outcome("critical"))

    assert await _rows() == []


# ---------------------------------------------------------------------------
# Dashboard events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worsening_edge_publishes_one_event_per_cause_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, "true")
    await _seed_tenant()
    members = [_member("a", target={"name": "esx-1"}), _member("b", target=None)]

    await inv.run_investigation(tenant_id=_TENANT, dashboard=_dash(), members=members)

    rows = await _rows(CHECK_DASHBOARD_CHANGED)
    assert len(rows) == 2
    for row in rows:
        payload = row.payload
        assert payload["worsening"] is True
        assert payload["known_noise"] is False
        assert payload["previous_state"] == "ok"
        assert payload["state"] == "critical"
        group = payload["cause_group"]
        assert payload["coalesce_key"] == f"dashboard:{payload['source']['id']}:{group['key']}"
        assert len(group["members"]) == 1
    names = {row.payload["cause_group"]["members"][0]["name"] for row in rows}
    assert names == {"a", "b"}


@pytest.mark.asyncio
async def test_known_noise_group_is_published_with_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, "true")
    await _seed_tenant()
    member = _member("solo")
    await MemoryService().remember(
        Operator(
            sub="op",
            name=None,
            email=None,
            tenant_id=_TENANT,
            tenant_role=TenantRole.TENANT_ADMIN,
            raw_jwt="",
        ),
        MemoryScope.TENANT,
        "verdict: benign\n",
        slug=f"checks-noise-{inv._sensor_group_key(member)}",
        metadata={"source": "checks-investigator", "re_escalate": False},
    )

    await inv.run_investigation(tenant_id=_TENANT, dashboard=_dash(), members=[member])

    (row,) = await _rows(CHECK_DASHBOARD_CHANGED)
    assert row.payload["known_noise"] is True


@pytest.mark.asyncio
async def test_recovery_edge_writes_one_row_without_cause_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, "true")
    await _seed_tenant()
    sid = await _seed_sensor(name="m", last_state="ok")
    await _seed_dashboard([sid], memo="critical")

    await inv.investigate_on_transition(sensor_id=sid, tenant_id=_TENANT)
    await _drain_investigations()

    (row,) = await _rows(CHECK_DASHBOARD_CHANGED)
    assert row.payload["worsening"] is False
    assert row.payload["cause_group"] is None
    assert row.payload["previous_state"] == "critical"
    assert row.payload["state"] == "ok"


@pytest.mark.asyncio
async def test_two_results_at_once_give_one_set_of_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, "true")
    await _seed_tenant()
    a = await _seed_sensor(name="a", last_state="critical")
    b = await _seed_sensor(name="b", last_state="critical")
    await _seed_dashboard([a, b], memo="ok")

    await asyncio.gather(
        inv.investigate_on_transition(sensor_id=a, tenant_id=_TENANT),
        inv.investigate_on_transition(sensor_id=b, tenant_id=_TENANT),
    )
    await _drain_investigations()

    rows = await _rows(CHECK_DASHBOARD_CHANGED)
    assert len(rows) == 2  # one per cause group; both Sensors have no target


@pytest.mark.asyncio
async def test_publish_failure_does_not_stop_the_investigator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, "true")
    await _seed_tenant()

    async def _boom(**_: object) -> None:
        raise RuntimeError("outbox down")

    monkeypatch.setattr(inv, "publish_dashboard_changed", _boom)
    # Returns without raising; the group is still processed (no agent configured).
    await inv.run_investigation(tenant_id=_TENANT, dashboard=_dash(), members=[_member("a")])
    assert await _rows() == []


# ---------------------------------------------------------------------------
# Matching and prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_filter_matches_sensor_event_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, "true")
    await _seed_tenant()
    sid = await _seed_sensor(name="disk", last_state="ok")
    await _persist_outcome(_snap(sid, name="disk"), _outcome("critical"))
    (row,) = await _rows(CHECK_SENSOR_CHANGED)

    assert _payload_contains(row.payload, {"source": {"kind": "sensor"}})
    assert _payload_contains(row.payload, {"source": {"kind": "sensor", "id": str(sid)}})
    assert not _payload_contains(row.payload, {"source": {"kind": "dashboard"}})
    webhook = {"alerts": [], "status": "firing", "labels": {"severity": "critical"}}
    completed = {"status": "succeeded", "run_id": "r1", "agent_definition_id": "d1"}
    assert not _payload_contains(webhook, {"source": {"kind": "sensor"}})
    assert not _payload_contains(completed, {"source": {"kind": "sensor"}})


def test_dashboard_event_prompt_names_dashboard_change_and_sensors() -> None:
    payload = {
        "event": CHECK_DASHBOARD_CHANGED,
        "source": {"kind": "dashboard", "id": "d", "name": "prod-core"},
        "previous_state": "ok",
        "state": "critical",
        "worsening": True,
        "known_noise": False,
        "cause_group": {
            "key": "esx-1",
            "members": [{"name": "disk-a", "state": "critical", "target": "esx-1"}],
        },
    }
    prompt = synthesize_event_prompt(CHECK_DASHBOARD_CHANGED, payload)
    assert BLOCK_START in prompt
    assert "prod-core" in prompt
    assert "ok -> critical" in prompt
    assert "disk-a" in prompt


def test_sensor_event_prompt_names_sensor_and_change() -> None:
    payload = {
        "event": CHECK_SENSOR_CHANGED,
        "source": {"kind": "sensor", "id": "s", "name": "disk-a"},
        "previous_state": "ok",
        "state": "degraded",
        "severity": "critical",
        "worsening": True,
        "target": "esx-1",
        "connector_id": "vmware-rest-9.0",
        "op_id": "vmware.vm.list",
    }
    prompt = synthesize_event_prompt(CHECK_SENSOR_CHANGED, payload)
    assert "disk-a" in prompt and "ok -> degraded" in prompt and "esx-1" in prompt


@pytest.mark.asyncio
async def test_publish_dashboard_changed_without_group_has_no_cause_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_tenant()
    await publish_dashboard_changed(
        tenant_id=_TENANT,
        dashboard_id=uuid4(),
        dashboard_name="prod",
        previous_state="critical",
        state="ok",
        worsening=False,
    )
    (row,) = await _rows(CHECK_DASHBOARD_CHANGED)
    assert row.payload["cause_group"] is None
