# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Edit a Sensor or a Dashboard in place (``PATCH``).

What these tests check:

* A Sensor edit keeps the id, the result history and the Dashboard
  membership, makes the Sensor due at once, and writes the changed fields
  before and after into the audit row.
* A Dashboard edit adds and removes members and keeps the id and the mail
  settings it does not name.
* The bad cases: an invalid result, an unknown or unsafe operation, a name
  that is taken, another tenant, the operator role, an empty body.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

import meho_backplane.checks.dashboard_edit as dashboard_edit
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, Sensor, SensorResult, Tenant
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._vault_fakes import install_fake_vault

_TENANT_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

_OLD_CONNECTOR = "vmware-rest-9.0"
_NEW_CONNECTOR = "vmware-rest-9.1"
_SAFE_OP = "vmware.vm.list"

_ASSERTION: dict[str, Any] = {
    "select": {"path": "$.count"},
    "compare": {"type": "threshold", "op": "lt", "critical": 10},
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


def _headers(
    key: Any,
    *,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    tenant_id: UUID = _TENANT_A,
) -> dict[str, str]:
    token = mint_token(key, sub="op-admin", tenant_role=role.value, tenant_id=str(tenant_id))
    return {"Authorization": f"Bearer {token}"}


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


async def _seed_tenant(tenant_id: UUID) -> None:
    async with get_sessionmaker()() as session:
        if await session.get(Tenant, tenant_id) is None:
            slug = str(tenant_id)[:8]
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
            await session.commit()


async def _seed_descriptor(*, version: str, op_id: str = _SAFE_OP, safety: str = "safe") -> None:
    async with get_sessionmaker()() as session:
        session.add(
            EndpointDescriptor(
                product="vmware",
                version=version,
                impl_id="vmware-rest",
                op_id=op_id,
                source_kind="ingested",
                method="GET",
                path=f"/{op_id}",
                parameter_schema={"type": "object", "properties": {}},
                safety_level=safety,
            )
        )
        await session.commit()


async def _seed_sensor(
    *,
    name: str,
    tenant_id: UUID = _TENANT_A,
    status: str = "active",
    pending_state: str | None = None,
    params: dict[str, object] | None = None,
) -> UUID:
    sensor_id = uuid4()
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        session.add(
            Sensor(
                id=sensor_id,
                tenant_id=tenant_id,
                name=name,
                connector_id=_OLD_CONNECTOR,
                op_id=_SAFE_OP,
                target=None,
                params=params or {},
                assertion=_ASSERTION,
                status=status,
                status_reason="invalid_cadence:InvalidCronExpressionError"
                if status == "paused"
                else None,
                cadence_kind="interval",
                interval_seconds=300,
                cron_expr=None,
                timezone="UTC",
                next_fire_at=now + timedelta(hours=1),
                severity="critical",
                for_seconds=0,
                last_state="degraded",
                pending_state=pending_state,
                pending_count=1 if pending_state else 0,
                identity_sub="__sensor__",
                created_by_sub="op-admin",
            )
        )
        session.add(
            SensorResult(
                sensor_id=sensor_id,
                evaluated_at=now - timedelta(minutes=5),
                state="degraded",
                value=3,
                evidence={"observed": 3},
                reason=None,
            )
        )
        await session.commit()
    return sensor_id


async def _setup() -> None:
    await _seed_tenant(_TENANT_A)
    await _seed_tenant(_TENANT_B)
    await _seed_descriptor(version="9.0")
    await _seed_descriptor(version="9.1")


async def _audit_payloads(path: str) -> list[dict[str, Any]]:
    """The audit payloads of the PATCH calls on *path*, oldest first."""
    async with get_sessionmaker()() as session:
        rows = (
            await session.scalars(
                select(AuditLog)
                .where(AuditLog.path == path, AuditLog.method == "PATCH")
                .order_by(AuditLog.occurred_at)
            )
        ).all()
    return [dict(r.payload or {}) for r in rows]


async def _sensor_audit(sensor_id: UUID) -> list[dict[str, Any]]:
    return await _audit_payloads(f"/api/v1/sensors/{sensor_id}")


async def _dashboard_audit(dashboard_id: str) -> list[dict[str, Any]]:
    return await _audit_payloads(f"/api/v1/checks/dashboards/{dashboard_id}")


# ---------------------------------------------------------------------------
# Sensor edit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sensor_edit_connector_keeps_id_history_and_dashboard(client: TestClient) -> None:
    await _setup()
    sensor_id = await _seed_sensor(name="vm-count")
    key = make_rsa_keypair("kid-sensor-edit")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = _headers(key)
        dash = client.post(
            "/api/v1/checks/dashboards",
            json={"name": "estate", "sensor_ids": [str(sensor_id)]},
            headers=headers,
        )
        assert dash.status_code == 201, dash.text
        before_call = datetime.now(UTC)

        resp = client.patch(
            f"/api/v1/sensors/{sensor_id}",
            json={"connector_id": _NEW_CONNECTOR},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == str(sensor_id)
        assert body["connector_id"] == _NEW_CONNECTOR
        assert body["op_id"] == _SAFE_OP
        assert body["interval_seconds"] == 300
        # Due at once: the runner checks it on its next tick.
        due = datetime.fromisoformat(body["next_fire_at"])
        assert _naive(due) <= _naive(datetime.now(UTC))
        assert _naive(due) >= _naive(before_call) - timedelta(seconds=1)

        results = client.get(f"/api/v1/sensors/{sensor_id}/results", headers=headers)
        assert results.status_code == 200
        assert len(results.json()["items"]) == 1

        shown = client.get(f"/api/v1/checks/dashboards/{dash.json()['id']}", headers=headers)
        assert [m["sensor_id"] for m in shown.json()["members"]] == [str(sensor_id)]
        assert shown.json()["members"][0]["connector_id"] == _NEW_CONNECTOR

    payloads = await _sensor_audit(sensor_id)
    assert len(payloads) == 1
    assert payloads[0]["op_id"] == "sensor.edit"
    assert payloads[0]["before"] == {"connector_id": _OLD_CONNECTOR}
    assert payloads[0]["after"] == {"connector_id": _NEW_CONNECTOR}


@pytest.mark.asyncio
async def test_sensor_edit_switches_cadence_and_clears_old_field(client: TestClient) -> None:
    await _setup()
    sensor_id = await _seed_sensor(name="vm-count")
    key = make_rsa_keypair("kid-sensor-cron")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.patch(
            f"/api/v1/sensors/{sensor_id}",
            json={
                "cadence_kind": "cron",
                "cron_expr": "*/5 * * * *",
                "severity": "degraded",
                "name": "vm-count-cron",
                "params": {"folder": "<folder-id>"},
                "assertion": {
                    "select": {"path": "$.count"},
                    "compare": {"type": "threshold", "op": "lt", "critical": 20},
                },
            },
            headers=_headers(key),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cadence_kind"] == "cron"
    assert body["cron_expr"] == "*/5 * * * *"
    assert body["interval_seconds"] is None
    assert body["severity"] == "degraded"
    assert body["name"] == "vm-count-cron"
    assert body["params"] == {"folder": "<folder-id>"}
    payload = (await _sensor_audit(sensor_id))[0]
    assert payload["before"]["interval_seconds"] == 300
    assert payload["after"]["interval_seconds"] is None
    assert payload["after"]["cadence_kind"] == "cron"
    assert payload["before"]["assertion"]["compare"]["critical"] == 10
    assert payload["after"]["assertion"]["compare"]["critical"] == 20


@pytest.mark.asyncio
async def test_sensor_edit_reactivates_paused_and_drops_pending(client: TestClient) -> None:
    await _setup()
    sensor_id = await _seed_sensor(name="parked", status="paused", pending_state="critical")
    key = make_rsa_keypair("kid-sensor-paused")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.patch(
            f"/api/v1/sensors/{sensor_id}",
            json={"interval_seconds": 120},
            headers=_headers(key),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "active"
    assert body["status_reason"] is None
    assert body["pending_state"] is None
    assert body["pending_count"] == 0
    # The last committed state is kept until the next run.
    assert body["last_state"] == "degraded"


@pytest.mark.asyncio
async def test_sensor_edit_without_change_does_not_rerun(client: TestClient) -> None:
    await _setup()
    sensor_id = await _seed_sensor(name="same")
    key = make_rsa_keypair("kid-sensor-same")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.patch(
            f"/api/v1/sensors/{sensor_id}",
            json={"connector_id": _OLD_CONNECTOR, "interval_seconds": 300},
            headers=_headers(key),
        )
    assert resp.status_code == 200, resp.text
    due = datetime.fromisoformat(resp.json()["next_fire_at"])
    assert _naive(due) > _naive(datetime.now(UTC)) + timedelta(minutes=30)
    payload = (await _sensor_audit(sensor_id))[0]
    assert payload.get("before") in (None, {})
    assert payload.get("after") in (None, {})


@pytest.mark.asyncio
async def test_sensor_edit_hides_secret_named_params_in_audit(client: TestClient) -> None:
    await _setup()
    sensor_id = await _seed_sensor(name="secret-params")
    key = make_rsa_keypair("kid-sensor-secret")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.patch(
            f"/api/v1/sensors/{sensor_id}",
            json={"params": {"password": "made-up-pw-123", "folder": "a"}},
            headers=_headers(key),
        )
    assert resp.status_code == 200, resp.text
    payload = (await _sensor_audit(sensor_id))[0]
    assert "made-up-pw-123" not in str(payload)
    assert payload["after"]["params"]["folder"] == "a"


@pytest.mark.parametrize(
    ("body", "status", "detail_part"),
    [
        ({"cadence_kind": "cron"}, 422, "sensor_edit_invalid"),
        ({"cron_expr": "*/5 * * * *"}, 422, "sensor_edit_invalid"),
        ({"op_id": "vmware.vm.nonexistent"}, 422, "sensor_operation_not_found"),
        ({"op_id": "vmware.vm.delete"}, 422, "sensor_requires_safe_operation"),
        ({"name": "taken"}, 409, "sensor_name_conflict"),
        ({}, 422, "at least one field"),
        ({"name": None}, 422, "at least one field"),
        ({"status": "paused"}, 422, "extra"),
        ({"identity_sub": "someone-else"}, 422, "extra"),
        ({"interval_seconds": 1}, 422, "greater than or equal"),
    ],
)
@pytest.mark.asyncio
async def test_sensor_edit_bad_cases_change_nothing(
    client: TestClient, body: dict[str, Any], status: int, detail_part: str
) -> None:
    await _setup()
    await _seed_descriptor(version="9.0", op_id="vmware.vm.delete", safety="dangerous")
    await _seed_sensor(name="taken")
    sensor_id = await _seed_sensor(name="vm-count")
    key = make_rsa_keypair("kid-sensor-bad")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.patch(f"/api/v1/sensors/{sensor_id}", json=body, headers=_headers(key))
    assert resp.status_code == status, resp.text
    assert detail_part in resp.text
    async with get_sessionmaker()() as session:
        row = await session.get(Sensor, sensor_id)
        assert row is not None
        assert row.name == "vm-count"
        assert row.op_id == _SAFE_OP
        assert row.cadence_kind == "interval"


@pytest.mark.asyncio
async def test_sensor_edit_other_tenant_and_operator_role(client: TestClient) -> None:
    await _setup()
    sensor_id = await _seed_sensor(name="vm-count", tenant_id=_TENANT_B)
    key = make_rsa_keypair("kid-sensor-tenant")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        other = client.patch(
            f"/api/v1/sensors/{sensor_id}", json={"name": "x"}, headers=_headers(key)
        )
        assert other.status_code == 404
        assert other.json()["detail"] == "sensor_not_found"
        operator = client.patch(
            f"/api/v1/sensors/{sensor_id}",
            json={"name": "x"},
            headers=_headers(key, role=TenantRole.OPERATOR, tenant_id=_TENANT_B),
        )
        assert operator.status_code == 403
    async with get_sessionmaker()() as session:
        row = await session.get(Sensor, sensor_id)
        assert row is not None
        assert row.name == "vm-count"


# ---------------------------------------------------------------------------
# Dashboard edit
# ---------------------------------------------------------------------------


async def _create_dashboard(client: TestClient, headers: dict[str, str], ids: list[UUID]) -> str:
    resp = client.post(
        "/api/v1/checks/dashboards",
        json={
            "name": "estate",
            "sensor_ids": [str(i) for i in ids],
            "notify_email": "ops@example.com",
            "notify_min_state": "degraded",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


@pytest.mark.asyncio
async def test_dashboard_edit_members_keeps_id_and_mail_settings(client: TestClient) -> None:
    await _setup()
    one = await _seed_sensor(name="one")
    two = await _seed_sensor(name="two")
    three = await _seed_sensor(name="three")
    key = make_rsa_keypair("kid-dash-edit")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = _headers(key)
        dash_id = await _create_dashboard(client, headers, [one, two])
        edit = {"add_sensor_ids": [str(three)], "remove_sensor_ids": [str(one)]}
        resp = client.patch(f"/api/v1/checks/dashboards/{dash_id}", json=edit, headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == dash_id
        assert {m["sensor_id"] for m in body["members"]} == {str(two), str(three)}
        assert body["notify_email"] == "ops@example.com"
        assert body["notify_min_state"] == "degraded"
        # Safe to send twice: the second time nothing changes.
        again = client.patch(f"/api/v1/checks/dashboards/{dash_id}", json=edit, headers=headers)
        assert again.status_code == 200
        assert again.json()["member_count"] == 2

    payloads = await _dashboard_audit(dash_id)
    assert payloads[0]["op_id"] == "dashboard.edit"
    assert payloads[0]["before"] == {"sensor_ids": sorted([str(one), str(two)])}
    assert payloads[0]["after"] == {"sensor_ids": sorted([str(two), str(three)])}
    assert payloads[1].get("after") in (None, {})


@pytest.mark.asyncio
async def test_dashboard_edit_name_and_mail_settings(client: TestClient) -> None:
    await _setup()
    one = await _seed_sensor(name="one")
    key = make_rsa_keypair("kid-dash-mail")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = _headers(key)
        dash_id = await _create_dashboard(client, headers, [one])
        resp = client.patch(
            f"/api/v1/checks/dashboards/{dash_id}",
            json={"name": "estate-2", "notify_min_state": "critical"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "estate-2"
        assert resp.json()["notify_min_state"] == "critical"
        assert resp.json()["member_count"] == 1
        off = client.patch(
            f"/api/v1/checks/dashboards/{dash_id}", json={"notify_email": ""}, headers=headers
        )
        assert off.status_code == 200, off.text
        assert off.json()["notify_email"] is None

    payloads = await _dashboard_audit(dash_id)
    assert payloads[0]["before"] == {"name": "estate", "notify_min_state": "degraded"}
    assert payloads[0]["after"] == {"name": "estate-2", "notify_min_state": "critical"}
    assert payloads[1]["before"] == {"notify_email": "ops@example.com"}
    assert payloads[1]["after"] == {"notify_email": None}


@pytest.mark.asyncio
async def test_dashboard_edit_bad_cases_change_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _setup()
    one = await _seed_sensor(name="one")
    two = await _seed_sensor(name="two")
    foreign = await _seed_sensor(name="foreign", tenant_id=_TENANT_B)
    key = make_rsa_keypair("kid-dash-bad")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = _headers(key)
        dash_id = await _create_dashboard(client, headers, [one])
        taken = client.post("/api/v1/checks/dashboards", json={"name": "taken"}, headers=headers)
        assert taken.status_code == 201
        url = f"/api/v1/checks/dashboards/{dash_id}"
        cases: list[tuple[dict[str, Any], int, str]] = [
            ({"add_sensor_ids": [str(foreign)]}, 422, "sensor_not_found"),
            ({"add_sensor_ids": [str(two)], "remove_sensor_ids": [str(two)]}, 422, "at once"),
            ({"name": "taken"}, 409, "dashboard_name_conflict"),
            ({}, 422, "at least one field"),
            ({"notify_email": "not-an-address"}, 422, "not a valid email"),
            ({"notify_min_state": "ok"}, 422, "literal_error"),
            ({"sensor_ids": [str(two)]}, 422, "extra"),
        ]
        for body, status, part in cases:
            resp = client.patch(url, json=body, headers=headers)
            assert resp.status_code == status, (body, resp.text)
            assert part in resp.text, (body, resp.text)
        monkeypatch.setattr(dashboard_edit, "_MAX_MEMBERS", 1)
        full = client.patch(url, json={"add_sensor_ids": [str(two)]}, headers=headers)
        assert full.status_code == 422
        assert "dashboard_edit_invalid" in full.text
        other = client.patch(url, json={"name": "x"}, headers=_headers(key, tenant_id=_TENANT_B))
        assert other.status_code == 404
        operator = client.patch(
            url, json={"name": "x"}, headers=_headers(key, role=TenantRole.OPERATOR)
        )
        assert operator.status_code == 403
        shown = client.get(url, headers=headers).json()
    assert shown["name"] == "estate"
    assert [m["sensor_id"] for m in shown["members"]] == [str(one)]
    assert shown["notify_email"] == "ops@example.com"
