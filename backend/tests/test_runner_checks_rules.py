# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The rules around a runner's check results, with ``RUNNER_CHECKS_ENABLED``.

Which ops a runner may run, how answers are cleaned of secrets, how runner
times are checked, how the dead-man sweeper and a result batch share a lock,
and what the read answers look like with the switch off. The helpers come
from :mod:`tests.test_runner_checks_count`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.dialects import postgresql

import meho_backplane.checks.investigate as inv
import meho_backplane.gateway.deadman as deadman
import meho_backplane.gateway.runner_checks as runner_checks
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.checks.auth_park import AUTH_FAILED_ERROR_CODE, AUTH_PARK_REASON
from meho_backplane.checks.dashboard_schemas import DashboardMemberView
from meho_backplane.checks.runner_binding import SensorRunnerBindingError
from meho_backplane.checks.schemas import SensorCreate, SensorRead
from meho_backplane.checks.sensor_edit import SensorUpdate, edit_sensor
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    OperationPolicyOverride,
    RunnerAssignmentRow,
    RunnerCheckResult,
    RunnerPrincipal,
    Sensor,
)
from meho_backplane.gateway.assignment_service import materialize_assignment
from meho_backplane.gateway.repository import lock_assignment_row
from meho_backplane.mcp.registry import get_tool, tool_for_settings
from meho_backplane.mcp.tools import sensor_pause as pause_tools
from meho_backplane.mcp.tools import sensors as sensor_tools
from meho_backplane.runner import wire
from meho_backplane.settings import get_settings

from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401  (autouse)
    _isolated_jwks_cache,  # noqa: F401  (autouse)
    _settings_env,  # noqa: F401  (autouse)
)
from .test_gateway_assignment_api import (
    _IMPL,
    _OP_SAFE,
    _PRODUCT,
    _RUNNER_A_ID,
    _TENANT,
    _VERSION,
    _build_app,
    _register_connector,
    _seed_descriptor,
    _seed_identities,
    _seed_target,
)
from .test_runner_checks_count import (
    _CONNECTOR_ID,
    _outcome,
    _post,
    _result,
    _seed_dashboard,
    _seed_sensor,
    _sensor,
    _stored_count,
    wire_empty_digest,
)

_OP_GENERIC = "check.generic"


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


@pytest.fixture
def switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNNER_CHECKS_ENABLED", "true")
    get_settings.cache_clear()


def _principal() -> wire.RunnerPrincipal:
    return wire.RunnerPrincipal(
        sub="runner-sub", tenant_id=_TENANT, tenant_role="read_only", principal_kind="runner"
    )


async def _assignment_refs() -> list[str]:
    async with get_sessionmaker()() as session:
        assignment = await materialize_assignment(
            session,
            tenant_id=_TENANT,
            runner_name="runner-a",
            principal=_principal(),
            runner_package_set=wire_empty_digest(),
            runner_features=frozenset({wire.FEATURE_SCHEDULED_CHECKS}),
        )
    return [item.check_ref for item in assignment.items]


async def _set_last_seen(when: datetime) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            update(RunnerPrincipal)
            .where(RunnerPrincipal.id == _RUNNER_A_ID)
            .values(last_seen_at=when)
        )
        await session.commit()


async def _add_assignment_row() -> uuid.UUID:
    async with get_sessionmaker()() as session:
        row = RunnerAssignmentRow(tenant_id=_TENANT, runner_name="runner-a", items=[])
        session.add(row)
        await session.commit()
        return row.id


async def _stale_at() -> datetime | None:
    async with get_sessionmaker()() as session:
        return await session.scalar(
            select(RunnerAssignmentRow.stale_at).where(
                RunnerAssignmentRow.runner_name == "runner-a"
            )
        )


def _admin() -> Operator:
    return Operator(
        sub="op-admin",
        raw_jwt="dummy",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        scopes=frozenset({"mcp:admin"}),
    )


def _create_args(**extra: Any) -> dict[str, Any]:
    return {
        "name": "site-check",
        "connector_id": _CONNECTOR_ID,
        "op_id": _OP_SAFE,
        "assertion": {
            "select": {"path": "$.count"},
            "compare": {"type": "threshold", "op": "gt", "critical": 10},
        },
        "cadence_kind": "interval",
        "interval_seconds": 60,
        **extra,
    }


# ---------------------------------------------------------------------------
# Which ops a runner runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_rule_that_needs_approval_set_after_create_takes_the_check_off_the_runner(
    switch_on: None,
) -> None:
    await _seed_identities()
    await _seed_target()
    await _seed_descriptor(op_id=_OP_SAFE)
    _register_connector()
    sensor_id = await _seed_sensor()
    assert await _assignment_refs() == [str(sensor_id)]

    async with get_sessionmaker()() as session:
        session.add(
            OperationPolicyOverride(
                tenant_id=_TENANT,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL,
                op_id=_OP_SAFE,
                requires_approval=True,
                set_by_sub="op-admin",
            )
        )
        await session.commit()

    assert await _assignment_refs() == []


@pytest.mark.asyncio
async def test_create_and_edit_refuse_an_op_the_runner_cannot_run(switch_on: None) -> None:
    await _seed_identities()
    await _seed_descriptor(op_id=_OP_SAFE)
    await _seed_descriptor(op_id=_OP_GENERIC, handler_ref=None)
    _register_connector()

    with pytest.raises(Exception) as refused:
        await sensor_tools._create_handler(
            _admin(), _create_args(op_id=_OP_GENERIC, runner_name="runner-a")
        )
    assert "sensor_runner_op_unsupported" in str(refused.value)

    sensor_id = await _seed_sensor()
    with pytest.raises(SensorRunnerBindingError) as edit:
        await edit_sensor(
            _TENANT, sensor_id, SensorUpdate(op_id=_OP_GENERIC), editor_sub="op-admin"
        )
    assert edit.value.error_code == "sensor_runner_op_unsupported"
    assert (await _sensor(sensor_id)).op_id == _OP_SAFE


# ---------------------------------------------------------------------------
# Create through MCP and the switch-off shapes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_create_takes_a_runner_name_only_while_the_switch_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, _handler = get_tool("meho_sensor_create") or (None, None)
    assert definition is not None
    off = tool_for_settings(definition, _admin())
    assert "runner_name" not in off.inputSchema["properties"]

    monkeypatch.setenv("RUNNER_CHECKS_ENABLED", "true")
    get_settings.cache_clear()
    on = tool_for_settings(definition, _admin())
    assert on.inputSchema["properties"]["runner_name"]["type"] == "string"

    await _seed_identities()
    await _seed_descriptor(op_id=_OP_SAFE)
    _register_connector()
    created = await sensor_tools._create_handler(_admin(), _create_args(runner_name="runner-a"))
    assert created["sensor"]["runner_name"] == "runner-a"
    row = await _sensor(uuid.UUID(created["sensor_id"]))
    assert row.runner_name == "runner-a"


def test_create_schema_leaves_runner_name_optional_and_not_null() -> None:
    prop = SensorCreate.model_json_schema()["properties"]["runner_name"]
    assert prop.get("type") == "string"
    assert "anyOf" not in prop


@pytest.mark.asyncio
async def test_switch_off_answers_have_no_runner_name(monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed_identities()
    central = await _seed_sensor(name="central", runner_name=None)
    bound = await _seed_sensor(name="bound", runner_name="runner-a")

    def dumped(sensor: Sensor) -> dict[str, Any]:
        return SensorRead.model_validate(sensor, from_attributes=True).model_dump(mode="json")

    assert "runner_name" not in dumped(await _sensor(central))
    assert "runner_name" not in dumped(await _sensor(bound))
    paused = await pause_tools._status_handler("pause", pause_tools.SensorStatus.PAUSED)(
        _admin(), {"sensor_id": str(central)}
    )
    resumed = await pause_tools._status_handler("resume", pause_tools.SensorStatus.ACTIVE)(
        _admin(), {"sensor_id": str(central)}
    )
    assert "runner_name" not in paused
    assert "runner_name" not in resumed
    member = DashboardMemberView.model_fields["runner_name"]
    assert member.default is None

    monkeypatch.setenv("RUNNER_CHECKS_ENABLED", "true")
    get_settings.cache_clear()
    assert dumped(await _sensor(bound))["runner_name"] == "runner-a"
    assert "runner_name" not in dumped(await _sensor(central))


# ---------------------------------------------------------------------------
# Secret cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_answers_and_errors_get_the_secret_cleanup(
    client: TestClient, switch_on: None
) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Sensor)
            .where(Sensor.id == sensor_id)
            .values(
                assertion={
                    "select": {"path": "$.password"},
                    "compare": {"type": "equals", "value": "expected"},
                }
            )
        )
        await session.commit()

    _post(client, _RUNNER_A_ID, "runner-a", _result(sensor_id, payload={"password": "hunter2xyz"}))
    answer = await _sensor(sensor_id)
    assert "hunter2xyz" not in str(answer.last_evidence)
    assert "hunter2xyz" not in str(answer.last_value)

    failed = _result(sensor_id, status="error", payload={})
    failed["result"] = None
    failed["error"] = 'LoginError: body {"password": "hunter2xyz"} was refused'
    _post(client, _RUNNER_A_ID, "runner-a", failed)
    error_row = await _sensor(sensor_id)
    assert error_row.last_state == "unknown"
    assert "hunter2xyz" not in str(error_row.last_evidence)

    async with get_sessionmaker()() as session:
        stored = (await session.execute(select(RunnerCheckResult))).scalars().all()
    assert len(stored) == 2
    assert all("hunter2xyz" not in f"{row.result_payload} {row.error}" for row in stored)


# ---------------------------------------------------------------------------
# Runner time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_slightly_future_time_is_refused_and_named_in_a_mixed_batch(
    client: TestClient, switch_on: None
) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    soon = datetime.now(UTC) + timedelta(seconds=5)

    resp = _post(
        client,
        _RUNNER_A_ID,
        "runner-a",
        _result(sensor_id, uid="ahead", payload={"count": 1}, produced_at=soon),
        _result(sensor_id, uid="now", payload={"count": 50}),
    )

    assert resp.json() == {
        "accepted": 1,
        "duplicates": 0,
        "refused": [{"result_uid": "ahead", "reason": "future_time"}],
    }
    assert await _stored_count() == 1
    assert (await _sensor(sensor_id)).last_state == "critical"


@pytest.mark.asyncio
async def test_a_replay_after_the_clean_up_does_not_change_the_check(
    client: TestClient, switch_on: None
) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    made = datetime.now(UTC) - timedelta(minutes=10)
    first = _result(sensor_id, uid="r-1", payload={"count": 50}, produced_at=made)
    newer = _result(sensor_id, uid="r-2", payload={"count": 1})
    _post(client, _RUNNER_A_ID, "runner-a", first, newer)
    assert (await _sensor(sensor_id)).last_state == "ok"

    async with get_sessionmaker()() as session:  # what the clean-up job does
        await session.execute(delete(RunnerCheckResult))
        await session.commit()
    before = _outcome("old")

    replay = _post(client, _RUNNER_A_ID, "runner-a", first)

    assert replay.json() == {"accepted": 1, "duplicates": 0}  # stored again, not counted
    assert (await _sensor(sensor_id)).last_state == "ok"
    assert _outcome("old") - before == 1


@pytest.mark.asyncio
async def test_a_result_read_after_a_newer_one_committed_is_dropped(switch_on: None) -> None:
    """The check of the runner time reads the stored time again, not a cached copy."""
    await _seed_identities()
    sensor_id = await _seed_sensor()
    now = datetime.now(UTC)
    async with get_sessionmaker()() as first:
        cached = await first.get(Sensor, sensor_id)
        assert cached is not None and cached.runner_produced_at is None
        async with get_sessionmaker()() as second:
            row = await second.get(Sensor, sensor_id)
            assert row is not None
            row.runner_produced_at = now
            await second.commit()
        item = wire.RunnerResult(
            result_uid="late",
            check_ref=str(sensor_id),
            op_id=_OP_SAFE,
            status="ok",
            result={"count": 50},
            produced_at=now - timedelta(seconds=1),
        )
        counted = await runner_checks._count_one(
            first,
            tenant_id=_TENANT,
            runner_name="runner-a",
            sensor=await runner_checks._runner_sensor(
                first, tenant_id=_TENANT, runner_name="runner-a", item=item
            ),
            item=item,
            too_large=False,
            received_at=now,
            evaluated_at=now,
        )
    assert counted is None
    assert (await _sensor(sensor_id)).last_state == "ok"


@pytest.mark.asyncio
async def test_the_runner_row_lock_is_a_row_lock_on_postgres() -> None:
    statements: list[Any] = []

    class _Recorder:
        async def execute(self, statement: Any) -> None:
            statements.append(statement)

    await lock_assignment_row(_Recorder(), tenant_id=_TENANT, runner_name="runner-a")  # type: ignore[arg-type]
    sql = str(statements[0].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql
    assert "runner_assignments" in sql


# ---------------------------------------------------------------------------
# Mails per reading, and refused logins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_post_with_a_critical_reading_and_a_recovery_sends_both_mails(
    client: TestClient, switch_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    await _seed_dashboard(sensor_id, memo="ok")
    investigations: list[dict[str, Any]] = []
    notices: list[Any] = []
    monkeypatch.setattr(inv, "_schedule_investigation", lambda **kw: investigations.append(kw))
    monkeypatch.setattr(inv, "schedule_dashboard_notification", notices.append)
    now = datetime.now(UTC)

    resp = _post(
        client,
        _RUNNER_A_ID,
        "runner-a",
        _result(sensor_id, payload={"count": 50}, produced_at=now - timedelta(seconds=2)),
        _result(sensor_id, payload={"count": 1}, produced_at=now - timedelta(seconds=1)),
    )

    assert resp.json() == {"accepted": 2, "duplicates": 0}
    assert (await _sensor(sensor_id)).last_state == "ok"
    assert len(investigations) == 1  # the warning starts the investigator
    assert len(notices) == 2  # the warning mail and the recovery mail


@pytest.mark.asyncio
async def test_refused_logins_on_the_runner_pause_the_check(
    client: TestClient, switch_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    parked: list[Any] = []
    monkeypatch.setattr(runner_checks, "schedule_sensor_parked_notification", parked.append)

    def refused_login() -> dict[str, Any]:
        body = _result(sensor_id, status="error")
        body.update(result=None, error="ConnectorAuthError: 401", error_code=AUTH_FAILED_ERROR_CODE)
        return body

    for _ in range(get_settings().sensor_auth_failure_park_threshold):
        _post(client, _RUNNER_A_ID, "runner-a", refused_login())

    row = await _sensor(sensor_id)
    assert row.status == "paused"
    assert row.status_reason == AUTH_PARK_REASON
    assert len(parked) == 1
    assert await _assignment_refs() == []


# ---------------------------------------------------------------------------
# Dead-man sweeper and recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_recovery_during_the_sweep_is_not_overwritten(
    switch_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    await _add_assignment_row()
    await _set_last_seen(datetime.now(UTC) - timedelta(hours=1))
    real_lock = deadman.lock_assignment_row

    async def recovery_then_lock(session: Any, **kwargs: Any) -> None:
        # The runner reports after the sweep picked it, before the lock.
        await _set_last_seen(datetime.now(UTC))
        await real_lock(session, **kwargs)

    monkeypatch.setattr(deadman, "lock_assignment_row", recovery_then_lock)

    await deadman._run_one_tick()

    assert await _stale_at() is None
    assert (await _sensor(sensor_id)).last_state == "ok"


@pytest.mark.asyncio
async def test_a_failed_marking_leaves_the_runner_for_the_next_sweep(
    switch_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    await _add_assignment_row()
    await _set_last_seen(datetime.now(UTC) - timedelta(hours=1))

    async def broken(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise RuntimeError("database went away")

    with monkeypatch.context() as patch:
        patch.setattr(deadman, "mark_runner_sensors_unknown", broken)
        await deadman._run_one_tick()
    assert await _stale_at() is None
    assert (await _sensor(sensor_id)).last_state == "ok"

    await deadman._run_one_tick()

    assert await _stale_at() is not None
    assert (await _sensor(sensor_id)).last_state == "unknown"


# ---------------------------------------------------------------------------
# Clean-up with the switch off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_up_keeps_every_raw_row_while_the_switch_is_off() -> None:
    from meho_backplane.checks.evidence_retention import _run_one_prune_tick

    await _seed_identities()
    async with get_sessionmaker()() as session:
        session.add(
            RunnerCheckResult(
                tenant_id=_TENANT,
                runner_name="runner-a",
                result_uid="old",
                check_ref="c",
                op_id=_OP_SAFE,
                status="ok",
                received_at=datetime.now(UTC) - timedelta(days=30),
            )
        )
        await session.commit()

    await _run_one_prune_tick()

    assert await _stored_count() == 1


# ---------------------------------------------------------------------------
# Schedule on the work item
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sensor_items_carry_the_check_schedule(switch_on: None) -> None:
    await _seed_identities()
    await _seed_target()
    await _seed_descriptor(op_id=_OP_SAFE)
    _register_connector()
    every_minute = await _seed_sensor(name="interval")
    nightly = await _seed_sensor(name="cron")
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Sensor)
            .where(Sensor.id == nightly)
            .values(
                cadence_kind="cron",
                cron_expr="0 2 * * *",
                interval_seconds=None,
                timezone="Europe/Vienna",
            )
        )
        await session.commit()
    async with get_sessionmaker()() as session:
        assignment = await materialize_assignment(
            session,
            tenant_id=_TENANT,
            runner_name="runner-a",
            principal=_principal(),
            runner_package_set=wire_empty_digest(),
            runner_features=frozenset({wire.FEATURE_SCHEDULED_CHECKS}),
        )
    items = {item.check_ref: item for item in assignment.items}
    assert items[str(every_minute)].interval_seconds == 60
    assert items[str(every_minute)].cron_expr is None
    assert items[str(nightly)].cron_expr == "0 2 * * *"
    assert items[str(nightly)].timezone == "Europe/Vienna"
    assert items[str(nightly)].interval_seconds is None
