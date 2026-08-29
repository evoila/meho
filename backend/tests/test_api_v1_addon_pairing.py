# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.addon_pairing` (#3025).

Coverage matrix:

* pair -> 201 with the one-time credentials + negotiated contract; the
  pair / unpair round-trip is audited (``audit_log.payload.op_id``).
* RBAC: ``operator`` may list but not pair / unpair (403 -> tenant_admin).
* contract skew -> 409 naming the direction.
* list returns the ``{items, next_cursor}`` envelope.
* get / unpair of an absent add-on -> 404.
* heartbeat: a ``service`` principal stamps liveness (200); a human
  principal is 403; an unpaired add-on is 404.

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
from sqlalchemy import select

import meho_backplane.audit as _audit_module
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Tenant
from meho_backplane.main import app
from meho_backplane.operations.addon_pairing_contract import BACKPLANE_CONTRACT_VERSION
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks
from ._vault_fakes import install_fake_vault

_TENANT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_KC_INTERNAL_ID = "cc000000-0000-0000-0000-0000000beef0"
_PATCH_TARGET = "meho_backplane.operations.addon_pairing.KeycloakAdminClient.from_settings"


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


def _pair_body(min_backplane: int = BACKPLANE_CONTRACT_VERSION) -> dict[str, object]:
    return {
        "name": "automation",
        "addon_contract_version": BACKPLANE_CONTRACT_VERSION,
        "addon_min_backplane_version": min_backplane,
    }


@pytest.mark.asyncio
async def test_pair_round_trip_is_audited(client: TestClient) -> None:
    await _seed_tenant()
    key = make_rsa_keypair("kid-pair")
    headers = {"Authorization": f"Bearer {_token(key)}"}
    with patch(_PATCH_TARGET, _mock_kc_ok()), respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        created = client.post("/api/v1/addons/pairings", json=_pair_body(), headers=headers)
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["client_id"] == "addon:automation"
        assert body["client_secret"] == "generated-secret"
        assert body["negotiated_contract_version"] == BACKPLANE_CONTRACT_VERSION
        assert body["pairing"]["name"] == "automation"

        deleted = client.delete("/api/v1/addons/pairings/automation", headers=headers)
        assert deleted.status_code == 204, deleted.text

    op_ids = await _audit_op_ids()
    assert "addon.pair" in op_ids
    assert "addon.unpair" in op_ids


@pytest.mark.asyncio
async def test_pair_requires_tenant_admin(client: TestClient) -> None:
    await _seed_tenant()
    key = make_rsa_keypair("kid-op")
    headers = {"Authorization": f"Bearer {_token(key, role=TenantRole.OPERATOR)}"}
    with patch(_PATCH_TARGET, _mock_kc_ok()), respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post("/api/v1/addons/pairings", json=_pair_body(), headers=headers)
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_contract_skew_returns_409(client: TestClient) -> None:
    await _seed_tenant()
    key = make_rsa_keypair("kid-skew")
    headers = {"Authorization": f"Bearer {_token(key)}"}
    with patch(_PATCH_TARGET, _mock_kc_ok()), respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/addons/pairings",
            json=_pair_body(min_backplane=BACKPLANE_CONTRACT_VERSION + 1),
            headers=headers,
        )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "backplane_contract_below_addon_minimum"


@pytest.mark.asyncio
async def test_list_returns_envelope(client: TestClient) -> None:
    await _seed_tenant()
    key = make_rsa_keypair("kid-list")
    headers = {"Authorization": f"Bearer {_token(key)}"}
    with patch(_PATCH_TARGET, _mock_kc_ok()), respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        client.post("/api/v1/addons/pairings", json=_pair_body(), headers=headers)
        listed = client.get("/api/v1/addons/pairings", headers=headers)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert isinstance(body["items"], list)
    assert "next_cursor" in body
    assert body["items"][0]["name"] == "automation"


@pytest.mark.asyncio
async def test_get_absent_is_404(client: TestClient) -> None:
    await _seed_tenant()
    key = make_rsa_keypair("kid-404")
    headers = {"Authorization": f"Bearer {_token(key)}"}
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get("/api/v1/addons/pairings/ghost", headers=headers)
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_unpair_absent_is_404(client: TestClient) -> None:
    await _seed_tenant()
    key = make_rsa_keypair("kid-unp")
    headers = {"Authorization": f"Bearer {_token(key)}"}
    with patch(_PATCH_TARGET, _mock_kc_ok()), respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.delete("/api/v1/addons/pairings/ghost", headers=headers)
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_heartbeat_service_principal_ok_human_403(client: TestClient) -> None:
    await _seed_tenant()
    key = make_rsa_keypair("kid-hb")
    admin_headers = {"Authorization": f"Bearer {_token(key)}"}
    service_headers = {"Authorization": f"Bearer {_service_token(key)}"}
    with patch(_PATCH_TARGET, _mock_kc_ok()), respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        client.post("/api/v1/addons/pairings", json=_pair_body(), headers=admin_headers)

        # A human principal cannot heartbeat.
        human = client.post("/api/v1/addons/pairings/automation/heartbeat", headers=admin_headers)
        assert human.status_code == 403, human.text

        # The paired service principal reports liveness.
        beat = client.post("/api/v1/addons/pairings/automation/heartbeat", headers=service_headers)
        assert beat.status_code == 200, beat.text
        assert beat.json()["last_seen_at"] is not None


@pytest.mark.asyncio
async def test_heartbeat_unpaired_is_404(client: TestClient) -> None:
    await _seed_tenant()
    key = make_rsa_keypair("kid-hb404")
    headers = {"Authorization": f"Bearer {_service_token(key)}"}
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post("/api/v1/addons/pairings/ghost/heartbeat", headers=headers)
    assert resp.status_code == 404, resp.text
