# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A Dashboard file import follows the identity rule of a Sensor edit.

A Sensor runs under its ``identity_sub``. When a tenant admin imports a file
that changes what a Sensor runs (operation, target, params or check), and the
Sensor runs as another person, MEHO sets the identity to ``__sensor__``. The
import result and its audit row show the before and after value. The
creator's own import, and an import that changes other fields, keep the
identity.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors import (
    Connector,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.connectors import registry as connector_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, Sensor, Tenant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks
from ._vault_fakes import install_fake_vault

_TENANT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_CREATOR = "person-creator"
_SECOND_ADMIN = "person-second-admin"
_PRODUCT = "idprod"
_CONNECTOR = "idprod-rest-9.0"
_OP = "idprod.item.list"
_OTHER_OP = "idprod.item.count"
_EXPORT = "/api/v1/checks/dashboard-files/export"
_IMPORT = "/api/v1/checks/dashboard-files/import"
_ASSERTION: dict[str, Any] = {
    "select": {"path": "$.count"},
    "compare": {"type": "threshold", "op": "lt", "critical": 10},
}
_SAME_TARGET = {"site-one": "site-one"}


class _Fake9(Connector):
    product = _PRODUCT
    impl_id = "idprod-rest"
    version = "9.0"
    supported_version_range = ">=9.0,<10.0"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _connectors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(connector_registry._REGISTRY_V2, (_PRODUCT, "9.0", "idprod-rest"), _Fake9)


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


@pytest.fixture
def key() -> Iterator[Any]:
    rsa = make_rsa_keypair("kid-dashboard-file-identity")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(rsa))
        yield rsa


def _headers(key: Any, sub: str) -> dict[str, str]:
    token = mint_token(
        key, sub=sub, tenant_role=TenantRole.TENANT_ADMIN.value, tenant_id=str(_TENANT)
    )
    return {"Authorization": f"Bearer {token}"}


async def _seed() -> None:
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=_TENANT, slug="tenant-a", name="Tenant A"))
        for op_id in (_OP, _OTHER_OP):
            session.add(
                EndpointDescriptor(
                    product=_PRODUCT,
                    version="9.0",
                    impl_id="idprod-rest",
                    op_id=op_id,
                    source_kind="ingested",
                    method="GET",
                    path=f"/{op_id}",
                    parameter_schema={"type": "object", "properties": {}},
                    safety_level="safe",
                )
            )
        for name in ("site-one", "site-two"):
            session.add(
                TargetORM(
                    id=uuid.uuid4(),
                    tenant_id=_TENANT,
                    name=name,
                    aliases=[],
                    product=_PRODUCT,
                    version=None,
                    fingerprint={"version": "9.0.2"},
                    host="appliance.example.test",
                    port=443,
                    fqdn=None,
                    secret_ref=None,
                    auth_model="shared_service_account",
                    vpn_required=False,
                    extras={},
                    notes=None,
                    created_at=now,
                    updated_at=now,
                )
            )
        await session.commit()


async def _setup(client: TestClient, key: Any) -> dict[str, Any]:
    """The creator makes a Sensor that runs as the creator, on a Dashboard; return its file."""
    await _seed()
    headers = _headers(key, _CREATOR)
    resp = client.post(
        "/api/v1/sensors",
        json={
            "name": "items",
            "connector_id": _CONNECTOR,
            "op_id": _OP,
            "target": {"name": "site-one"},
            "params": {"limit": 5},
            "assertion": _ASSERTION,
            "cadence_kind": "interval",
            "interval_seconds": 120,
            "severity": "degraded",
            "identity_sub": _CREATOR,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(
        "/api/v1/checks/dashboards",
        json={"name": "estate", "sensor_ids": [resp.json()["id"]]},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    resp = client.get(_EXPORT, params={"name": "estate"}, headers=headers)
    assert resp.status_code == 200, resp.text
    exported: dict[str, Any] = resp.json()
    return exported


def _with(exported: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {**exported, "sensors": [{**s, **changes} for s in exported["sensors"]]}


async def _identity() -> str:
    async with get_sessionmaker()() as session:
        row = await session.scalar(select(Sensor).where(Sensor.name == "items"))
    assert row is not None
    return row.identity_sub


async def _import_audit() -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        row = await session.scalar(
            select(AuditLog).where(AuditLog.path == _IMPORT, AuditLog.method == "POST")
        )
    assert row is not None
    return dict(row.payload or {})


#: File changes that change what the Sensor runs: (changes to the Sensor, target map).
_RUN_CHANGES: dict[str, tuple[dict[str, Any], dict[str, str]]] = {
    "op_id": ({"op_id": _OTHER_OP}, _SAME_TARGET),
    "target": ({}, {"site-one": "site-two"}),
    "params": ({"params": {"limit": 9}}, _SAME_TARGET),
    "assertion": (
        {
            "assertion": {
                "select": {"path": "$.count"},
                "compare": {"type": "threshold", "op": "lt", "critical": 5},
            }
        },
        _SAME_TARGET,
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("field", sorted(_RUN_CHANGES))
async def test_a_second_admin_importing_what_runs_resets_the_identity(
    client: TestClient, key: Any, field: str
) -> None:
    exported = await _setup(client, key)
    changes, target_map = _RUN_CHANGES[field]

    resp = client.post(
        _IMPORT,
        json={"file": _with(exported, **changes), "target_map": target_map},
        headers=_headers(key, _SECOND_ADMIN),
    )

    assert resp.status_code == 200, resp.text
    (sensor,) = resp.json()["sensors"]
    assert sensor["action"] == "update"
    assert field in sensor["changed_fields"]
    assert sensor["identity_sub_before"] == _CREATOR
    assert sensor["identity_sub_after"] == "__sensor__"
    assert await _identity() == "__sensor__"
    audit = await _import_audit()
    assert audit["import"]["identity_sub"] == {"items": {"before": _CREATOR, "after": "__sensor__"}}


@pytest.mark.asyncio
async def test_the_creator_importing_what_runs_keeps_the_identity(
    client: TestClient, key: Any
) -> None:
    exported = await _setup(client, key)

    resp = client.post(
        _IMPORT,
        json={"file": _with(exported, op_id=_OTHER_OP), "target_map": _SAME_TARGET},
        headers=_headers(key, _CREATOR),
    )

    assert resp.status_code == 200, resp.text
    (sensor,) = resp.json()["sensors"]
    assert sensor["action"] == "update"
    assert sensor["identity_sub_before"] is None
    assert sensor["identity_sub_after"] is None
    assert await _identity() == _CREATOR
    assert (await _import_audit())["import"]["identity_sub"] == {}


@pytest.mark.asyncio
async def test_a_second_admin_importing_other_fields_keeps_the_identity(
    client: TestClient, key: Any
) -> None:
    exported = await _setup(client, key)

    resp = client.post(
        _IMPORT,
        json={
            "file": _with(exported, interval_seconds=300, severity="critical"),
            "target_map": _SAME_TARGET,
        },
        headers=_headers(key, _SECOND_ADMIN),
    )

    assert resp.status_code == 200, resp.text
    (sensor,) = resp.json()["sensors"]
    assert sensor["action"] == "update"
    assert sensor["identity_sub_after"] is None
    assert await _identity() == _CREATOR


@pytest.mark.asyncio
async def test_a_dry_run_names_the_identity_change_and_writes_nothing(
    client: TestClient, key: Any
) -> None:
    exported = await _setup(client, key)

    resp = client.post(
        _IMPORT,
        json={
            "file": _with(exported, op_id=_OTHER_OP),
            "target_map": _SAME_TARGET,
            "dry_run": True,
        },
        headers=_headers(key, _SECOND_ADMIN),
    )

    assert resp.status_code == 200, resp.text
    (sensor,) = resp.json()["sensors"]
    assert sensor["identity_sub_before"] == _CREATOR
    assert sensor["identity_sub_after"] == "__sensor__"
    assert await _identity() == _CREATOR
