# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Dashboard admin surface (#2506) -- service + REST + rollup.

Initiative #2416 (parent goal #221), Task #2506. Coverage:

* **Service** -- create (with membership validation + rollup), list, get,
  delete; tenant boundary; ``sensor_not_found`` on a foreign member; name
  conflict.
* **Rollup wire-through** -- a seeded member's projected state folds into the
  Dashboard ``state`` on read, including the ``for:`` hysteresis DoD.
* **REST** -- RBAC (operator reads, tenant_admin writes), the 422 / 404
  contracts, the detail per-member fields, and the ``last_rollup_state``
  stays-NULL guarantee.

Runs on the SQLite engine from :mod:`tests.conftest`; the REST middleware
chain is exercised via :class:`fastapi.testclient.TestClient`.
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
from meho_backplane.checks.dashboard_schemas import DashboardCreate
from meho_backplane.checks.dashboard_service import (
    CheckDashboardAdminService,
    DashboardNameConflictError,
    SensorNotFoundError,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import CheckDashboard, Sensor, Tenant
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

_ASSERTION: dict[str, Any] = {
    "select": {"path": "$.count"},
    "compare": {"type": "threshold", "op": "lt", "critical": 10},
}


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
        if (
            await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        ).scalar_one_or_none() is None:
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
            await session.commit()


async def _seed_sensor(
    *,
    tenant_id: UUID = _TENANT_A,
    name: str,
    last_state: str = "ok",
    severity: str = "critical",
    for_seconds: int = 0,
    status: str = "active",
    state_since: datetime | None = None,
    last_evaluated_at: datetime | None = None,
    next_fire_at: datetime | None = None,
    last_value: Any = None,
    last_evidence: dict[str, object] | None = None,
) -> UUID:
    """Insert a Sensor row directly with an explicit latest-state projection."""
    now = datetime.now(UTC)
    sensor_id = uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            Sensor(
                id=sensor_id,
                tenant_id=tenant_id,
                name=name,
                connector_id="vmware-rest-9.0",
                op_id="vmware.vm.list",
                target=None,
                params={},
                assertion=_ASSERTION,
                status=status,
                cadence_kind="interval",
                interval_seconds=60,
                cron_expr=None,
                timezone="UTC",
                next_fire_at=next_fire_at
                if next_fire_at is not None
                else now + timedelta(seconds=60),
                severity=severity,
                for_seconds=for_seconds,
                last_state=last_state,
                last_value=last_value,
                last_evidence=last_evidence,
                last_evaluated_at=(
                    last_evaluated_at
                    if last_evaluated_at is not None
                    else now - timedelta(seconds=30)
                ),
                state_since=state_since if state_since is not None else now - timedelta(hours=1),
                identity_sub="__sensor__",
                created_by_sub="op-admin",
            )
        )
        await session.commit()
    return sensor_id


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_create_folds_member_states() -> None:
    """A dashboard's rollup is the worst of its seeded members on read."""
    await _seed_tenant(_TENANT_A, "tenant-a")
    ok = await _seed_sensor(name="ok-sensor", last_state="ok")
    crit = await _seed_sensor(name="crit-sensor", last_state="critical")
    service = CheckDashboardAdminService()
    detail = await service.create(
        tenant_id=_TENANT_A,
        created_by_sub="op-admin",
        payload=DashboardCreate(name="prod", sensor_ids=[ok, crit]),
    )
    assert detail.state == "critical"
    assert detail.member_count == 2
    assert {m.sensor_id for m in detail.members} == {ok, crit}


@pytest.mark.asyncio
async def test_service_create_rejects_foreign_sensor() -> None:
    """A sensor id absent from the tenant raises SensorNotFoundError."""
    await _seed_tenant(_TENANT_A, "tenant-a")
    real = await _seed_sensor(name="real")
    service = CheckDashboardAdminService()
    ghost = uuid4()
    with pytest.raises(SensorNotFoundError) as exc:
        await service.create(
            tenant_id=_TENANT_A,
            created_by_sub="op-admin",
            payload=DashboardCreate(name="mixed", sensor_ids=[real, ghost]),
        )
    assert ghost in exc.value.missing


@pytest.mark.asyncio
async def test_service_create_dedups_membership() -> None:
    """A body listing the same sensor twice does not trip the composite PK."""
    await _seed_tenant(_TENANT_A, "tenant-a")
    sid = await _seed_sensor(name="solo")
    service = CheckDashboardAdminService()
    detail = await service.create(
        tenant_id=_TENANT_A,
        created_by_sub="op-admin",
        payload=DashboardCreate(name="dupe-member", sensor_ids=[sid, sid]),
    )
    assert detail.member_count == 1


@pytest.mark.asyncio
async def test_service_create_duplicate_name_conflicts() -> None:
    """A duplicate dashboard name in one tenant raises DashboardNameConflictError."""
    await _seed_tenant(_TENANT_A, "tenant-a")
    service = CheckDashboardAdminService()
    await service.create(
        tenant_id=_TENANT_A, created_by_sub="op-admin", payload=DashboardCreate(name="dupe")
    )
    with pytest.raises(DashboardNameConflictError):
        await service.create(
            tenant_id=_TENANT_A, created_by_sub="op-admin", payload=DashboardCreate(name="dupe")
        )


@pytest.mark.asyncio
async def test_service_empty_dashboard_rolls_up_unknown() -> None:
    """A member-less dashboard rolls up to unknown."""
    await _seed_tenant(_TENANT_A, "tenant-a")
    service = CheckDashboardAdminService()
    detail = await service.create(
        tenant_id=_TENANT_A, created_by_sub="op-admin", payload=DashboardCreate(name="empty")
    )
    assert detail.state == "unknown"
    assert detail.member_count == 0


@pytest.mark.asyncio
async def test_service_delete_removes_dashboard_and_memberships() -> None:
    """Delete removes the dashboard row and its membership rows."""
    await _seed_tenant(_TENANT_A, "tenant-a")
    sid = await _seed_sensor(name="member")
    service = CheckDashboardAdminService()
    detail = await service.create(
        tenant_id=_TENANT_A,
        created_by_sub="op-admin",
        payload=DashboardCreate(name="doomed", sensor_ids=[sid]),
    )
    assert await service.delete(_TENANT_A, detail.id) is True
    assert await service.get(_TENANT_A, detail.id) is None
    # No orphan membership rows.
    from meho_backplane.db.models import CheckDashboardSensor

    async with get_sessionmaker()() as session:
        remaining = (
            await session.execute(
                select(CheckDashboardSensor).where(CheckDashboardSensor.dashboard_id == detail.id)
            )
        ).all()
    assert remaining == []
    # Repeat delete is a no-op.
    assert await service.delete(_TENANT_A, detail.id) is False


@pytest.mark.asyncio
async def test_service_tenant_boundary() -> None:
    """Tenant B cannot see or delete tenant A's dashboard."""
    await _seed_tenant(_TENANT_A, "tenant-a")
    await _seed_tenant(_TENANT_B, "tenant-b")
    service = CheckDashboardAdminService()
    detail = await service.create(
        tenant_id=_TENANT_A, created_by_sub="op-admin", payload=DashboardCreate(name="a-only")
    )
    assert await service.get(_TENANT_B, detail.id) is None
    assert [d.id for d in await service.list_(_TENANT_B)] == []
    assert await service.delete(_TENANT_B, detail.id) is False
    assert await service.get(_TENANT_A, detail.id) is not None


# ---------------------------------------------------------------------------
# REST surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_create_list_delete_round_trip(client: TestClient) -> None:
    """POST creates -> GET lists (with state) -> DELETE removes -> detail 404s."""
    await _seed_tenant(_TENANT_A, "tenant-a")
    sid = await _seed_sensor(name="disk", last_state="ok")
    key = make_rsa_keypair("kid-rest")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        created = client.post(
            "/api/v1/checks/dashboards",
            json={"name": "prod-health", "sensor_ids": [str(sid)]},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        dashboard_id = body["id"]
        assert body["state"] == "ok"
        assert body["member_count"] == 1
        assert body["created_by_sub"] == "op-admin"

        listed = client.get("/api/v1/checks/dashboards", headers=headers)
        assert listed.status_code == 200
        rows = listed.json()["dashboards"]
        assert [d["id"] for d in rows] == [dashboard_id]
        assert rows[0]["state"] == "ok"

        deleted = client.delete(f"/api/v1/checks/dashboards/{dashboard_id}", headers=headers)
        assert deleted.status_code == 204
        # Hard delete: detail then 404s and the delete repeat is 404.
        assert (
            client.get(f"/api/v1/checks/dashboards/{dashboard_id}", headers=headers).status_code
            == 404
        )
        assert (
            client.delete(f"/api/v1/checks/dashboards/{dashboard_id}", headers=headers).status_code
            == 404
        )


@pytest.mark.asyncio
async def test_rest_create_foreign_sensor_returns_422(client: TestClient) -> None:
    """A foreign / absent sensor id in the body is 422 ``sensor_not_found``."""
    await _seed_tenant(_TENANT_A, "tenant-a")
    key = make_rsa_keypair("kid-foreign")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        result = client.post(
            "/api/v1/checks/dashboards",
            json={"name": "ghost", "sensor_ids": [str(uuid4())]},
            headers=headers,
        )
        assert result.status_code == 422, result.text
        assert result.json()["detail"] == "sensor_not_found"


@pytest.mark.asyncio
async def test_rest_operator_can_list_but_not_create(client: TestClient) -> None:
    """An operator lists / gets but is 403 on create / delete."""
    await _seed_tenant(_TENANT_A, "tenant-a")
    op_key = make_rsa_keypair("kid-op")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(op_key))
        op_headers = {
            "Authorization": f"Bearer {_token(op_key, sub='op-user', role=TenantRole.OPERATOR)}"
        }
        assert client.get("/api/v1/checks/dashboards", headers=op_headers).status_code == 200
        rejected = client.post(
            "/api/v1/checks/dashboards",
            json={"name": "op-made", "sensor_ids": []},
            headers=op_headers,
        )
        assert rejected.status_code == 403
        assert (
            client.delete(f"/api/v1/checks/dashboards/{uuid4()}", headers=op_headers).status_code
            == 403
        )


@pytest.mark.asyncio
async def test_rest_cross_tenant_dashboard_id_is_404(client: TestClient) -> None:
    """Tenant B probing tenant A's dashboard id gets 404 (no existence leak)."""
    await _seed_tenant(_TENANT_A, "tenant-a")
    await _seed_tenant(_TENANT_B, "tenant-b")
    service = CheckDashboardAdminService()
    detail = await service.create(
        tenant_id=_TENANT_A, created_by_sub="op-admin", payload=DashboardCreate(name="a-secret")
    )
    b_key = make_rsa_keypair("kid-b")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(b_key))
        b_headers = {"Authorization": f"Bearer {_token(b_key, sub='b-admin', tenant_id=_TENANT_B)}"}
        got = client.get(f"/api/v1/checks/dashboards/{detail.id}", headers=b_headers)
        assert got.status_code == 404
        assert got.json()["detail"] == "dashboard_not_found"
        # And B's list does not include A's dashboard.
        assert client.get("/api/v1/checks/dashboards", headers=b_headers).json()["dashboards"] == []


@pytest.mark.asyncio
async def test_rest_detail_carries_state_and_member_fields(client: TestClient) -> None:
    """The detail response carries the rollup state + per-member fields."""
    await _seed_tenant(_TENANT_A, "tenant-a")
    sid = await _seed_sensor(
        name="degraded-sensor",
        last_state="degraded",
        severity="critical",
        last_value=42,
        last_evidence={"observed": 42},
    )
    key = make_rsa_keypair("kid-detail")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        created = client.post(
            "/api/v1/checks/dashboards",
            json={"name": "detail-dash", "sensor_ids": [str(sid)]},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        dashboard_id = created.json()["id"]
        got = client.get(f"/api/v1/checks/dashboards/{dashboard_id}", headers=headers)
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["state"] == "degraded"
        assert len(body["members"]) == 1
        member = body["members"][0]
        assert member["sensor_id"] == str(sid)
        assert member["raw_state"] == "degraded"
        assert member["effective_state"] == "degraded"
        assert member["pending"] is False
        assert member["severity"] == "critical"
        assert member["for_seconds"] == 0
        assert member["state_since"] is not None
        assert member["last_value"] == 42
        assert member["last_evidence"] == {"observed": 42}


@pytest.mark.asyncio
async def test_rest_dod_hysteresis_wire_through(client: TestClient) -> None:
    """DoD: a held critical member -> state critical; a fresh one -> ok + pending."""
    await _seed_tenant(_TENANT_A, "tenant-a")
    now = datetime.now(UTC)
    held = await _seed_sensor(
        name="held-crit",
        last_state="critical",
        for_seconds=300,
        state_since=now - timedelta(seconds=600),  # >= for_seconds -> fires
    )
    fresh = await _seed_sensor(
        name="fresh-crit",
        last_state="critical",
        for_seconds=300,
        state_since=now - timedelta(seconds=10),  # < for_seconds -> pending
    )
    key = make_rsa_keypair("kid-dod")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        held_dash = client.post(
            "/api/v1/checks/dashboards",
            json={"name": "held", "sensor_ids": [str(held)]},
            headers=headers,
        ).json()
        assert held_dash["state"] == "critical"

        fresh_resp = client.post(
            "/api/v1/checks/dashboards",
            json={"name": "fresh", "sensor_ids": [str(fresh)]},
            headers=headers,
        )
        fresh_dash = fresh_resp.json()
        assert fresh_dash["state"] == "ok"
        assert fresh_dash["members"][0]["pending"] is True


@pytest.mark.asyncio
async def test_rest_last_rollup_state_stays_null(client: TestClient) -> None:
    """This Task never writes ``last_rollup_state``: it stays NULL after reads."""
    await _seed_tenant(_TENANT_A, "tenant-a")
    sid = await _seed_sensor(name="memo-sensor", last_state="critical")
    key = make_rsa_keypair("kid-memo")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        created = client.post(
            "/api/v1/checks/dashboards",
            json={"name": "memo", "sensor_ids": [str(sid)]},
            headers=headers,
        )
        dashboard_id = created.json()["id"]
        # The read surfaces last_rollup_state and it is NULL.
        assert created.json()["last_rollup_state"] is None
        client.get("/api/v1/checks/dashboards", headers=headers)
        client.get(f"/api/v1/checks/dashboards/{dashboard_id}", headers=headers)
    # And the column in the DB was never written by the read path.
    async with get_sessionmaker()() as session:
        row = await session.get(CheckDashboard, UUID(dashboard_id))
        assert row is not None
        assert row.last_rollup_state is None
