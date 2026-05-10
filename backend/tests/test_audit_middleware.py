# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :class:`meho_backplane.audit.AuditMiddleware`.

Coverage matrix (Task #28 acceptance criteria):

* **Happy path** — a ``GET /api/v1/health`` call with a valid mock
  JWT produces exactly one new ``audit_log`` row whose
  ``operator_sub`` matches the JWT's ``sub``, ``method=GET``,
  ``path=/api/v1/health``, ``status_code=200``, and ``request_id``
  matches the response ``X-Request-Id`` header.
* **Skip rule — unauthenticated** — public surfaces (``/healthz``)
  produce no row and never invoke the session factory.
* **Skip rule — 401** — protected route hit without a Bearer token
  returns 401 and produces no row (no operator to attribute).
* **Fail-closed** — when the session factory raises mid-request, the
  response converts to 500 ``{"detail": "audit_write_failed"}`` and
  the buffered handler response is discarded.

The tests drive the production ``meho_backplane.main:app`` so they
exercise the real middleware-stack ordering — ``RequestContextMiddleware``
outermost, ``AuditMiddleware`` directly inside it. Asserting on the
ordering implicitly is what proves AC #3 (middleware registered
*after* RequestContextMiddleware so ``operator_sub`` and
``request_id`` are bound by the time audit runs).

The DB layer is wired against per-test aiosqlite engines via the
``isolated_audit_engine`` fixture — same pattern as
:mod:`tests.test_alembic_probe`'s ``sqlite_engine``. Each test runs
``alembic upgrade head`` on its dedicated DB so the ``audit_log``
table exists with the production schema. Mocks for Keycloak's JWKS
and Vault's KV v2 read mirror the patterns established in
:mod:`tests.test_api_v1_health`.
"""

from __future__ import annotations

import time
import uuid
import warnings
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from authlib.jose import JsonWebKey, JsonWebToken

from meho_backplane.auth import vault as vault_module
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db import engine as engine_module
from meho_backplane.db.engine import (
    create_engine_for_url,
    dispose_engine,
    get_sessionmaker,
    reset_engine_for_testing,
)
from meho_backplane.db.migrations import alembic_config
from meho_backplane.db.models import AuditLog
from meho_backplane.main import app
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Constants — match the JWT/Vault test conventions established in T22/T23/T24.
# ---------------------------------------------------------------------------

_ISSUER: str = "https://keycloak.test/realms/meho"
_AUDIENCE: str = "meho-backplane"
_DISCOVERY_URL: str = f"{_ISSUER}/.well-known/openid-configuration"
_JWKS_URL: str = f"{_ISSUER}/protocol/openid-connect/certs"


# ---------------------------------------------------------------------------
# Settings + JWKS-cache fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    """Pin every env var :class:`Settings` reads + a tmp-path SQLite DB.

    Overrides the autouse ``_default_database_url`` from conftest.py
    with a tmp-path DB so each test owns an isolated audit table; the
    ``isolated_audit_engine`` fixture below runs ``alembic upgrade
    head`` against that DB.
    """
    db_path = tmp_path / "audit.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)
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
def _audit_db_url(tmp_path: Path) -> str:
    """Resolve the per-test SQLite URL and run ``alembic upgrade head``.

    The Alembic upgrade is split into a *synchronous* fixture because
    :func:`alembic.command.upgrade` calls :func:`asyncio.run` internally
    via env.py's ``run_migrations_online`` entry point; running it
    from within an async test (or async fixture) raises
    ``RuntimeError: asyncio.run() cannot be called from a running
    event loop``. Pytest-asyncio's auto-mode lets a sync fixture feed
    its return value into an async test, which is the seam we use here.
    """
    db_path = tmp_path / "audit.db"
    url = f"sqlite+aiosqlite:///{db_path}"

    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    return url


@pytest.fixture
async def isolated_audit_engine(
    _audit_db_url: str,
) -> AsyncIterator[AsyncEngine]:
    """Per-test aiosqlite engine bound to the migrated audit DB.

    The schema was applied by ``_audit_db_url`` (a sync fixture) so
    this fixture only constructs the engine and injects it into the
    module-level cache — the audit middleware's
    :func:`~meho_backplane.db.engine.get_sessionmaker` returns a
    factory rooted at *this* DB. Cache resets bracket the yield so
    the next test gets a fresh engine.
    """
    reset_engine_for_testing()
    eng = create_engine_for_url(_audit_db_url, pool_size=5, pool_timeout=10.0)
    engine_module._engine = eng
    try:
        yield eng
    finally:
        await dispose_engine()
        reset_engine_for_testing()


# ---------------------------------------------------------------------------
# JWT helpers — mirror tests/test_api_v1_health.py
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
# Vault fake — mirror tests/test_api_v1_health.py
# ---------------------------------------------------------------------------


@dataclass
class _FakeJWTAuth:
    login_calls: list[dict[str, Any]] = field(default_factory=list)
    issued_token: str = "fake-vault-token"
    parent: _FakeClient | None = None

    def jwt_login(self, role: str, jwt: str, path: str | None = None) -> dict[str, Any]:
        self.login_calls.append({"role": role, "jwt": jwt, "path": path})
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

    def read_secret_version(self, path: str, **_kwargs: Any) -> dict[str, Any]:
        self.read_calls.append({"path": path})
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


def _install_fake_vault(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    jwt_auth = _FakeJWTAuth()
    token_auth = _FakeTokenAuth()
    kv_v2 = _FakeKVv2(version=11)
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
# DB helpers
# ---------------------------------------------------------------------------


async def _fetch_audit_rows(eng: AsyncEngine) -> list[AuditLog]:
    """Return every audit_log row, ordered by occurred_at."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Happy path — authenticated request → exactly one audit row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authenticated_request_writes_one_audit_row(
    isolated_audit_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GET /api/v1/health`` with a valid JWT writes one row before response.

    Asserts AC #4: row exists post-request, with operator_sub matching
    the JWT's ``sub``, method=GET, path=/api/v1/health, status=200,
    request_id parseable from the X-Request-Id response header.

    The TestClient is created *after* the engine fixture has populated
    the schema and injected the engine into the module cache, so the
    middleware's ``get_sessionmaker()`` resolves to the test's
    aiosqlite DB.
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-100", name="Alice", email="alice@example.com")
    _install_fake_vault(monkeypatch)

    client = TestClient(app)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200

    response_request_id = response.headers["x-request-id"]
    assert response_request_id  # the request-context middleware always sets it

    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert len(rows) == 1
    row = rows[0]
    assert row.operator_sub == "op-100"
    assert row.method == "GET"
    assert row.path == "/api/v1/health"
    assert row.status_code == 200
    # request_id is stored as UUID; the middleware mints UUID4 hex when
    # the client doesn't send X-Request-Id, which parses cleanly.
    assert row.request_id is not None
    assert str(row.request_id).replace("-", "") == response_request_id
    assert row.duration_ms is not None
    assert row.duration_ms >= 0
    assert row.payload == {}


# ---------------------------------------------------------------------------
# Skip rule — public surfaces (no operator_sub bound)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unauthenticated_health_check_writes_no_audit_row(
    isolated_audit_engine: AsyncEngine,
) -> None:
    """``GET /healthz`` is public — no operator, no audit row.

    The skip rule keys on the ``operator_sub`` contextvar's presence.
    ``/healthz`` is not behind ``verify_jwt_and_bind`` so the binding
    never fires; the audit middleware sees an empty ``operator_sub``
    and forwards the response unchanged.
    """
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200

    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert rows == []


@pytest.mark.asyncio
async def test_protected_route_without_token_returns_401_and_no_row(
    isolated_audit_engine: AsyncEngine,
) -> None:
    """A 401 from ``verify_jwt`` produces no audit row (no operator).

    ``verify_jwt_and_bind`` only binds ``operator_sub`` *after* the
    JWT validates; a missing-token 401 short-circuits before the
    binding fires, and the middleware sees an empty contextvar dict.
    """
    client = TestClient(app)
    response = client.get("/api/v1/health")
    assert response.status_code == 401

    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert rows == []


# ---------------------------------------------------------------------------
# Fail-closed — DB unreachable / commit raises → 500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_write_failure_converts_request_to_500(
    isolated_audit_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the audit insert raises, the response is replaced with 500.

    Asserts the fail-closed contract: an unaudited action is an
    unallowed action. The handler still produced its 200 response
    (the buffered messages are in the middleware's local list), but
    the audit insert raises → those messages are discarded and a
    fresh 500 ``{"detail": "audit_write_failed"}`` is sent.

    We patch ``meho_backplane.audit.get_sessionmaker`` to return a
    factory whose session raises on commit. Patching at the audit
    module's import site (rather than the engine module) is what
    keeps the ``isolated_audit_engine`` fixture's session factory
    intact for the post-test ``_fetch_audit_rows`` query — otherwise
    we'd have to introspect the failure differently because the
    audit table itself would be unreachable.
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-200")
    _install_fake_vault(monkeypatch)

    class _RaisingSession:
        def __init__(self) -> None:
            pass

        async def __aenter__(self) -> _RaisingSession:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        def add(self, _row: object) -> None:
            return None

        async def commit(self) -> None:
            raise RuntimeError("simulated DB outage")

    def _raising_sessionmaker() -> Any:
        return _RaisingSession

    import meho_backplane.audit as audit_module

    monkeypatch.setattr(audit_module, "get_sessionmaker", _raising_sessionmaker)

    client = TestClient(app)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 500
    assert response.json() == {"detail": "audit_write_failed"}
    # Even on the failure path, RequestContextMiddleware (outermost)
    # still injects X-Request-Id. The header is the operator's only
    # crumb to correlate the failure with backplane logs.
    assert response.headers.get("x-request-id")


@pytest.mark.asyncio
async def test_request_id_passes_through_when_uuid_shaped(
    isolated_audit_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller-supplied UUID-shaped X-Request-Id is preserved on the audit row."""
    key = _make_rsa_keypair("kid-B")
    token = _mint_token(key, sub="op-300")
    _install_fake_vault(monkeypatch)

    caller_request_id = str(uuid.uuid4())
    client = TestClient(app)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-Id": caller_request_id,
            },
        )

    assert response.status_code == 200
    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert len(rows) == 1
    assert rows[0].request_id is not None
    assert str(rows[0].request_id) == caller_request_id


@pytest.mark.asyncio
async def test_request_id_null_when_caller_sends_opaque_string(
    isolated_audit_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-UUID X-Request-Id values store as NULL — never fail the audit insert."""
    key = _make_rsa_keypair("kid-C")
    token = _mint_token(key, sub="op-400")
    _install_fake_vault(monkeypatch)

    client = TestClient(app)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-Id": "k8s-correlation-12345",
            },
        )

    assert response.status_code == 200
    rows = await _fetch_audit_rows(isolated_audit_engine)
    assert len(rows) == 1
    assert rows[0].request_id is None
    # The other fields land normally — failing the insert on a request
    # shape mismatch would convert a benign client into a 5xx.
    assert rows[0].operator_sub == "op-400"
