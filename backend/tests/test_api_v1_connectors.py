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

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector,
    register_connector_v2,
)
from meho_backplane.connectors.vault import (
    VaultConnector,
    register_vault_typed_operations,
)
from meho_backplane.main import app
from meho_backplane.operations import reset_dispatcher_caches
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
def _registry_with_vault(_settings_env: None) -> Iterator[None]:
    """Re-establish VaultConnector + the typed-op descriptor row.

    The legacy ``/api/v1/connectors/{product}/{op_id}`` route looks up
    via :func:`get_connector` (v1 registry), so we keep the v1
    registration in place. Post-G0.6-T-Refactor-Vault, the
    :meth:`VaultConnector.execute` shim delegates to
    :func:`~meho_backplane.operations.dispatch`; the dispatcher's
    natural-key lookup runs against the v2 key ``("vault", "1.x",
    "vault")``, so the test fixture additionally registers the v2
    entry and upserts the typed-op descriptor row (the lifespan's
    ``run_typed_op_registrars`` step is bypassed because the
    ``TestClient(app)`` fixture doesn't enter the lifespan).
    """
    clear_registry()
    register_connector("vault", VaultConnector)
    register_connector_v2(
        product="vault",
        version="1.x",
        impl_id="vault",
        cls=VaultConnector,
    )
    reset_dispatcher_caches()
    stub_embedding_service = AsyncMock()
    stub_embedding_service.encode_one.return_value = [0.1] * 384
    stub_embedding_service.encode.return_value = [[0.1] * 384]
    stub_embedding_service.dimension = 384
    asyncio.run(register_vault_typed_operations(embedding_service=stub_embedding_service))
    yield
    reset_dispatcher_caches()
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
    # Post-G0.6-T-Refactor-Vault the handler returns
    # ``{"data": <secret>, "version": <int|None>}`` and the
    # dispatcher's PassThroughReducer lands that dict as ``result``.
    # The pre-refactor shape ``result == <secret>`` +
    # ``extras["version"] == 42`` is gone -- ``version`` now lives
    # under ``result`` alongside ``data``.
    assert body["result"] == {"data": {"api_key": "s3cr3t"}, "version": 42}
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


def test_unknown_op_returns_400(
    client: TestClient,
    keypair: Any,
    jwt: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown op_id surfaces as 400 with the dispatcher's structured shape.

    Post-G0.6-T-Refactor-Vault, the in-connector ``known_ops`` listing
    moved to the meta-tools (G0.6-T8 #399); the route's
    ``unknown_op`` 400 still fires (the route detects the
    ``"unknown_op:"`` prefix on ``result.error``) but the enumerated
    op list is empty in the detail payload. The pre-G0.6
    ``detail["known_ops"]`` list is now best-resolved via
    ``GET /api/v1/operations`` (T8 #399).
    """
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
    assert detail["op_id"] == "vault.unknown.op"
    assert detail["known_ops"] == []


def test_unauthenticated_returns_401(client: TestClient) -> None:
    resp = client.post(_VAULT_KV_READ_URL, json=_VALID_BODY)
    assert resp.status_code == 401


@pytest.mark.parametrize("params", [{}, {"path": ""}])
def test_invalid_path_param_returns_200_with_invalid_params_error(
    client: TestClient,
    keypair: Any,
    jwt: str,
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, str],
) -> None:
    """Missing or empty path: dispatcher's ``invalid_params`` shape; route stays 200.

    Post-G0.6-T-Refactor-Vault, the dispatcher's
    :class:`Draft202012Validator` validates ``params`` against the
    registered parameter_schema (``"path": {"type": "string",
    "minLength": 1, "pattern": "\\S"}``) before invoking the handler.
    The route still returns 200 with an error-status :class:`OperationResult`;
    the ``error`` string now starts with ``"invalid_params:"`` and
    the structured detail lives in ``extras["validation_errors"]``.
    """
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
    assert (body["error"] or "").startswith("invalid_params:")
    assert body["extras"]["error_code"] == "invalid_params"
    assert isinstance(body["extras"]["validation_errors"], list)
    assert body["extras"]["validation_errors"]


def test_vault_login_failure_returns_200_with_connector_error_status(
    client: TestClient,
    keypair: Any,
    jwt: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vault login failure: dispatcher's ``connector_error`` shape; route stays 200.

    Post-G0.6-T-Refactor-Vault the handler raises the
    :class:`~meho_backplane.auth.vault.VaultClientError` (or a subclass)
    on login failure rather than returning ``status="error"`` with
    ``extras["phase"]="login"``. The dispatcher's ``connector_error``
    branch catches the exception and records the class name in
    ``extras["exception_class"]``; callers distinguish login-phase
    failure from read-phase failure by string-matching the class
    name against the known :class:`VaultClientError` subclasses
    (see :data:`meho_backplane.api.v1.health._VAULT_LOGIN_PHASE_EXCEPTION_CLASSES`).
    """
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
    assert (body["error"] or "").startswith("connector_error:")
    assert body["extras"]["error_code"] == "connector_error"
    assert body["extras"]["exception_class"] == "VaultClientError"
