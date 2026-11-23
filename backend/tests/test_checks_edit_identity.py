# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A Sensor edit never keeps another person as the identity the Sensor runs as.

A Sensor runs under its ``identity_sub``. When a tenant admin changes what
the Sensor runs (connector, operation, params or check), and the identity is
another person, MEHO sets it to the Sensor's own identity ``__sensor__``.
The audit row of the edit shows the change in ``before`` and ``after``.
The creator's own edit, and an edit of other fields, keep the identity.
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
_CREATOR = "person-creator"
_SECOND_ADMIN = "person-second-admin"
_CONNECTOR = "vmware-rest-9.0"
_OP = "vmware.vm.list"
_OTHER_OP = "vmware.vm.count"
_ASSERTION: dict[str, Any] = {
    "select": {"path": "$.count"},
    "compare": {"type": "threshold", "op": "lt", "critical": 10},
}

#: Edits that change what the Sensor runs.
_RUN_EDITS: dict[str, dict[str, Any]] = {
    "op_id": {"op_id": _OTHER_OP},
    "connector_id": {"connector_id": "vmware-rest-9.1"},
    "params": {"params": {"folder": "prod"}},
    "assertion": {
        "assertion": {
            "select": {"path": "$.count"},
            "compare": {"type": "threshold", "op": "lt", "critical": 5},
        }
    },
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


async def _seed(identity_sub: str) -> UUID:
    """A tenant, the safe read operations, and one Sensor that runs as *identity_sub*."""
    sensor_id = uuid4()
    async with get_sessionmaker()() as session:
        if await session.get(Tenant, _TENANT) is None:
            session.add(Tenant(id=_TENANT, slug="tenant-a", name="Tenant A"))
        for version, op_id in (("9.0", _OP), ("9.0", _OTHER_OP), ("9.1", _OP)):
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
                    safety_level="safe",
                )
            )
        session.add(
            Sensor(
                id=sensor_id,
                tenant_id=_TENANT,
                name="vm-count",
                connector_id=_CONNECTOR,
                op_id=_OP,
                target=None,
                params={},
                assertion=_ASSERTION,
                status="active",
                cadence_kind="interval",
                interval_seconds=300,
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


def _patch(client: TestClient, sensor_id: UUID, body: dict[str, Any], editor: str) -> Any:
    key = make_rsa_keypair("kid-sensor-identity")
    token = mint_token(
        key, sub=editor, tenant_role=TenantRole.TENANT_ADMIN.value, tenant_id=str(_TENANT)
    )
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        return client.patch(
            f"/api/v1/sensors/{sensor_id}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )


async def _identity(sensor_id: UUID) -> str:
    async with get_sessionmaker()() as session:
        return (await session.get_one(Sensor, sensor_id)).identity_sub


async def _audit(sensor_id: UUID) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        row = await session.scalar(
            select(AuditLog).where(
                AuditLog.path == f"/api/v1/sensors/{sensor_id}", AuditLog.method == "PATCH"
            )
        )
    assert row is not None
    return dict(row.payload or {})


@pytest.mark.asyncio
@pytest.mark.parametrize("field", sorted(_RUN_EDITS))
async def test_a_second_admin_changing_what_runs_resets_the_identity(
    client: TestClient, field: str
) -> None:
    sensor_id = await _seed(_CREATOR)

    response = _patch(client, sensor_id, _RUN_EDITS[field], _SECOND_ADMIN)

    assert response.status_code == 200, response.text
    assert response.json()["identity_sub"] == "__sensor__"
    assert await _identity(sensor_id) == "__sensor__"
    payload = await _audit(sensor_id)
    assert payload["before"]["identity_sub"] == _CREATOR
    assert payload["after"]["identity_sub"] == "__sensor__"
    assert field in payload["after"]


@pytest.mark.asyncio
async def test_the_creator_editing_their_own_sensor_keeps_the_identity(
    client: TestClient,
) -> None:
    sensor_id = await _seed(_CREATOR)

    response = _patch(client, sensor_id, _RUN_EDITS["op_id"], _CREATOR)

    assert response.status_code == 200, response.text
    assert await _identity(sensor_id) == _CREATOR
    payload = await _audit(sensor_id)
    assert "identity_sub" not in payload["before"]
    assert "identity_sub" not in payload["after"]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"name": "vm-count-renamed"}, {"severity": "degraded"}])
async def test_a_second_admin_changing_other_fields_keeps_the_identity(
    client: TestClient, body: dict[str, Any]
) -> None:
    sensor_id = await _seed(_CREATOR)

    response = _patch(client, sensor_id, body, _SECOND_ADMIN)

    assert response.status_code == 200, response.text
    assert await _identity(sensor_id) == _CREATOR
