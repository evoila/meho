# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cross-module failure-mode tests for ``/api/v1/health`` (Task #25 — route half).

This module proves the federation-proof endpoint preserves the
contractual error shapes when *combinations* of dependencies fail. The
single-axis failure modes are exercised in
:mod:`tests.test_auth_failures` (JWT layer) and
:mod:`tests.test_vault_failures` (Vault layer); this file focuses on
the cross-axis matrix the issue body calls out:

* JWT invalid + Vault healthy → 401 (auth runs first; Vault is never
  consulted; the route body never executes).
* JWT valid + Vault unreachable → 200 with structured ``vault.reachable=false``.
  Smoke-test contract: the federation chain reports its own failure
  through the response body, never as a 5xx.
* JWT valid + Vault role-denied → 200 with ``login_failed: VaultRoleDeniedError``
  in ``vault.detail``.
* JWT valid + Vault sealed (read raises 5xx after login) → 200 with
  ``vault.reachable=true``, ``vault.read_ok=false``, ``read_failed: <Cls>``
  in ``vault.detail``.
* JWT valid + secret-read denied (403 on the actual KV read) → 200
  with ``vault.read_ok=false`` and a structured ``read_failed`` detail.
  The bearer token / secret value never leak into logs (verified by
  the conftest autouse sweep + a targeted assertion in
  :mod:`tests.test_secret_leak_checks`).
* Both broken: invalid JWT + dead Vault → 401 (auth wins; Vault is
  never reached; readiness is irrelevant to the dependency chain).

The tests reuse the JWT minting + JWKS-mock helpers from
:mod:`tests.conftest` and the Vault fake-client pattern from
:mod:`tests.test_auth_vault`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import hvac.exceptions
import pytest
import requests.exceptions
import respx
from fastapi.testclient import TestClient

from meho_backplane.auth import vault as vault_module
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vault import VaultConnector, register_vault_typed_operations
from meho_backplane.main import app
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.settings import get_settings
from tests.conftest import (
    DEFAULT_AUDIENCE,
    DEFAULT_DISCOVERY_URL,
    DEFAULT_ISSUER,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

# ---------------------------------------------------------------------------
# Settings + JWKS-cache fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` reads."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", DEFAULT_AUDIENCE)
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
    """Reset the JWKS cache around every test."""
    clear_jwks_cache()
    yield
    clear_jwks_cache()


@pytest.fixture(autouse=True)
def _registered_vault_substrate(_settings_env: None) -> Iterator[None]:
    """Re-establish the v2 connector entry + the ``vault.kv.read`` descriptor row.

    The ``TestClient(app)`` fixture in this file does not enter the
    FastAPI lifespan, so the lifespan's ``run_typed_op_registrars``
    step never fires. We replicate the two things the lifespan would
    have done — v2 registry entry + typed-op descriptor upsert —
    directly in this autouse fixture so the dispatcher's natural-key
    lookup finds the row at request time. Without this, every
    ``/api/v1/health`` request from this file would land an
    ``unknown_op`` from the dispatcher instead of exercising the
    real Vault failure axis the test is asserting on.
    """
    clear_registry()
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
# Vault fake (matches tests/test_api_v1_health.py shape)
# ---------------------------------------------------------------------------


@dataclass
class _FakeJWTAuth:
    login_calls: list[dict[str, Any]] = field(default_factory=list)
    raise_on_login: Exception | None = None
    issued_token: str = "fake-vault-token"
    parent: _FakeClient | None = None

    def jwt_login(self, role: str, jwt: str, path: str | None = None) -> dict[str, Any]:
        self.login_calls.append({"role": role, "jwt": jwt, "path": path})
        if self.raise_on_login is not None:
            raise self.raise_on_login
        if self.parent is not None:
            self.parent.token = self.issued_token
        return {"auth": {"client_token": self.issued_token}}


@dataclass
class _FakeTokenAuth:
    revoke_calls: int = 0

    def revoke_self(self, mount_point: str = "token") -> None:
        self.revoke_calls += 1


@dataclass
class _FakeAuth:
    jwt: _FakeJWTAuth
    token: _FakeTokenAuth


@dataclass
class _FakeKVv2:
    secret: dict[str, Any] = field(default_factory=lambda: {"username": "demo"})
    version: int = 7
    read_calls: list[dict[str, Any]] = field(default_factory=list)
    read_exc: Exception | None = None

    def read_secret_version(self, path: str, **_kwargs: Any) -> dict[str, Any]:
        self.read_calls.append({"path": path})
        if self.read_exc is not None:
            raise self.read_exc
        return {
            "data": {
                "data": self.secret,
                "metadata": {"version": self.version, "path": path},
            }
        }


@dataclass
class _FakeKV:
    v2: _FakeKVv2


@dataclass
class _FakeSecrets:
    kv: _FakeKV


@dataclass
class _FakeSysBackend:
    payload: Any = None

    def read_health_status(self, *, method: str = "HEAD", **_kwargs: Any) -> Any:
        return self.payload


@dataclass
class _FakeClient:
    url: str
    timeout: float
    namespace: str | None
    token: str | None
    auth: _FakeAuth
    sys: _FakeSysBackend
    secrets: _FakeSecrets


def _install_fake_vault(
    monkeypatch: pytest.MonkeyPatch,
    *,
    login_exc: Exception | None = None,
    read_exc: Exception | None = None,
    secret: dict[str, Any] | None = None,
    version: int = 7,
) -> _FakeClient:
    jwt_auth = _FakeJWTAuth(raise_on_login=login_exc)
    token_auth = _FakeTokenAuth()
    kv_v2 = _FakeKVv2(
        secret=secret or {"username": "demo"},
        version=version,
        read_exc=read_exc,
    )
    fake = _FakeClient(
        url="https://vault.test",
        timeout=5.0,
        namespace=None,
        token=None,
        auth=_FakeAuth(jwt=jwt_auth, token=token_auth),
        sys=_FakeSysBackend(),
        secrets=_FakeSecrets(kv=_FakeKV(v2=kv_v2)),
    )
    jwt_auth.parent = fake

    def _fake_build_client(_settings: Any, *, token: str | None = None) -> _FakeClient:
        fake.token = token
        return fake

    monkeypatch.setattr(vault_module, "_build_client", _fake_build_client)
    return fake


@pytest.fixture
def http_client() -> TestClient:
    """``TestClient`` driving the production ``app``.

    Renamed from ``client`` to avoid colliding with the conftest's
    autouse ``capfd``/``caplog`` capture in surprising ways. Using the
    bare ``app`` (no ``with`` block) skips the lifespan, which is the
    same trick the existing ``test_api_v1_health.py`` uses to avoid
    re-running ``configure_logging`` on every request.
    """
    return TestClient(app)


# ---------------------------------------------------------------------------
# Single-axis: invalid JWT + healthy Vault
# ---------------------------------------------------------------------------


def test_invalid_jwt_with_healthy_vault_returns_401(
    http_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired JWT short-circuits to 401 — the Vault chain never runs.

    Vault is fully wired (login would succeed, secret would read) but
    the dependency layer must reject the request before the route body
    runs. Asserts the no-Vault-call invariant via the fake's call log.
    Detail code is ``token_expired`` per G0.9.1-T12 (was ``invalid_token``
    in v0.3.1).
    """
    key = make_rsa_keypair("kid-A")
    expired_token = mint_token(key, expires_in=-600)
    fake_vault = _install_fake_vault(monkeypatch)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = http_client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {expired_token}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "token_expired"}
    # The route never reached its body — Vault must not have seen any
    # ``jwt_login`` call.
    assert fake_vault.auth.jwt.login_calls == []
    assert fake_vault.secrets.kv.v2.read_calls == []


def test_wrong_audience_with_healthy_vault_returns_401(
    http_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token for a different OIDC client is rejected before Vault runs.

    Detail code is ``invalid_audience`` per G0.9.1-T12 (was
    ``invalid_token`` in v0.3.1).
    """
    key = make_rsa_keypair("kid-A")
    token = mint_token(key, audience="some-other-client")
    fake_vault = _install_fake_vault(monkeypatch)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = http_client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid_audience"}
    assert fake_vault.auth.jwt.login_calls == []


def test_missing_authorization_header_returns_401_without_vault_consultation(
    http_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``Authorization`` header → 401 ``missing_token``; Vault not consulted."""
    fake_vault = _install_fake_vault(monkeypatch)

    response = http_client.get("/api/v1/health")

    assert response.status_code == 401
    assert response.json() == {"detail": "missing_token"}
    assert fake_vault.auth.jwt.login_calls == []


# ---------------------------------------------------------------------------
# Single-axis: valid JWT + Vault failures (cross-checks the route's
# never-5xx contract from a different angle than test_api_v1_health.py)
# ---------------------------------------------------------------------------


def test_valid_jwt_vault_unreachable_returns_200_with_structured_detail(
    http_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid JWT + Vault TCP failure → 200 with ``vault.reachable=false``.

    Confirms the cross-module integration: the JWT layer finishes happy,
    binds operator identity, and the Vault layer's exception is mapped
    onto the structured response shape. The smoke test reads this body
    to render an actionable message; a 5xx would break dogfood DoD #2.
    """
    key = make_rsa_keypair("kid-A")
    token = mint_token(key, sub="op-cross-axis-1")
    _install_fake_vault(
        monkeypatch,
        login_exc=requests.exceptions.ConnectionError("no route"),
    )

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = http_client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["operator"]["sub"] == "op-cross-axis-1"
    assert body["vault"]["reachable"] is False
    assert body["vault"]["read_ok"] is False
    assert body["vault"]["detail"] == "login_failed: VaultUnreachableError"
    # ``db.migrated`` reflects the T27 DB-migration-state probe verdict;
    # post-T28, the autouse default DATABASE_URL has the audit-log
    # migration applied at fixture setup, so the probe reports healthy.
    assert body["db"]["migrated"] is True


def test_valid_jwt_vault_role_denied_returns_200_with_role_denied_detail(
    http_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid JWT + Vault 403 on login → ``login_failed: VaultRoleDeniedError``.

    Operator-actionable on the Vault side: the ``meho-mcp`` role is
    misconfigured. The body reports the failure shape; no 5xx; no
    operator-controllable detail in the message.
    """
    key = make_rsa_keypair("kid-A")
    token = mint_token(key)
    _install_fake_vault(
        monkeypatch,
        login_exc=hvac.exceptions.Forbidden("role denied"),
    )

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = http_client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["vault"]["reachable"] is False
    assert body["vault"]["read_ok"] is False
    assert body["vault"]["detail"] == "login_failed: VaultRoleDeniedError"
    # The Vault response message ("role denied") must not leak into
    # the structured detail surfaced to the smoke test.
    assert "role denied" not in body["vault"]["detail"]


def test_valid_jwt_vault_sealed_during_login_returns_200_with_login_failed(
    http_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vault sealed → 503 on login → ``login_failed: VaultClientError``.

    Sealed Vault is a known operator state; the route still returns 200
    with structured failure. The ``/ready`` probe will be flapping in
    lock-step (proven separately in :mod:`tests.test_vault_failures`)."""
    key = make_rsa_keypair("kid-A")
    token = mint_token(key)
    _install_fake_vault(
        monkeypatch,
        # hvac surfaces 503 from JSONAdapter as VaultDown (a VaultError
        # subclass that is *not* Forbidden) during login attempts.
        login_exc=hvac.exceptions.VaultDown("vault is sealed"),
    )

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = http_client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["vault"]["reachable"] is False
    assert body["vault"]["read_ok"] is False
    # Generic VaultClientError surface — not unreachable, not role-denied.
    assert body["vault"]["detail"] is not None
    assert body["vault"]["detail"].startswith("login_failed: VaultClientError")


def test_valid_jwt_secret_read_denied_returns_200_with_read_failed(
    http_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid JWT + Vault login OK + secret read 403 → 200 with
    ``read_ok=false`` and ``read_failed: Forbidden`` detail.

    This is the operator's policy denying the KV read after the OIDC
    login already issued a token — the most common Vault-side
    misconfiguration once the role itself works."""
    key = make_rsa_keypair("kid-A")
    token = mint_token(key)
    _install_fake_vault(
        monkeypatch,
        read_exc=hvac.exceptions.Forbidden("read capability denied on secret/meho/test/federation"),
    )

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = http_client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["vault"]["reachable"] is True
    assert body["vault"]["read_ok"] is False
    assert body["vault"]["detail"] == "read_failed: Forbidden"
    # The path string was in the original Vault message — must not
    # leak into the structured detail.
    assert "secret/meho/test/federation" not in (body["vault"]["detail"] or "")


def test_valid_jwt_secret_read_invalid_path_returns_200_with_read_failed(
    http_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 on the secret read (path doesn't exist) → ``read_ok=false``.

    Captured separately from the 403 path because operators routinely
    swap the two when troubleshooting (path missing vs policy missing
    look identical until you read the message)."""
    key = make_rsa_keypair("kid-A")
    token = mint_token(key)
    _install_fake_vault(
        monkeypatch,
        read_exc=hvac.exceptions.InvalidPath("missing path"),
    )

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = http_client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["vault"]["reachable"] is True
    assert body["vault"]["read_ok"] is False
    assert body["vault"]["detail"] == "read_failed: InvalidPath"


def test_valid_jwt_vault_read_unreachable_returns_200_with_read_failed(
    http_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connection failure on the secret read (login OK but the next call
    drops) → ``vault.reachable=true`` (login worked) but ``read_ok=false``.

    Distinguishes a transient post-login network blip from a hard
    Vault-down condition — the smoke test surfaces both as failures
    but operators chase them differently."""
    key = make_rsa_keypair("kid-A")
    token = mint_token(key)
    _install_fake_vault(
        monkeypatch,
        read_exc=requests.exceptions.ConnectionError("connection reset"),
    )

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = http_client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["vault"]["reachable"] is True
    assert body["vault"]["read_ok"] is False
    assert body["vault"]["detail"] == "read_failed: ConnectionError"


# ---------------------------------------------------------------------------
# Cross-axis: JWT broken AND Vault broken — auth wins
# ---------------------------------------------------------------------------


def test_invalid_jwt_with_dead_vault_returns_401_auth_runs_first(
    http_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both broken → 401 wins. Auth is the outermost gate; Vault is never
    even attempted; the response is the auth failure verbatim. Detail
    code is ``token_expired`` per G0.9.1-T12 (was ``invalid_token`` in
    v0.3.1)."""
    key = make_rsa_keypair("kid-A")
    expired_token = mint_token(key, expires_in=-3600)
    fake_vault = _install_fake_vault(
        monkeypatch,
        login_exc=requests.exceptions.ConnectionError("vault down"),
    )

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = http_client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {expired_token}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "token_expired"}
    assert fake_vault.auth.jwt.login_calls == []


def test_jwks_unreachable_with_healthy_vault_returns_401(
    http_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JWKS endpoint down + Vault healthy → 401 ``jwks_unavailable``.

    The auth layer cannot validate the JWT (JWKS unreachable), so the
    request fails before Vault is consulted. Distinguishable from
    ``invalid_token`` by the detail string — operators chasing 401s
    can tell credential-issue from dependency-issue at a glance."""
    import httpx

    key = make_rsa_keypair("kid-A")
    token = mint_token(key)
    fake_vault = _install_fake_vault(monkeypatch)

    with respx.mock as mock_router:
        mock_router.get(DEFAULT_DISCOVERY_URL).mock(
            return_value=httpx.Response(503),
        )
        response = http_client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "jwks_unavailable"}
    assert fake_vault.auth.jwt.login_calls == []
