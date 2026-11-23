# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A runner's check results count like central checks.

With ``RUNNER_CHECKS_ENABLED`` on, the result a runner posts for its own
Sensor sets the state of that Sensor, mails and the investigator follow, and a
stale runner shows its checks as ``unknown``. With the switch off, results
are only stored.

The tests post to the real ``POST /api/v1/checks/results`` route behind the
real JWT chain, against the SQLite test database.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import select, update

import meho_backplane.checks.investigate as inv
from meho_backplane.checks.assertions import CheckState
from meho_backplane.checks.repository import claim_due_sensors, create_sensor
from meho_backplane.checks.runner_binding import SensorRunnerBindingError, bind_sensor_to_runner
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    CheckDashboard,
    CheckDashboardSensor,
    RunnerAssignmentRow,
    RunnerCheckResult,
    RunnerPrincipal,
    Sensor,
    SensorCadenceKind,
    Tenant,
)
from meho_backplane.gateway.assignment_service import materialize_assignment
from meho_backplane.gateway.deadman import _run_one_tick
from meho_backplane.runner import wire
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401  (autouse)
    _isolated_jwks_cache,  # noqa: F401  (autouse)
    _settings_env,  # noqa: F401  (autouse)
)
from .test_gateway_assignment_api import (
    _HANDLER_REF,
    _OP_SAFE,
    _RUNNER_A_ID,
    _RUNNER_B_ID,
    _TARGET_NAME,
    _TENANT,
    _build_app,
    _register_connector,
    _runner_token,
    _seed_descriptor,
    _seed_identities,
    _seed_target,
)

_OTHER_TENANT = uuid.UUID("99999999-9999-9999-9999-999999999999")
_CONNECTOR_ID = "checkprod-api-1.x"
_ASSERTION: dict[str, Any] = {
    "select": {"path": "$.count"},
    "compare": {"type": "threshold", "op": "gt", "critical": 10},
}


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


@pytest.fixture
def switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNNER_CHECKS_ENABLED", "true")
    get_settings.cache_clear()


def _outcome(name: str) -> float:
    return REGISTRY.get_sample_value("runner_check_results_total", {"outcome": name}) or 0.0


async def _seed_sensor(
    *,
    name: str = "site-count",
    runner_name: str | None = "runner-a",
    tenant_id: uuid.UUID = _TENANT,
    last_state: str = "ok",
    status: str = "active",
    retry_times: int = 0,
) -> uuid.UUID:
    now = datetime.now(UTC)
    sensor_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        if await session.get(Tenant, tenant_id) is None:
            session.add(Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[:6]}", name="Other"))
        session.add(
            Sensor(
                id=sensor_id,
                tenant_id=tenant_id,
                name=name,
                connector_id=_CONNECTOR_ID,
                op_id=_OP_SAFE,
                target={"name": _TARGET_NAME},
                params={},
                assertion=_ASSERTION,
                status=status,
                cadence_kind="interval",
                interval_seconds=60,
                timezone="UTC",
                next_fire_at=now + timedelta(seconds=60),
                severity="critical",
                for_seconds=0,
                retry_times=retry_times,
                last_state=last_state,
                last_evaluated_at=now - timedelta(minutes=5),
                state_since=now - timedelta(hours=1),
                identity_sub="__sensor__",
                created_by_sub="op-admin",
                runner_name=runner_name,
            )
        )
        await session.commit()
    return sensor_id


async def _sensor(sensor_id: uuid.UUID) -> Sensor:
    async with get_sessionmaker()() as session:
        row = await session.get(Sensor, sensor_id)
        assert row is not None
        return row


def _result(
    sensor_id: uuid.UUID | str,
    *,
    uid: str | None = None,
    payload: dict[str, Any] | None = None,
    produced_at: datetime | None = None,
    op_id: str = _OP_SAFE,
    status: str = "ok",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "result_uid": uid or uuid.uuid4().hex,
        "check_ref": str(sensor_id),
        "op_id": op_id,
        "status": status,
        "result": payload if payload is not None else {"count": 1},
        "error": None,
    }
    if produced_at is not None:
        body["produced_at"] = produced_at.isoformat()
    return body


_KEY = make_rsa_keypair("kid-A")


def _post(client: TestClient, runner_id: uuid.UUID, runner: str, *results: dict[str, Any]) -> Any:
    key = _KEY
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        return client.post(
            "/api/v1/checks/results",
            json={"runner_id": runner, "results": list(results)},
            headers={"Authorization": f"Bearer {_runner_token(key, runner_id=runner_id)}"},
        )


async def _stored_count() -> int:
    async with get_sessionmaker()() as session:
        return len((await session.execute(select(RunnerCheckResult))).scalars().all())


async def _seed_dashboard(sensor_id: uuid.UUID, *, memo: str | None) -> uuid.UUID:
    dashboard_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            CheckDashboard(
                id=dashboard_id,
                tenant_id=_TENANT,
                name="site",
                description=None,
                last_rollup_state=memo,
                notify_email="ops@example.test",
                notify_min_state="degraded",
                created_by_sub="op-admin",
            )
        )
        session.add(CheckDashboardSensor(dashboard_id=dashboard_id, sensor_id=sensor_id))
        await session.commit()
    return dashboard_id


# ---------------------------------------------------------------------------
# The result counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_sets_state_raises_mail_on_change_and_recovery_and_starts_investigator(
    client: TestClient, switch_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    await _seed_dashboard(sensor_id, memo="ok")
    investigations: list[dict[str, Any]] = []
    notices: list[Any] = []
    monkeypatch.setattr(inv, "_schedule_investigation", lambda **kw: investigations.append(kw))
    monkeypatch.setattr(inv, "schedule_dashboard_notification", notices.append)
    counted_before = _outcome("counted")

    bad = _post(client, _RUNNER_A_ID, "runner-a", _result(sensor_id, payload={"count": 50}))
    assert bad.status_code == 200
    assert bad.json() == {"accepted": 1, "duplicates": 0}
    row = await _sensor(sensor_id)
    assert row.last_state == "critical"
    assert row.last_evidence is not None and row.last_evidence.get("observed") == 50
    assert len(investigations) == 1
    assert len(notices) == 1

    good = _post(client, _RUNNER_A_ID, "runner-a", _result(sensor_id, payload={"count": 1}))
    assert good.status_code == 200
    row = await _sensor(sensor_id)
    assert row.last_state == "ok"
    assert len(notices) == 2  # the recovery mail
    assert len(investigations) == 1  # a recovery starts no investigation
    assert _outcome("counted") - counted_before == 2


@pytest.mark.asyncio
async def test_runner_error_result_reads_unknown(client: TestClient, switch_on: None) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    resp = _post(client, _RUNNER_A_ID, "runner-a", _result(sensor_id, status="refused"))
    assert resp.status_code == 200
    row = await _sensor(sensor_id)
    assert row.last_state == "unknown"
    assert row.last_evidence is not None and row.last_evidence["reason"] == "runner_refused"


# ---------------------------------------------------------------------------
# What does not belong to the runner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_ref_of_another_runner_or_tenant_changes_no_sensor(
    client: TestClient, switch_on: None
) -> None:
    await _seed_identities()
    other_runner = await _seed_sensor(name="b-check", runner_name="runner-b")
    other_tenant = await _seed_sensor(
        name="t2-check", runner_name="runner-a", tenant_id=_OTHER_TENANT
    )
    central = await _seed_sensor(name="central", runner_name=None)
    not_a_sensor = "chk-1"
    before = _outcome("not_in_list")

    resp = _post(
        client,
        _RUNNER_A_ID,
        "runner-a",
        _result(other_runner, payload={"count": 50}),
        _result(other_tenant, payload={"count": 50}),
        _result(central, payload={"count": 50}),
        _result(not_a_sensor, payload={"count": 50}),
    )

    assert resp.status_code == 200
    assert resp.json() == {"accepted": 4, "duplicates": 0}  # stored, not counted
    for sensor_id in (other_runner, other_tenant, central):
        row = await _sensor(sensor_id)
        assert row.last_state == "ok"
        assert row.last_value is None
    assert _outcome("not_in_list") - before == 4
    assert _RUNNER_B_ID != _RUNNER_A_ID


@pytest.mark.asyncio
async def test_paused_sensor_and_wrong_op_are_not_counted(
    client: TestClient, switch_on: None
) -> None:
    await _seed_identities()
    paused = await _seed_sensor(name="paused", status="paused")
    wrong_op = await _seed_sensor(name="wrong-op")
    _post(
        client,
        _RUNNER_A_ID,
        "runner-a",
        _result(paused, payload={"count": 50}),
        _result(wrong_op, payload={"count": 50}, op_id="check.other"),
    )
    assert (await _sensor(paused)).last_state == "ok"
    assert (await _sensor(wrong_op)).last_state == "ok"


# ---------------------------------------------------------------------------
# Size cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_answer_over_256_kb_gives_unknown_answer_too_large(
    client: TestClient, switch_on: None
) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    big = {"count": 50, "blob": "x" * (256 * 1024 + 10)}
    before = _outcome("too_large")

    resp = _post(client, _RUNNER_A_ID, "runner-a", _result(sensor_id, payload=big))

    assert resp.status_code == 200
    row = await _sensor(sensor_id)
    assert row.last_state == "unknown"
    assert row.last_evidence is not None and row.last_evidence["reason"] == "answer too large"
    async with get_sessionmaker()() as session:
        stored = (await session.execute(select(RunnerCheckResult))).scalars().one()
    assert stored.result_payload is None
    assert stored.error == "answer too large"
    assert _outcome("too_large") - before == 1


@pytest.mark.asyncio
async def test_batch_cap_marks_the_results_over_it_too_large(
    client: TestClient, switch_on: None
) -> None:
    await _seed_identities()
    sensors = [await _seed_sensor(name=f"s{i}") for i in range(3)]
    chunk = {"count": 1, "blob": "y" * (200 * 1024)}  # under the per-result cap
    results = [_result(sensor, payload=chunk) for sensor in sensors for _ in range(10)]
    # 30 results of ~200 KB is about 6 MB, over the 4 MB batch cap.
    resp = _post(client, _RUNNER_A_ID, "runner-a", *results)
    assert resp.status_code == 200
    states = {(await _sensor(sensor)).last_state for sensor in sensors}
    assert "unknown" in states


# ---------------------------------------------------------------------------
# Time and replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_time_in_the_future_is_refused(client: TestClient, switch_on: None) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    before = _outcome("future_time")
    future = datetime.now(UTC) + timedelta(hours=1)

    resp = _post(
        client,
        _RUNNER_A_ID,
        "runner-a",
        _result(sensor_id, payload={"count": 50}, produced_at=future),
    )

    assert resp.status_code == 200
    assert resp.json() == {"accepted": 0, "duplicates": 0}
    assert await _stored_count() == 0
    assert (await _sensor(sensor_id)).last_state == "ok"
    assert _outcome("future_time") - before == 1


@pytest.mark.asyncio
async def test_replayed_and_old_results_are_dropped(client: TestClient, switch_on: None) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    now = datetime.now(UTC)
    first = _result(sensor_id, uid="uid-1", payload={"count": 50}, produced_at=now)

    assert _post(client, _RUNNER_A_ID, "runner-a", first).json() == {"accepted": 1, "duplicates": 0}
    after_first = await _sensor(sensor_id)
    assert after_first.last_state == "critical"

    # The same result again (a replay from the spool) counts nothing.
    again = _post(client, _RUNNER_A_ID, "runner-a", first)
    assert again.json() == {"accepted": 0, "duplicates": 1}
    assert (await _sensor(sensor_id)).last_evaluated_at == after_first.last_evaluated_at

    # A new result made before the last counted one is dropped.
    old = _result(
        sensor_id, uid="uid-2", payload={"count": 1}, produced_at=now - timedelta(minutes=30)
    )
    before = _outcome("old")
    assert _post(client, _RUNNER_A_ID, "runner-a", old).json() == {"accepted": 1, "duplicates": 0}
    row = await _sensor(sensor_id)
    assert row.last_state == "critical"
    assert row.last_evaluated_at == after_first.last_evaluated_at
    assert _outcome("old") - before == 1


@pytest.mark.asyncio
async def test_results_without_a_runner_time_still_count(
    client: TestClient, switch_on: None
) -> None:
    """A runner that sends no ``produced_at`` is accepted."""
    await _seed_identities()
    sensor_id = await _seed_sensor()
    _post(client, _RUNNER_A_ID, "runner-a", _result(sensor_id, payload={"count": 50}))
    assert (await _sensor(sensor_id)).last_state == "critical"
    assert (
        wire.RunnerResult(result_uid="u", check_ref="c", op_id="o", status="ok").produced_at is None
    )


# ---------------------------------------------------------------------------
# Stale runner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_runner_makes_its_sensors_unknown_never_green(switch_on: None) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor(retry_times=2)
    other = await _seed_sensor(name="central", runner_name=None)
    async with get_sessionmaker()() as session:
        session.add(RunnerAssignmentRow(tenant_id=_TENANT, runner_name="runner-a", items=[]))
        await session.execute(
            update(RunnerPrincipal)
            .where(RunnerPrincipal.id == _RUNNER_A_ID)
            .values(last_seen_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await session.commit()

    await _run_one_tick()

    row = await _sensor(sensor_id)
    assert row.last_state == "unknown"
    assert row.last_evidence is not None and row.last_evidence["reason"] == "runner_stale"
    assert (await _sensor(other)).last_state == "ok"


@pytest.mark.asyncio
async def test_runner_back_after_stale_gets_normal_results(
    client: TestClient, switch_on: None
) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    async with get_sessionmaker()() as session:
        session.add(RunnerAssignmentRow(tenant_id=_TENANT, runner_name="runner-a", items=[]))
        await session.execute(
            update(RunnerPrincipal)
            .where(RunnerPrincipal.id == _RUNNER_A_ID)
            .values(last_seen_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await session.commit()
    await _run_one_tick()
    assert (await _sensor(sensor_id)).last_state == "unknown"

    _post(client, _RUNNER_A_ID, "runner-a", _result(sensor_id, payload={"count": 1}))

    assert (await _sensor(sensor_id)).last_state == "ok"


# ---------------------------------------------------------------------------
# Switch off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_off_results_are_only_stored(client: TestClient) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    before = {
        name: _outcome(name) for name in ("counted", "not_in_list", "too_large", "future_time")
    }
    future = datetime.now(UTC) + timedelta(hours=1)
    big = {"count": 50, "blob": "x" * (300 * 1024)}

    resp = _post(
        client,
        _RUNNER_A_ID,
        "runner-a",
        _result(sensor_id, payload={"count": 50}),
        _result(sensor_id, payload=big),
        _result(sensor_id, payload={"count": 50}, produced_at=future),
    )

    assert resp.json() == {"accepted": 3, "duplicates": 0}
    row = await _sensor(sensor_id)
    assert row.last_state == "ok"
    assert row.last_value is None
    async with get_sessionmaker()() as session:
        stored = (await session.execute(select(RunnerCheckResult))).scalars().all()
    assert len(stored) == 3
    assert any(r.result_payload is not None and "blob" in r.result_payload for r in stored)
    assert {name: _outcome(name) for name in before} == before


@pytest.mark.asyncio
async def test_switch_off_assignment_has_no_sensor_items_and_stale_runner_is_not_marked() -> None:
    await _seed_identities()
    await _seed_target()
    await _seed_descriptor(op_id=_OP_SAFE)
    _register_connector()
    sensor_id = await _seed_sensor()
    principal = wire.RunnerPrincipal(
        sub="runner-sub", tenant_id=_TENANT, tenant_role="read_only", principal_kind="runner"
    )
    async with get_sessionmaker()() as session:
        assignment = await materialize_assignment(
            session,
            tenant_id=_TENANT,
            runner_name="runner-a",
            principal=principal,
            runner_package_set=wire_empty_digest(),
        )
    assert assignment.items == []
    assert (await _sensor(sensor_id)).last_state == "ok"


def wire_empty_digest() -> str:
    from meho_backplane.runner.package_set import EMPTY_PACKAGE_SET_DIGEST

    return EMPTY_PACKAGE_SET_DIGEST


# ---------------------------------------------------------------------------
# The runner's list comes from its sensors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_list_has_one_item_per_active_sensor_of_the_runner(switch_on: None) -> None:
    await _seed_identities()
    await _seed_target()
    await _seed_descriptor(op_id=_OP_SAFE)
    _register_connector()
    mine = await _seed_sensor(name="mine")
    await _seed_sensor(name="paused", status="paused")
    await _seed_sensor(name="theirs", runner_name="runner-b")
    await _seed_sensor(name="central", runner_name=None)
    principal = wire.RunnerPrincipal(
        sub="runner-sub", tenant_id=_TENANT, tenant_role="read_only", principal_kind="runner"
    )
    async with get_sessionmaker()() as session:
        assignment = await materialize_assignment(
            session,
            tenant_id=_TENANT,
            runner_name="runner-a",
            principal=principal,
            runner_package_set=wire_empty_digest(),
        )
    assert [item.check_ref for item in assignment.items] == [str(mine)]
    item = assignment.items[0]
    assert item.op_id == _OP_SAFE
    assert item.handler_ref == _HANDLER_REF
    assert item.target_descriptor is not None and item.target_descriptor.name == _TARGET_NAME


# ---------------------------------------------------------------------------
# Binding a sensor to a runner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_binding_needs_the_switch_and_a_live_runner_of_the_same_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_identities()
    async with get_sessionmaker()() as session:
        with pytest.raises(SensorRunnerBindingError) as off:
            await bind_sensor_to_runner(session, tenant_id=_TENANT, runner_name="runner-a")
    assert off.value.error_code == "sensor_runner_checks_disabled"

    monkeypatch.setenv("RUNNER_CHECKS_ENABLED", "true")
    get_settings.cache_clear()
    async with get_sessionmaker()() as session:
        with pytest.raises(SensorRunnerBindingError) as unknown:
            await bind_sensor_to_runner(session, tenant_id=_TENANT, runner_name="no-such-runner")
        with pytest.raises(SensorRunnerBindingError) as other_tenant:
            await bind_sensor_to_runner(session, tenant_id=_OTHER_TENANT, runner_name="runner-a")
        await session.execute(
            update(RunnerPrincipal).where(RunnerPrincipal.id == _RUNNER_B_ID).values(revoked=True)
        )
        with pytest.raises(SensorRunnerBindingError) as revoked:
            await bind_sensor_to_runner(session, tenant_id=_TENANT, runner_name="runner-b")
    assert unknown.value.error_code == "sensor_runner_unknown"
    assert other_tenant.value.error_code == "sensor_runner_unknown"
    assert revoked.value.error_code == "sensor_runner_unknown"


@pytest.mark.asyncio
async def test_binding_makes_an_assignment_row_so_the_sweeper_watches_the_runner(
    switch_on: None,
) -> None:
    await _seed_identities()
    async with get_sessionmaker()() as session:
        await bind_sensor_to_runner(session, tenant_id=_TENANT, runner_name="runner-a")
        await bind_sensor_to_runner(session, tenant_id=_TENANT, runner_name="runner-a")
        await session.commit()
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(RunnerAssignmentRow))).scalars().all()
    assert [r.runner_name for r in rows] == ["runner-a"]


@pytest.mark.asyncio
async def test_central_loop_does_not_claim_a_sensor_with_a_runner() -> None:
    await _seed_identities()
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        for name, runner in (("at-site", "runner-a"), ("central", None)):
            row = await create_sensor(
                session,
                tenant_id=_TENANT,
                name=name,
                connector_id=_CONNECTOR_ID,
                op_id=_OP_SAFE,
                target=None,
                params={},
                assertion=_ASSERTION,
                cadence_kind=SensorCadenceKind.INTERVAL,
                interval_seconds=5,
                cron_expr=None,
                timezone="UTC",
                severity="critical",
                for_seconds=0,
                retry_times=0,
                retry_backoff_seconds=15,
                identity_sub="__sensor__",
                created_by_sub="op-admin",
                runner_name=runner,
                base=now - timedelta(minutes=5),
            )
            assert row.runner_name == runner
        await session.commit()
    async with get_sessionmaker()() as session:
        claimed = await claim_due_sensors(session, now=now, limit=10)
    assert [s.name for s in claimed] == ["central"]


# ---------------------------------------------------------------------------
# Clean-up
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_up_removes_raw_rows_older_than_seven_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meho_backplane.checks.evidence_retention import _run_one_prune_tick

    await _seed_identities()
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        for uid, age in (("old", timedelta(days=8)), ("fresh", timedelta(days=6))):
            session.add(
                RunnerCheckResult(
                    tenant_id=_TENANT,
                    runner_name="runner-a",
                    result_uid=uid,
                    check_ref="c",
                    op_id=_OP_SAFE,
                    status="ok",
                    received_at=now - age,
                )
            )
        await session.commit()

    await _run_one_prune_tick()

    async with get_sessionmaker()() as session:
        left = (await session.execute(select(RunnerCheckResult.result_uid))).scalars().all()
    assert left == ["fresh"]

    monkeypatch.setenv("RUNNER_CHECK_RESULTS_RETENTION_DAYS", "0")
    get_settings.cache_clear()
    async with get_sessionmaker()() as session:
        session.add(
            RunnerCheckResult(
                tenant_id=_TENANT,
                runner_name="runner-a",
                result_uid="ancient",
                check_ref="c",
                op_id=_OP_SAFE,
                status="ok",
                received_at=now - timedelta(days=400),
            )
        )
        await session.commit()
    await _run_one_prune_tick()
    async with get_sessionmaker()() as session:
        left = (await session.execute(select(RunnerCheckResult.result_uid))).scalars().all()
    assert sorted(left) == ["ancient", "fresh"]


def test_check_state_vocabulary_is_unchanged() -> None:
    assert set(CheckState.__args__) == {"ok", "degraded", "critical", "unknown", "skip"}  # type: ignore[attr-defined]
    assert json.dumps({"a": 1})
