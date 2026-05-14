# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.targets`.

Coverage matrix (G0.3-T3 / Task #254 acceptance criteria):

* **List** — empty tenant returns []; product filter; keyset cursor.
* **Describe** — exact name; alias match (via resolve_target); 404 with
  near-misses.
* **Probe** — no connector registered → 501; target not found → 404.
* **Create** — 201 with full Target body; duplicate name → 409;
  operator role (not tenant_admin) → 403.
* **Update** — partial PATCH applies only supplied fields; operator
  role → 403.
* **Cross-tenant** — a target in tenant_a is invisible to a JWT scoped
  to tenant_b.
* **Unauthenticated** — every route returns 401 without a Bearer header.
* **Audit contextvar** — ``audit_target_id`` is bound after resolve_target
  succeeds; the AuditLog row's payload carries it on the next T4 pass.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.api.v1.targets import router as targets_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.connectors.schemas import ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Settings + JWKS cache fixtures
# ---------------------------------------------------------------------------
from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._oidc_jwt_helpers import ISSUER as _ISSUER


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
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    clear_jwks_cache()
    yield
    clear_jwks_cache()


@pytest.fixture(autouse=True)
def _empty_connector_registry() -> Iterator[None]:
    """Ensure the connector registry is empty for each test."""
    clear_registry()
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(targets_router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def _operator_token(key: Any, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    return mint_token(key, sub="op-1", tenant_role=TenantRole.OPERATOR.value, tenant_id=tenant_id)


def _admin_token(key: Any, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    return mint_token(
        key, sub="admin-1", tenant_role=TenantRole.TENANT_ADMIN.value, tenant_id=tenant_id
    )


# ---------------------------------------------------------------------------
# DB helpers — direct inserts without going through the API
# ---------------------------------------------------------------------------


async def _insert_target(**kwargs: Any) -> TargetORM:
    """Insert a TargetORM row directly via the test sessionmaker."""
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.UUID(DEFAULT_TENANT_ID),
        "name": "default-target",
        "product": "ssh",
        "host": "10.0.0.1",
        "aliases": [],
        "vpn_required": False,
        "auth_model": "shared_service_account",
        "extras": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(kwargs)
    t = TargetORM(**defaults)
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(t)
        await session.commit()
    return t


# ---------------------------------------------------------------------------
# GET /api/v1/targets — list
# ---------------------------------------------------------------------------


def test_list_targets_empty_for_new_tenant(client: TestClient) -> None:
    """A tenant with no targets returns an empty list."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/targets",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_list_targets_returns_targets(client: TestClient) -> None:
    """Targets belonging to the tenant are returned as TargetSummary list."""
    tenant_id = DEFAULT_TENANT_ID
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id),
        name="rdc-vcenter",
        product="vsphere",
        host="10.1.0.1",
    )
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id),
        name="rdc-vault",
        product="vault",
        host="10.1.0.2",
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/targets",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert response.status_code == 200
    names = {t["name"] for t in response.json()}
    assert "rdc-vcenter" in names
    assert "rdc-vault" in names


@pytest.mark.asyncio
async def test_list_targets_product_filter(client: TestClient) -> None:
    """``?product=vsphere`` returns only vsphere targets."""
    tenant_id = DEFAULT_TENANT_ID
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id), name="vc1", product="vsphere", host="10.1.0.1"
    )
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id), name="v1", product="vault", host="10.1.0.2"
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/targets?product=vsphere",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["product"] == "vsphere"


@pytest.mark.asyncio
async def test_list_targets_cursor_pagination(client: TestClient) -> None:
    """Cursor-based pagination returns only targets after the cursor name."""
    tenant_id = DEFAULT_TENANT_ID
    for name in ["alpha", "beta", "gamma"]:
        await _insert_target(
            tenant_id=uuid.UUID(tenant_id), name=name, product="ssh", host="10.0.0.1"
        )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/targets?cursor=beta",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert response.status_code == 200
    names = [t["name"] for t in response.json()]
    assert names == ["gamma"]


# ---------------------------------------------------------------------------
# GET /api/v1/targets/{name} — describe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_target_exact_name(client: TestClient) -> None:
    """Exact name match returns the full Target document."""
    tenant_id = DEFAULT_TENANT_ID
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id),
        name="rdc-vcenter",
        product="vsphere",
        host="vcenter.corp.internal",
        port=443,
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/targets/rdc-vcenter",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "rdc-vcenter"
    assert data["product"] == "vsphere"
    assert data["port"] == 443
    # Full Target must carry tenant_id
    assert "tenant_id" in data


@pytest.mark.asyncio
async def test_describe_target_alias_match(client: TestClient) -> None:
    """Alias match (element-equality) returns the same target."""
    tenant_id = DEFAULT_TENANT_ID
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id),
        name="rdc-vcenter",
        product="vsphere",
        host="10.1.0.1",
        aliases=["vcenter", "vc.corp.internal"],
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/targets/vcenter",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert response.status_code == 200
    assert response.json()["name"] == "rdc-vcenter"


def test_describe_target_not_found_returns_404(client: TestClient) -> None:
    """No match returns 404 with ``error=no_target``."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/targets/nonexistent",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "no_target"


@pytest.mark.asyncio
async def test_describe_target_not_found_includes_near_misses(client: TestClient) -> None:
    """404 detail includes near-misses when the prefix matches."""
    tenant_id = DEFAULT_TENANT_ID
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id), name="rdc-vcenter", product="vsphere", host="10.1.0.1"
    )
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id), name="rdc-vault", product="vault", host="10.1.0.2"
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/targets/rdc-v",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert response.status_code == 404
    detail = response.json()["detail"]
    near_miss_names = {m["name"] for m in detail["matches"]}
    assert "rdc-vcenter" in near_miss_names
    assert "rdc-vault" in near_miss_names


# ---------------------------------------------------------------------------
# POST /api/v1/targets/{name}/probe — probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_no_connector_returns_501(client: TestClient) -> None:
    """No connector registered for the target's product → 501."""
    tenant_id = DEFAULT_TENANT_ID
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id), name="my-nsx", product="nsx", host="10.0.0.1"
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets/my-nsx/probe",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert response.status_code == 501
    assert "nsx" in response.json()["detail"]


def test_probe_target_not_found_returns_404(client: TestClient) -> None:
    """Probe on a non-existent target returns 404 via resolve_target."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets/nonexistent/probe",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_probe_invokes_connector(client: TestClient) -> None:
    """When a connector is registered, probe returns its ProbeResult."""
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import register_connector
    from meho_backplane.connectors.schemas import FingerprintResult, OperationResult

    probe_result = ProbeResult(
        ok=True,
        reason="reachable",
        latency_ms=12.0,
        probed_at=datetime.now(UTC),
    )

    class _FakeConnector(Connector):
        product = "vault"

        async def probe(self, target: Any) -> ProbeResult:
            return probe_result

        async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    register_connector("vault", _FakeConnector)

    tenant_id = DEFAULT_TENANT_ID
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id),
        name="prod-vault",
        product="vault",
        host="vault.corp.internal",
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets/prod-vault/probe",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["reason"] == "reachable"


# ---------------------------------------------------------------------------
# POST /api/v1/targets — create
# ---------------------------------------------------------------------------


def test_create_target_returns_201(client: TestClient) -> None:
    """Valid body → 201 with full Target document."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={"name": "new-host", "product": "ssh", "host": "10.0.0.5"},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "new-host"
    assert data["product"] == "ssh"
    assert data["host"] == "10.0.0.5"
    assert "id" in data
    assert "tenant_id" in data
    assert "created_at" in data


def test_create_target_duplicate_returns_409(client: TestClient) -> None:
    """Creating the same target name twice returns 409 on the second call."""
    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        r1 = client.post(
            "/api/v1/targets",
            json={"name": "dup-host", "product": "ssh", "host": "10.0.0.1"},
            headers=headers,
        )
        r2 = client.post(
            "/api/v1/targets",
            json={"name": "dup-host", "product": "ssh", "host": "10.0.0.1"},
            headers=headers,
        )
    assert r1.status_code == 201
    assert r2.status_code == 409


def test_create_target_operator_role_returns_403(client: TestClient) -> None:
    """``operator`` role (not ``tenant_admin``) is rejected with 403."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={"name": "should-fail", "product": "ssh", "host": "10.0.0.1"},
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient_role"}


def test_create_target_with_all_fields(client: TestClient) -> None:
    """Create succeeds with all optional fields populated."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "rdc-vcenter",
                "aliases": ["vcenter", "vc"],
                "product": "vsphere",
                "host": "vcenter.corp.internal",
                "port": 443,
                "fqdn": "vcenter.corp.internal",
                "secret_ref": "secret/meho/vcenter",
                "auth_model": "impersonation",
                "vpn_required": True,
                "extras": {"dc": "fra1"},
                "notes": "Production vCenter",
            },
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 201
    data = response.json()
    assert data["aliases"] == ["vcenter", "vc"]
    assert data["port"] == 443
    assert data["auth_model"] == "impersonation"
    assert data["vpn_required"] is True
    assert data["extras"] == {"dc": "fra1"}


# ---------------------------------------------------------------------------
# PATCH /api/v1/targets/{name} — update
# ---------------------------------------------------------------------------


def test_update_target_partial_fields(client: TestClient) -> None:
    """PATCH applies only supplied fields; unset fields are unchanged."""
    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        # Create first
        client.post(
            "/api/v1/targets",
            json={"name": "patch-target", "product": "ssh", "host": "10.0.0.1", "port": 22},
            headers=headers,
        )
        # Patch only host
        response = client.patch(
            "/api/v1/targets/patch-target",
            json={"host": "new-host.corp.internal"},
            headers=headers,
        )
    assert response.status_code == 200
    data = response.json()
    assert data["host"] == "new-host.corp.internal"
    assert data["port"] == 22  # untouched


def test_update_target_operator_role_returns_403(client: TestClient) -> None:
    """``operator`` role cannot PATCH a target."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.patch(
            "/api/v1/targets/some-target",
            json={"host": "x"},
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_update_target_not_found_returns_404(client: TestClient) -> None:
    """PATCH on a non-existent target returns 404 via resolve_target."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.patch(
            "/api/v1/targets/nonexistent",
            json={"host": "x"},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_isolation(client: TestClient) -> None:
    """A target in tenant_a is invisible to a JWT scoped to tenant_b."""
    tenant_a = str(uuid.uuid4())
    tenant_b = str(uuid.uuid4())
    await _insert_target(
        tenant_id=uuid.UUID(tenant_a),
        name="rdc-vcenter",
        product="vsphere",
        host="10.1.0.1",
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/targets/rdc-vcenter",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_b)}"},
        )
    assert response.status_code == 404
    # Near-misses must be empty — tenant_b has no targets
    assert response.json()["detail"]["matches"] == []


@pytest.mark.asyncio
async def test_list_cross_tenant_isolation(client: TestClient) -> None:
    """List does not expose targets from another tenant."""
    tenant_a = str(uuid.uuid4())
    tenant_b = str(uuid.uuid4())
    await _insert_target(
        tenant_id=uuid.UUID(tenant_a),
        name="tenant-a-target",
        product="ssh",
        host="10.0.0.1",
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/targets",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_b)}"},
        )
    assert response.status_code == 200
    assert response.json() == []


# ---------------------------------------------------------------------------
# Unauthenticated
# ---------------------------------------------------------------------------


def test_list_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.get("/api/v1/targets")
    assert response.status_code == 401


def test_describe_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.get("/api/v1/targets/any-name")
    assert response.status_code == 401


def test_probe_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.post("/api/v1/targets/any-name/probe")
    assert response.status_code == 401


def test_create_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.post("/api/v1/targets", json={"name": "x", "product": "ssh", "host": "h"})
    assert response.status_code == 401


def test_update_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.patch("/api/v1/targets/any-name", json={"host": "x"})
    assert response.status_code == 401
