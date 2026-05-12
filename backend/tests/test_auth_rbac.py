# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit + integration coverage for ``require_role`` and the env-gated stub routes.

Covers Task #234's acceptance criteria:

* ``require_role(role)`` exists in :mod:`meho_backplane.auth.rbac` and
  is importable.
* ``tenant_admin`` JWT → ``/api/v1/rbac-test/admin`` returns 200.
* ``operator`` JWT → ``/api/v1/rbac-test/admin`` returns 403 plus an
  ``insufficient_role`` log line.
* ``read_only`` JWT → ``/api/v1/rbac-test/admin`` returns 403 and
  ``/api/v1/rbac-test/operator`` returns 403.
* ``operator`` JWT → ``/api/v1/rbac-test/operator`` returns 200 (the
  operator-or-higher gate).
* ``tenant_admin`` JWT → ``/api/v1/rbac-test/operator`` returns 200
  (admin >= operator under the linear ordering).
* In production deploys (``MEHO_ENABLE_RBAC_TEST_ROUTE`` unset / falsy)
  the test routes 404. The unit suite asserts on a freshly-built app
  with the flag *off*.
* The router-level dependency rejection enforces that the
  ``insufficient_role`` event includes the operator ``sub`` and both
  the actual and required role values — the on-call telemetry contract
  the rbac module's docstring promises.

Test strategy mirrors ``test_api_v1_health.py``:

* Settings env vars are pinned per-test through monkeypatch +
  ``get_settings.cache_clear`` so the autouse default DB and the
  per-test ``MEHO_ENABLE_RBAC_TEST_ROUTE`` toggle interleave cleanly.
* Each test that needs the stub routes constructs its own
  :class:`fastapi.FastAPI` (the module-level ``app`` in
  :mod:`meho_backplane.main` was instantiated before the env-var
  toggle existed; reusing it would leave the routes unmounted).
* JWTs are signed locally with the shared
  ``tests._oidc_jwt_helpers`` minter; Keycloak discovery + JWKS are
  stubbed via :mod:`respx`, exactly as ``test_api_v1_health`` does.
* Logs are captured via the same in-memory ``StringIO`` rebinding of
  structlog that ``test_observability`` and ``test_api_v1_health`` use,
  so the ``insufficient_role`` event can be parsed back into a dict and
  asserted on.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
import respx
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.api.v1.rbac_test import router as rbac_test_router
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

# ---------------------------------------------------------------------------
# Settings + JWKS-cache fixtures (lifted from test_api_v1_health.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` reads, around every test.

    ``MEHO_ENABLE_RBAC_TEST_ROUTE`` is intentionally **not** set here;
    individual tests that need the routes mounted set it (and rebuild
    their app), and the prod-default-off test asserts the unset state.
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
    monkeypatch.delenv("MEHO_ENABLE_RBAC_TEST_ROUTE", raising=False)
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


def _read_log_lines(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# App construction with the rbac-test routes mounted
# ---------------------------------------------------------------------------


def _build_app_with_rbac_test_routes() -> FastAPI:
    """Return a :class:`FastAPI` mirroring prod shape with the stub routes on.

    We deliberately do **not** import :data:`meho_backplane.main.app`
    and call ``app.include_router(rbac_test_router)`` after the fact:
    the production gate runs at module-load time on ``get_settings()``,
    and a test that mutated the production singleton would leak the
    stub mount across files. A fresh :class:`FastAPI` is cheap and
    isolates the test surface.

    The mounted router is the *real* :data:`rbac_test_router` so the
    integration coverage exercises the actual production path through
    :func:`require_role` → :func:`verify_jwt_and_bind` → :func:`verify_jwt`.
    """
    fresh = FastAPI()
    fresh.add_middleware(RequestContextMiddleware)
    fresh.include_router(rbac_test_router)
    return fresh


# ---------------------------------------------------------------------------
# Phase 1 — pure unit tests on require_role (no FastAPI plumbing)
# ---------------------------------------------------------------------------


def _make_operator(role: TenantRole, *, sub: str = "op-unit") -> Operator:
    """Construct a synthetic :class:`Operator` for unit-level checks."""
    return Operator(
        sub=sub,
        name="Unit",
        email="unit@example.com",
        raw_jwt="unit-fake-jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=role,
    )


def test_require_role_is_importable() -> None:
    """AC #1: ``require_role`` is importable from ``auth.rbac``."""
    assert callable(require_role)


def test_require_role_returns_callable_dependency() -> None:
    """The factory returns a callable suitable for ``Depends(...)``."""
    dep = require_role(TenantRole.OPERATOR)
    assert callable(dep)


def test_require_role_admin_passes_admin_gate() -> None:
    dep = require_role(TenantRole.TENANT_ADMIN)
    op = _make_operator(TenantRole.TENANT_ADMIN)
    assert dep(operator=op) is op


def test_require_role_admin_passes_operator_gate() -> None:
    dep = require_role(TenantRole.OPERATOR)
    op = _make_operator(TenantRole.TENANT_ADMIN)
    assert dep(operator=op) is op


def test_require_role_operator_passes_operator_gate() -> None:
    dep = require_role(TenantRole.OPERATOR)
    op = _make_operator(TenantRole.OPERATOR)
    assert dep(operator=op) is op


def test_require_role_operator_blocked_from_admin_gate() -> None:
    from fastapi import HTTPException

    dep = require_role(TenantRole.TENANT_ADMIN)
    op = _make_operator(TenantRole.OPERATOR)
    with pytest.raises(HTTPException) as excinfo:
        dep(operator=op)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "insufficient_role"


def test_require_role_read_only_blocked_from_operator_gate() -> None:
    from fastapi import HTTPException

    dep = require_role(TenantRole.OPERATOR)
    op = _make_operator(TenantRole.READ_ONLY)
    with pytest.raises(HTTPException) as excinfo:
        dep(operator=op)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "insufficient_role"


def test_require_role_read_only_blocked_from_admin_gate() -> None:
    from fastapi import HTTPException

    dep = require_role(TenantRole.TENANT_ADMIN)
    op = _make_operator(TenantRole.READ_ONLY)
    with pytest.raises(HTTPException) as excinfo:
        dep(operator=op)
    assert excinfo.value.status_code == 403


def test_require_role_read_only_passes_read_only_gate() -> None:
    """The least-privileged gate is satisfied by every role."""
    dep = require_role(TenantRole.READ_ONLY)
    for role in (TenantRole.READ_ONLY, TenantRole.OPERATOR, TenantRole.TENANT_ADMIN):
        op = _make_operator(role)
        assert dep(operator=op) is op


# ---------------------------------------------------------------------------
# Phase 2 — integration tests through the FastAPI dependency graph
# ---------------------------------------------------------------------------


@pytest.fixture
def rbac_client(log_buffer: io.StringIO) -> Iterator[TestClient]:
    """``TestClient`` driving a fresh app with the stub routes mounted.

    ``log_buffer`` precedes the client so structlog's factory is rebound
    before any handler logs through it. The TestClient is *not* used as
    a context manager: entering it would re-run lifespan hooks (which
    on the production app re-call ``configure_logging`` and clobber
    capture). The fresh FastAPI instance built here has no lifespan, so
    the entry-context distinction is moot, but we keep the same shape
    for consistency with ``test_api_v1_health.client``.
    """
    yield TestClient(_build_app_with_rbac_test_routes())


def test_admin_jwt_admin_route_returns_200(
    rbac_client: TestClient,
    log_buffer: io.StringIO,
) -> None:
    """AC: tenant_admin JWT → /rbac-test/admin returns 200."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key,
        sub="op-admin-1",
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = rbac_client.get(
            "/api/v1/rbac-test/admin",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] == "true"
    assert body["operator_sub"] == "op-admin-1"
    # No insufficient_role event on the happy path.
    assert all(line.get("event") != "insufficient_role" for line in _read_log_lines(log_buffer))


def test_operator_jwt_admin_route_returns_403_with_log(
    rbac_client: TestClient,
    log_buffer: io.StringIO,
) -> None:
    """AC: operator JWT → /rbac-test/admin returns 403 + log event."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key,
        sub="op-mid-1",
        tenant_role=TenantRole.OPERATOR.value,
    )

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = rbac_client.get(
            "/api/v1/rbac-test/admin",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient_role"}

    insufficient = [
        line for line in _read_log_lines(log_buffer) if line.get("event") == "insufficient_role"
    ]
    assert len(insufficient) == 1
    payload = insufficient[0]
    assert payload["operator_sub"] == "op-mid-1"
    assert payload["actual_role"] == "operator"
    assert payload["required_role"] == "tenant_admin"


def test_read_only_jwt_admin_route_returns_403(rbac_client: TestClient) -> None:
    """AC: read_only JWT → /rbac-test/admin returns 403."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key,
        sub="op-ro-1",
        tenant_role=TenantRole.READ_ONLY.value,
    )

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = rbac_client.get(
            "/api/v1/rbac-test/admin",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient_role"}


def test_read_only_jwt_operator_route_returns_403(rbac_client: TestClient) -> None:
    """AC: read_only JWT → /rbac-test/operator returns 403."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key,
        sub="op-ro-2",
        tenant_role=TenantRole.READ_ONLY.value,
    )

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = rbac_client.get(
            "/api/v1/rbac-test/operator",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient_role"}


def test_operator_jwt_operator_route_returns_200(rbac_client: TestClient) -> None:
    """AC: operator JWT → /rbac-test/operator returns 200 (operator-or-higher)."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key,
        sub="op-mid-2",
        tenant_role=TenantRole.OPERATOR.value,
    )

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = rbac_client.get(
            "/api/v1/rbac-test/operator",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json()["operator_sub"] == "op-mid-2"


def test_admin_jwt_operator_route_returns_200(rbac_client: TestClient) -> None:
    """AC: tenant_admin JWT → /rbac-test/operator returns 200 (admin >= operator)."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key,
        sub="op-admin-2",
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = rbac_client.get(
            "/api/v1/rbac-test/operator",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json()["operator_sub"] == "op-admin-2"


def test_missing_authorization_returns_401(rbac_client: TestClient) -> None:
    """The verify_jwt 401 contract is preserved through the rbac dependency."""
    response = rbac_client.get("/api/v1/rbac-test/admin")
    assert response.status_code == 401
    assert response.json() == {"detail": "missing_token"}


# ---------------------------------------------------------------------------
# Phase 3 — production-default-off contract
# ---------------------------------------------------------------------------


def test_production_default_does_not_mount_rbac_test_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: with ``MEHO_ENABLE_RBAC_TEST_ROUTE`` unset, the routes 404.

    Builds a fresh app the same way :mod:`meho_backplane.main` does, but
    inline (so the assertion does not depend on the production module's
    import-time state). The autouse ``_settings_env`` fixture has
    already deleted ``MEHO_ENABLE_RBAC_TEST_ROUTE``, so
    :class:`Settings.enable_rbac_test_route` is the default ``False``;
    the conditional import + include never runs and the routes are
    genuinely unmounted (404, not 403).
    """
    fresh = FastAPI()
    fresh.add_middleware(RequestContextMiddleware)
    if get_settings().enable_rbac_test_route:
        fresh.include_router(rbac_test_router)

    test_client = TestClient(fresh)
    for path in ("/api/v1/rbac-test/admin", "/api/v1/rbac-test/operator"):
        response = test_client.get(path)
        assert response.status_code == 404, path


def test_settings_enable_rbac_test_route_default_is_false() -> None:
    """The Settings field defaults to False even when the env var is unset."""
    settings = get_settings()
    assert settings.enable_rbac_test_route is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_settings_enable_rbac_test_route_truthy_values(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    """The truthy spellings flip the flag on."""
    monkeypatch.setenv("MEHO_ENABLE_RBAC_TEST_ROUTE", value)
    get_settings.cache_clear()
    assert get_settings().enable_rbac_test_route is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "disabled", "FOO"])
def test_settings_enable_rbac_test_route_falsy_values(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    """Anything outside the truthy whitelist evaluates to False."""
    monkeypatch.setenv("MEHO_ENABLE_RBAC_TEST_ROUTE", value)
    get_settings.cache_clear()
    assert get_settings().enable_rbac_test_route is False
