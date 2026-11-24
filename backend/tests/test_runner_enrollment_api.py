# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A runner enrolls its own public key with a one-time code.

* ``register`` returns a one-time code; the code is stored only as a hash.
* The code enrolls one key; a second key, an expired code, a wrong code and
  the code of a revoked runner are all refused the same way.
* The runner with the enrolled key can ask for the same answer again.
* A bad key is refused and keeps the code. After a Keycloak failure the code
  works only for the key it accepted, also after its end.
* The code of a runner with another name is refused and stays usable.
* ``revoke`` removes the runner's key from Keycloak.

The Keycloak admin client is replaced by a mock, so no live Keycloak is needed.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.keycloak_admin import KeycloakAdminError
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunnerPrincipal, Tenant
from meho_backplane.main import app
from meho_backplane.runner.key_id import ec_key_id
from meho_backplane.runner.login import (
    LOGIN_FILE_NAME,
    RunnerLoginError,
    load_or_create_private_key,
    open_signed_jwt_login,
    public_jwk,
)
from meho_backplane.runner.settings import RunnerSettings
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks
from ._vault_fakes import install_fake_vault


class _ValidationErrorEntry(BaseModel):
    """One entry of the published 422 answer."""

    loc: list[str | int]
    msg: str
    type: str


class HTTPValidationError(BaseModel):
    """The published 422 answer (the schema in the API file)."""

    detail: list[_ValidationErrorEntry]


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
        first = client.post(
            _ENROLL,
            json={"enrollment_code": code, "public_key": _jwk(tmp_path), "runner_name": "site-b"},
        )
        other_key = _jwk(tmp_path / "other")
        second = client.post(
            _ENROLL,
            json={"enrollment_code": code, "public_key": other_key, "runner_name": "site-b"},
        )
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
    row = await _row("site-b")
    assert row.key_enrolled_at is not None
    assert row.enrolled_key_id == body["key_id"]


@pytest.mark.asyncio
async def test_an_expired_code_is_refused(client: TestClient, tmp_path: Any) -> None:
    code = (await _register(client, "site-c"))["enrollment_code"]
    await _expire_code("site-c")
    factory, kc = _kc_mock()
    with patch("meho_backplane.auth.runner_enrollment.KeycloakAdminClient.from_settings", factory):
        resp = client.post(
            _ENROLL,
            json={"enrollment_code": code, "public_key": _jwk(tmp_path), "runner_name": "site-c"},
        )
    assert resp.status_code == 403
    kc.set_client_signed_jwt_key.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    ["", "nodot", "zz.abc", "0123456789abcdef0123456789abcdef.wrong", "0" * 32 + "." + "x" * 5000],
    ids=["empty", "no-dot", "no-id", "unknown-runner", "too-long"],
)
async def test_a_wrong_code_is_refused(client: TestClient, tmp_path: Any, bad: str) -> None:
    await _register(client, "site-d")
    for key in (_jwk(tmp_path), {"kty": "RSA"}):
        resp = client.post(
            _ENROLL, json={"enrollment_code": bad, "public_key": key, "runner_name": "site-d"}
        )
        assert resp.status_code == 403
        assert resp.json() == {"detail": "enrollment_code_invalid"}


@pytest.mark.asyncio
async def test_the_wrong_secret_of_a_known_runner_is_refused(
    client: TestClient, tmp_path: Any
) -> None:
    code = (await _register(client, "site-d2"))["enrollment_code"]
    bad = code.partition(".")[0] + ".wrong"
    resp = client.post(
        _ENROLL,
        json={"enrollment_code": bad, "public_key": _jwk(tmp_path), "runner_name": "site-d2"},
    )
    assert resp.status_code == 403
    assert (await _row("site-d2")).enrolled_key_id is None


async def _revoke(client: TestClient, name: str) -> AsyncMock:
    key = make_rsa_keypair(f"kid-revoke-{name}")
    tok = mint_token(
        key, sub="op-admin", tenant_role=TenantRole.TENANT_ADMIN.value, tenant_id=str(_TENANT)
    )
    factory, kc = _kc_mock()
    with (
        patch("meho_backplane.auth.runner_principals.KeycloakAdminClient.from_settings", factory),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.delete(
            f"/api/v1/runner-principals/{name}/revoke",
            headers={"Authorization": f"Bearer {tok}"},
        )
    assert resp.status_code == 200, resp.text
    return kc


async def _expire_code(name: str) -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(RunnerPrincipal).where(RunnerPrincipal.name == name))
        ).scalar_one()
        row.enrollment_code_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()


@pytest.mark.asyncio
async def test_the_enrolled_key_gets_the_same_answer_again_and_nothing_changes(
    client: TestClient, tmp_path: Any
) -> None:
    """The answer was lost: the same code and key work again, also after the code's end."""
    code = (await _register(client, "site-i"))["enrollment_code"]
    body = {"enrollment_code": code, "public_key": _jwk(tmp_path), "runner_name": "site-i"}
    factory, kc = _kc_mock()
    with patch("meho_backplane.auth.runner_enrollment.KeycloakAdminClient.from_settings", factory):
        first = client.post(_ENROLL, json=body)
        enrolled_at = (await _row("site-i")).key_enrolled_at
        again = client.post(_ENROLL, json=body)
        await _expire_code("site-i")
        late = client.post(_ENROLL, json=body)
        for other in (_jwk(tmp_path / "other"), {"kty": "RSA"}):
            refused = client.post(
                _ENROLL,
                json={"enrollment_code": code, "public_key": other, "runner_name": "site-i"},
            )
            assert refused.status_code == 403
    assert first.status_code == again.status_code == late.status_code == 200
    assert first.json() == again.json() == late.json()
    kc.set_client_signed_jwt_key.assert_awaited_once()
    assert (await _row("site-i")).key_enrolled_at == enrolled_at


@pytest.mark.asyncio
async def test_a_revoked_runner_does_not_get_the_answer_again(
    client: TestClient, tmp_path: Any
) -> None:
    code = (await _register(client, "site-j"))["enrollment_code"]
    body = {"enrollment_code": code, "public_key": _jwk(tmp_path), "runner_name": "site-j"}
    factory, _kc = _kc_mock()
    with patch("meho_backplane.auth.runner_enrollment.KeycloakAdminClient.from_settings", factory):
        assert client.post(_ENROLL, json=body).status_code == 200
        await _revoke(client, "site-j")
        assert client.post(_ENROLL, json=body).status_code == 403


class _LoseFirstAnswer(httpx.ASGITransport):
    """Central handles the first call, and the answer never reaches the runner."""

    calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await super().handle_async_request(request)
        self.calls += 1
        if self.calls == 1:
            raise httpx.ReadError("connection lost", request=request)
        return response


@pytest.mark.asyncio
async def test_a_runner_that_lost_the_answer_finishes_at_its_next_start(
    client: TestClient, tmp_path: Any
) -> None:
    code = (await _register(client, "site-k"))["enrollment_code"]
    settings = RunnerSettings(
        central_url="https://central.test",
        runner_id="site-k",
        enrollment_code=code,
        key_dir=str(tmp_path / "keys"),
        tick_interval_seconds=1.0,
        spool_dir=str(tmp_path / "spool"),
        spool_max_files=10,
    )
    transport = _LoseFirstAnswer(app=app)
    factory, kc = _kc_mock()
    with patch("meho_backplane.auth.runner_enrollment.KeycloakAdminClient.from_settings", factory):
        with pytest.raises(RunnerLoginError, match="enrollment failed"):
            await open_signed_jwt_login(settings, transport=transport)
        assert (await _row("site-k")).enrolled_key_id is not None
        assert not (tmp_path / "keys" / LOGIN_FILE_NAME).exists()
        await _expire_code("site-k")
        assert await open_signed_jwt_login(settings, transport=transport) is not None
    kc.set_client_signed_jwt_key.assert_awaited_once()
    record = json.loads((tmp_path / "keys" / LOGIN_FILE_NAME).read_text())
    assert record["client_id"] == "runner:site-k"
    assert record["key_id"] == (await _row("site-k")).enrolled_key_id


@pytest.mark.asyncio
async def test_a_bad_key_is_refused_and_keeps_the_code(client: TestClient, tmp_path: Any) -> None:
    code = (await _register(client, "site-e"))["enrollment_code"]
    factory, kc = _kc_mock()
    with patch("meho_backplane.auth.runner_enrollment.KeycloakAdminClient.from_settings", factory):
        with_private = {**_jwk(tmp_path), "d": "AAAA"}
        bad_curve = {"kty": "EC", "crv": "P-384", "x": "AA", "y": "AA"}
        for key in (with_private, bad_curve, {"kty": "RSA", "n": "AQAB", "e": "AQAB"}):
            resp = client.post(
                _ENROLL, json={"enrollment_code": code, "public_key": key, "runner_name": "site-e"}
            )
            assert resp.status_code == 422, resp.text
            (error,) = HTTPValidationError.model_validate(resp.json()).detail
            assert error.loc == ["body", "public_key"]
            assert error.type == "runner_public_key_invalid"
            assert "public key" in error.msg
        ok = client.post(
            _ENROLL,
            json={"enrollment_code": code, "public_key": _jwk(tmp_path), "runner_name": "site-e"},
        )
    assert ok.status_code == 200
    kc.set_client_signed_jwt_key.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_keycloak_failure_keeps_the_code_for_the_first_key_only(
    client: TestClient, tmp_path: Any
) -> None:
    """After a failed Keycloak write, another key is refused and the first one finishes."""
    code = (await _register(client, "site-f"))["enrollment_code"]
    first_key = _jwk(tmp_path)
    body = {"enrollment_code": code, "public_key": first_key, "runner_name": "site-f"}
    factory, kc = _kc_mock()
    kc.set_client_signed_jwt_key = AsyncMock(side_effect=KeycloakAdminError("boom"))
    with patch("meho_backplane.auth.runner_enrollment.KeycloakAdminClient.from_settings", factory):
        failed = client.post(_ENROLL, json=body)
    assert failed.status_code == 502
    row = await _row("site-f")
    assert row.key_enrolled_at is None
    assert row.enrolled_key_id == ec_key_id(first_key)
    await _expire_code("site-f")
    factory2, kc2 = _kc_mock()
    with patch("meho_backplane.auth.runner_enrollment.KeycloakAdminClient.from_settings", factory2):
        other = {**body, "public_key": _jwk(tmp_path / "other")}
        refused = client.post(_ENROLL, json=other)
        retry = client.post(_ENROLL, json=body)
    assert refused.status_code == 403
    assert retry.status_code == 200
    assert retry.json()["key_id"] == ec_key_id(first_key)
    kc2.set_client_signed_jwt_key.assert_awaited_once()
    (jwk,) = kc2.set_client_signed_jwt_key.await_args.kwargs["jwks"]["keys"]
    assert jwk["kid"] == ec_key_id(first_key)
    assert (await _row("site-f")).key_enrolled_at is not None


@pytest.mark.asyncio
async def test_the_code_of_another_runner_is_refused_and_stays_usable(
    client: TestClient, tmp_path: Any
) -> None:
    code = (await _register(client, "site-m"))["enrollment_code"]
    body = {"enrollment_code": code, "public_key": _jwk(tmp_path), "runner_name": "site-n"}
    factory, kc = _kc_mock()
    with patch("meho_backplane.auth.runner_enrollment.KeycloakAdminClient.from_settings", factory):
        wrong = client.post(_ENROLL, json=body)
        assert wrong.status_code == 409
        assert wrong.json() == {"detail": "enrollment_code_for_another_runner"}
        kc.set_client_signed_jwt_key.assert_not_awaited()
        row = await _row("site-m")
        assert row.enrolled_key_id is None and row.key_enrolled_at is None
        other_key = _jwk(tmp_path / "intended")
        right = client.post(
            _ENROLL, json={**body, "public_key": other_key, "runner_name": "site-m"}
        )
    assert right.status_code == 200
    assert right.json()["key_id"] == ec_key_id(other_key)


@pytest.mark.asyncio
async def test_a_runner_with_the_code_of_another_runner_stops_and_changes_nothing(
    client: TestClient, tmp_path: Any
) -> None:
    code = (await _register(client, "site-o"))["enrollment_code"]

    def _settings(name: str, folder: str) -> RunnerSettings:
        return RunnerSettings(
            central_url="https://central.test",
            runner_id=name,
            enrollment_code=code,
            key_dir=str(tmp_path / folder),
            tick_interval_seconds=1.0,
            spool_dir=str(tmp_path / "spool"),
            spool_max_files=10,
        )

    transport = httpx.ASGITransport(app=app)
    factory, kc = _kc_mock()
    with patch("meho_backplane.auth.runner_enrollment.KeycloakAdminClient.from_settings", factory):
        with pytest.raises(RunnerLoginError, match="code is for another runner"):
            await open_signed_jwt_login(_settings("site-p", "wrong"), transport=transport)
        kc.set_client_signed_jwt_key.assert_not_awaited()
        assert (await _row("site-o")).enrolled_key_id is None
        assert await open_signed_jwt_login(_settings("site-o", "right"), transport=transport)
    kc.set_client_signed_jwt_key.assert_awaited_once()


@pytest.mark.asyncio
async def test_revoke_removes_the_key_and_the_code_of_a_revoked_runner_is_refused(
    client: TestClient, tmp_path: Any
) -> None:
    code = (await _register(client, "site-g"))["enrollment_code"]
    factory, _kc = _kc_mock()
    with patch("meho_backplane.auth.runner_enrollment.KeycloakAdminClient.from_settings", factory):
        assert (
            client.post(
                _ENROLL,
                json={
                    "enrollment_code": code,
                    "public_key": _jwk(tmp_path),
                    "runner_name": "site-g",
                },
            ).status_code
            == 200
        )
    kc2 = await _revoke(client, "site-g")
    kc2.disable_client.assert_awaited_once()
    kc2.clear_client_signed_jwt_key.assert_awaited_once_with(_KC_INTERNAL_ID)


@pytest.mark.asyncio
async def test_the_code_of_a_runner_revoked_before_enrolling_is_refused(
    client: TestClient, tmp_path: Any
) -> None:
    code = (await _register(client, "site-h"))["enrollment_code"]
    kc = await _revoke(client, "site-h")
    kc.clear_client_signed_jwt_key.assert_awaited_once_with(_KC_INTERNAL_ID)
    factory2, kc2 = _kc_mock()
    with patch("meho_backplane.auth.runner_enrollment.KeycloakAdminClient.from_settings", factory2):
        resp = client.post(
            _ENROLL,
            json={"enrollment_code": code, "public_key": _jwk(tmp_path), "runner_name": "site-h"},
        )
    assert resp.status_code == 403
    kc2.set_client_signed_jwt_key.assert_not_awaited()
