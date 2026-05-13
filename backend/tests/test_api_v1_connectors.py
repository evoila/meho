# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for POST /api/v1/connectors/{product}/{op_id} — G0.2-T6 acceptance.

Coverage per Task #245 acceptance criteria:

* Happy path: vault.kv.read → 200 with OperationResult body.
* Unknown product → 404 with structured error detail.
* Unknown op → 400 with known_ops list in error detail.
* Unauthenticated request → 401.
* Missing / empty ``path`` param → 200 with error-status OperationResult
  (the connector, not the route, owns param validation — HTTP status stays 200).
* Login failure (Vault unreachable) → 200 with OperationResult status="error".

Test strategy: mirrors test_api_v1_health.py — monkeypatches
``meho_backplane.auth.vault._build_client`` to avoid any real Vault
container; Keycloak is mocked via respx + RSA fixture key.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.connectors.registry import clear_registry, register_connector
from meho_backplane.connectors.vault.connector import VaultConnector
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks
from ._vault_fakes import install_fake_client

# ---------------------------------------------------------------------------
# Settings + JWKS-cache fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_jwks_cache() -> Iterator[None]:
    clear_jwks_cache()
    yield
    clear_jwks_cache()


@pytest.fixture(autouse=True)
def _registry_with_vault() -> Iterator[None]:
    """Ensure VaultConnector is the only registered connector for each test."""
    clear_registry()
    register_connector("vault", VaultConnector)
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def keypair() -> Any:
    return _make_rsa_keypair("test-kid-connectors")


@pytest.fixture()
def jwt(keypair: Any) -> str:
    return _mint_token(keypair)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


_VAULT_KV_READ_URL = "/api/v1/connectors/vault/kv.read"
_VALID_BODY = {"target": "vault-test", "params": {"path": "meho/test/federation"}}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_vault_kv_read_happy_path(
    client: TestClient,
    keypair: Any,
    jwt: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: vault.kv.read returns 200 with OperationResult."""
    fake = install_fake_client(
        monkeypatch,
        secret={"api_key": "s3cr3t"},
        kv_version=42,
    )
    with respx.mock:
        _mock_discovery_and_jwks(respx.mock, _public_jwks(keypair))
        resp = client.post(_VAULT_KV_READ_URL, json=_VALID_BODY, headers=_auth_headers(jwt))

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["op_id"] == "vault.kv.read"
    assert body["result"] == {"api_key": "s3cr3t"}
    assert body["extras"]["version"] == 42
    _ = fake  # referenced to confirm the fake was installed


def test_unknown_product_returns_404(
    client: TestClient,
    keypair: Any,
    jwt: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_client(monkeypatch)
    with respx.mock:
        _mock_discovery_and_jwks(respx.mock, _public_jwks(keypair))
        resp = client.post(
            "/api/v1/connectors/nonexistent-product/kv.read",
            json=_VALID_BODY,
            headers=_auth_headers(jwt),
        )
    assert resp.status_code == 404
    assert "unknown product" in resp.json()["detail"]


def test_unknown_op_returns_400_with_known_ops(
    client: TestClient,
    keypair: Any,
    jwt: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_client(monkeypatch)
    with respx.mock:
        _mock_discovery_and_jwks(respx.mock, _public_jwks(keypair))
        resp = client.post(
            "/api/v1/connectors/vault/unknown.op",
            json=_VALID_BODY,
            headers=_auth_headers(jwt),
        )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["error"] == "unknown_op"
    assert "vault.kv.read" in detail["known_ops"]


def test_unauthenticated_returns_401(client: TestClient) -> None:
    resp = client.post(_VAULT_KV_READ_URL, json=_VALID_BODY)
    assert resp.status_code == 401


@pytest.mark.parametrize("params", [{}, {"path": ""}])
def test_invalid_path_param_returns_200_with_error_status(
    client: TestClient,
    keypair: Any,
    jwt: str,
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, str],
) -> None:
    """Missing or empty path: connector validates and returns error status; route stays 200."""
    install_fake_client(monkeypatch)
    with respx.mock:
        _mock_discovery_and_jwks(respx.mock, _public_jwks(keypair))
        resp = client.post(
            _VAULT_KV_READ_URL,
            json={"target": "vault-test", "params": params},
            headers=_auth_headers(jwt),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "path" in (body["error"] or "")


def test_vault_login_failure_returns_200_with_error_status(
    client: TestClient,
    keypair: Any,
    jwt: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vault login failure: connector returns error result; route stays 200."""
    from meho_backplane.auth.vault import VaultClientError

    install_fake_client(monkeypatch, login_exc=VaultClientError("auth failed"))
    with respx.mock:
        _mock_discovery_and_jwks(respx.mock, _public_jwks(keypair))
        resp = client.post(
            _VAULT_KV_READ_URL,
            json=_VALID_BODY,
            headers=_auth_headers(jwt),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert body["extras"]["phase"] == "login"
