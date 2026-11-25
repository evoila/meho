# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A check that a runner does not get shows ``unknown``; refused results and the clock.

With ``RUNNER_CHECKS_ENABLED`` on:

* a bound check that MEHO leaves off the runner's list gets a stored
  ``unknown`` with the reason, once for each change of reason, and one notice,
* a check that is resumed while its runner is stale reads ``unknown``,
* a result up to 30 seconds ahead of MEHO's clock counts; one further ahead
  is refused and, when the whole batch is refused, answered with a 422,
* a refused result is counted once however often its batch is sent,
* a database commit that fails once changes and sends nothing.

The helpers come from :mod:`tests.test_runner_checks_count`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.checks.investigate as inv
import meho_backplane.gateway.assignment_service as assignment_service
import meho_backplane.gateway.runner_checks as runner_checks
from meho_backplane.checks.sensor_pause import set_sensor_status
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    EndpointDescriptor,
    RunnerAssignmentRow,
    SensorStatus,
    Target,
)
from meho_backplane.gateway.deadman import _run_one_tick
from meho_backplane.runner import wire
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
    _build_app,
    _register_connector,
    _runner_token,
    _seed_descriptor,
    _seed_identities,
    _seed_target,
)
from .test_runner_checks_count import (
    _KEY,
    _outcome,
    _post,
    _result,
    _seed_dashboard,
    _seed_sensor,
    _sensor,
    _stored_count,
)

_FEATURE = wire.FEATURE_SCHEDULED_CHECKS


@pytest.fixture
def switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNNER_CHECKS_ENABLED", "true")
    get_settings.cache_clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


@pytest.fixture
def notices(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    sent: list[Any] = []
    monkeypatch.setattr(inv, "_schedule_investigation", lambda **_kw: None)
    monkeypatch.setattr(inv, "schedule_dashboard_notification", sent.append)
    return sent


def _get_assignment(client: TestClient, *, features: str | None = _FEATURE) -> Any:
    headers = {"Authorization": f"Bearer {_runner_token(_KEY, runner_id=_RUNNER_A_ID)}"}
    if features is not None:
        headers[wire.FEATURES_HEADER] = features
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(_KEY))
        return client.get("/api/v1/checks/assignment?runner=runner-a", headers=headers)


async def _seed_runnable_world() -> None:
    await _seed_identities()
    await _seed_target()
    await _seed_descriptor(op_id=_OP_SAFE)
    _register_connector()


async def _evidence(sensor_id: uuid.UUID) -> dict[str, Any]:
    row = await _sensor(sensor_id)
    assert row.last_state == "unknown"
    assert row.last_evidence is not None
    return dict(row.last_evidence)


# ---------------------------------------------------------------------------
# Point: a bound check that the runner does not get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_without_the_feature_word_gets_unknown_and_one_notice(
    client: TestClient, switch_on: None, notices: list[Any]
) -> None:
    await _seed_runnable_world()
    sensor_id = await _seed_sensor()
    await _seed_dashboard(sensor_id, memo="ok")

    assert _get_assignment(client, features=None).json()["items"] == []
    evidence = await _evidence(sensor_id)
    assert evidence["reason"] == "not_on_runner_list"
    assert evidence["why"] == "runner_cannot_schedule"
    assert len(notices) == 1

    # The next poll has the same reason: nothing is written or sent again.
    _get_assignment(client, features=None)
    assert len(notices) == 1


@pytest.mark.asyncio
async def test_a_change_of_reason_is_recorded_again(
    client: TestClient, switch_on: None, notices: list[Any]
) -> None:
    await _seed_runnable_world()
    sensor_id = await _seed_sensor()
    await _seed_dashboard(sensor_id, memo="ok")
    _get_assignment(client, features=None)
    async with get_sessionmaker()() as session:
        await session.execute(update(Target).values(deleted_at=datetime.now(UTC)))
        await session.commit()

    _get_assignment(client)

    assert (await _evidence(sensor_id))["why"] == "target_unresolved"
    assert len(notices) == 1  # the Dashboard state is the same: no second notice


@pytest.mark.asyncio
async def test_approval_rule_set_after_create_gives_unknown(
    client: TestClient, switch_on: None, notices: list[Any]
) -> None:
    await _seed_runnable_world()
    sensor_id = await _seed_sensor()
    await _seed_dashboard(sensor_id, memo="ok")
    async with get_sessionmaker()() as session:
        await session.execute(update(EndpointDescriptor).values(requires_approval=True))
        await session.commit()

    assert _get_assignment(client).json()["items"] == []

    assert (await _evidence(sensor_id))["why"] == "op_requires_approval"
    assert len(notices) == 1


@pytest.mark.asyncio
async def test_target_that_is_not_found_gives_unknown(
    client: TestClient, switch_on: None, notices: list[Any]
) -> None:
    await _seed_identities()
    await _seed_descriptor(op_id=_OP_SAFE)
    _register_connector()
    sensor_id = await _seed_sensor()
    await _seed_dashboard(sensor_id, memo="ok")

    assert _get_assignment(client).json()["items"] == []

    assert (await _evidence(sensor_id))["why"] == "target_unresolved"
    assert len(notices) == 1


@pytest.mark.asyncio
async def test_a_different_package_set_gives_unknown(
    client: TestClient,
    switch_on: None,
    notices: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_runnable_world()
    sensor_id = await _seed_sensor()
    await _seed_dashboard(sensor_id, memo="ok")
    monkeypatch.setattr(assignment_service, "_package_set_matches", lambda _item, _digest: False)

    assert _get_assignment(client).json()["items"] == []

    assert (await _evidence(sensor_id))["why"] == "connector_package_set_mismatch"
    assert len(notices) == 1


@pytest.mark.asyncio
async def test_a_dashboard_whose_checks_all_run_on_the_runner_gets_the_notice(
    client: TestClient, switch_on: None, notices: list[Any]
) -> None:
    await _seed_runnable_world()
    first = await _seed_sensor(name="one")
    second = await _seed_sensor(name="two")
    await _seed_dashboard(first, memo="ok")
    async with get_sessionmaker()() as session:
        from sqlalchemy import select

        from meho_backplane.db.models import CheckDashboardSensor

        dashboard_id = (await session.execute(select(CheckDashboardSensor))).scalars().one()
        session.add(CheckDashboardSensor(dashboard_id=dashboard_id.dashboard_id, sensor_id=second))
        await session.commit()

    _get_assignment(client, features=None)

    assert notices
    assert notices[-1].current_state != "ok"


@pytest.mark.asyncio
async def test_switch_off_leaves_the_states_alone(client: TestClient) -> None:
    await _seed_runnable_world()
    sensor_id = await _seed_sensor()

    _get_assignment(client, features=None)

    assert (await _sensor(sensor_id)).last_state == "ok"


@pytest.mark.asyncio
async def test_a_check_resumed_while_its_runner_is_stale_reads_unknown(
    switch_on: None, notices: list[Any]
) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor(status="paused")
    await _seed_dashboard(sensor_id, memo="ok")
    async with get_sessionmaker()() as session:
        session.add(
            RunnerAssignmentRow(
                tenant_id=uuid.UUID("00000000-0000-0000-0000-00000000a0a0"),
                runner_name="runner-a",
                items=[],
                stale_at=datetime.now(UTC),
            )
        )
        await session.commit()

    await set_sensor_status(
        uuid.UUID("00000000-0000-0000-0000-00000000a0a0"), sensor_id, SensorStatus.ACTIVE
    )

    assert (await _evidence(sensor_id))["reason"] == "runner_stale"
    assert len(notices) == 1


@pytest.mark.asyncio
async def test_a_resumed_check_of_a_live_runner_keeps_its_state(switch_on: None) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor(status="paused")
    await set_sensor_status(
        uuid.UUID("00000000-0000-0000-0000-00000000a0a0"), sensor_id, SensorStatus.ACTIVE
    )
    assert (await _sensor(sensor_id)).last_state == "ok"


@pytest.mark.asyncio
async def test_dead_man_sweep_with_the_switch_off_marks_no_check() -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    async with get_sessionmaker()() as session:
        from meho_backplane.db.models import RunnerPrincipal

        session.add(
            RunnerAssignmentRow(
                tenant_id=uuid.UUID("00000000-0000-0000-0000-00000000a0a0"),
                runner_name="runner-a",
                items=[],
            )
        )
        await session.execute(
            update(RunnerPrincipal).values(last_seen_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await session.commit()

    await _run_one_tick()

    assert (await _sensor(sensor_id)).last_state == "ok"


# ---------------------------------------------------------------------------
# The runner's clock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_clock_a_few_seconds_ahead_loses_no_result(
    client: TestClient, switch_on: None
) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    ahead = datetime.now(UTC) + timedelta(seconds=10)

    resp = _post(
        client,
        _RUNNER_A_ID,
        "runner-a",
        _result(sensor_id, payload={"count": 50}, produced_at=ahead),
    )

    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1, "duplicates": 0}
    assert (await _sensor(sensor_id)).last_state == "critical"


@pytest.mark.asyncio
async def test_a_clock_far_ahead_is_refused_and_the_answer_says_so(
    client: TestClient, switch_on: None
) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    far = datetime.now(UTC) + timedelta(minutes=5)

    resp = _post(client, _RUNNER_A_ID, "runner-a", _result(sensor_id, uid="far", produced_at=far))

    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "results_refused"
    assert await _stored_count() == 0


@pytest.mark.asyncio
async def test_a_batch_with_one_good_and_one_refused_result_answers_200(
    client: TestClient, switch_on: None
) -> None:
    await _seed_identities()
    good = await _seed_sensor(name="good")
    bad = await _seed_sensor(name="bad")
    far = datetime.now(UTC) + timedelta(minutes=5)

    resp = _post(
        client,
        _RUNNER_A_ID,
        "runner-a",
        _result(good, payload={"count": 50}),
        _result(bad, uid="far", produced_at=far),
    )

    assert resp.status_code == 200
    assert resp.json()["refused"] == [{"result_uid": "far", "reason": "future_time"}]


# ---------------------------------------------------------------------------
# Counted once; a failed commit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_future_time_result_is_counted_once_when_its_batch_is_sent_again(
    switch_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    other = await _seed_sensor(name="other")
    far = datetime.now(UTC) + timedelta(minutes=5)
    real = runner_checks.ingest_new_results
    fail = {"left": 1}

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        if fail["left"]:
            fail["left"] -= 1
            raise RuntimeError("save failed")
        return await real(*args, **kwargs)

    monkeypatch.setattr(runner_checks, "ingest_new_results", flaky)
    client = TestClient(_build_app(), raise_server_exceptions=False)
    before = _outcome("future_time")
    results = (
        _result(sensor_id, uid="far", produced_at=far),
        _result(other, uid="ok", payload={"count": 1}),
    )

    first = _post(client, _RUNNER_A_ID, "runner-a", *results)
    assert first.status_code == 500
    assert _outcome("future_time") - before == 0

    second = _post(client, _RUNNER_A_ID, "runner-a", *results)
    assert second.status_code == 200
    assert _outcome("future_time") - before == 1


@pytest.mark.asyncio
async def test_a_failed_commit_counts_and_sends_nothing_and_the_retry_does_each_once(
    switch_on: None, notices: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_identities()
    sensor_id = await _seed_sensor()
    await _seed_dashboard(sensor_id, memo="ok")
    armed = {"on": False, "failed": False}
    real_lock = runner_checks.lock_assignment_row
    real_commit = AsyncSession.commit

    async def arm(*args: Any, **kwargs: Any) -> None:
        armed["on"] = True
        await real_lock(*args, **kwargs)

    async def commit(self: AsyncSession) -> None:
        if armed["on"] and not armed["failed"]:
            armed["failed"] = True
            raise RuntimeError("commit failed")
        await real_commit(self)

    monkeypatch.setattr(runner_checks, "lock_assignment_row", arm)
    monkeypatch.setattr(AsyncSession, "commit", commit)
    client = TestClient(_build_app(), raise_server_exceptions=False)
    counted_before = _outcome("counted")
    batch = _result(sensor_id, uid="one", payload={"count": 50})

    first = _post(client, _RUNNER_A_ID, "runner-a", batch)

    assert first.status_code == 500
    assert _outcome("counted") - counted_before == 0
    assert notices == []
    assert (await _sensor(sensor_id)).last_state == "ok"

    second = _post(client, _RUNNER_A_ID, "runner-a", batch)

    assert second.status_code == 200
    assert second.json() == {"accepted": 1, "duplicates": 0}
    assert _outcome("counted") - counted_before == 1
    assert len(notices) == 1
