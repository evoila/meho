# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Sensor admin surface (#2503) -- service + REST.

Initiative #2416 (parent goal #221), Task #2503. Coverage matrix:

* **Service layer (CRUD)** -- create / list / get / delete against
  :class:`SensorAdminService`; the safe-only create guard; hard delete;
  tenant boundary; ``record_sensor_result`` projection semantics.
* **Safe-only guard** -- a non-safe (caution / dangerous) op is refused
  422 ``sensor_requires_safe_operation``; an unknown op 422
  ``sensor_operation_not_found``; a safe op is 201.
* **Wire validation** -- a malformed assertion (bad select path / unknown
  comparator) and a malformed cadence union each surface as 422.
* **RBAC + tenant scoping at REST.**

The tests run on the SQLite engine from :mod:`tests.conftest`; the REST
middleware chain is exercised via :class:`fastapi.testclient.TestClient`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.checks.repository import record_sensor_result
from meho_backplane.checks.schemas import SensorCreate
from meho_backplane.checks.service import (
    SensorAdminService,
    SensorNameConflictError,
    SensorOperationNotFoundError,
    SensorRequiresSafeOperationError,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, Sensor, Tenant
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

_SAFE_CONNECTOR = "vmware-rest-9.0"
_SAFE_OP = "vmware.vm.list"
_DANGEROUS_OP = "vmware.vm.delete"

_ASSERTION: dict[str, Any] = {
    "select": {"path": "$.count"},
    "compare": {"type": "threshold", "op": "lt", "critical": 10},
}


def _naive(dt: datetime | None) -> datetime | None:
    """Drop tzinfo for comparison.

    ``DateTime(timezone=True)`` columns round-trip *naive* on aiosqlite (the
    unit-test path) and *aware* on PG; the service deliberately does not
    force-attach UTC on SQLite. Comparisons in this module normalise both
    sides so the assertion holds on either dialect.
    """
    return dt.replace(tzinfo=None) if dt is not None and dt.tzinfo is not None else dt


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
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


def _token(
    key: Any,
    *,
    sub: str = "op-admin",
    role: TenantRole = TenantRole.TENANT_ADMIN,
    tenant_id: UUID = _TENANT_A,
    platform_admin: bool = False,
) -> str:
    return mint_token(
        key,
        sub=sub,
        tenant_role=role.value,
        tenant_id=str(tenant_id),
        platform_admin=platform_admin if platform_admin else None,
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


async def _seed_tenant(tenant_id: UUID, slug: str) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        existing = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        if existing.scalar_one_or_none() is None:
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
            await session.commit()


async def _seed_descriptor(
    *,
    op_id: str,
    safety_level: str = "safe",
    product: str = "vmware",
    version: str = "9.0",
    impl_id: str = "vmware-rest",
) -> None:
    """Insert a global (built-in) enabled EndpointDescriptor row.

    ``lookup_descriptor`` resolves the tenant-scoped composite first then
    the global (``tenant_id IS NULL``) fallback, so a global descriptor is
    visible to every tenant's create path.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        existing = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id.is_(None),
                EndpointDescriptor.product == product,
                EndpointDescriptor.version == version,
                EndpointDescriptor.impl_id == impl_id,
                EndpointDescriptor.op_id == op_id,
            )
        )
        if existing.scalar_one_or_none() is None:
            session.add(
                EndpointDescriptor(
                    product=product,
                    version=version,
                    impl_id=impl_id,
                    op_id=op_id,
                    source_kind="ingested",
                    method="GET",
                    path=f"/{op_id}",
                    parameter_schema={"type": "object", "properties": {}},
                    safety_level=safety_level,
                )
            )
            await session.commit()


async def _seed_tenant_and_safe_op(tenant_id: UUID = _TENANT_A) -> None:
    await _seed_tenant(tenant_id, slug=str(tenant_id)[:8])
    await _seed_descriptor(op_id=_SAFE_OP, safety_level="safe")


def _create_payload(
    *,
    name: str = "disk-space",
    op_id: str = _SAFE_OP,
    connector_id: str = _SAFE_CONNECTOR,
    **overrides: Any,
) -> SensorCreate:
    body: dict[str, Any] = {
        "name": name,
        "connector_id": connector_id,
        "op_id": op_id,
        "assertion": _ASSERTION,
        "cadence_kind": "interval",
        "interval_seconds": 60,
    }
    body.update(overrides)
    return SensorCreate.model_validate(body)


# ---------------------------------------------------------------------------
# Service layer -- safe-only guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_create_safe_op() -> None:
    await _seed_tenant_and_safe_op()
    service = SensorAdminService()
    entry = await service.create(
        tenant_id=_TENANT_A,
        created_by_sub="op-admin",
        payload=_create_payload(),
    )
    assert entry.name == "disk-space"
    assert entry.cadence_kind.value == "interval"
    assert entry.interval_seconds == 60
    assert entry.next_fire_at is not None
    assert entry.status.value == "active"
    assert entry.last_state == "unknown"


@pytest.mark.asyncio
async def test_service_create_rejects_non_safe_op() -> None:
    await _seed_tenant(_TENANT_A, "tenant-a")
    await _seed_descriptor(op_id=_DANGEROUS_OP, safety_level="dangerous")
    service = SensorAdminService()
    with pytest.raises(SensorRequiresSafeOperationError):
        await service.create(
            tenant_id=_TENANT_A,
            created_by_sub="op-admin",
            payload=_create_payload(op_id=_DANGEROUS_OP),
        )


@pytest.mark.asyncio
async def test_service_create_rejects_unknown_op() -> None:
    await _seed_tenant(_TENANT_A, "tenant-a")
    service = SensorAdminService()
    with pytest.raises(SensorOperationNotFoundError):
        await service.create(
            tenant_id=_TENANT_A,
            created_by_sub="op-admin",
            payload=_create_payload(op_id="vmware.vm.nonexistent"),
        )


@pytest.mark.asyncio
async def test_service_create_rejects_duplicate_name() -> None:
    await _seed_tenant_and_safe_op()
    service = SensorAdminService()
    await service.create(
        tenant_id=_TENANT_A, created_by_sub="op-admin", payload=_create_payload(name="dupe")
    )
    with pytest.raises(SensorNameConflictError):
        await service.create(
            tenant_id=_TENANT_A, created_by_sub="op-admin", payload=_create_payload(name="dupe")
        )


# ---------------------------------------------------------------------------
# Service layer -- list / get / delete / tenant boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_list_filters_status_and_cadence() -> None:
    await _seed_tenant_and_safe_op()
    await _seed_descriptor(op_id=_SAFE_OP, safety_level="safe")
    service = SensorAdminService()
    interval = await service.create(
        tenant_id=_TENANT_A, created_by_sub="op-admin", payload=_create_payload(name="a")
    )
    cron = await service.create(
        tenant_id=_TENANT_A,
        created_by_sub="op-admin",
        payload=_create_payload(
            name="b", cadence_kind="cron", cron_expr="0 9 * * *", interval_seconds=None
        ),
    )
    interval_only = await service.list_(_TENANT_A, cadence_kind="interval")
    assert [s.id for s in interval_only] == [interval.id]
    cron_only = await service.list_(_TENANT_A, cadence_kind="cron")
    assert [s.id for s in cron_only] == [cron.id]
    active = await service.list_(_TENANT_A, status="active")
    assert {s.id for s in active} == {interval.id, cron.id}


@pytest.mark.asyncio
async def test_service_delete_is_hard_delete() -> None:
    await _seed_tenant_and_safe_op()
    service = SensorAdminService()
    entry = await service.create(
        tenant_id=_TENANT_A, created_by_sub="op-admin", payload=_create_payload()
    )
    assert await service.delete(_TENANT_A, entry.id) is True
    # No tombstone row -- a direct row count is zero.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        count = (
            await session.execute(
                select(func.count()).select_from(Sensor).where(Sensor.id == entry.id)
            )
        ).scalar_one()
    assert count == 0
    # Repeat delete is a no-op (row gone).
    assert await service.delete(_TENANT_A, entry.id) is False
    assert await service.get(_TENANT_A, entry.id) is None


@pytest.mark.asyncio
async def test_service_tenant_boundary() -> None:
    await _seed_tenant_and_safe_op(_TENANT_A)
    await _seed_tenant(_TENANT_B, "tenant-b")
    service = SensorAdminService()
    entry_a = await service.create(
        tenant_id=_TENANT_A, created_by_sub="op-admin", payload=_create_payload()
    )
    # Tenant B cannot see tenant A's sensor.
    assert await service.get(_TENANT_B, entry_a.id) is None
    assert [s.id for s in await service.list_(_TENANT_B)] == []
    # Tenant B's delete of A's id is a no-op; A's row survives.
    assert await service.delete(_TENANT_B, entry_a.id) is False
    assert await service.get(_TENANT_A, entry_a.id) is not None


# ---------------------------------------------------------------------------
# record_sensor_result projection semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_sensor_result_projection_and_state_since() -> None:
    """Same state twice keeps ``state_since`` + returns False; a change moves it + True."""
    await _seed_tenant_and_safe_op()
    service = SensorAdminService()
    entry = await service.create(
        tenant_id=_TENANT_A, created_by_sub="op-admin", payload=_create_payload()
    )
    sessionmaker = get_sessionmaker()
    t0 = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 7, 16, 12, 1, 0, tzinfo=UTC)
    t2 = datetime(2026, 7, 16, 12, 2, 0, tzinfo=UTC)

    # First result: unknown -> ok is a change (returns True, sets state_since=t0).
    async with sessionmaker() as session:
        changed = await record_sensor_result(
            session,
            sensor_id=entry.id,
            state="ok",
            value=3,
            evidence={"observed": 3},
            evaluated_at=t0,
        )
        await session.commit()
        assert changed is True

    async with sessionmaker() as session:
        row = await session.get(Sensor, entry.id)
        assert row is not None
        assert row.last_state == "ok"
        assert row.last_value == 3
        assert row.last_evidence == {"observed": 3}
        assert _naive(row.last_evaluated_at) == _naive(t0)
        first_state_since = row.state_since
        assert first_state_since is not None

    # Second result: ok -> ok is no change (returns False, keeps state_since).
    async with sessionmaker() as session:
        changed = await record_sensor_result(
            session,
            sensor_id=entry.id,
            state="ok",
            value=4,
            evidence={"observed": 4},
            evaluated_at=t1,
        )
        await session.commit()
        assert changed is False

    async with sessionmaker() as session:
        row = await session.get(Sensor, entry.id)
        assert row is not None
        assert _naive(row.last_evaluated_at) == _naive(t1)  # projection still updates
        assert row.state_since == first_state_since  # unchanged

    # Third result: ok -> critical is a change (returns True, moves state_since=t2).
    async with sessionmaker() as session:
        changed = await record_sensor_result(
            session,
            sensor_id=entry.id,
            state="critical",
            value=99,
            evidence={"observed": 99},
            evaluated_at=t2,
        )
        await session.commit()
        assert changed is True

    async with sessionmaker() as session:
        row = await session.get(Sensor, entry.id)
        assert row is not None
        assert row.last_state == "critical"
        assert _naive(row.state_since) == _naive(t2)


# ---------------------------------------------------------------------------
# REST surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_create_list_delete_round_trip(client: TestClient) -> None:
    """POST creates -> GET lists -> DELETE removes -> GET is empty."""
    await _seed_tenant_and_safe_op()
    key = make_rsa_keypair("kid-rest")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        created = client.post(
            "/api/v1/sensors",
            json={
                "name": "disk-space",
                "connector_id": _SAFE_CONNECTOR,
                "op_id": _SAFE_OP,
                "assertion": _ASSERTION,
                "cadence_kind": "interval",
                "interval_seconds": 60,
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        sensor_id = body["id"]
        assert body["status"] == "active"
        assert body["last_state"] == "unknown"
        assert body["created_by_sub"] == "op-admin"

        listed = client.get("/api/v1/sensors", headers=headers)
        assert listed.status_code == 200
        assert [s["id"] for s in listed.json()["sensors"]] == [sensor_id]

        deleted = client.delete(f"/api/v1/sensors/{sensor_id}", headers=headers)
        assert deleted.status_code == 204
        # Hard delete: repeat returns 404 and the list is empty.
        assert client.delete(f"/api/v1/sensors/{sensor_id}", headers=headers).status_code == 404
        assert client.get("/api/v1/sensors", headers=headers).json()["sensors"] == []


@pytest.mark.asyncio
async def test_rest_create_non_safe_op_returns_422(client: TestClient) -> None:
    await _seed_tenant(_TENANT_A, "tenant-a")
    await _seed_descriptor(op_id=_DANGEROUS_OP, safety_level="caution")
    key = make_rsa_keypair("kid-caution")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        result = client.post(
            "/api/v1/sensors",
            json={
                "name": "risky",
                "connector_id": _SAFE_CONNECTOR,
                "op_id": _DANGEROUS_OP,
                "assertion": _ASSERTION,
                "cadence_kind": "interval",
                "interval_seconds": 60,
            },
            headers=headers,
        )
        assert result.status_code == 422, result.text
        assert result.json()["detail"] == "sensor_requires_safe_operation"


@pytest.mark.asyncio
async def test_rest_create_unknown_op_returns_422(client: TestClient) -> None:
    await _seed_tenant(_TENANT_A, "tenant-a")
    key = make_rsa_keypair("kid-unknown")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        result = client.post(
            "/api/v1/sensors",
            json={
                "name": "ghost",
                "connector_id": _SAFE_CONNECTOR,
                "op_id": "vmware.vm.nonexistent",
                "assertion": _ASSERTION,
                "cadence_kind": "interval",
                "interval_seconds": 60,
            },
            headers=headers,
        )
        assert result.status_code == 422, result.text
        assert result.json()["detail"] == "sensor_operation_not_found"


@pytest.mark.asyncio
async def test_rest_malformed_assertion_returns_422(client: TestClient) -> None:
    """A bad select path (two [*]) and an unknown comparator type each 422 at the wire."""
    await _seed_tenant_and_safe_op()
    key = make_rsa_keypair("kid-assert")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        bad_path = client.post(
            "/api/v1/sensors",
            json={
                "name": "bad-path",
                "connector_id": _SAFE_CONNECTOR,
                "op_id": _SAFE_OP,
                "assertion": {
                    "select": {"path": "$.items[*].tags[*]"},
                    "compare": {"type": "threshold", "op": "lt", "critical": 10},
                },
                "cadence_kind": "interval",
                "interval_seconds": 60,
            },
            headers=headers,
        )
        assert bad_path.status_code == 422, bad_path.text
        bad_cmp = client.post(
            "/api/v1/sensors",
            json={
                "name": "bad-cmp",
                "connector_id": _SAFE_CONNECTOR,
                "op_id": _SAFE_OP,
                "assertion": {"select": {"path": "$.count"}, "compare": {"type": "nonsense"}},
                "cadence_kind": "interval",
                "interval_seconds": 60,
            },
            headers=headers,
        )
        assert bad_cmp.status_code == 422, bad_cmp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cadence_overrides",
    [
        {"cadence_kind": "interval", "interval_seconds": 60, "cron_expr": "* * * * *"},  # both
        {"cadence_kind": "interval"},  # neither (interval kind, no seconds)
        {"cadence_kind": "interval", "interval_seconds": 1},  # below floor 5
        {"cadence_kind": "cron", "cron_expr": "not a cron"},  # invalid cron
    ],
)
async def test_rest_cadence_union_returns_422(
    client: TestClient, cadence_overrides: dict[str, Any]
) -> None:
    await _seed_tenant_and_safe_op()
    key = make_rsa_keypair("kid-cadence")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        body: dict[str, Any] = {
            "name": "cadence-test",
            "connector_id": _SAFE_CONNECTOR,
            "op_id": _SAFE_OP,
            "assertion": _ASSERTION,
        }
        body.update(cadence_overrides)
        result = client.post("/api/v1/sensors", json=body, headers=headers)
        assert result.status_code == 422, result.text


@pytest.mark.asyncio
async def test_rest_for_seconds_and_status_set_at_create(client: TestClient) -> None:
    """``for_seconds`` persists; omitting it defaults 0; a ``status`` field is rejected."""
    await _seed_tenant_and_safe_op()
    key = make_rsa_keypair("kid-forsec")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        with_for = client.post(
            "/api/v1/sensors",
            json={
                "name": "held",
                "connector_id": _SAFE_CONNECTOR,
                "op_id": _SAFE_OP,
                "assertion": _ASSERTION,
                "cadence_kind": "interval",
                "interval_seconds": 60,
                "for_seconds": 300,
            },
            headers=headers,
        )
        assert with_for.status_code == 201, with_for.text
        assert with_for.json()["for_seconds"] == 300
        assert with_for.json()["status"] == "active"

        default_for = client.post(
            "/api/v1/sensors",
            json={
                "name": "unheld",
                "connector_id": _SAFE_CONNECTOR,
                "op_id": _SAFE_OP,
                "assertion": _ASSERTION,
                "cadence_kind": "interval",
                "interval_seconds": 60,
            },
            headers=headers,
        )
        assert default_for.status_code == 201
        assert default_for.json()["for_seconds"] == 0

        # A body carrying `status` is rejected (set-at-create-only; extra=forbid).
        with_status = client.post(
            "/api/v1/sensors",
            json={
                "name": "paused-at-create",
                "connector_id": _SAFE_CONNECTOR,
                "op_id": _SAFE_OP,
                "assertion": _ASSERTION,
                "cadence_kind": "interval",
                "interval_seconds": 60,
                "status": "paused",
            },
            headers=headers,
        )
        assert with_status.status_code == 422, with_status.text


@pytest.mark.asyncio
async def test_rest_tenant_scoping(client: TestClient) -> None:
    """A sensor under tenant A is absent from B's list; B's delete of A's id is 404."""
    await _seed_tenant_and_safe_op(_TENANT_A)
    await _seed_tenant(_TENANT_B, "tenant-b")
    service = SensorAdminService()
    entry_a = await service.create(
        tenant_id=_TENANT_A, created_by_sub="op-admin", payload=_create_payload()
    )
    b_key = make_rsa_keypair("kid-b")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(b_key))
        b_headers = {"Authorization": f"Bearer {_token(b_key, sub='b-admin', tenant_id=_TENANT_B)}"}
        listed = client.get("/api/v1/sensors", headers=b_headers)
        assert listed.status_code == 200
        assert listed.json()["sensors"] == []
        result = client.delete(f"/api/v1/sensors/{entry_a.id}", headers=b_headers)
        assert result.status_code == 404
        assert result.json()["detail"] == "sensor_not_found"


@pytest.mark.asyncio
async def test_rest_operator_can_list_but_not_create(client: TestClient) -> None:
    """An ``operator`` lists but is 403 on create / delete."""
    await _seed_tenant_and_safe_op()
    op_key = make_rsa_keypair("kid-op")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(op_key))
        op_headers = {
            "Authorization": f"Bearer {_token(op_key, sub='op-user', role=TenantRole.OPERATOR)}",
        }
        listed = client.get("/api/v1/sensors", headers=op_headers)
        assert listed.status_code == 200
        rejected = client.post(
            "/api/v1/sensors",
            json={
                "name": "op-created",
                "connector_id": _SAFE_CONNECTOR,
                "op_id": _SAFE_OP,
                "assertion": _ASSERTION,
                "cadence_kind": "interval",
                "interval_seconds": 60,
            },
            headers=op_headers,
        )
        assert rejected.status_code == 403
        rejected_delete = client.delete(f"/api/v1/sensors/{uuid4()}", headers=op_headers)
        assert rejected_delete.status_code == 403
