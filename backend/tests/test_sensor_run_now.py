# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``POST /api/v1/sensors/{id}/run`` makes a sensor due now.

What these tests check:

* After the call, the next runner tick runs the sensor and stores a result.
* The operator role gets 403, a paused sensor gets 409, an unknown id and
  the id of another tenant get 404.
* The call writes one audit row with op id ``sensor.run``.
* A sensor that is already due keeps its due time, and the call succeeds.
* A sensor due far ahead is pulled to now.
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

from meho_backplane.auth.operator import TenantRole
from meho_backplane.checks.runner import run_one_sensor_tick
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, SensorResult
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._vault_fakes import install_fake_vault
from .test_checks_edit import _TENANT_A, _TENANT_B, _headers, _seed_sensor, _setup
from .test_checks_edit import _settings_env as _edit_settings_env
from .test_sensor_runner import _drain_in_flight, _force_due, _get_sensor, _ok_result
from .test_sensor_runner import _settings_env as _runner_settings_env

_ = (_edit_settings_env, _runner_settings_env)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SENSOR_RUNNER_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


async def _result_count(sensor_id: uuid.UUID) -> int:
    async with get_sessionmaker()() as session:
        rows = (
            await session.scalars(select(SensorResult).where(SensorResult.sensor_id == sensor_id))
        ).all()
    return len(rows)


@pytest.mark.asyncio
async def test_run_makes_the_next_tick_run_the_sensor(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    calls: list[int] = []

    async def _dispatch(**_kwargs: Any) -> Any:
        calls.append(1)
        return _ok_result({"count": 1})

    monkeypatch.setattr("meho_backplane.checks.runner.dispatch", _dispatch)
    await _setup()
    sensor_id = await _seed_sensor(name="later")
    await _force_due(sensor_id, datetime.now(UTC) + timedelta(hours=1))
    before = await _result_count(sensor_id)

    # Not due yet: a tick runs nothing.
    assert await run_one_sensor_tick() == 0
    key = make_rsa_keypair("kid-run")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(f"/api/v1/sensors/{sensor_id}/run", headers=_headers(key))
    assert resp.status_code == 202, resp.text
    assert resp.json()["sensor_id"] == str(sensor_id)
    assert _aware(datetime.fromisoformat(resp.json()["next_fire_at"])) <= datetime.now(UTC)

    assert await run_one_sensor_tick() == 1
    await _drain_in_flight()
    assert calls == [1]
    assert await _result_count(sensor_id) == before + 1


@pytest.mark.asyncio
async def test_run_bad_cases(client: TestClient) -> None:
    await _setup()
    paused = await _seed_sensor(name="paused", status="paused")
    other = await _seed_sensor(name="other", tenant_id=_TENANT_B)
    active = await _seed_sensor(name="active")
    paused_due = (await _get_sensor(paused)).next_fire_at
    key = make_rsa_keypair("kid-run-bad")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        admin = _headers(key)
        operator = _headers(key, role=TenantRole.OPERATOR)

        assert client.post(f"/api/v1/sensors/{active}/run", headers=operator).status_code == 403
        refused = client.post(f"/api/v1/sensors/{paused}/run", headers=admin)
        assert refused.status_code == 409
        assert "paused" in refused.json()["detail"]
        assert f"meho sensor resume {paused}" in refused.json()["detail"]
        assert client.post(f"/api/v1/sensors/{uuid.uuid4()}/run", headers=admin).status_code == 404
        # The id of another tenant is not leaked: 404, and nothing changes.
        other_due = (await _get_sensor(other)).next_fire_at
        assert client.post(f"/api/v1/sensors/{other}/run", headers=admin).status_code == 404
    assert (await _get_sensor(other)).next_fire_at == other_due
    assert (await _get_sensor(paused)).next_fire_at == paused_due
    assert (await _get_sensor(paused)).status == "paused"
    assert _TENANT_A != _TENANT_B


@pytest.mark.asyncio
async def test_run_writes_one_audit_row(client: TestClient) -> None:
    await _setup()
    sensor_id = await _seed_sensor(name="audited")
    key = make_rsa_keypair("kid-run-audit")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(f"/api/v1/sensors/{sensor_id}/run", headers=_headers(key))
    assert resp.status_code == 202, resp.text
    async with get_sessionmaker()() as session:
        rows = (
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.path == f"/api/v1/sensors/{sensor_id}/run",
                    AuditLog.method == "POST",
                )
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].payload["op_id"] == "sensor.run"
    assert rows[0].payload["op_class"] == "write"


@pytest.mark.asyncio
async def test_run_on_a_due_sensor_keeps_its_due_time(client: TestClient) -> None:
    await _setup()
    sensor_id = await _seed_sensor(name="due")
    due = datetime.now(UTC) - timedelta(minutes=5)
    await _force_due(sensor_id, due)
    key = make_rsa_keypair("kid-run-due")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = _headers(key)
        for _ in range(2):
            resp = client.post(f"/api/v1/sensors/{sensor_id}/run", headers=headers)
            assert resp.status_code == 202, resp.text
    assert _aware((await _get_sensor(sensor_id)).next_fire_at) == due
    assert _aware(datetime.fromisoformat(resp.json()["next_fire_at"])) == due
