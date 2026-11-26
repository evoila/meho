# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Order, mails, schedule and answer shapes of a runner's counted checks.

With ``RUNNER_CHECKS_ENABLED`` on: two batches of one runner that overlap,
a recovery that commits before the mail of the reading before it, the
work list of an older runner, the retry due time of a waiting state change,
list answers, stored copies without secret values, and a dashboard import
that names every Sensor a runner cannot run. The helpers come from
:mod:`tests.test_runner_checks_count`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select, update

import meho_backplane.checks.investigate as inv
import meho_backplane.gateway.runner_checks as runner_checks
from meho_backplane.auth.operator import TenantRole
from meho_backplane.checks.dashboard_file import DashboardImportRefusedError, import_dashboard
from meho_backplane.checks.dashboard_file_schemas import (
    DashboardFile,
    DashboardFileSensor,
    DashboardFileSettings,
    DashboardImportRequest,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    CheckDashboard,
    CheckDashboardSensor,
    RunnerCheckResult,
    Sensor,
)
from meho_backplane.gateway.assignment_service import materialize_assignment
from meho_backplane.runner import loop as runner_loop
from meho_backplane.runner import wire
from meho_backplane.runner.client import RunnerClient
from meho_backplane.runner.loop import RunnerState, run_one_tick
from meho_backplane.runner.spool import ResultSpool
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401  (autouse)
    _isolated_jwks_cache,  # noqa: F401  (autouse)
    _settings_env,  # noqa: F401  (autouse)
)
from .test_gateway_assignment_api import (
    _OP_SAFE,
    _RUNNER_A_ID,
    _TARGET_NAME,
    _TENANT,
    _build_app,
    _register_connector,
    _runner_token,
    _seed_descriptor,
    _seed_identities,
    _seed_target,
)
from .test_runner_checks_count import (
    _ASSERTION,
    _CONNECTOR_ID,
    _KEY,
    _outcome,
    _post,
    _result,
    _seed_dashboard,
    _seed_sensor,
    _sensor,
    wire_empty_digest,
)

_OP_GENERIC = "check.generic"


def _state(**fields: Any) -> RunnerState:
    """A runner state that already sent the set-aside count of its start.

    So each post that these tests see is a result batch.
    """
    return RunnerState(summary_sent=True, **fields)


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


@pytest.fixture
def switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNNER_CHECKS_ENABLED", "true")
    get_settings.cache_clear()


def _wire(sensor_id: uuid.UUID, *, count: int, produced_at: datetime) -> wire.RunnerResult:
    return wire.RunnerResult(
        result_uid=uuid.uuid4().hex,
        check_ref=str(sensor_id),
        op_id=_OP_SAFE,
        status="ok",
        result={"count": count},
        produced_at=produced_at,
    )


async def _count(
    results: list[wire.RunnerResult], received_at: datetime, *, runner_name: str = "runner-a"
) -> None:
    screened = runner_checks.screen_batch(results, received_at, runner_name=runner_name)
    await runner_checks.ingest_and_count(
        tenant_id=_TENANT, runner_name=runner_name, screened=screened, received_at=received_at
    )


# ---------------------------------------------------------------------------
# Two batches of one runner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_batch_that_waited_for_the_lock_keeps_the_newest_reading(
    switch_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Batch A is received first; batch B commits while A is between its two results."""
    await _seed_identities()
    other = await _seed_sensor(name="other")
    sensor_id = await _seed_sensor()
    now = datetime.now(UTC)
    first_received = now - timedelta(seconds=20)
    later_received = now - timedelta(seconds=10)
    batch_a = [
        _wire(other, count=1, produced_at=now - timedelta(seconds=60)),
        _wire(sensor_id, count=50, produced_at=now - timedelta(seconds=25)),  # the newest
    ]
    batch_b = [_wire(sensor_id, count=1, produced_at=now - timedelta(seconds=40))]

    real_lock = runner_checks.lock_assignment_row
    calls = 0

    async def lock_then_let_b_in(session: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:  # A is between its two results
            monkeypatch.setattr(runner_checks, "lock_assignment_row", real_lock)
            await _count(batch_b, later_received)
        await real_lock(session, **kwargs)

    monkeypatch.setattr(runner_checks, "lock_assignment_row", lock_then_let_b_in)
    await _count(batch_a, first_received)

    row = await _sensor(sensor_id)
    assert row.last_state == "critical"
    assert row.last_value == 50


# ---------------------------------------------------------------------------
# A recovery before the mail of the reading before it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_recovery_that_commits_before_the_first_mail_keeps_both_mails(
    switch_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    await _seed_dashboard(sensor_id, memo="ok")
    investigations: list[dict[str, Any]] = []
    notices: list[Any] = []
    monkeypatch.setattr(inv, "_schedule_investigation", lambda **kw: investigations.append(kw))
    monkeypatch.setattr(inv, "schedule_dashboard_notification", notices.append)
    now = datetime.now(UTC)
    critical = [_wire(sensor_id, count=50, produced_at=now - timedelta(seconds=2))]
    recovery = [_wire(sensor_id, count=1, produced_at=now - timedelta(seconds=1))]

    real_after_commit = runner_checks.after_commit
    recovered = False

    async def recovery_first(tenant_id: uuid.UUID, counted: list[Any]) -> None:
        nonlocal recovered
        if not recovered:
            recovered = True
            await _count(recovery, datetime.now(UTC))  # commits before this mail runs
        await real_after_commit(tenant_id, counted)

    monkeypatch.setattr(runner_checks, "after_commit", recovery_first)
    await _count(critical, now)

    assert (await _sensor(sensor_id)).last_state == "ok"
    assert len(investigations) == 1  # the warning starts the investigator
    assert len(notices) == 2  # the warning mail and the recovery mail


# ---------------------------------------------------------------------------
# Two runners on one dashboard, and a failed dashboard claim
# ---------------------------------------------------------------------------


async def _add_member(dashboard_id: uuid.UUID, sensor_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as session:
        session.add(CheckDashboardSensor(dashboard_id=dashboard_id, sensor_id=sensor_id))
        await session.commit()


async def _memo(dashboard_id: uuid.UUID) -> str | None:
    async with get_sessionmaker()() as session:
        row = await session.get(CheckDashboard, dashboard_id)
        assert row is not None
        return row.last_rollup_state


@pytest.mark.asyncio
async def test_two_runners_move_one_dashboard_from_green_to_degraded_to_critical(
    switch_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runner B moves the dashboard to degraded while runner A holds an older copy of it."""
    await _seed_identities()
    sensor_a = await _seed_sensor(name="site-a")
    sensor_b = await _seed_sensor(name="site-b", runner_name="runner-b")
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Sensor)
            .where(Sensor.id == sensor_b)
            .values(
                assertion={
                    "select": {"path": "$.count"},
                    "compare": {"type": "threshold", "op": "gt", "degraded": 10},
                }
            )
        )
        await session.commit()
    dashboard_id = await _seed_dashboard(sensor_a, memo="ok")
    await _add_member(dashboard_id, sensor_b)
    notices: list[Any] = []
    monkeypatch.setattr(inv, "_schedule_investigation", lambda **_kw: None)
    monkeypatch.setattr(inv, "schedule_dashboard_notification", notices.append)
    now = datetime.now(UTC)
    real_runner_sensor = runner_checks._runner_sensor
    held: list[Any] = []  # the session keeps only the copies something holds

    async def b_counts_first(session: Any, **kwargs: Any) -> Any:
        held.append(await session.get(CheckDashboard, dashboard_id))  # A's copy: still green
        monkeypatch.setattr(runner_checks, "_runner_sensor", real_runner_sensor)
        b_result = _wire(sensor_b, count=50, produced_at=now - timedelta(seconds=1))
        await _count([b_result], now, runner_name="runner-b")
        return await real_runner_sensor(session, **kwargs)

    monkeypatch.setattr(runner_checks, "_runner_sensor", b_counts_first)
    await _count([_wire(sensor_a, count=50, produced_at=now - timedelta(seconds=1))], now)

    assert await _memo(dashboard_id) == "critical"
    assert [(n.previous_state, n.current_state) for n in notices] == [
        ("ok", "degraded"),
        ("degraded", "critical"),
    ]


@pytest.mark.asyncio
async def test_a_failed_claim_on_the_second_dashboard_keeps_the_result_for_the_retry(
    client: TestClient, switch_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    first = await _seed_dashboard(sensor_id, memo="ok")
    second = await _seed_dashboard(sensor_id, memo="ok", name="site-two")
    notices: list[Any] = []
    monkeypatch.setattr(inv, "_schedule_investigation", lambda **_kw: None)
    monkeypatch.setattr(inv, "schedule_dashboard_notification", notices.append)
    real_claim = inv._claim_dashboard_in_session
    calls = 0

    async def second_claim_fails(session: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("claim failed")
        return await real_claim(session, **kwargs)

    monkeypatch.setattr(inv, "_claim_dashboard_in_session", second_claim_fails)
    result = _result(sensor_id, payload={"count": 50})
    counted_before = _outcome("counted")

    with pytest.raises(RuntimeError, match="claim failed"):
        _post(client, _RUNNER_A_ID, "runner-a", result)

    assert await _memo(first) == "ok" and await _memo(second) == "ok"
    assert (await _sensor(sensor_id)).last_state == "ok"
    assert notices == []
    assert _outcome("counted") == counted_before  # nothing was saved, so nothing is counted

    retry = _post(client, _RUNNER_A_ID, "runner-a", result)  # the runner sends it again

    assert retry.json() == {"accepted": 1, "duplicates": 0}
    assert _outcome("counted") - counted_before == 1
    assert await _memo(first) == "critical" and await _memo(second) == "critical"
    assert {n.dashboard_id for n in notices} == {first, second}


# ---------------------------------------------------------------------------
# A saved reading that could not be sent, and a newer one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_recovery_waits_behind_a_saved_critical_reading_that_could_not_be_sent(
    switch_on: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The re-post of a saved critical reading fails; the same tick makes a recovery."""
    await _seed_identities()
    sensor_id = await _seed_sensor()
    await _seed_dashboard(sensor_id, memo="ok")
    notices: list[Any] = []
    monkeypatch.setattr(inv, "_schedule_investigation", lambda **_kw: None)
    monkeypatch.setattr(inv, "schedule_dashboard_notification", notices.append)
    spool = ResultSpool(tmp_path, max_files=10)
    critical = _wire(sensor_id, count=50, produced_at=datetime.now(UTC) - timedelta(minutes=1))
    spool.write_batch(wire.RunnerResultBatch(runner_id="runner-a", results=[critical]))
    item = wire.RunnerWorkItem(
        check_ref=str(sensor_id),
        op_id=_OP_SAFE,
        product="net",
        version="1.x",
        impl_id="net-probe",
        handler_ref="unused",
        params={},
        safety_level="safe",
        principal=wire.RunnerPrincipal(
            sub="runner-sub", tenant_id=_TENANT, tenant_role=TenantRole.READ_ONLY
        ),
    )
    assignment = wire.RunnerAssignment(assignment_version="v1", items=[item])

    async def recovery(work: wire.RunnerWorkItem) -> wire.RunnerResult:
        return _wire(uuid.UUID(work.check_ref), count=1, produced_at=datetime.now(UTC))

    monkeypatch.setattr(runner_loop, "execute_work_item", recovery)
    failures_left = 1  # only the re-post of the saved reading fails
    received: list[int] = []

    async def central(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/assignment"):
            return httpx.Response(304)
        nonlocal failures_left
        if failures_left:
            failures_left -= 1
            return httpx.Response(503)
        batch = wire.RunnerResultBatch.model_validate_json(request.content)
        received.extend((r.result or {})["count"] for r in batch.results)
        await _count(batch.results, datetime.now(UTC))
        return httpx.Response(200, json={"accepted": len(batch.results), "duplicates": 0})

    runner = RunnerClient(
        central_url="https://central.example",
        runner_id="runner-a",
        token="tok",
        transport=httpx.MockTransport(central),
    )
    state = _state(assignment=assignment, assignment_version="v1")
    async with runner:
        await run_one_tick(client=runner, spool=spool, state=state, runner_id="runner-a")
        assert received == []
        assert len(spool.load_oldest_first()) == 2  # the recovery waits behind the reading
        await run_one_tick(client=runner, spool=spool, state=state, runner_id="runner-a")

    # The critical reading, the recovery behind it, then the reading of this tick.
    assert received == [50, 1, 1]
    assert spool.load_oldest_first() == []
    assert (await _sensor(sensor_id)).last_state == "ok"
    assert [(n.previous_state, n.current_state) for n in notices] == [
        ("ok", "critical"),
        ("critical", "ok"),
    ]


# ---------------------------------------------------------------------------
# The work list of a runner
# ---------------------------------------------------------------------------


async def _assignment(features: frozenset[str]) -> wire.RunnerAssignment:
    async with get_sessionmaker()() as session:
        return await materialize_assignment(
            session,
            tenant_id=_TENANT,
            runner_name="runner-a",
            principal=wire.RunnerPrincipal(
                sub="runner-sub", tenant_id=_TENANT, tenant_role=TenantRole.READ_ONLY
            ),
            runner_package_set=wire_empty_digest(),
            runner_features=features,
        )


@pytest.mark.asyncio
async def test_an_older_runner_gets_no_counted_check(switch_on: None) -> None:
    await _seed_identities()
    await _seed_target()
    await _seed_descriptor(op_id=_OP_SAFE)
    _register_connector()
    sensor_id = await _seed_sensor()

    older = await _assignment(frozenset())
    current = await _assignment(frozenset({wire.FEATURE_SCHEDULED_CHECKS}))

    assert older.items == []
    assert [item.check_ref for item in current.items] == [str(sensor_id)]
    due = current.items[0].next_due_at
    stored = (await _sensor(sensor_id)).next_fire_at
    assert due is not None and stored is not None
    assert due.tzinfo is not None and due == stored.replace(tzinfo=UTC)


@pytest.mark.asyncio
async def test_the_assignment_route_reads_what_the_runner_can_do(
    client: TestClient, switch_on: None
) -> None:
    await _seed_identities()
    await _seed_target()
    await _seed_descriptor(op_id=_OP_SAFE)
    _register_connector()
    sensor_id = await _seed_sensor()
    headers = {"Authorization": f"Bearer {_runner_token(_KEY, runner_id=_RUNNER_A_ID)}"}

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(_KEY))
        older = client.get("/api/v1/checks/assignment?runner=runner-a", headers=headers)
        current = client.get(
            "/api/v1/checks/assignment?runner=runner-a",
            headers={**headers, wire.FEATURES_HEADER: wire.FEATURE_SCHEDULED_CHECKS},
        )

    assert older.status_code == 200 and older.json()["items"] == []
    assert [item["check_ref"] for item in current.json()["items"]] == [str(sensor_id)]


@pytest.mark.asyncio
async def test_a_runner_tick_reads_the_due_times_the_api_sends(
    client: TestClient, switch_on: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The due times come from the SQLite-backed API; one is the create time fallback."""
    await _seed_identities()
    await _seed_target()
    await _seed_descriptor(op_id=_OP_SAFE)
    _register_connector()
    due_now = await _seed_sensor(name="due-now")
    created_only = await _seed_sensor(name="created-only")
    later = await _seed_sensor(name="later")
    async with get_sessionmaker()() as session:
        past = datetime.now(UTC) - timedelta(minutes=1)
        await session.execute(update(Sensor).where(Sensor.id == due_now).values(next_fire_at=past))
        await session.execute(
            update(Sensor).where(Sensor.id == created_only).values(next_fire_at=None)
        )
        await session.commit()
    headers = {
        "Authorization": f"Bearer {_runner_token(_KEY, runner_id=_RUNNER_A_ID)}",
        wire.FEATURES_HEADER: wire.FEATURE_SCHEDULED_CHECKS,
    }
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(_KEY))
        body = client.get("/api/v1/checks/assignment?runner=runner-a", headers=headers).json()
    assignment = wire.RunnerAssignment.model_validate(body)
    posted: list[str] = []

    def central(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/assignment"):
            return httpx.Response(200, json=body)
        posted.append(request.content.decode())
        return httpx.Response(200, json={"accepted": 1, "duplicates": 0})

    async def run(item: wire.RunnerWorkItem) -> wire.RunnerResult:
        return wire.RunnerResult(
            result_uid=uuid.uuid4().hex, check_ref=item.check_ref, op_id=item.op_id, status="ok"
        )

    monkeypatch.setattr(runner_loop, "execute_work_item", run)
    runner = RunnerClient(
        central_url="https://central.example",
        runner_id="runner-a",
        token="tok",
        transport=httpx.MockTransport(central),
    )
    state = _state(assignment=assignment, assignment_version=assignment.assignment_version)
    async with runner:
        await run_one_tick(
            client=runner,
            spool=ResultSpool(tmp_path, max_files=10),
            state=state,
            runner_id="runner-a",
        )

    assert len(posted) == 1
    sent = {r["check_ref"] for r in json.loads(posted[0])["results"]}
    assert sent == {str(due_now), str(created_only)}  # "later" is due in a minute
    assert str(later) in {item.check_ref for item in assignment.items}


@pytest.mark.asyncio
async def test_a_waiting_state_change_brings_the_next_due_time_forward(switch_on: None) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor(retry_times=2)
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Sensor).where(Sensor.id == sensor_id).values(retry_backoff_seconds=30)
        )
        await session.commit()
    now = datetime.now(UTC)

    await _count([_wire(sensor_id, count=50, produced_at=now - timedelta(seconds=1))], now)

    row = await _sensor(sensor_id)
    assert row.pending_state == "critical"
    assert row.last_state == "ok"
    assert row.next_fire_at is not None and row.last_evaluated_at is not None
    assert row.next_fire_at <= row.last_evaluated_at + timedelta(seconds=30)


# ---------------------------------------------------------------------------
# Answer shapes and stored copies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_list_answer_is_evaluated(client: TestClient, switch_on: None) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Sensor)
            .where(Sensor.id == sensor_id)
            .values(
                assertion={
                    "select": {"path": "$[*].running", "aggregate": "all"},
                    "compare": {"type": "bool", "expect": True},
                }
            )
        )
        await session.commit()
    body = _result(sensor_id)
    body["result"] = [{"name": "node-1", "running": True}, {"name": "node-2", "running": False}]

    resp = _post(client, _RUNNER_A_ID, "runner-a", body)

    assert resp.json() == {"accepted": 1, "duplicates": 0}
    assert (await _sensor(sensor_id)).last_state == "critical"


@pytest.mark.asyncio
async def test_the_stored_result_holds_no_secret_value(client: TestClient, switch_on: None) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    failed = _result(sensor_id, status="error")
    failed["result"] = None
    failed["error"] = 'LoginError: body {"password": "hunter2xyz"} was refused'

    _post(client, _RUNNER_A_ID, "runner-a", _result(sensor_id, payload={"password": "hunter2xyz"}))
    _post(client, _RUNNER_A_ID, "runner-a", failed)

    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(RunnerCheckResult))).scalars().all()
    assert len(rows) == 2
    for row in rows:
        assert "hunter2xyz" not in str(row.result_payload)
        assert "hunter2xyz" not in str(row.error)


# ---------------------------------------------------------------------------
# Dashboard import
# ---------------------------------------------------------------------------


def _file_sensor(name: str) -> DashboardFileSensor:
    return DashboardFileSensor.model_validate(
        {
            "name": name,
            "connector_id": _CONNECTOR_ID,
            "op_id": _OP_GENERIC,
            "target": "site",
            "assertion": _ASSERTION,
            "cadence_kind": "interval",
            "interval_seconds": 60,
        }
    )


@pytest.mark.asyncio
async def test_an_import_names_every_runner_check_whose_new_op_the_runner_cannot_run(
    switch_on: None,
) -> None:
    await _seed_identities()
    await _seed_target()
    await _seed_descriptor(op_id=_OP_SAFE)
    await _seed_descriptor(op_id=_OP_GENERIC, handler_ref=None)
    _register_connector()
    await _seed_sensor(name="first")
    await _seed_sensor(name="second")
    request = DashboardImportRequest(
        file=DashboardFile(
            dashboard=DashboardFileSettings(name="site"),
            sensors=[_file_sensor("first"), _file_sensor("second")],
        ),
        target_map={"site": _TARGET_NAME},
    )

    with pytest.raises(DashboardImportRefusedError) as refused:
        await import_dashboard(_TENANT, "op-admin", request)

    problems = refused.value.problems
    assert [p.kind for p in problems] == ["dashboard_import_runner_cannot_run"] * 2
    assert [p.loc[3] for p in problems] == ["0", "1"]
    assert (await _sensor_by_name("first")).op_id == _OP_SAFE
    assert (await _sensor_by_name("second")).op_id == _OP_SAFE


@pytest.mark.asyncio
async def test_an_import_names_an_unknown_op_and_a_runner_op_together_and_writes_nothing(
    switch_on: None,
) -> None:
    await _seed_identities()
    await _seed_target()
    await _seed_descriptor(op_id=_OP_SAFE)
    await _seed_descriptor(op_id=_OP_GENERIC, handler_ref=None)
    _register_connector()
    await _seed_sensor(name="bound")
    unknown = _file_sensor("new-one").model_copy(update={"op_id": "check.not-here"})
    request = DashboardImportRequest(
        file=DashboardFile(
            dashboard=DashboardFileSettings(name="site"),
            sensors=[unknown, _file_sensor("bound")],
        ),
        target_map={"site": _TARGET_NAME},
    )

    with pytest.raises(DashboardImportRefusedError) as refused:
        await import_dashboard(_TENANT, "op-admin", request)

    problems = refused.value.problems
    assert [(p.kind, p.loc[3]) for p in problems] == [
        ("dashboard_import_unknown_operation", "0"),
        ("dashboard_import_runner_cannot_run", "1"),
    ]
    assert (await _sensor_by_name("bound")).op_id == _OP_SAFE
    async with get_sessionmaker()() as session:
        assert await session.scalar(select(Sensor).where(Sensor.name == "new-one")) is None
        assert (await session.scalars(select(CheckDashboard))).all() == []


async def _sensor_by_name(name: str) -> Sensor:
    async with get_sessionmaker()() as session:
        row = await session.scalar(select(Sensor).where(Sensor.name == name))
        assert row is not None
        return row
