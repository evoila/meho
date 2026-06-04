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

import asyncio
import io
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import hvac.exceptions
import pytest
import respx
import structlog
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from meho_backplane.auth import vault as vault_module
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vault import VaultConnector, register_vault_typed_operations
from meho_backplane.main import app
from meho_backplane.operations import reset_dispatcher_caches
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


@pytest.fixture(autouse=True)
def _registered_vault_substrate(_settings_env: None) -> Iterator[None]:
    """Re-establish the v2 connector entry + the ``vault.kv.read`` descriptor row.

    The ``TestClient(app)`` fixture in this file does not enter the
    FastAPI lifespan (the log-capture configuration would be clobbered
    by the lifespan's ``configure_logging`` step), so the lifespan's
    ``run_typed_op_registrars`` step never fires. Replicate the v2
    registry entry + typed-op descriptor upsert directly here so the
    dispatcher's natural-key lookup finds the row at request time.
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


# ---------------------------------------------------------------------------
# Scheduler agent client_credentials secret leak (#1488)
#
# A failed scheduled agent run is logged via ``_log.exception(
# "scheduler_fire_failed", ...)`` on ``run_one_tick``'s broad ``except``.
# structlog's ``dict_tracebacks`` renders frame locals; the agent's
# ``client_credentials`` secret used to be a plain ``str`` frame local on
# the captured traceback (``_PreparedInvocation.agent_client_secret`` /
# the ``agent_client_secret`` kwarg through ``run_scheduled``), so every
# failed fire wrote it to stdout in cleartext (CWE-532).
#
# The fix is defense in depth: (1) the secret is a ``SecretStr`` from
# ``_PreparedInvocation`` through ``run_scheduled`` (masks to
# ``'**********'`` even as a bare frame local), unwrapped only at the
# token-mint call; and (2) production ``configure_logging`` runs the
# traceback transformer with ``show_locals=False`` (drops every frame's
# locals dict). Each test below pins one layer so a regression in either
# is caught independently.
# ---------------------------------------------------------------------------

#: Sentinel standing in for the agent ``client_credentials`` secret. An
#: obvious dummy — never a realistic credential value — that we grep the
#: rendered traceback for. Module-level so the value is never a bare local
#: in a test *function* frame (which would itself land in a ``show_locals``
#: dump and make the assertion vacuously fail on the test's own frame
#: rather than the production frames under test).
_AGENT_SECRET_CANARY = "dummy-agent-client-secret-CANARY-1488"


def _configure_production_capture(buf: io.StringIO) -> None:
    """Bind the **production** structlog chain to ``buf``.

    Unlike :func:`_configure_capture` (which mirrors the pre-#1488
    default with ``show_locals=True``), this drives the real
    :func:`meho_backplane.logging.configure_logging` processor list, so
    the ``show_locals=False`` layer is exercised exactly as it runs in
    production. The logger factory is swapped to write to ``buf`` after
    the production configure call.
    """
    from meho_backplane.logging import configure_logging

    structlog.reset_defaults()
    configure_logging(level=logging.INFO)
    # Re-point the factory at the buffer; the production configure writes
    # to the lazy stdout factory, which pytest capture would intercept
    # less reliably than a private buffer.
    current = structlog.get_config()
    structlog.configure(
        processors=current["processors"],
        wrapper_class=current["wrapper_class"],
        logger_factory=structlog.PrintLoggerFactory(file=buf),
        cache_logger_on_first_use=False,
    )


def _raise_dispatch_through_scheduler_log(buf: io.StringIO, exc: Exception) -> None:
    """Drive a failed fire through the real ``scheduler_fire_failed`` path.

    Mirrors ``run_one_tick``'s per-row failure handler verbatim: build a
    ``_PreparedInvocation`` holding the secret as a ``SecretStr``, call
    ``_dispatch_invocation`` (which forwards into ``run_scheduled``),
    and log any escaping exception via ``_log.exception(
    "scheduler_fire_failed", ...)`` so the captured traceback spans the
    secret-bearing frames. *exc* is raised from inside ``run_scheduled``
    (stubbed) so it is **not** in ``_dispatch_invocation``'s narrow
    ``except`` list and escapes to the broad handler — the exact path
    the leak travelled.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from meho_backplane.scheduler import loop as scheduler_loop

    invoker = SimpleNamespace(run_scheduled=AsyncMock(side_effect=exc))
    prepared = scheduler_loop._PreparedInvocation(  # type: ignore[attr-defined]
        name="reporter",
        identity_ref="agent:reporter",
        agent_client_id="agent:reporter",
        # The canary lives only inside the SecretStr from here on.
        agent_client_secret=SecretStr(_AGENT_SECRET_CANARY),
        inputs_str="ping",
    )
    row = SimpleNamespace(id="11111111-1111-1111-1111-111111111111", kind="cron")
    log = structlog.get_logger("meho_backplane.scheduler.loop")
    try:
        asyncio.run(
            scheduler_loop._dispatch_invocation(row, prepared, invoker)  # type: ignore[arg-type]
        )
    except Exception:
        # Verbatim shape of run_one_tick's per-row except (loop.py).
        log.exception("scheduler_fire_failed", trigger_id=str(row.id))


@pytest.mark.parametrize(
    "exc",
    [
        # The audience 401 the sibling ticket triggers: an HTTPException
        # is not in _dispatch_invocation's narrow except list, so it
        # escapes to the broad scheduler_fire_failed handler.
        HTTPException(status_code=401, detail="invalid_audience"),
        # A distinct, unexpected failure cause (e.g. a bug in the budget
        # system surfacing as a bare RuntimeError) — also escapes.
        RuntimeError("unexpected budget-subsystem failure"),
    ],
    ids=["audience_401", "runtime_error"],
)
def test_scheduler_secret_never_renders_in_fire_failed_log(
    exc: Exception,
) -> None:
    """A failed scheduled run never writes the agent secret to the logs.

    Drives the production ``configure_logging`` chain (``show_locals=
    False`` plus ``SecretStr``) and asserts the secret canary is absent
    from the ``scheduler_fire_failed`` JSON log line, for two distinct
    escaping exceptions (acceptance criterion: holds regardless of which
    exception escapes)."""
    buf = io.StringIO()
    _configure_production_capture(buf)
    try:
        _raise_dispatch_through_scheduler_log(buf, exc)
        captured = buf.getvalue()
    finally:
        structlog.reset_defaults()

    # Non-vacuous: the failure WAS logged (else the assertion below is
    # trivially true on an empty buffer).
    assert "scheduler_fire_failed" in captured, (
        "expected the scheduler_fire_failed event to be logged"
    )
    assert _AGENT_SECRET_CANARY not in captured, (
        "agent client_credentials secret leaked into the scheduler_fire_failed log line"
    )


def test_scheduler_secret_masked_even_with_show_locals_enabled() -> None:
    """The ``SecretStr`` layer masks the secret even if locals are rendered.

    Pins the type-level guarantee independently of the
    ``show_locals=False`` logging config: with frame locals *explicitly
    re-enabled* (the pre-#1488 structlog default), the secret still does
    not appear — it renders as ``SecretStr('**********')`` because it is
    held as a ``SecretStr``. This is the defense that survives anyone
    later re-enabling ``show_locals`` for triage, or a new log call
    rendering a prepared-invocation frame."""
    from structlog.tracebacks import ExceptionDictTransformer

    buf = io.StringIO()
    structlog.reset_defaults()
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            # Frame locals ON — the exact vector #1488 closes elsewhere.
            structlog.processors.ExceptionRenderer(ExceptionDictTransformer(show_locals=True)),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=buf),
        cache_logger_on_first_use=False,
    )
    try:
        _raise_dispatch_through_scheduler_log(buf, RuntimeError("boom with locals on"))
        captured = buf.getvalue()
    finally:
        structlog.reset_defaults()

    assert "scheduler_fire_failed" in captured
    # Locals are present (proves show_locals=True actually rendered them),
    # but the secret is masked, not leaked.
    assert _AGENT_SECRET_CANARY not in captured, (
        "SecretStr failed to mask the agent secret with show_locals=True"
    )
    assert "**********" in captured, (
        "expected the SecretStr-masked form to appear in the rendered locals "
        "(else the locals dump did not include the secret-bearing frame and "
        "this test asserts on nothing)"
    )


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
