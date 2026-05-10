# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end tests for the federation-proof authenticated health route.

Covers Task #24's acceptance criteria:

* Happy path: a valid JWT plus a reachable Vault returns 200 with the
  full federation-chain response shape (operator identity, vault
  status, db status). The Vault test secret read succeeds and its
  KV v2 metadata version surfaces in ``vault.detail``.
* 401 on missing ``Authorization`` header (delegated to ``verify_jwt``;
  asserted here as a contract guard so future refactors of the wrapper
  cannot regress the auth gate).
* Vault unreachable: returns **200** with ``vault.reachable=false`` and
  a structured ``detail`` — never a 5xx. The smoke test reads this
  flag, so the no-5xx contract is load-bearing.
* Vault read failure (login succeeds but secret read raises): returns
  200 with ``vault.reachable=true`` and ``vault.read_ok=false``.
* Operator-identity propagation: every log line emitted under the
  authenticated request carries ``operator_sub`` matching the JWT's
  ``sub`` claim, including the middleware's ``request_completed`` line.

Comprehensive failure-mode coverage (expired / wrong-aud / wrong-iss /
tampered JWT, every Vault error subclass, etc.) belongs to Task #25;
this file ships a happy-path-plus-sanity-check suite so #24 can land in
isolation.

Test strategy:

* Mock Keycloak via the same ``respx`` pattern Task #22 uses — RSA
  fixture key, JWKS document, JWT signed locally.
* Mock Vault via the same ``_install_fake_client`` pattern Task #23
  uses — patch ``meho_backplane.auth.vault._build_client`` to return
  a controllable fake; configure the fake's ``read_secret_version``
  return value or raise behaviour per scenario.
* Capture logs via the same in-memory ``StringIO`` buffer pattern
  ``test_observability`` uses — rebind ``structlog``'s factory so we
  can parse each emitted JSON line.
"""

from __future__ import annotations

import io
import json
import logging
import time
import warnings
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import respx
import structlog
from fastapi.testclient import TestClient

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from authlib.jose import JsonWebKey, JsonWebToken

from meho_backplane.auth import vault as vault_module
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.main import app
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ISSUER: str = "https://keycloak.test/realms/meho"
_AUDIENCE: str = "meho-backplane"
_DISCOVERY_URL: str = f"{_ISSUER}/.well-known/openid-configuration"
_JWKS_URL: str = f"{_ISSUER}/protocol/openid-connect/certs"


# ---------------------------------------------------------------------------
# Settings + JWKS-cache fixtures (mirror Task #22 / #23 patterns)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# JWT minting helpers (lifted shape from tests/test_auth_jwt.py)
# ---------------------------------------------------------------------------


def _make_rsa_keypair(kid: str) -> Any:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return JsonWebKey.generate_key(
            "RSA",
            2048,
            options={"kid": kid},
            is_private=True,
        )


def _public_jwks(*keys: Any) -> dict[str, list[dict[str, Any]]]:
    return {"keys": [k.as_dict(is_private=False) for k in keys]}


def _mint_token(
    private_key: Any,
    *,
    sub: str = "op-42",
    name: str | None = "Damir",
    email: str | None = "damir@example.com",
) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        jwt = JsonWebToken(["RS256"])
        now = int(time.time())
        payload: dict[str, Any] = {
            "sub": sub,
            "iss": _ISSUER,
            "aud": _AUDIENCE,
            "iat": now,
            "exp": now + 3600,
            "nbf": now,
        }
        if name is not None:
            payload["name"] = name
        if email is not None:
            payload["email"] = email
        header = {"alg": "RS256", "kid": private_key.as_dict()["kid"], "typ": "JWT"}
        token: bytes | str = jwt.encode(header, payload, private_key)
        return token.decode("ascii") if isinstance(token, bytes) else token


def _mock_discovery_and_jwks(
    mock_router: respx.MockRouter,
    jwks: dict[str, Any],
) -> None:
    mock_router.get(_DISCOVERY_URL).mock(
        return_value=httpx.Response(
            200,
            json={"issuer": _ISSUER, "jwks_uri": _JWKS_URL},
        ),
    )
    mock_router.get(_JWKS_URL).mock(
        return_value=httpx.Response(200, json=jwks),
    )


# ---------------------------------------------------------------------------
# Vault fake (lifted shape from tests/test_auth_vault.py)
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
    """Stand-in for ``client.secrets.kv.v2``.

    Returns the canonical hvac KV v2 read shape:
    ``{"data": {"data": <secret>, "metadata": {"version": int, ...}}}``.
    Tests pin ``read_exc`` to raise from the read, leaving login intact.
    """

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


# ---------------------------------------------------------------------------
# Log capture (mirrors tests/test_observability.py)
# ---------------------------------------------------------------------------


def _configure_capture(buf: io.StringIO) -> None:
    structlog.reset_defaults()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=buf),
        cache_logger_on_first_use=False,
    )


@pytest.fixture
def log_buffer() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    _configure_capture(buf)
    yield buf
    structlog.reset_defaults()


@pytest.fixture
def client(log_buffer: io.StringIO) -> Iterator[TestClient]:
    """``TestClient`` driving the production ``app`` with logs captured.

    The ``log_buffer`` fixture must come first so structlog's factory
    is rebound before any handler logs through it. The ``with`` form
    is *not* used: entering it would re-run ``configure_logging`` via
    the lifespan and clobber the capture (same trick as
    ``test_observability``).
    """
    yield TestClient(app)


def _read_log_lines(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Header-shape failures
# ---------------------------------------------------------------------------


def test_missing_authorization_returns_401(client: TestClient) -> None:
    """Without a Bearer token the wrapper inherits ``verify_jwt``'s 401."""
    response = client.get("/api/v1/health")
    assert response.status_code == 401
    assert response.json() == {"detail": "missing_token"}


# ---------------------------------------------------------------------------
# Happy path — full chain green
# ---------------------------------------------------------------------------


def test_happy_path_returns_full_federation_response(
    client: TestClient,
    log_buffer: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid JWT + reachable Vault + secret read OK → 200 with full shape."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-100", name="Alice", email="alice@example.com")
    fake = _install_fake_vault(monkeypatch, version=11)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    # ``db.migrated`` now reflects the DB-migration-state probe verdict.
    # Post-T28, the autouse default DATABASE_URL has the audit-log
    # migration applied at fixture setup, so the probe reports healthy
    # and ``migrated`` is True.
    assert body == {
        "operator": {"sub": "op-100", "name": "Alice", "email": "alice@example.com"},
        "vault": {"reachable": True, "read_ok": True, "detail": "version=11"},
        "db": {"migrated": True},
    }

    # Vault was hit with the configured role / mount and the operator's
    # *raw* JWT — bit-for-bit forwarding is what makes the Vault audit
    # row carry the operator's identity.
    assert fake.auth.jwt.login_calls == [
        {"role": "meho-mcp", "jwt": token, "path": "jwt"},
    ]
    # The federation-proof path is hardcoded for v0.1.
    assert fake.secrets.kv.v2.read_calls == [{"path": "meho/test/federation"}]
    # Per-request login → revoke pair.
    assert fake.auth.token.revoke_calls == 1


# ---------------------------------------------------------------------------
# Operator-identity propagation
# ---------------------------------------------------------------------------


def test_operator_sub_propagates_into_structlog_contextvars(
    client: TestClient,
    log_buffer: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every log line under an authenticated request carries ``operator_sub``.

    The acceptance criterion that earned this Task its name. Specifically:

    * The handler-emitted ``federation_health_ok`` log line carries
      the JWT's ``sub`` claim.
    * The middleware's own ``request_completed`` line carries the same
      value — proving the binding is live for the entire request scope,
      not just inside the handler.
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-traceable")
    _install_fake_vault(monkeypatch)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200

    lines = _read_log_lines(log_buffer)

    handler_lines = [line for line in lines if line.get("event") == "federation_health_ok"]
    assert handler_lines, "expected a federation_health_ok log line"
    assert handler_lines[-1].get("operator_sub") == "op-traceable"

    completed_lines = [line for line in lines if line.get("event") == "request_completed"]
    assert completed_lines, "expected a request_completed log line from the middleware"
    # The binding lives in the same contextvar scope the middleware's
    # final log uses, so request_completed inherits operator_sub too.
    assert completed_lines[-1].get("operator_sub") == "op-traceable"


def test_operator_sub_does_not_leak_to_unauthenticated_requests(
    client: TestClient,
    log_buffer: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The middleware's ``clear_contextvars`` zeroes ``operator_sub`` per request.

    Drives an authenticated request first (binding ``operator_sub``),
    then an unauthenticated request to ``/`` on the same TestClient
    (which reuses the same asyncio task). The second request's logs
    must not carry ``operator_sub``.
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-first")
    _install_fake_vault(monkeypatch)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        first = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    second = client.get("/")  # unauthenticated route on the same client

    assert first.status_code == 200
    assert second.status_code == 200

    lines = _read_log_lines(log_buffer)

    # Match the request_completed line for the second request by path.
    second_completed = [
        line
        for line in lines
        if line.get("event") == "request_completed" and line.get("path") == "/"
    ]
    assert second_completed, "expected a request_completed log line for the second request"
    # No ``operator_sub`` key — the middleware cleared contextvars at
    # request entry, and the unauthenticated route never re-bound it.
    assert "operator_sub" not in second_completed[-1]


# ---------------------------------------------------------------------------
# Vault failure modes — never 5xx, always structured response
# ---------------------------------------------------------------------------


def test_vault_unreachable_returns_200_with_structured_detail(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vault TCP failure surfaces as ``vault.reachable=false`` — not a 5xx.

    The smoke test reads the structured ``detail`` to render an
    operator-actionable message; an unhandled 5xx would break the
    dogfood loop's Definition-of-Done bullet 2.
    """
    import requests.exceptions

    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-200")
    _install_fake_vault(
        monkeypatch,
        login_exc=requests.exceptions.ConnectionError("no route to host"),
    )

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["operator"]["sub"] == "op-200"
    assert body["vault"]["reachable"] is False
    assert body["vault"]["read_ok"] is False
    assert body["vault"]["detail"] == "login_failed: VaultUnreachableError"
    # ``db.migrated`` now reflects the T27 probe verdict; post-T28
    # the autouse default has the migration applied, so the probe is
    # healthy and ``migrated`` is True.
    assert body["db"]["migrated"] is True


def test_vault_role_denied_returns_200_with_role_denied_detail(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vault 403 surfaces as ``login_failed: VaultRoleDeniedError``."""
    import hvac.exceptions

    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key)
    _install_fake_vault(
        monkeypatch,
        login_exc=hvac.exceptions.Forbidden("role denied"),
    )

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["vault"]["reachable"] is False
    assert body["vault"]["detail"] == "login_failed: VaultRoleDeniedError"


def test_vault_secret_read_failure_returns_200_with_read_failed_detail(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Login succeeds but the secret read raises → ``read_ok=false``.

    The KV mount is missing, the path is missing, or hvac's response
    shape changed. The route surfaces ``read_ok=false`` with a
    structured ``detail`` carrying only the exception class name —
    operator-controllable URL substrings never make it into the body.
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key)
    _install_fake_vault(
        monkeypatch,
        read_exc=RuntimeError("secret missing"),
    )

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["vault"]["reachable"] is True
    assert body["vault"]["read_ok"] is False
    assert body["vault"]["detail"] == "read_failed: RuntimeError"


def test_vault_malformed_payload_returns_200_with_read_failed_detail(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed hvac payload (missing version key) is a 200, not a 500.

    Regression for the bug where ``_extract_version`` raised ``KeyError``
    on payloads like ``{"data": {}}`` and the exception escaped the route
    as an HTTP 500 — violating AC #3 (the endpoint must never 5xx; a
    Vault read failure is always a 200 with ``vault.read_ok=false``).
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key)
    fake = _install_fake_vault(monkeypatch)
    # Override the fake's read to return a structurally-broken payload —
    # ``data`` exists but ``metadata`` (and ``version``) do not. The
    # _extract_version unwrap raises KeyError on this shape.
    monkeypatch.setattr(
        fake.secrets.kv.v2,
        "read_secret_version",
        lambda path, **_kwargs: {"data": {}},
    )

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["vault"]["reachable"] is True
    assert body["vault"]["read_ok"] is False
    assert body["vault"]["detail"] == "read_failed: KeyError"
