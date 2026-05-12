# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end tests for :mod:`meho_backplane.middleware`.

The chassis tests in :mod:`tests.test_observability` cover the
``RequestContextMiddleware`` shape (request_id propagation, log JSON
shape, sensitive-header redaction). This file is the G0.1-T3 surface:
the dependency wrapper :func:`~meho_backplane.middleware.verify_jwt_and_bind`
binds ``operator_sub`` *and* ``tenant_id`` into structlog contextvars
so every JSON log line emitted under the same request scope carries
both fields.

The single end-to-end test drives a real FastAPI request through the
production app, captures the structlog JSON output into an
``io.StringIO``, and asserts the ``request_completed`` line carries
the expected ``tenant_id`` (the value the helper-minted JWT encoded).
This is the explicit acceptance-criterion assertion from issue #233:
"verified by an integration test that emits a log line on the request
path and asserts the JSON includes ``tenant_id``."

The capture pattern mirrors :mod:`tests.test_observability` —
``cache_logger_on_first_use=True`` in production means the first
:func:`structlog.get_logger` call pins the file handle for the
process lifetime, so rebinding the factory to an in-memory buffer
inside the test body is the cleanest seam.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest
import respx
import structlog
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks
from ._vault_fakes import install_fake_vault as _install_fake_vault


@pytest.fixture(autouse=True)
def _settings_env(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Pin every env var :class:`Settings` reads.

    ``DATABASE_URL`` is intentionally *not* set here — the conftest
    autouse ``_default_database_url`` fixture provisions a tmp-path
    SQLite DB **and** runs ``alembic upgrade head`` against it, so
    overriding the URL here would point the audit middleware at an
    unmigrated DB and the test would fail with ``no such table:
    audit_log``. The Keycloak / Vault knobs are still pinned because
    the production dependencies dereference them on every request.
    """
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


def _configure_capture(buf: io.StringIO) -> None:
    """Configure structlog to write JSON lines to ``buf``.

    Mirrors :func:`meho_backplane.logging.configure_logging` but with
    the logger factory pointed at the in-memory buffer. ``contextvars.merge_contextvars``
    must come first so the bound ``tenant_id`` lands in the rendered
    JSON.
    """
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
    """Per-test log capture buffer."""
    buf = io.StringIO()
    _configure_capture(buf)
    yield buf
    structlog.reset_defaults()


def _read_log_lines(buf: io.StringIO) -> list[dict[str, object]]:
    """Parse each non-empty line in *buf* as JSON."""
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def test_request_completed_log_carries_tenant_id(
    monkeypatch: pytest.MonkeyPatch,
    log_buffer: io.StringIO,
) -> None:
    """``request_completed`` JSON line carries the JWT's ``tenant_id`` claim.

    Acceptance criterion #1 from issue #233: ``verify_jwt_and_bind``
    binds ``tenant_id`` into structlog contextvars so the
    ``request_completed`` log line emitted by
    :class:`~meho_backplane.middleware.RequestContextMiddleware` (which
    runs *after* the handler completes, inheriting the contextvars
    bound during it) carries the value.

    A non-default ``tenant_id`` is minted into the JWT so a regression
    that hard-codes the default still fails — the assertion proves the
    value rode through JWT → ``verify_jwt_and_bind`` →
    ``contextvars`` → ``merge_contextvars`` JSON renderer.
    """
    custom_tenant = "deadbeef-cafe-1234-5678-abcdefabcdef"
    key = _make_rsa_keypair("kid-LOG")
    token = _mint_token(key, sub="op-log", tenant_id=custom_tenant)
    _install_fake_vault(monkeypatch)

    client = TestClient(app)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200

    completed = [
        entry for entry in _read_log_lines(log_buffer) if entry.get("event") == "request_completed"
    ]
    assert completed, "expected at least one request_completed log line"
    entry = completed[-1]
    assert entry.get("tenant_id") == custom_tenant
    # Sibling chassis context still on the line — proves the binding
    # didn't accidentally clobber the existing operator_sub.
    assert entry.get("operator_sub") == "op-log"


def test_unauthenticated_request_log_has_no_tenant_id(
    log_buffer: io.StringIO,
) -> None:
    """Public surfaces emit ``request_completed`` without ``tenant_id``.

    The skip-rule symmetry: ``verify_jwt_and_bind`` only fires on
    authenticated routes, so a public ``GET /healthz`` produces a
    ``request_completed`` line with no ``tenant_id`` key. This is the
    flip side of the binding contract — proves the bound value is
    *only* present when the auth dependency ran.
    """
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200

    completed = [
        entry for entry in _read_log_lines(log_buffer) if entry.get("event") == "request_completed"
    ]
    assert completed, "expected at least one request_completed log line"
    entry = completed[-1]
    assert "tenant_id" not in entry
    assert "operator_sub" not in entry
