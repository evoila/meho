# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""RBAC-split tests for the two authenticated health routes.

The /health surface is split into two routes with different privilege
requirements (least-privilege hardening, meho-internal#159):

* ``GET /api/v1/health`` — the federation-proof deep check. Every call
  federates a live per-operator Vault credential and reads a KV v2
  secret, so it is gated at ``TenantRole.OPERATOR`` via
  :func:`~meho_backplane.auth.rbac.require_role`. This file pins the
  load-bearing property: a ``read_only`` caller receives 403
  ``insufficient_role`` **before** any Vault interaction — the
  federation probe is patched and asserted not-awaited.
* ``GET /api/v1/health/live`` — the cheap liveness probe. Requires
  only a valid JWT (any role, including ``read_only``) and reports
  operator identity + DB-migration liveness. Its handler must stay
  Vault-free; a source-level guard test pins that invariant against
  future edits.

The deep check's Vault failure-mode matrix (never-5xx contract, detail
string shapes) stays in ``test_api_v1_health.py`` /
``test_api_health_failures.py`` — those suites drive default
(``operator``-role) tokens and are unaffected by the gate.

Test strategy mirrors ``test_api_v1_health.py``: mock Keycloak via
``respx`` (RSA fixture key, JWKS document, locally-signed JWT) and
patch the health module's ``_probe_vault_federation`` /
``dispatch`` attributes directly — the RBAC gate under test sits in
front of both, so no fake Vault client or connector registry setup is
needed.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.api.v1 import health as health_module
from meho_backplane.api.v1.health import VaultStatus
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` reads, around every test."""
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
    """Empty the module-level JWKS cache around every test."""
    clear_jwks_cache()
    yield
    clear_jwks_cache()


@pytest.fixture
def client() -> Iterator[TestClient]:
    """``TestClient`` driving the production ``app``.

    Deliberately not the ``with`` form — entering the lifespan would
    re-run ``configure_logging`` and the typed-op registrars, neither
    of which these RBAC-gate tests need (the federation probe is
    patched out at the health-module boundary).
    """
    yield TestClient(app)


@pytest.fixture
def _vault_path_stubs(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, AsyncMock]:
    """Patch the health module's Vault-touching attributes.

    Returns ``(probe_mock, dispatch_mock)``. ``build_health_response``
    resolves ``_probe_vault_federation`` as a module global and the
    probe resolves ``dispatch`` the same way, so patching both module
    attributes makes any Vault-ward call observable — and the tests
    assert the mocks are *never* awaited on the 403 / liveness paths.
    """
    probe_mock = AsyncMock(
        return_value=VaultStatus(reachable=True, read_ok=True, detail="version=7")
    )
    dispatch_mock = AsyncMock()
    monkeypatch.setattr(health_module, "_probe_vault_federation", probe_mock)
    monkeypatch.setattr(health_module, "dispatch", dispatch_mock)
    return probe_mock, dispatch_mock


# ---------------------------------------------------------------------------
# Deep check — OPERATOR gate
# ---------------------------------------------------------------------------


def test_read_only_health_returns_403_before_any_vault_federation(
    client: TestClient,
    _vault_path_stubs: tuple[AsyncMock, AsyncMock],
) -> None:
    """A ``read_only`` caller gets 403 and Vault is never approached.

    The acceptance criterion of meho-internal#159: the RBAC gate must
    fire *before* ``_probe_vault_federation`` / the ``vault.kv.read``
    dispatch, so no per-operator Vault credential is federated and no
    secret is read on behalf of the lowest-trust role.
    """
    probe_mock, dispatch_mock = _vault_path_stubs
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-ro", tenant_role="read_only")

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient_role"}
    assert probe_mock.await_count == 0
    assert dispatch_mock.await_count == 0


@pytest.mark.parametrize("role", ["operator", "tenant_admin"])
def test_operator_rank_health_reaches_federation_probe(
    client: TestClient,
    _vault_path_stubs: tuple[AsyncMock, AsyncMock],
    role: str,
) -> None:
    """OPERATOR and TENANT_ADMIN still get the full deep-check response."""
    probe_mock, _dispatch_mock = _vault_path_stubs
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key,
        sub=f"op-{role}",
        name="Alice",
        email="alice@example.com",
        tenant_role=role,
    )

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["operator"] == {
        "sub": f"op-{role}",
        "name": "Alice",
        "email": "alice@example.com",
    }
    assert body["vault"] == {"reachable": True, "read_ok": True, "detail": "version=7"}
    assert body["db"] == {"migrated": True}
    assert probe_mock.await_count == 1


# ---------------------------------------------------------------------------
# Liveness probe — low-privilege reachability, Vault-free
# ---------------------------------------------------------------------------


def test_read_only_liveness_returns_200_without_vault_touch(
    client: TestClient,
    _vault_path_stubs: tuple[AsyncMock, AsyncMock],
) -> None:
    """The lowest-trust role can poll liveness; Vault is never approached."""
    probe_mock, dispatch_mock = _vault_path_stubs
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key,
        sub="op-monitor",
        name="Monitor",
        email="monitor@example.com",
        tenant_role="read_only",
    )

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health/live",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "operator": {
            "sub": "op-monitor",
            "name": "Monitor",
            "email": "monitor@example.com",
        },
        "db": {"migrated": True},
    }
    assert probe_mock.await_count == 0
    assert dispatch_mock.await_count == 0


def test_liveness_missing_authorization_returns_401(client: TestClient) -> None:
    """Liveness drops the role gate, not authentication."""
    response = client.get("/api/v1/health/live")
    assert response.status_code == 401
    assert response.json() == {"detail": "missing_token"}


def test_liveness_handler_source_is_vault_free() -> None:
    """Source-level guard for the split's structural invariant.

    Mirrors the issue's grep acceptance criterion: the liveness
    handler's code path must contain no reference to the dispatcher,
    the ``vault.kv.read`` op, or the federation probe. Catches a
    future edit that quietly re-couples the cheap probe to Vault.
    """
    source = inspect.getsource(health_module.liveness)
    for forbidden in ("dispatch", "vault.kv.read", "_probe_vault_federation"):
        assert forbidden not in source, (
            f"liveness handler must stay Vault-free but references {forbidden!r}"
        )
