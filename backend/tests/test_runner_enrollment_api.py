# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A runner enrolls its own public key with a one-time code.

* ``register`` returns a one-time code; the code is stored only as a hash.
* The code works once; a second use, an expired code, a wrong code and the
  code of a revoked runner are all refused the same way.
* A bad key is refused and keeps the code; a Keycloak failure keeps the code.
* ``revoke`` removes the runner's key from Keycloak.

The Keycloak admin client is replaced by a mock, so no live Keycloak is needed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.keycloak_admin import KeycloakAdminError
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunnerPrincipal, Tenant
from meho_backplane.main import app
from meho_backplane.runner.login import load_or_create_private_key, public_jwk
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks
from ._vault_fakes import install_fake_vault

_TENANT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_KC_INTERNAL_ID = "cc000000-0000-0000-0000-000000000009"
_ENROLL = "/api/v1/runner-principals/enroll"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
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

    async def _noop_write(identity_ref: str, client_secret: str, *, tenant_id: uuid.UUID) -> str:
        return "secret/data/x"

    monkeypatch.setattr("meho_backplane.auth.runner_principals.write_agent_secret", _noop_write)
    yield TestClient(app)


def _kc_mock() -> tuple[MagicMock, AsyncMock]:
    mock_client = AsyncMock()
    mock_client.create_client = AsyncMock(return_value=_KC_INTERNAL_ID)
    mock_client.get_client_secret = AsyncMock(return_value="generated-secret")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=mock_client), mock_client


async def _seed_tenant() -> None:
    async with get_sessionmaker()() as session:
        existing = await session.execute(select(Tenant).where(Tenant.id == _TENANT))
        if existing.scalar_one_or_none() is None:
            session.add(Tenant(id=_TENANT, slug="tenant-a", name="Tenant A"))
        await session.commit()


async def _row(name: str) -> RunnerPrincipal:
    async with get_sessionmaker()() as session:
        result = await session.execute(select(RunnerPrincipal).where(RunnerPrincipal.name == name))
        return result.scalar_one()


def _jwk(tmp_path: Any) -> dict[str, str]:
    return public_jwk(load_or_create_private_key(str(tmp_path)))


async def _register(client: TestClient, name: str) -> dict[str, Any]:
    await _seed_tenant()
    key = make_rsa_keypair("kid-enroll")
    tok = mint_token(
        key, sub="op-admin", tenant_role=TenantRole.TENANT_ADMIN.value, tenant_id=str(_TENANT)
    )
    factory, _ = _kc_mock()
    with (
        patch("meho_backplane.auth.runner_principals.KeycloakAdminClient.from_settings", factory),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/runner-principals",
            json={"name": name},
            headers={"Authorization": f"Bearer {tok}"},
        )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


@pytest.mark.asyncio
async def test_register_gives_a_one_time_code_and_stores_only_its_hash(client: TestClient) -> None:
    body = await _register(client, "site-a")
    code = body["enrollment_code"]
    assert code.startswith(body["id"].replace("-", ""))
    assert body["enrollment_code_expires_at"]
    row = await _row("site-a")
    assert row.enrollment_code_hash and code not in row.enrollment_code_hash
    assert len(row.enrollment_code_hash) == 64


@pytest.mark.asyncio
async def test_enroll_puts_the_public_key_on_the_client_and_code_works_once(
    client: TestClient, tmp_path: Any
) -> None:
    code = (await _register(client, "site-b"))["enrollment_code"]
    factory, kc = _kc_mock()
    with patch("meho_backplane.auth.runner_enrollment.KeycloakAdminClient.from_settings", factory):
        first = client.post(_ENROLL, json={"enrollment_code": code, "public_key": _jwk(tmp_path)})
        second = client.post(_ENROLL, json={"enrollment_code": code, "public_key": _jwk(tmp_path)})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["client_id"] == "runner:site-b"
    assert body["token_endpoint"].endswith("/protocol/openid-connect/token")
    assert body["signing_alg"] == "ES256"
    kc.set_client_signed_jwt_key.assert_awaited_once()
    args = kc.set_client_signed_jwt_key.await_args
    assert args.args[0] == _KC_INTERNAL_ID
    jwk = args.kwargs["jwks"]["keys"][0]
    assert jwk["kid"] == body["key_id"] and "d" not in jwk
    assert second.status_code == 403
    assert second.json()["detail"] == "enrollment_code_invalid"
    assert (await _row("site-b")).key_enrolled_at is not None


@pytest.mark.asyncio
async def test_an_expired_code_is_refused(client: TestClient, tmp_path: Any) -> None:
    code = (await _register(client, "site-c"))["enrollment_code"]
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(RunnerPrincipal).where(RunnerPrincipal.name == "site-c"))
        ).scalar_one()
        row.enrollment_code_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    factory, kc = _kc_mock()
    with patch("meho_backplane.auth.runner_enrollment.KeycloakAdminClient.from_settings", factory):
        resp = client.post(_ENROLL, json={"enrollment_code": code, "public_key": _jwk(tmp_path)})
    assert resp.status_code == 403
    kc.set_client_signed_jwt_key.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "nodot", "zz.abc", f"{uuid.uuid4().hex}.wrong"])
async def test_a_wrong_code_is_refused(client: TestClient, tmp_path: Any, bad: str) -> None:
    await _register(client, "site-d")
    resp = client.post(_ENROLL, json={"enrollment_code": bad or "x", "public_key": _jwk(tmp_path)})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_a_bad_key_is_refused_and_keeps_the_code(client: TestClient, tmp_path: Any) -> None:
    code = (await _register(client, "site-e"))["enrollment_code"]
    factory, kc = _kc_mock()
    with patch("meho_backplane.auth.runner_enrollment.KeycloakAdminClient.from_settings", factory):
        with_private = {**_jwk(tmp_path), "d": "AAAA"}
        bad_curve = {"kty": "EC", "crv": "P-384", "x": "AA", "y": "AA"}
        for key in (with_private, bad_curve, {"kty": "RSA", "n": "AQAB", "e": "AQAB"}):
            resp = client.post(_ENROLL, json={"enrollment_code": code, "public_key": key})
            assert resp.status_code == 422, resp.text
        ok = client.post(_ENROLL, json={"enrollment_code": code, "public_key": _jwk(tmp_path)})
    assert ok.status_code == 200
    kc.set_client_signed_jwt_key.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_keycloak_failure_keeps_the_code_so_the_runner_can_retry(
    client: TestClient, tmp_path: Any
) -> None:
    code = (await _register(client, "site-f"))["enrollment_code"]
    factory, kc = _kc_mock()
    kc.set_client_signed_jwt_key = AsyncMock(side_effect=KeycloakAdminError("boom"))
    with patch("meho_backplane.auth.runner_enrollment.KeycloakAdminClient.from_settings", factory):
        failed = client.post(_ENROLL, json={"enrollment_code": code, "public_key": _jwk(tmp_path)})
    assert failed.status_code == 502
    assert (await _row("site-f")).key_enrolled_at is None
    factory2, _kc2 = _kc_mock()
    with patch("meho_backplane.auth.runner_enrollment.KeycloakAdminClient.from_settings", factory2):
        retry = client.post(_ENROLL, json={"enrollment_code": code, "public_key": _jwk(tmp_path)})
    assert retry.status_code == 200


@pytest.mark.asyncio
async def test_revoke_removes_the_key_and_the_code_of_a_revoked_runner_is_refused(
    client: TestClient, tmp_path: Any
) -> None:
    code = (await _register(client, "site-g"))["enrollment_code"]
    factory, _kc = _kc_mock()
    with patch("meho_backplane.auth.runner_enrollment.KeycloakAdminClient.from_settings", factory):
        assert (
            client.post(
                _ENROLL, json={"enrollment_code": code, "public_key": _jwk(tmp_path)}
            ).status_code
            == 200
        )
    key = make_rsa_keypair("kid-revoke")
    tok = mint_token(
        key, sub="op-admin", tenant_role=TenantRole.TENANT_ADMIN.value, tenant_id=str(_TENANT)
    )
    factory2, kc2 = _kc_mock()
    with (
        patch("meho_backplane.auth.runner_principals.KeycloakAdminClient.from_settings", factory2),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.delete(
            "/api/v1/runner-principals/site-g/revoke",
            headers={"Authorization": f"Bearer {tok}"},
        )
    assert resp.status_code == 200, resp.text
    kc2.disable_client.assert_awaited_once()
    kc2.clear_client_signed_jwt_key.assert_awaited_once_with(_KC_INTERNAL_ID)


@pytest.mark.asyncio
async def test_the_code_of_a_runner_revoked_before_enrolling_is_refused(
    client: TestClient, tmp_path: Any
) -> None:
    code = (await _register(client, "site-h"))["enrollment_code"]
    key = make_rsa_keypair("kid-revoke-2")
    tok = mint_token(
        key, sub="op-admin", tenant_role=TenantRole.TENANT_ADMIN.value, tenant_id=str(_TENANT)
    )
    factory, _ = _kc_mock()
    with (
        patch("meho_backplane.auth.runner_principals.KeycloakAdminClient.from_settings", factory),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        client.delete(
            "/api/v1/runner-principals/site-h/revoke",
            headers={"Authorization": f"Bearer {tok}"},
        )
    factory2, kc2 = _kc_mock()
    with patch("meho_backplane.auth.runner_enrollment.KeycloakAdminClient.from_settings", factory2):
        resp = client.post(_ENROLL, json={"enrollment_code": code, "public_key": _jwk(tmp_path)})
    assert resp.status_code == 403
    kc2.set_client_signed_jwt_key.assert_not_awaited()
