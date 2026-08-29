# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.addon_capability` (#3026).

Coverage matrix:

* declare (PUT): a paired **service** principal declares its surfaces (200); a
  human principal is 403; an unpaired add-on is 404.
* unknown capability kind -> 422 (rejected loudly); duplicate declaration ->
  422.
* show (GET): an operator reads the declaration + activation state; an
  unpaired add-on is 404.
* activation flips with pairing health: a pairing driven contract-incompatible
  reads ``active=False`` on the read surface.
* declare / show are audited (``addon.capabilities.declare`` /
  ``addon.capabilities.show``).

Keycloak admin is monkey-patched so tests need no live Keycloak.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select, update

import meho_backplane.audit as _audit_module
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AddonPairing, AuditLog, Tenant
from meho_backplane.main import app
from meho_backplane.operations.addon_pairing_contract import BACKPLANE_CONTRACT_VERSION
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks
from ._vault_fakes import install_fake_vault

_TENANT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_KC_INTERNAL_ID = "cc000000-0000-0000-0000-0000000cafe0"
_PATCH_TARGET = "meho_backplane.operations.addon_pairing.KeycloakAdminClient.from_settings"
_CAPS_URL = "/api/v1/addons/pairings/automation/capabilities"


@pytest.fixture(autouse=True)
def _noop_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(*_a: object, **_kw: object) -> None:
        pass

    monkeypatch.setattr(_audit_module, "publish_event", _noop)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("KEYCLOAK_ADMIN_URL", "https://keycloak.test/admin/realms/meho")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "meho-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "s3cr3t")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


def _token(
    key: Any,
    *,
    sub: str = "op-admin",
    role: TenantRole = TenantRole.TENANT_ADMIN,
    principal_kind: str | None = None,
) -> str:
    return mint_token(
        key,
        sub=sub,
        tenant_role=role.value,
        tenant_id=str(_TENANT),
        **({"principal_kind": principal_kind} if principal_kind else {}),
    )


def _service_token(key: Any) -> str:
    """A paired add-on's ``principal_kind=service`` / ``read_only`` token."""
    return _token(key, sub="addon-svc", role=TenantRole.READ_ONLY, principal_kind="service")


def _mock_kc_ok() -> MagicMock:
    mock_client = AsyncMock()
    mock_client.create_client = AsyncMock(return_value=_KC_INTERNAL_ID)
    mock_client.get_client_secret = AsyncMock(return_value="generated-secret")
    mock_client.delete_client = AsyncMock(return_value=None)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=mock_client)


async def _seed_tenant() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if (
            await session.execute(select(Tenant).where(Tenant.id == _TENANT))
        ).scalar_one_or_none() is None:
            session.add(Tenant(id=_TENANT, slug="tenant-a", name="Tenant A"))
            await session.commit()


async def _audit_op_ids() -> list[str]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    return [r.payload.get("op_id") for r in rows if isinstance(r.payload, dict)]


def _pair_body() -> dict[str, object]:
    return {
        "name": "automation",
        "addon_contract_version": BACKPLANE_CONTRACT_VERSION,
        "addon_min_backplane_version": BACKPLANE_CONTRACT_VERSION,
    }


def _decl_body() -> dict[str, object]:
    return {
        "capabilities": [
            {"kind": "meta_tool_family", "name": "inventory", "display_label": "Inventory"},
            {"kind": "event_kind", "name": "run.step.completed"},
        ]
    }


@pytest.mark.asyncio
async def test_declare_service_principal_ok_and_audited(client: TestClient) -> None:
    await _seed_tenant()
    key = make_rsa_keypair("kid-decl")
    admin = {"Authorization": f"Bearer {_token(key)}"}
    service = {"Authorization": f"Bearer {_service_token(key)}"}
    with patch(_PATCH_TARGET, _mock_kc_ok()), respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        client.post("/api/v1/addons/pairings", json=_pair_body(), headers=admin)
        resp = client.put(_CAPS_URL, json=_decl_body(), headers=service)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["addon"] == "automation"
    assert body["active"] is True
    assert body["declared_contract_version"] == BACKPLANE_CONTRACT_VERSION
    assert {(c["kind"], c["name"]) for c in body["capabilities"]} == {
        ("meta_tool_family", "inventory"),
        ("event_kind", "run.step.completed"),
    }
    assert "addon.capabilities.declare" in await _audit_op_ids()


@pytest.mark.asyncio
async def test_declare_human_principal_is_403(client: TestClient) -> None:
    await _seed_tenant()
    key = make_rsa_keypair("kid-decl-human")
    admin = {"Authorization": f"Bearer {_token(key)}"}
    with patch(_PATCH_TARGET, _mock_kc_ok()), respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        client.post("/api/v1/addons/pairings", json=_pair_body(), headers=admin)
        resp = client.put(_CAPS_URL, json=_decl_body(), headers=admin)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "capabilities_declare_requires_service_principal"


@pytest.mark.asyncio
async def test_declare_unknown_kind_is_422(client: TestClient) -> None:
    await _seed_tenant()
    key = make_rsa_keypair("kid-decl-bad")
    service = {"Authorization": f"Bearer {_service_token(key)}"}
    with patch(_PATCH_TARGET, _mock_kc_ok()), respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        client.post(
            "/api/v1/addons/pairings",
            json=_pair_body(),
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
        resp = client.put(
            _CAPS_URL,
            json={"capabilities": [{"kind": "wormhole", "name": "x"}]},
            headers=service,
        )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_declare_duplicate_capability_is_422(client: TestClient) -> None:
    await _seed_tenant()
    key = make_rsa_keypair("kid-decl-dup")
    service = {"Authorization": f"Bearer {_service_token(key)}"}
    with patch(_PATCH_TARGET, _mock_kc_ok()), respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        client.post(
            "/api/v1/addons/pairings",
            json=_pair_body(),
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
        resp = client.put(
            _CAPS_URL,
            json={
                "capabilities": [
                    {"kind": "event_kind", "name": "dup"},
                    {"kind": "event_kind", "name": "dup"},
                ]
            },
            headers=service,
        )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_declare_unpaired_is_404(client: TestClient) -> None:
    await _seed_tenant()
    key = make_rsa_keypair("kid-decl-404")
    service = {"Authorization": f"Bearer {_service_token(key)}"}
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.put(
            "/api/v1/addons/pairings/ghost/capabilities",
            json=_decl_body(),
            headers=service,
        )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "addon_not_paired"


@pytest.mark.asyncio
async def test_show_reflects_declaration_and_is_audited(client: TestClient) -> None:
    await _seed_tenant()
    key = make_rsa_keypair("kid-show")
    admin = {"Authorization": f"Bearer {_token(key)}"}
    service = {"Authorization": f"Bearer {_service_token(key)}"}
    with patch(_PATCH_TARGET, _mock_kc_ok()), respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        client.post("/api/v1/addons/pairings", json=_pair_body(), headers=admin)
        client.put(_CAPS_URL, json=_decl_body(), headers=service)
        resp = client.get(
            _CAPS_URL, headers={"Authorization": f"Bearer {_token(key, role=TenantRole.OPERATOR)}"}
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active"] is True
    assert len(body["capabilities"]) == 2
    assert "addon.capabilities.show" in await _audit_op_ids()


@pytest.mark.asyncio
async def test_show_unpaired_is_404(client: TestClient) -> None:
    await _seed_tenant()
    key = make_rsa_keypair("kid-show-404")
    headers = {"Authorization": f"Bearer {_token(key)}"}
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get("/api/v1/addons/pairings/ghost/capabilities", headers=headers)
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_show_activation_flips_with_pairing_health(client: TestClient) -> None:
    await _seed_tenant()
    key = make_rsa_keypair("kid-flip")
    admin = {"Authorization": f"Bearer {_token(key)}"}
    service = {"Authorization": f"Bearer {_service_token(key)}"}
    with patch(_PATCH_TARGET, _mock_kc_ok()), respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        client.post("/api/v1/addons/pairings", json=_pair_body(), headers=admin)
        client.put(_CAPS_URL, json=_decl_body(), headers=service)

        healthy = client.get(_CAPS_URL, headers=admin)
        assert healthy.json()["active"] is True

        # Drive the pairing contract-incompatible (backplane now too old for
        # the add-on's declared floor); the declaration persists but deactivates.
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            await session.execute(
                update(AddonPairing)
                .where(AddonPairing.tenant_id == _TENANT, AddonPairing.name == "automation")
                .values(addon_min_backplane_version=BACKPLANE_CONTRACT_VERSION + 1)
            )
            await session.commit()

        unhealthy = client.get(_CAPS_URL, headers=admin)
    assert unhealthy.status_code == 200, unhealthy.text
    body = unhealthy.json()
    assert body["active"] is False
    assert len(body["capabilities"]) == 2
