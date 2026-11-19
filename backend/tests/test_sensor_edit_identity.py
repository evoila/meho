# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A sensor edit keeps or resets the sensor's identity (``identity_sub``).

When an editor changes the connector, operation, params or assertion of a
sensor whose identity names another principal, MEHO sets the identity to
the sensor's own identity (``__sensor__``). The audit row of the edit shows
the change in ``before`` and ``after``. The creator's own edits, and edits
of other fields, keep the identity.
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

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, Sensor, Tenant
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks
from ._vault_fakes import install_fake_vault

_TENANT = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_CONNECTOR = "vmware-rest-9.0"
_OLD_OP = "vmware.vm.list"
_NEW_OP = "vmware.vm.count"
_CREATOR = "admin-creator"
_SECOND_ADMIN = "admin-second"
_SENTINEL = "__sensor__"

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


async def _seed(identity_sub: str) -> UUID:
    sensor_id = uuid4()
    async with get_sessionmaker()() as session:
        if await session.get(Tenant, _TENANT) is None:
            session.add(Tenant(id=_TENANT, slug="tenant-edit", name="Tenant edit"))
        for op_id in (_OLD_OP, _NEW_OP):
            session.add(
                EndpointDescriptor(
                    product="vmware",
                    version="9.0",
                    impl_id="vmware-rest",
                    op_id=op_id,
                    source_kind="ingested",
                    method="GET",
                    path=f"/{op_id}",
                    parameter_schema={"type": "object", "properties": {}},
                    safety_level="safe",
                )
            )
        session.add(
            Sensor(
                id=sensor_id,
                tenant_id=_TENANT,
                name="vm-count",
                connector_id=_CONNECTOR,
                op_id=_OLD_OP,
                target=None,
                params={},
                assertion=_ASSERTION,
                status="active",
                cadence_kind="interval",
                interval_seconds=300,
                cron_expr=None,
                timezone="UTC",
                next_fire_at=datetime.now(UTC) + timedelta(hours=1),
                severity="critical",
                for_seconds=0,
                identity_sub=identity_sub,
                created_by_sub=_CREATOR,
            )
        )
        await session.commit()
    return sensor_id


async def _identity(sensor_id: UUID) -> str:
    async with get_sessionmaker()() as session:
        row = await session.get(Sensor, sensor_id)
        assert row is not None
        return row.identity_sub


async def _edit_audit(sensor_id: UUID) -> list[dict[str, Any]]:
    async with get_sessionmaker()() as session:
        rows = (
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.path == f"/api/v1/sensors/{sensor_id}", AuditLog.method == "PATCH"
                )
            )
        ).all()
    return [dict(r.payload or {}) for r in rows]


def _patch(client: TestClient, sensor_id: UUID, body: dict[str, Any], *, sub: str) -> Any:
    key = make_rsa_keypair("kid-sensor-identity")
    token = mint_token(
        key, sub=sub, tenant_role=TenantRole.TENANT_ADMIN.value, tenant_id=str(_TENANT)
    )
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        return client.patch(
            f"/api/v1/sensors/{sensor_id}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )


@pytest.mark.asyncio
async def test_a_second_admin_changing_the_operation_resets_the_identity(
    client: TestClient,
) -> None:
    sensor_id = await _seed(_CREATOR)
    resp = _patch(client, sensor_id, {"op_id": _NEW_OP}, sub=_SECOND_ADMIN)
    assert resp.status_code == 200, resp.text
    assert resp.json()["identity_sub"] == _SENTINEL
    assert await _identity(sensor_id) == _SENTINEL
    payloads = await _edit_audit(sensor_id)
    assert len(payloads) == 1
    assert payloads[0]["before"] == {"op_id": _OLD_OP, "identity_sub": _CREATOR}
    assert payloads[0]["after"] == {"op_id": _NEW_OP, "identity_sub": _SENTINEL}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"params": {"limit": 5}}, id="params"),
        pytest.param(
            {"assertion": {**_ASSERTION, "compare": {**_ASSERTION["compare"], "critical": 20}}},
            id="assertion",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_second_admin_changing_what_runs_resets_the_identity(
    client: TestClient, body: dict[str, Any]
) -> None:
    sensor_id = await _seed(_CREATOR)
    resp = _patch(client, sensor_id, body, sub=_SECOND_ADMIN)
    assert resp.status_code == 200, resp.text
    assert await _identity(sensor_id) == _SENTINEL


@pytest.mark.asyncio
async def test_the_creator_editing_their_own_sensor_keeps_the_identity(
    client: TestClient,
) -> None:
    sensor_id = await _seed(_CREATOR)
    resp = _patch(client, sensor_id, {"op_id": _NEW_OP}, sub=_CREATOR)
    assert resp.status_code == 200, resp.text
    assert resp.json()["identity_sub"] == _CREATOR
    payloads = await _edit_audit(sensor_id)
    assert payloads[0]["before"] == {"op_id": _OLD_OP}
    assert payloads[0]["after"] == {"op_id": _NEW_OP}


@pytest.mark.asyncio
async def test_a_second_admin_changing_only_the_severity_keeps_the_identity(
    client: TestClient,
) -> None:
    sensor_id = await _seed(_CREATOR)
    resp = _patch(client, sensor_id, {"severity": "degraded"}, sub=_SECOND_ADMIN)
    assert resp.status_code == 200, resp.text
    assert await _identity(sensor_id) == _CREATOR


@pytest.mark.asyncio
async def test_a_sensor_with_its_own_identity_keeps_it(client: TestClient) -> None:
    sensor_id = await _seed(_SENTINEL)
    resp = _patch(client, sensor_id, {"op_id": _NEW_OP}, sub=_SECOND_ADMIN)
    assert resp.status_code == 200, resp.text
    payloads = await _edit_audit(sensor_id)
    assert payloads[0]["after"] == {"op_id": _NEW_OP}
    assert await _identity(sensor_id) == _SENTINEL
