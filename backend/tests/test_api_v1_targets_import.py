# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration tests for the bulk-import flow (G0.3-T6 / Task #257).

These tests drive the backplane API directly (no CLI), verifying that
POST /api/v1/targets and PATCH /api/v1/targets/{name} behave correctly
for the operations the import tool performs:

* Create a batch of targets → all appear in list / describe.
* Re-import without --update → 409 on duplicate names.
* Re-import with --update (PATCH) → notes and extras updated in place.
* Round-trip: import entry fields match describe response semantically.
* Extras (unknown YAML fields) survive the round-trip as JSONB.
* Missing required fields → 422 from the create endpoint.
* Cross-tenant isolation — imported targets invisible to other tenants.
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
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._oidc_jwt_helpers import ISSUER as _ISSUER

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    clear_registry()
    yield
    clear_registry()


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(targets_router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


def _admin_token(key: Any, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    return mint_token(
        key, sub="admin-1", tenant_role=TenantRole.TENANT_ADMIN.value, tenant_id=tenant_id
    )


def _operator_token(key: Any, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    return mint_token(
        key, sub="op-1", tenant_role=TenantRole.OPERATOR.value, tenant_id=tenant_id
    )


async def _insert_target(**kwargs: Any) -> TargetORM:
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
# Create (POST) — import tool's happy path
# ---------------------------------------------------------------------------


def test_import_create_single_target(client: TestClient) -> None:
    """POST /api/v1/targets with a full import entry returns 201."""
    key = make_rsa_keypair("kid-A")
    payload = {
        "name": "rdc-vcenter",
        "product": "vcenter",
        "host": "vc-dc.evba.lab",
        "port": 443,
        "aliases": ["rdc", "host-vcenter"],
        "secret_ref": "secret/rdc/vsphere",
        "vpn_required": True,
        "auth_model": "shared_service_account",
        "extras": {"sso_realm": "evba.lab"},
        "notes": "Host vCenter for 4 Hetzner hosts.",
    }
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json=payload,
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "rdc-vcenter"
    assert data["product"] == "vcenter"
    assert data["host"] == "vc-dc.evba.lab"
    assert data["port"] == 443
    assert set(data["aliases"]) == {"rdc", "host-vcenter"}
    assert data["secret_ref"] == "secret/rdc/vsphere"
    assert data["vpn_required"] is True
    assert data["extras"]["sso_realm"] == "evba.lab"
    assert data["notes"] == "Host vCenter for 4 Hetzner hosts."


def test_import_create_minimal_target(client: TestClient) -> None:
    """POST with only required fields uses correct defaults."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={"name": "minimal", "product": "ssh", "host": "10.0.0.1",
                  "aliases": [], "extras": {}, "auth_model": "shared_service_account",
                  "vpn_required": False},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 201
    data = response.json()
    assert data["auth_model"] == "shared_service_account"
    assert data["vpn_required"] is False
    assert data["aliases"] == []
    assert data["extras"] == {}
    assert data["port"] is None
    assert data["notes"] is None


async def test_import_batch_creates_all(client: TestClient) -> None:
    """Sequential POSTs for a batch of targets all land in the list endpoint."""
    key = make_rsa_keypair("kid-A")
    targets_to_create = [
        {"name": f"target-{i}", "product": "rke2", "host": f"10.0.0.{i}",
         "aliases": [], "extras": {}, "auth_model": "shared_service_account",
         "vpn_required": False}
        for i in range(1, 6)
    ]
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        for payload in targets_to_create:
            r = client.post(
                "/api/v1/targets",
                json=payload,
                headers={"Authorization": f"Bearer {_admin_token(key)}"},
            )
            assert r.status_code == 201, f"failed creating {payload['name']}: {r.text}"

        # Verify all appear in list
        list_resp = client.get(
            "/api/v1/targets",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert list_resp.status_code == 200
    names = {t["name"] for t in list_resp.json()}
    for i in range(1, 6):
        assert f"target-{i}" in names


# ---------------------------------------------------------------------------
# Duplicate → 409 (default import mode — no --update)
# ---------------------------------------------------------------------------


def test_import_duplicate_name_returns_409(client: TestClient) -> None:
    """Re-importing without --update returns 409 on the second POST."""
    key = make_rsa_keypair("kid-A")
    payload = {"name": "dup-target", "product": "ssh", "host": "10.0.0.1",
               "aliases": [], "extras": {}, "auth_model": "shared_service_account",
               "vpn_required": False}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        r1 = client.post(
            "/api/v1/targets",
            json=payload,
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
        r2 = client.post(
            "/api/v1/targets",
            json=payload,
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert r1.status_code == 201
    assert r2.status_code == 409
    # detail is a plain string from the HTTPException raised in create_target
    assert "already exists" in r2.json()["detail"]


# ---------------------------------------------------------------------------
# Update (PATCH) — import tool's --update path
# ---------------------------------------------------------------------------


async def test_import_update_patches_notes(client: TestClient) -> None:
    """PATCH /api/v1/targets/{name} with updated notes persists the change."""
    await _insert_target(
        tenant_id=uuid.UUID(DEFAULT_TENANT_ID),
        name="rdc-vcenter",
        product="vcenter",
        host="vc-dc.evba.lab",
        notes="original notes",
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.patch(
            "/api/v1/targets/rdc-vcenter",
            json={"notes": "updated notes from import --update"},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 200
    assert response.json()["notes"] == "updated notes from import --update"


async def test_import_update_patches_extras(client: TestClient) -> None:
    """PATCH with updated extras replaces the extras JSONB field."""
    await _insert_target(
        tenant_id=uuid.UUID(DEFAULT_TENANT_ID),
        name="rdc-vcenter",
        product="vcenter",
        host="vc-dc.evba.lab",
        extras={"sso_realm": "old-realm"},
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.patch(
            "/api/v1/targets/rdc-vcenter",
            json={"extras": {"sso_realm": "evba.lab", "new_field": "value"}},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 200
    extras = response.json()["extras"]
    assert extras["sso_realm"] == "evba.lab"
    assert extras["new_field"] == "value"


async def test_import_update_only_supplied_fields(client: TestClient) -> None:
    """PATCH does not overwrite fields absent from the request body."""
    await _insert_target(
        tenant_id=uuid.UUID(DEFAULT_TENANT_ID),
        name="alpha",
        product="rke2",
        host="10.0.0.1",
        port=6443,
        notes="kept notes",
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        # Only update host; port and notes should survive.
        response = client.patch(
            "/api/v1/targets/alpha",
            json={"host": "10.0.0.2"},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["host"] == "10.0.0.2"
    assert data["port"] == 6443
    assert data["notes"] == "kept notes"


# ---------------------------------------------------------------------------
# Round-trip: import entry → describe → fields match semantically
# ---------------------------------------------------------------------------


async def test_import_round_trip_fields_match(client: TestClient) -> None:
    """POST a target then GET it — every field from the import entry matches."""
    key = make_rsa_keypair("kid-A")
    payload = {
        "name": "rdc-vault",
        "product": "vault",
        "host": "vault.evba.lab",
        "port": 8200,
        "aliases": ["vault", "secrets"],
        "secret_ref": "secret/rdc/vault/token",
        "vpn_required": True,
        "auth_model": "shared_service_account",
        "extras": {"namespace": "admin", "account": "vault-sa"},
        "notes": "Production Vault cluster.\nVault 1.17.",
    }
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        create_resp = client.post(
            "/api/v1/targets",
            json=payload,
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
        assert create_resp.status_code == 201

        describe_resp = client.get(
            "/api/v1/targets/rdc-vault",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert describe_resp.status_code == 200
    data = describe_resp.json()

    assert data["name"] == payload["name"]
    assert data["product"] == payload["product"]
    assert data["host"] == payload["host"]
    assert data["port"] == payload["port"]
    assert set(data["aliases"]) == set(payload["aliases"])
    assert data["secret_ref"] == payload["secret_ref"]
    assert data["vpn_required"] == payload["vpn_required"]
    assert data["extras"] == payload["extras"]
    assert data["notes"] == payload["notes"]


# ---------------------------------------------------------------------------
# Extras round-trip — unknown YAML fields survive as JSONB
# ---------------------------------------------------------------------------


async def test_import_extras_preserved_through_patch(client: TestClient) -> None:
    """Extras set on create are preserved after a PATCH that doesn't touch them."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        client.post(
            "/api/v1/targets",
            json={"name": "k8s-prod", "product": "kubernetes", "host": "10.5.50.1",
                  "aliases": [], "extras": {"kubeconfig_field": "kubeconfig",
                                            "impersonate_sa": "meho-sa"},
                  "auth_model": "shared_service_account", "vpn_required": False},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
        # PATCH only notes
        client.patch(
            "/api/v1/targets/k8s-prod",
            json={"notes": "updated"},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
        describe = client.get(
            "/api/v1/targets/k8s-prod",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert describe.status_code == 200
    extras = describe.json()["extras"]
    assert extras["kubeconfig_field"] == "kubeconfig"
    assert extras["impersonate_sa"] == "meho-sa"


# ---------------------------------------------------------------------------
# Missing required fields → 422
# ---------------------------------------------------------------------------


def test_import_missing_name_returns_422(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={"product": "ssh", "host": "10.0.0.1"},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 422


def test_import_missing_host_returns_422(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={"name": "alpha", "product": "ssh"},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


async def test_import_cross_tenant_invisible(client: TestClient) -> None:
    """Targets imported into tenant_a are not visible to tenant_b."""
    tenant_a = DEFAULT_TENANT_ID
    tenant_b = str(uuid.uuid4())

    await _insert_target(
        tenant_id=uuid.UUID(tenant_a),
        name="tenant-a-target",
        product="rke2",
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
    names = [t["name"] for t in response.json()]
    assert "tenant-a-target" not in names


# ---------------------------------------------------------------------------
# Operator role cannot create (only tenant_admin can)
# ---------------------------------------------------------------------------


def test_import_operator_role_cannot_create(client: TestClient) -> None:
    """POST /api/v1/targets requires tenant_admin; operator gets 403."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={"name": "alpha", "product": "ssh", "host": "10.0.0.1"},
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 403
