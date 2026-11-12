# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pause and resume a Sensor, and the pause after refused logins.

What these tests check:

* ``PATCH /api/v1/sensors/{id}`` with ``status`` pauses and resumes a Sensor.
  The row, the result history and the Dashboard membership stay.
* A paused Sensor is not run. Pause and resume are safe to send twice.
* ``status`` cannot be mixed with other fields; the operator role is refused;
  another tenant gets 404.
* After N refused logins in a row the runner pauses the Sensor and stops
  calling the target. Any other result sets the count back to 0.
* The pause writes a ``check.sensor.parked`` event when events are on.
* The MCP tools do the same as the route.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.checks.auth_park import AUTH_PARK_REASON
from meho_backplane.checks.runner import run_one_sensor_tick
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EventOutbox, Sensor, SensorResult
from meho_backplane.main import app
from meho_backplane.mcp.tools import sensor_pause as mcp_sensors
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._vault_fakes import install_fake_vault
from .test_checks_edit import (
    _TENANT_A,
    _TENANT_B,
    _headers,
    _seed_sensor,
    _sensor_audit,
    _setup,
)
from .test_checks_edit import _settings_env as _edit_settings_env
from .test_sensor_runner import (
    _create_interval_sensor,
    _drain_in_flight,
    _force_due,
    _get_sensor,
    _ok_result,
)
from .test_sensor_runner import _settings_env as _runner_settings_env

_ = (_edit_settings_env, _runner_settings_env)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SENSOR_RUNNER_ENABLED", "false")
    monkeypatch.setenv("SENSOR_AUTH_FAILURE_PARK_THRESHOLD", "3")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


def _auth_failed_result() -> OperationResult:
    return OperationResult(
        status="error",
        op_id="vmware.vm.list",
        error="connector_auth_failed: the login was refused",
        duration_ms=1.0,
        extras={"error_code": "connector_auth_failed"},
    )


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_and_resume_keep_row_history_and_dashboard(client: TestClient) -> None:
    await _setup()
    sensor_id = await _seed_sensor(name="vm-count")
    key = make_rsa_keypair("kid-pause")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = _headers(key)
        dash = client.post(
            "/api/v1/checks/dashboards",
            json={"name": "estate", "sensor_ids": [str(sensor_id)]},
            headers=headers,
        )
        assert dash.status_code == 201, dash.text

        paused = client.patch(
            f"/api/v1/sensors/{sensor_id}", json={"status": "paused"}, headers=headers
        )
        assert paused.status_code == 200, paused.text
        assert paused.json()["id"] == str(sensor_id)
        assert paused.json()["status"] == "paused"
        assert paused.json()["status_reason"] == "paused_by_operator"

        # Paused: the row, the history and the dashboard place are all still there.
        results = client.get(f"/api/v1/sensors/{sensor_id}/results", headers=headers)
        assert len(results.json()["items"]) == 1
        shown = client.get(f"/api/v1/checks/dashboards/{dash.json()['id']}", headers=headers)
        assert [m["sensor_id"] for m in shown.json()["members"]] == [str(sensor_id)]

        resumed = client.patch(
            f"/api/v1/sensors/{sensor_id}", json={"status": "active"}, headers=headers
        )
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["status"] == "active"
        assert resumed.json()["status_reason"] is None

        results = client.get(f"/api/v1/sensors/{sensor_id}/results", headers=headers)
        assert len(results.json()["items"]) == 1
        shown = client.get(f"/api/v1/checks/dashboards/{dash.json()['id']}", headers=headers)
        assert [m["sensor_id"] for m in shown.json()["members"]] == [str(sensor_id)]

    payloads = await _sensor_audit(sensor_id)
    assert payloads[0]["before"]["status"] == "active"
    assert payloads[0]["after"]["status"] == "paused"


@pytest.mark.asyncio
async def test_pause_and_resume_are_safe_to_send_twice(client: TestClient) -> None:
    await _setup()
    sensor_id = await _seed_sensor(name="twice")
    key = make_rsa_keypair("kid-twice")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = _headers(key)
        url = f"/api/v1/sensors/{sensor_id}"
        for _ in range(2):
            assert client.patch(url, json={"status": "paused"}, headers=headers).status_code == 200
        assert client.get(url + "/results", headers=headers).status_code == 200
        for _ in range(2):
            assert client.patch(url, json={"status": "active"}, headers=headers).status_code == 200
    row = await _get_sensor(sensor_id)
    assert row.status == "active"
    assert row.status_reason is None


@pytest.mark.asyncio
async def test_paused_sensor_is_not_run(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    calls: list[int] = []

    async def _dispatch(**_kwargs: Any) -> OperationResult:
        calls.append(1)
        return _ok_result({"count": 1})

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _dispatch)
    sensor_id = await _create_interval_sensor()
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))
    key = make_rsa_keypair("kid-skip")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.patch(
            f"/api/v1/sensors/{sensor_id}",
            json={"status": "paused"},
            headers=_headers(key, tenant_id=uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")),
        )
    assert resp.status_code == 200, resp.text
    await run_one_sensor_tick()
    await _drain_in_flight()
    assert calls == []


@pytest.mark.asyncio
async def test_status_bad_cases(client: TestClient) -> None:
    await _setup()
    sensor_id = await _seed_sensor(name="bad")
    other = await _seed_sensor(name="other", tenant_id=_TENANT_B)
    key = make_rsa_keypair("kid-bad")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        admin = _headers(key)
        url = f"/api/v1/sensors/{sensor_id}"
        assert client.patch(url, json={"status": "gone"}, headers=admin).status_code == 422
        mixed = client.patch(url, json={"status": "paused", "name": "x"}, headers=admin)
        assert mixed.status_code == 422
        operator = _headers(key, role=TenantRole.OPERATOR)
        assert client.patch(url, json={"status": "paused"}, headers=operator).status_code == 403
        cross = client.patch(f"/api/v1/sensors/{other}", json={"status": "paused"}, headers=admin)
        assert cross.status_code == 404
        missing = client.patch(
            f"/api/v1/sensors/{uuid.uuid4()}", json={"status": "paused"}, headers=admin
        )
        assert missing.status_code == 404
    assert (await _get_sensor(sensor_id)).status == "active"
    assert (await _get_sensor(other)).status == "active"


@pytest.mark.asyncio
async def test_edit_keeps_a_paused_sensor_paused(client: TestClient) -> None:
    await _setup()
    sensor_id = await _seed_sensor(name="stay")
    key = make_rsa_keypair("kid-stay")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = _headers(key)
        url = f"/api/v1/sensors/{sensor_id}"
        assert client.patch(url, json={"status": "paused"}, headers=headers).status_code == 200
        edited = client.patch(url, json={"interval_seconds": 120}, headers=headers)
    assert edited.status_code == 200, edited.text
    assert edited.json()["status"] == "paused"
    assert edited.json()["status_reason"] == "paused_by_operator"


# ---------------------------------------------------------------------------
# Pause after refused logins
# ---------------------------------------------------------------------------


async def _tick(sensor_id: uuid.UUID) -> Sensor:
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))
    await run_one_sensor_tick()
    await _drain_in_flight()
    return await _get_sensor(sensor_id)


@pytest.mark.asyncio
async def test_refused_logins_in_a_row_pause_the_sensor_and_stop_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    async def _dispatch(**_kwargs: Any) -> OperationResult:
        calls.append(1)
        return _auth_failed_result()

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _dispatch)
    sensor_id = await _create_interval_sensor()

    row = await _tick(sensor_id)
    assert (row.status, row.auth_failure_count) == ("active", 1)
    row = await _tick(sensor_id)
    assert (row.status, row.auth_failure_count) == ("active", 2)
    row = await _tick(sensor_id)
    assert row.status == "paused"
    assert row.status_reason == AUTH_PARK_REASON
    assert row.last_evidence is not None
    assert row.last_evidence["error_code"] == "connector_auth_failed"
    assert len(calls) == 3

    # Paused: later ticks make no call to the target.
    await _tick(sensor_id)
    await _tick(sensor_id)
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_other_results_set_the_count_back_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    results = [_auth_failed_result(), _auth_failed_result(), _ok_result({"count": 1})]

    async def _dispatch(**_kwargs: Any) -> OperationResult:
        return results.pop(0)

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _dispatch)
    sensor_id = await _create_interval_sensor()
    assert (await _tick(sensor_id)).auth_failure_count == 1
    assert (await _tick(sensor_id)).auth_failure_count == 2
    row = await _tick(sensor_id)
    assert (row.status, row.auth_failure_count) == ("active", 0)


@pytest.mark.asyncio
async def test_a_failure_that_is_not_a_refused_login_is_not_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _dispatch(**_kwargs: Any) -> OperationResult:
        return OperationResult(status="error", op_id="x", error="timeout", duration_ms=1.0)

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _dispatch)
    sensor_id = await _create_interval_sensor()
    for _ in range(4):
        row = await _tick(sensor_id)
    assert (row.status, row.auth_failure_count) == ("active", 0)


@pytest.mark.asyncio
async def test_resume_after_auth_pause_runs_again_and_resets_count(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    monkeypatch.setenv("SENSOR_AUTH_FAILURE_PARK_THRESHOLD", "1")
    get_settings.cache_clear()
    calls: list[int] = []

    async def _dispatch(**_kwargs: Any) -> OperationResult:
        calls.append(1)
        return _auth_failed_result()

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _dispatch)
    sensor_id = await _create_interval_sensor()
    assert (await _tick(sensor_id)).status == "paused"

    key = make_rsa_keypair("kid-resume")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.patch(
            f"/api/v1/sensors/{sensor_id}",
            json={"status": "active"},
            headers=_headers(key, tenant_id=uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")),
        )
    assert resp.status_code == 200, resp.text
    assert (await _get_sensor(sensor_id)).auth_failure_count == 0
    await run_one_sensor_tick()
    await _drain_in_flight()
    # The credential is still stale: it ran once and paused again (the operator's choice).
    assert len(calls) == 2
    assert (await _get_sensor(sensor_id)).status == "paused"


@pytest.mark.asyncio
async def test_pause_after_refused_logins_writes_an_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECKS_AGENT_EVENTS", "true")
    monkeypatch.setenv("SENSOR_AUTH_FAILURE_PARK_THRESHOLD", "1")
    get_settings.cache_clear()

    async def _dispatch(**_kwargs: Any) -> OperationResult:
        return _auth_failed_result()

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _dispatch)
    sensor_id = await _create_interval_sensor()
    await _tick(sensor_id)
    async with get_sessionmaker()() as session:
        rows = (
            await session.scalars(
                select(EventOutbox).where(EventOutbox.event_kind == "check.sensor.parked")
            )
        ).all()
    assert len(rows) == 1
    payload = dict(rows[0].payload)
    assert payload["reason"] == "target_credential_stale"
    assert payload["source"]["id"] == str(sensor_id)


@pytest.mark.asyncio
async def test_pause_after_refused_logins_writes_no_event_when_events_are_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SENSOR_AUTH_FAILURE_PARK_THRESHOLD", "1")
    get_settings.cache_clear()

    async def _dispatch(**_kwargs: Any) -> OperationResult:
        return _auth_failed_result()

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _dispatch)
    sensor_id = await _create_interval_sensor()
    assert (await _tick(sensor_id)).status == "paused"
    async with get_sessionmaker()() as session:
        rows = (await session.scalars(select(EventOutbox))).all()
    assert rows == []


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


def _admin(tenant_id: uuid.UUID) -> Operator:
    return Operator(
        sub="op-admin",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
        raw_jwt="",
    )


@pytest.mark.asyncio
async def test_mcp_pause_resume_and_not_found() -> None:
    from meho_backplane.mcp.server import McpInvalidParamsError

    await _setup()
    sensor_id = await _seed_sensor(name="mcp")
    other = await _seed_sensor(name="mcp-other", tenant_id=_TENANT_B)
    args = {"sensor_id": str(sensor_id)}
    paused = await mcp_sensors._status_handler("pause", mcp_sensors.SensorStatus.PAUSED)(
        _admin(_TENANT_A), args
    )
    assert paused["status"] == "paused"
    again = await mcp_sensors._status_handler("pause", mcp_sensors.SensorStatus.PAUSED)(
        _admin(_TENANT_A), args
    )
    assert again["status"] == "paused"
    resumed = await mcp_sensors._status_handler("resume", mcp_sensors.SensorStatus.ACTIVE)(
        _admin(_TENANT_A), args
    )
    assert resumed["status"] == "active"
    with pytest.raises(McpInvalidParamsError, match="sensor_not_found"):
        await mcp_sensors._status_handler("pause", mcp_sensors.SensorStatus.PAUSED)(
            _admin(_TENANT_A), {"sensor_id": str(other)}
        )
    async with get_sessionmaker()() as session:
        history = (
            await session.scalars(select(SensorResult).where(SensorResult.sensor_id == sensor_id))
        ).all()
    assert len(history) == 1
