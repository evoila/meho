# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for VaultConnector — G0.2-T5 reference implementation.

Coverage matrix (per Task #244 acceptance criteria):

* Importing ``connectors.vault`` registers ``VaultConnector`` against
  the registry under the ``"vault"`` product slug.
* ``VaultConnector.fingerprint(target)`` returns a :class:`FingerprintResult`
  with ``vendor="hashicorp"`` and ``product="vault"``; the ``version`` field
  is populated from the ``/v1/sys/health`` payload.
* ``VaultConnector.probe(target)`` returns ``ok=True`` for a reachable
  unsealed Vault and ``ok=False`` for unreachable / sealed / uninitialised
  Vault; the ``reason`` field carries the same structured strings the
  existing ``vault_readiness_probe`` tests assert on.
* ``VaultConnector.execute(target, "vault.kv.read", {"path": "..."})``
  returns ``OperationResult(status="ok")`` with ``result`` carrying the
  secret data and ``extras["version"]`` carrying the KV v2 metadata version.
* ``VaultConnector.execute(target, "vault.nonexistent.op", ...)`` returns
  ``OperationResult(status="error")`` with ``known_ops`` in extras.
* ``VaultConnector.execute`` with a missing ``path`` param returns a
  structured error without touching Vault.
* Login failures (unreachable, role-denied) return
  ``OperationResult(status="error", extras={"phase": "login", ...})``.
* Read failures (login ok, secret read raises) return
  ``OperationResult(status="error", extras={"phase": "read", ...})``.
* Malformed hvac payload (missing metadata/version keys) is a
  structured read-phase error, not an unhandled exception.

Test isolation: the production code builds hvac clients through the private
``_build_client`` helper (single seam). Tests monkey-patch that helper to
return a controllable fake — no real HTTP, no Vault container.

The ``_clean_vault_registry`` fixture re-registers ``VaultConnector`` before
each test because ``test_connectors_registry.py`` (alphabetically earlier) has
an autouse ``clear_registry()`` that empties the registry after its last test.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import hvac.exceptions
import pytest
import requests.exceptions

from meho_backplane.auth import vault as vault_module
from meho_backplane.connectors import all_connectors, get_connector
from meho_backplane.connectors.registry import clear_registry, register_connector
from meho_backplane.connectors.vault.connector import VaultConnector, VaultTarget
from meho_backplane.settings import get_settings

# Shared fake for hvac's non-200 health response (standby / active-perf-standby).
# Defined once here to avoid duplicating the class body in every test that needs it.


class _StandbyResponse:
    status_code = 429


# ---------------------------------------------------------------------------
# Registry isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_vault_registry() -> Iterator[None]:
    """Ensure VaultConnector is registered (and only it) for each test.

    test_connectors_registry.py runs before this file (alphabetically
    "connectors_registry" < "connectors_vault") and clears the registry
    after each of its tests via its own autouse fixture. This fixture
    re-registers VaultConnector so tests here start with a known state.
    """
    clear_registry()
    register_connector("vault", VaultConnector)
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Settings env
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars needed by Settings / VaultConnector."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Vault fakes (shared seam: vault_module._build_client)
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
class _FakeSysBackend:
    payload: Any = None
    raise_on_read: Exception | None = None
    read_calls: list[dict[str, Any]] = field(default_factory=list)

    def read_health_status(self, *, method: str = "HEAD", **_kwargs: Any) -> Any:
        self.read_calls.append({"method": method})
        if self.raise_on_read is not None:
            raise self.raise_on_read
        return self.payload


@dataclass
class _FakeKVv2:
    secret: dict[str, Any] = field(default_factory=lambda: {"key": "value"})
    version: int = 3
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
class _FakeClient:
    url: str
    timeout: float
    namespace: str | None
    token: str | None
    auth: _FakeAuth
    sys: _FakeSysBackend
    secrets: _FakeSecrets


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    health_payload: Any = None,
    health_exc: Exception | None = None,
    login_exc: Exception | None = None,
    secret: dict[str, Any] | None = None,
    version: int = 3,
    read_exc: Exception | None = None,
) -> _FakeClient:
    jwt_auth = _FakeJWTAuth(raise_on_login=login_exc)
    token_auth = _FakeTokenAuth()
    sys_backend = _FakeSysBackend(payload=health_payload, raise_on_read=health_exc)
    kv_v2 = _FakeKVv2(secret=secret or {"key": "value"}, version=version, read_exc=read_exc)
    fake = _FakeClient(
        url="https://vault.test",
        timeout=5.0,
        namespace=None,
        token=None,
        auth=_FakeAuth(jwt=jwt_auth, token=token_auth),
        sys=sys_backend,
        secrets=_FakeSecrets(kv=_FakeKV(v2=kv_v2)),
    )
    jwt_auth.parent = fake

    def _fake_build_client(_settings: Any, *, token: str | None = None) -> _FakeClient:
        fake.token = token
        return fake

    monkeypatch.setattr(vault_module, "_build_client", _fake_build_client)
    return fake


def _make_target(jwt: str = "fake.jwt.value") -> VaultTarget:
    return VaultTarget(raw_jwt=jwt)


# ---------------------------------------------------------------------------
# Registry acceptance criterion
# ---------------------------------------------------------------------------


def test_importing_vault_package_registers_vault_connector() -> None:
    """Importing connectors.vault registers VaultConnector under 'vault'.

    The autouse _clean_vault_registry fixture re-registers after the
    clear from test_connectors_registry.py, so we assert the slug is
    populated and the class is the expected one.
    """
    assert get_connector("vault") is VaultConnector
    assert "vault" in all_connectors()


def test_vault_connector_has_product_slug() -> None:
    assert VaultConnector.product == "vault"


# ---------------------------------------------------------------------------
# fingerprint
# ---------------------------------------------------------------------------


async def test_fingerprint_returns_hashicorp_vault_with_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_client(
        monkeypatch,
        health_payload={
            "initialized": True,
            "sealed": False,
            "version": "1.18.0",
            "build_date": "2025-01-01",
            "cluster_id": "abc123",
            "cluster_name": "meho-vault",
        },
    )
    connector = VaultConnector()
    result = await connector.fingerprint(_make_target())

    assert result.vendor == "hashicorp"
    assert result.product == "vault"
    assert result.version == "1.18.0"
    assert result.build == "2025-01-01"
    assert result.reachable is True
    assert result.probe_method == "GET /v1/sys/health"
    assert result.extras["cluster_id"] == "abc123"
    assert result.extras["cluster_name"] == "meho-vault"
    assert fake.sys.read_calls == [{"method": "GET"}]


async def test_fingerprint_version_none_for_non_200_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-dict health payload (hvac standby response) → version=None."""
    _install_fake_client(monkeypatch, health_payload=_StandbyResponse())
    result = await VaultConnector().fingerprint(_make_target())

    assert result.vendor == "hashicorp"
    assert result.version is None
    assert result.reachable is True


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


async def test_probe_returns_ok_for_unsealed_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_client(
        monkeypatch,
        health_payload={"initialized": True, "sealed": False, "standby": False},
    )
    result = await VaultConnector().probe(_make_target())

    assert result.ok is True
    assert result.reason == "sealed=False"


async def test_probe_returns_ok_false_for_sealed_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_client(monkeypatch, health_payload={"initialized": True, "sealed": True})
    result = await VaultConnector().probe(_make_target())

    assert result.ok is False
    assert result.reason == "sealed"


async def test_probe_returns_ok_false_for_uninitialized_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_client(monkeypatch, health_payload={"initialized": False, "sealed": False})
    result = await VaultConnector().probe(_make_target())

    assert result.ok is False
    assert result.reason == "uninitialized"


async def test_probe_returns_ok_true_for_standby_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_client(monkeypatch, health_payload=_StandbyResponse())
    result = await VaultConnector().probe(_make_target())

    assert result.ok is True
    assert result.reason == "http_429"


async def test_probe_returns_ok_false_when_vault_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_client(
        monkeypatch,
        health_exc=requests.exceptions.ConnectionError("dns failure"),
    )
    result = await VaultConnector().probe(_make_target())

    assert result.ok is False
    assert result.reason is not None
    assert result.reason.startswith("unreachable: ConnectionError")


# ---------------------------------------------------------------------------
# execute — unknown op
# ---------------------------------------------------------------------------


async def test_execute_unknown_op_returns_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_client(monkeypatch)
    connector = VaultConnector()
    result = await connector.execute(_make_target(), "vault.nonexistent.op", {})

    assert result.status == "error"
    assert "vault.nonexistent.op" in (result.error or "")
    assert "known_ops" in result.extras
    assert "vault.kv.read" in result.extras["known_ops"]


# ---------------------------------------------------------------------------
# execute — vault.kv.read happy path
# ---------------------------------------------------------------------------


async def test_execute_vault_kv_read_returns_secret_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_client(
        monkeypatch,
        secret={"username": "demo", "region": "eu-central-1"},
        version=7,
    )
    connector = VaultConnector()
    result = await connector.execute(
        _make_target(jwt="op-jwt"),
        "vault.kv.read",
        {"path": "secret/meho/test/federation"},
    )

    assert result.status == "ok"
    assert result.result == {"username": "demo", "region": "eu-central-1"}
    assert result.extras.get("version") == 7
    assert fake.auth.jwt.login_calls == [
        {"role": "meho-mcp", "jwt": "op-jwt", "path": "jwt"},
    ]
    assert fake.secrets.kv.v2.read_calls == [{"path": "secret/meho/test/federation"}]
    assert fake.auth.token.revoke_calls == 1


@pytest.mark.parametrize(
    "params",
    [{}, {"path": 123}, {"path": ""}, {"path": "   "}],
    ids=["missing", "non-string", "empty-string", "whitespace-only"],
)
async def test_execute_vault_kv_read_invalid_path_returns_error(
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, Any],
) -> None:
    """Missing, non-string, empty, or whitespace-only path returns input-validation error."""
    _install_fake_client(monkeypatch)
    result = await VaultConnector().execute(_make_target(), "vault.kv.read", params)

    assert result.status == "error"
    assert result.error == "path must be a non-empty string"
    assert result.duration_ms == 0


# ---------------------------------------------------------------------------
# execute — login failures (phase=login)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "login_exc,expected_exc_type",
    [
        (requests.exceptions.ConnectionError("no route"), "VaultUnreachableError"),
        (hvac.exceptions.Forbidden("role denied"), "VaultRoleDeniedError"),
    ],
    ids=["unreachable", "role-denied"],
)
async def test_execute_login_failure_returns_login_phase_error(
    monkeypatch: pytest.MonkeyPatch,
    login_exc: Exception,
    expected_exc_type: str,
) -> None:
    _install_fake_client(monkeypatch, login_exc=login_exc)
    result = await VaultConnector().execute(_make_target(), "vault.kv.read", {"path": "some/path"})

    assert result.status == "error"
    assert result.extras.get("phase") == "login"
    assert result.extras.get("exc_type") == expected_exc_type


# ---------------------------------------------------------------------------
# execute — read failures (phase=read)
# ---------------------------------------------------------------------------


async def test_execute_read_exception_returns_read_phase_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_client(monkeypatch, read_exc=RuntimeError("secret missing"))
    result = await VaultConnector().execute(_make_target(), "vault.kv.read", {"path": "some/path"})

    assert result.status == "error"
    assert result.extras.get("phase") == "read"
    assert result.extras.get("exc_type") == "RuntimeError"


async def test_execute_malformed_payload_returns_read_phase_key_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed hvac payload (missing data/metadata keys) → KeyError as read failure."""
    fake = _install_fake_client(monkeypatch)
    monkeypatch.setattr(
        fake.secrets.kv.v2,
        "read_secret_version",
        lambda path, **_kwargs: {"data": {}},  # missing nested "data" and "metadata"
    )
    result = await VaultConnector().execute(_make_target(), "vault.kv.read", {"path": "some/path"})

    assert result.status == "error"
    assert result.extras.get("phase") == "read"
    assert result.extras.get("exc_type") == "KeyError"
