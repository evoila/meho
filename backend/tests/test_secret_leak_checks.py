# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Targeted secret-leak checks for the federation chain (Task #25 — leak half).

The :mod:`tests.conftest` autouse sweep is the always-on safety net
for every test in the suite — it scans stdout / stderr / stdlib
``logging`` for credential-shaped substrings on every test exit. This
file complements that sweep with **targeted** assertions: each test
drives the production code paths most likely to leak a secret and
inspects the structlog buffer the production app actually writes to.

Targets:

* The bearer token (raw_jwt) NEVER appears in any log line emitted by
  the auth-success path through ``/api/v1/health``. Verifies that
  ``verify_jwt`` / ``verify_jwt_and_bind`` / the route handler / the
  middleware never log the token, even though they all run on the
  same request.
* The Vault token (the post-login ``client_token`` issued by
  ``jwt_login``) NEVER appears in any log line. The fake's issued
  token is a known sentinel string we grep for verbatim.
* The Vault secret value (``data["data"]`` payload returned by
  ``read_secret_version``) NEVER appears in any log line. The route
  emits ``federation_health_ok`` with ``vault_read_path`` and the KV
  metadata version — never the secret value itself.
* ``Operator.repr()`` does not leak ``raw_jwt`` (regression coverage
  from Task #22 already, but exercised here on the live request path
  too in case structlog is extended to bind operator dicts in future).

The captures use the same ``StringIO`` + ``PrintLoggerFactory`` pattern
established in ``tests/test_observability.py`` and reused in
``tests/test_api_v1_health.py``. Tests assert with ``in`` /
``not in`` against the raw buffer text rather than parsing each line,
because the failure mode being caught is "the token appears anywhere"
— whether it's in an event field, a key, or the rendered traceback of
an unhandled exception.
"""

from __future__ import annotations

import io
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import hvac.exceptions
import pytest
import respx
import structlog
from fastapi.testclient import TestClient

from meho_backplane.auth import vault as vault_module
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator
from meho_backplane.main import app
from meho_backplane.settings import get_settings
from tests.conftest import (
    DEFAULT_AUDIENCE,
    DEFAULT_ISSUER,
    SECRET_LEAK_PATTERNS,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

# ---------------------------------------------------------------------------
# Settings + log capture (mirrors tests/test_api_v1_health.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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
    clear_jwks_cache()
    yield
    clear_jwks_cache()


def _configure_capture(buf: io.StringIO) -> None:
    """Bind structlog to write JSON lines to ``buf``."""
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
def http_client(log_buffer: io.StringIO) -> Iterator[TestClient]:
    yield TestClient(app)


# ---------------------------------------------------------------------------
# Vault fake (matches the surrounding test files)
# ---------------------------------------------------------------------------


@dataclass
class _FakeJWTAuth:
    login_calls: list[dict[str, Any]] = field(default_factory=list)
    raise_on_login: Exception | None = None
    issued_token: str = "VAULT-TOKEN-SENTINEL-MARKER"
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
    secret: dict[str, Any] | None = None,
    issued_token: str = "VAULT-TOKEN-SENTINEL-MARKER",
    read_exc: Exception | None = None,
) -> _FakeClient:
    jwt_auth = _FakeJWTAuth(issued_token=issued_token)
    token_auth = _FakeTokenAuth()
    kv_v2 = _FakeKVv2(
        secret=secret or {"federation_secret_value": "TOP-SECRET-FEDERATION-PAYLOAD"},
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
# Bearer-token leak checks
# ---------------------------------------------------------------------------


def test_bearer_token_does_not_leak_into_logs_on_success_path(
    http_client: TestClient,
    log_buffer: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful auth-and-Vault-read request must not log the bearer token.

    Drives the full happy path (valid JWT → valid Vault login → valid
    secret read → 200) and asserts the verbatim JWT string is absent
    from every log line emitted during the request — handler logs,
    middleware ``request_completed`` log, structlog contextvars dump.
    """
    key = make_rsa_keypair("kid-A")
    token = mint_token(key, sub="op-leak-1")
    _install_fake_vault(monkeypatch)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = http_client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    captured = log_buffer.getvalue()
    assert captured, "expected at least one log line during the authenticated request"
    assert token not in captured, (
        "bearer token leaked into structlog output during the auth-success path"
    )
    # The longest non-trivial JWT segment is the payload; spot-check
    # that no JWS segment leaks even partially.
    for segment in token.split("."):
        if len(segment) >= 20:  # skip the empty / short header segments
            assert segment not in captured, f"JWS segment {segment[:20]!r}... leaked into logs"


def test_bearer_token_does_not_leak_on_jwt_failure_path(
    http_client: TestClient,
    log_buffer: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired JWT 401 path must not log the (rejected) bearer token.

    Drives a failed auth (expired JWT) and asserts the (still
    sensitive) token bytes are absent from the request_completed log
    line that the middleware emits with status=401.
    """
    key = make_rsa_keypair("kid-A")
    token = mint_token(key, expires_in=-3600)
    _install_fake_vault(monkeypatch)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = http_client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    captured = log_buffer.getvalue()
    assert captured, "expected at least one log line during the failed-auth request"
    assert token not in captured, "rejected bearer token leaked into logs"


def test_bearer_token_does_not_leak_when_vault_login_fails(
    http_client: TestClient,
    log_buffer: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth succeeds, Vault login fails — the route logs
    ``federation_health_login_failed`` with the exception class name.
    The bearer token must not appear in that log line."""
    import requests.exceptions

    key = make_rsa_keypair("kid-A")
    token = mint_token(key)
    _install_fake_vault(monkeypatch)

    # Override the fake's login to raise so the failure log fires.
    def _raise_conn(role: str, jwt: str, path: str | None = None) -> dict[str, Any]:
        raise requests.exceptions.ConnectionError("vault unreachable")

    # Re-install with login_exc — simpler than re-patching post-install.
    monkeypatch.setattr(
        vault_module._build_client(get_settings()).auth.jwt,
        "jwt_login",
        _raise_conn,
    )

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = http_client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    captured = log_buffer.getvalue()
    assert "federation_health_login_failed" in captured, (
        "expected the login-failed event to be logged"
    )
    assert token not in captured, "bearer token leaked into the login-failure log line"


# ---------------------------------------------------------------------------
# Vault-token leak checks
# ---------------------------------------------------------------------------


def test_vault_token_never_appears_in_logs_on_success_path(
    http_client: TestClient,
    log_buffer: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Vault token issued by ``jwt_login`` must not appear in any log
    line. The fake's sentinel value (``VAULT-TOKEN-SENTINEL-MARKER``)
    is a known string we grep for verbatim — production code emits
    `federation_health_ok` with `vault_read_path` and the KV version,
    never the auth token."""
    sentinel = "VAULT-TOKEN-DEEP-SENTINEL-XYZ-NEVER-LOG-THIS"
    key = make_rsa_keypair("kid-A")
    token = mint_token(key)
    _install_fake_vault(monkeypatch, issued_token=sentinel)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = http_client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    captured = log_buffer.getvalue()
    assert sentinel not in captured, "Vault-issued token leaked into structlog output"


def test_vault_token_never_appears_in_logs_on_read_failure(
    http_client: TestClient,
    log_buffer: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the secret read fails the route logs ``federation_health_read_failed``;
    the Vault token must still not appear in that log line."""
    sentinel = "VAULT-TOKEN-LEAK-CHECK-READ-FAILURE-MARKER"
    key = make_rsa_keypair("kid-A")
    token = mint_token(key)
    _install_fake_vault(
        monkeypatch,
        issued_token=sentinel,
        read_exc=hvac.exceptions.Forbidden("read denied"),
    )

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = http_client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    captured = log_buffer.getvalue()
    assert "federation_health_read_failed" in captured, (
        "expected the read-failed event to be logged"
    )
    assert sentinel not in captured


# ---------------------------------------------------------------------------
# Secret-value leak checks
# ---------------------------------------------------------------------------


def test_secret_value_does_not_appear_in_logs_on_success_path(
    http_client: TestClient,
    log_buffer: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vault-read success surfaces ``read_ok=true`` with a structured
    ``version=N`` detail — the actual secret value must never appear
    in any log line.

    The fake returns a known sentinel string in ``data["data"]``; the
    test grep-asserts the sentinel is absent from the captured logs.
    The response body is also asserted not to carry the sentinel
    (defence-in-depth against the route accidentally rendering the
    payload alongside the version)."""
    secret_marker = "TOP-SECRET-FEDERATION-VALUE-DO-NOT-LOG-EVER"
    key = make_rsa_keypair("kid-A")
    token = mint_token(key)
    _install_fake_vault(
        monkeypatch,
        secret={"federation_secret_value": secret_marker},
    )

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = http_client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body_text = response.text
    captured = log_buffer.getvalue()
    assert secret_marker not in captured, "secret value leaked into structlog output"
    assert secret_marker not in body_text, (
        "secret value leaked into the /api/v1/health response body"
    )


def test_secret_value_does_not_appear_in_response_or_logs_on_read_failure(
    http_client: TestClient,
    log_buffer: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the read fails the secret value can't have been read — but the
    response body still must not echo any secret-shaped content. The
    detail string only carries the exception class name."""
    key = make_rsa_keypair("kid-A")
    token = mint_token(key)
    _install_fake_vault(
        monkeypatch,
        # The exception message includes the path to test that the
        # path string doesn't bleed into the structured detail.
        read_exc=hvac.exceptions.Forbidden("permission denied on secret/meho/test/federation"),
    )

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = http_client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    detail = body["vault"]["detail"]
    assert detail == "read_failed: Forbidden", (
        f"detail must surface only the class name, got {detail!r}"
    )
    # The Vault path leak shape: "permission denied on secret/..." would
    # be an information-disclosure regression. The response body must
    # not echo it.
    assert "permission denied" not in (detail or "")

    # The autouse ``_no_secret_leak_sweep`` fixture only reads
    # ``capfd`` / ``caplog`` — it does NOT see structlog output that
    # the production app routes through ``PrintLoggerFactory(file=buf)``.
    # This test is therefore the sole guard for the read-failure
    # secret-leak path; assert directly on the structlog buffer that
    # neither the exception's message ("permission denied") nor the
    # Vault path ("secret/meho/test/federation") leak, while the
    # exception class name ("Forbidden") DOES surface (proving the
    # error-shaping logic emitted *something* — the test isn't
    # vacuously passing on an empty buffer).
    captured = log_buffer.getvalue()
    assert "permission denied" not in captured, (
        "Vault exception message leaked into structlog output"
    )
    assert "secret/meho/test/federation" not in captured, (
        "Vault secret path leaked into structlog output"
    )
    assert "Forbidden" in captured, (
        "expected exception class name to surface in logs — "
        "empty/missing log output would mean this test asserts on nothing"
    )


# ---------------------------------------------------------------------------
# Operator-repr leak (regression coverage on the live route path)
# ---------------------------------------------------------------------------


def test_operator_logged_via_request_does_not_leak_raw_jwt(
    http_client: TestClient,
    log_buffer: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a future log line accidentally binds an :class:`Operator`,
    its ``__repr__`` must not surface ``raw_jwt``.

    The Operator model excludes ``raw_jwt`` from ``repr`` via
    ``Field(repr=False)``; this test exercises a real authenticated
    request and asserts no ``raw_jwt`` field name and no token bytes
    appear in the captured log buffer. The contract is enforced at
    the model layer (not the call site) so adding new ``logger.bind(operator=op)``
    sites in future Initiatives can't regress this guarantee."""
    key = make_rsa_keypair("kid-A")
    token = mint_token(key, sub="op-repr-check")
    _install_fake_vault(monkeypatch)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = http_client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    captured = log_buffer.getvalue()
    assert "raw_jwt" not in captured, (
        "raw_jwt field name appeared in logs — Operator.repr must omit it"
    )
    # Also assert the verbatim token value isn't anywhere in the dump.
    assert token not in captured


# ---------------------------------------------------------------------------
# Regex sweep: same patterns the conftest autouse fixture enforces, run
# once explicitly against the structlog buffer to give a clearer failure
# message when a leak is introduced under a private capture (where
# capfd / caplog don't see anything).
# ---------------------------------------------------------------------------


# Re-use the conftest denylist verbatim — keeping the autouse sweep and
# this explicit structlog-buffer pass locked to one source of truth so
# they can never drift. Adding a pattern in conftest automatically
# covers this sweep too.
_LEAK_PATTERNS: tuple[re.Pattern[str], ...] = SECRET_LEAK_PATTERNS


def test_regex_sweep_on_authenticated_request_log_buffer(
    http_client: TestClient,
    log_buffer: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the conftest patterns against a private structlog buffer
    on a live authenticated request.

    The autouse sweep in :mod:`tests.conftest` runs against
    ``capfd`` / ``caplog`` — which doesn't see structlog output that
    goes through ``PrintLoggerFactory(file=buf)``. That's the same
    pattern ``test_observability`` and ``test_api_v1_health`` use; this
    test runs an equivalent regex pass against that buffer so adding
    new log lines under those tests can't introduce a leak that the
    private-buffer assertion misses."""
    key = make_rsa_keypair("kid-A")
    token = mint_token(key, sub="op-sweep-1")
    _install_fake_vault(monkeypatch)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = http_client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    captured = log_buffer.getvalue()
    for pattern in _LEAK_PATTERNS:
        match = pattern.search(captured)
        assert match is None, (
            f"leak pattern {pattern.pattern!r} matched on the structlog buffer: "
            f"preview={match.group(0)[:40]!r}..."
        )


# ---------------------------------------------------------------------------
# Operator-model unit-level leak guard (cheap belt-and-suspenders)
# ---------------------------------------------------------------------------


def test_operator_repr_excludes_raw_jwt() -> None:
    """``repr(Operator(...))`` does not contain the ``raw_jwt`` value.

    Mirrors the existing guard in ``tests/test_auth_jwt.py`` —
    duplicated here so the leak-check file reads as a self-contained
    audit of the secret-bearing surface."""
    sentinel = "header.payload.SIG-VERY-SECRET-DO-NOT-LOG"
    op = Operator(
        sub="op-1",
        name="Alice",
        email="alice@example.com",
        raw_jwt=sentinel,
        tenant_id="00000000-0000-0000-0000-00000000a0a0",
        tenant_role="operator",
    )

    text = repr(op)
    assert sentinel not in text, f"raw_jwt value leaked into repr: {text!r}"
    assert "raw_jwt" not in text, f"raw_jwt field name leaked into repr: {text!r}"
    # The field is still populated and accessible by name.
    assert op.raw_jwt == sentinel
