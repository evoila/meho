# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.memory`.

G5.1-T2 (#422) acceptance criteria coverage:

* **Route mounting** -- all four routes appear in the FastAPI app's
  route table; the OpenAPI document advertises them at
  ``/api/v1/openapi.json``.
* **Unauthenticated** -- every route returns 401 without a token.
* **Remember** -- ``operator`` POSTs; service-layer
  :class:`PermissionDeniedError` → 403; service-layer
  :class:`ValueError` (target_name missing for target-scoped write)
  → 422; audit row carries ``op_id="memory.remember"`` +
  ``op_class="write"`` + ``scope`` + ``slug``; body NOT in payload.
* **List** -- ``operator`` GETs; filters (``scope`` / ``slug_pattern``
  / ``tag`` / ``include_expired`` / ``limit``) reach the service;
  audit row carries ``op_id="memory.list"`` + ``op_class="read"``.
* **Recall** -- existing key returns 200 + full entry; service
  returning ``None`` (not-found OR RBAC-denied OR cross-user) returns
  404 (info-leak avoidance); audit row carries ``op_id="memory.recall"``
  + ``op_class="read"``.
* **Forget** -- existing row returns 204; idempotent (204 even when
  the row was already absent); :class:`PermissionDeniedError` → 403;
  :class:`ValueError` → 422; audit row carries ``op_id="memory.forget"``
  + ``op_class="write"`` + ``existed`` flag.
* **Info-leak avoidance regression** -- cross-user recall against a
  user-scoped slug returns 404, NOT 403 (the route never raises 403
  on read).

Tests boot the FastAPI app with the production middleware stack
(:class:`RequestContextMiddleware` + :class:`AuditMiddleware`) so
audit rows are inserted into the autouse-migrated SQLite engine.
:class:`MemoryService` is patched on the route's import site for the
happy / sad path unit tests; integration coverage against a real PG
cluster lives in the G5.1-T5 (#426) canary acceptance.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
import respx
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.memory import router as memory_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.memory.rbac import PermissionDeniedError
from meho_backplane.memory.schemas import MemoryEntry, MemoryScope
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

# ---------------------------------------------------------------------------
# Settings + JWKS cache fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` reads."""
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
# Log capture (mirrors test_api_v1_kb.py)
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
# App construction
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """Return a :class:`FastAPI` mirroring prod with only the memory router mounted.

    Includes the production middleware stack so audit payload tests see
    the same contextvar-binding flow production uses.
    """
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(memory_router)
    return app


@pytest.fixture
def client(log_buffer: io.StringIO) -> Iterator[TestClient]:
    """``TestClient`` driving a fresh app per test."""
    yield TestClient(_build_app())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_token(*, tenant_id: UUID | None = None, sub: str = "op-admin") -> tuple[Any, str]:
    """Mint a JWT for a ``tenant_admin`` operator."""
    key = _make_rsa_keypair("kid-admin")
    tid = tenant_id if tenant_id is not None else uuid.uuid4()
    token = _mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=str(tid),
    )
    return key, token


def _operator_token(*, tenant_id: UUID | None = None, sub: str = "op-operator") -> tuple[Any, str]:
    """Mint a JWT for a non-admin ``operator``."""
    key = _make_rsa_keypair("kid-operator")
    tid = tenant_id if tenant_id is not None else uuid.uuid4()
    token = _mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(tid),
    )
    return key, token


def _readonly_token(*, tenant_id: UUID | None = None, sub: str = "op-readonly") -> tuple[Any, str]:
    """Mint a JWT for a ``read_only`` operator."""
    key = _make_rsa_keypair("kid-readonly")
    tid = tenant_id if tenant_id is not None else uuid.uuid4()
    token = _mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.READ_ONLY.value,
        tenant_id=str(tid),
    )
    return key, token


def _authed(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_entry(
    *,
    scope: MemoryScope = MemoryScope.USER,
    slug: str = "wine-preference",
    body: str = "Prefers Pinot Noir.",
    user_sub: str | None = "op-operator",
    target_name: str | None = None,
    expires_at: datetime | None = None,
) -> MemoryEntry:
    """Build a synthetic :class:`MemoryEntry` for stubbed service responses."""
    return MemoryEntry(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        tenant_id=UUID("22222222-2222-2222-2222-222222222222"),
        scope=scope,
        slug=slug,
        body=body,
        metadata={"user_sub": user_sub, "target_name": target_name},
        expires_at=expires_at,
        user_sub=user_sub,
        target_name=target_name,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
    )


async def _audit_rows_for_path(path: str) -> list[AuditLog]:
    """Read back every audit row whose ``path`` equals *path*."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == path))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Route mounting (acceptance criterion: visible in openapi.json)
# ---------------------------------------------------------------------------


def test_all_four_routes_mounted_on_main_app() -> None:
    """All four routes appear in :mod:`meho_backplane.main`'s app + OpenAPI."""
    from meho_backplane.main import app

    expected_paths = {
        "/api/v1/memory",
        "/api/v1/memory/{scope}/{slug}",
    }
    actual_paths = {getattr(r, "path", None) for r in app.routes}
    missing = expected_paths - actual_paths
    assert not missing, f"missing routes: {missing}"

    openapi = app.openapi()
    paths = openapi["paths"]
    assert "post" in paths["/api/v1/memory"]
    assert "get" in paths["/api/v1/memory"]
    assert "get" in paths["/api/v1/memory/{scope}/{slug}"]
    assert "delete" in paths["/api/v1/memory/{scope}/{slug}"]


# ---------------------------------------------------------------------------
# Unauthenticated (401) -- every route
# ---------------------------------------------------------------------------


def test_remember_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.post(
        "/api/v1/memory",
        json={"scope": "user", "body": "x"},
    )
    assert response.status_code == 401


def test_list_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.get("/api/v1/memory")
    assert response.status_code == 401


def test_recall_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.get("/api/v1/memory/user/my-pref")
    assert response.status_code == 401


def test_forget_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.delete("/api/v1/memory/user/my-pref")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# RBAC -- read_only role is denied all writes; read routes accept operator+
# ---------------------------------------------------------------------------


def test_remember_readonly_role_returns_403_at_require_role_gate(
    client: TestClient,
) -> None:
    """``read_only`` role on POST → 403 (``require_role(OPERATOR)`` gate).

    The route's FastAPI dependency is ``require_role(OPERATOR)``, so a
    ``read_only`` JWT is rejected with 403 ``insufficient_role`` *before*
    the service is reached. The matrix's "read_only cannot write" rule
    is also enforced one layer deeper at the service-level RBAC
    resolver (proven by ``test_memory_service.py``); this test pins
    the outer gate so a future change relaxing the dependency to
    ``require_role(READ_ONLY)`` would still surface the failure.
    """
    tenant_a = uuid.uuid4()
    key, token = _readonly_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory",
            json={"scope": "user", "body": "hi"},
            headers=_authed(token),
        )
    assert response.status_code == 403
    assert response.json()["detail"] == "insufficient_role"


def test_list_readonly_role_accepted(client: TestClient) -> None:
    """``read_only`` role on GET / → 200 (read routes are read_only-or-above).

    The :class:`MemoryRbacResolver` matrix explicitly allows
    ``read_only`` operators to read ``tenant`` / ``target`` scopes
    (consumer-needs.md §G5 L131: "the team becomes the unit of
    memory"). The route's FastAPI dependency is ``require_role(
    READ_ONLY)`` so read_only passes; user-scoped row visibility is
    still filtered to ``operator.sub == stored.user_sub`` at the
    service layer.
    """
    tenant_a = uuid.uuid4()
    key, token = _readonly_token(tenant_id=tenant_a)
    fake_list = AsyncMock(return_value=[])
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.list_memories", fake_list),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/memory", headers=_authed(token))
    assert response.status_code == 200


def test_recall_readonly_role_accepted(client: TestClient) -> None:
    """``read_only`` role on recall → 200 (read routes are read_only-or-above)."""
    tenant_a = uuid.uuid4()
    key, token = _readonly_token(tenant_id=tenant_a)
    fake_recall = AsyncMock(
        return_value=_make_entry(
            scope=MemoryScope.TENANT,
            slug="team-runbook",
            user_sub=None,
        ),
    )
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.recall", fake_recall),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/memory/tenant/team-runbook",
            headers=_authed(token),
        )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# POST -- remember (happy path + matrix errors)
# ---------------------------------------------------------------------------


def test_remember_returns_201_and_entry(client: TestClient) -> None:
    """Operator POST → 201 + the stored entry."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_remember = AsyncMock(
        return_value=_make_entry(scope=MemoryScope.USER, slug="wine-preference"),
    )
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.remember", fake_remember),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory",
            json={
                "scope": "user",
                "body": "Prefers Pinot Noir.",
                "slug": "wine-preference",
            },
            headers=_authed(token),
        )

    assert response.status_code == 201
    body = response.json()
    assert body["scope"] == "user"
    assert body["slug"] == "wine-preference"
    fake_remember.assert_awaited_once()
    call_kwargs = fake_remember.await_args.kwargs
    assert call_kwargs["scope"] is MemoryScope.USER
    assert call_kwargs["body"] == "Prefers Pinot Noir."
    assert call_kwargs["slug"] == "wine-preference"


def test_remember_target_scoped_without_target_name_returns_422(
    client: TestClient,
) -> None:
    """Service raising ValueError (target_name missing) → 422."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_remember = AsyncMock(
        side_effect=ValueError("target_name is required for scope=target"),
    )
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.remember", fake_remember),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory",
            json={"scope": "target", "body": "shared"},
            headers=_authed(token),
        )
    assert response.status_code == 422
    assert "target_name" in response.json()["detail"]


def test_remember_tenant_scope_operator_role_returns_403(client: TestClient) -> None:
    """Non-admin operator writing tenant scope → service raises 403."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_remember = AsyncMock(
        side_effect=PermissionDeniedError(
            MemoryScope.TENANT, "role=operator cannot write scope=tenant"
        ),
    )
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.remember", fake_remember),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory",
            json={"scope": "tenant", "body": "team note"},
            headers=_authed(token),
        )
    assert response.status_code == 403


def test_remember_unknown_field_returns_422(client: TestClient) -> None:
    """``extra="forbid"`` rejects unknown body fields at 422."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory",
            json={"scope": "user", "body": "x", "typo_field": "oops"},
            headers=_authed(token),
        )
    assert response.status_code == 422


def test_remember_empty_body_returns_422(client: TestClient) -> None:
    """Empty ``body`` field fails Pydantic min_length validation."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory",
            json={"scope": "user", "body": ""},
            headers=_authed(token),
        )
    assert response.status_code == 422


def test_remember_invalid_slug_returns_422(client: TestClient) -> None:
    """Slug failing :data:`SLUG_PATTERN` surfaces as 422 at the pydantic gate."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory",
            json={"scope": "user", "body": "x", "slug": "bad slug!"},
            headers=_authed(token),
        )
    assert response.status_code == 422


def test_remember_accepts_expires_at_iso_string(client: TestClient) -> None:
    """ISO-8601 ``expires_at`` parses through Pydantic to a datetime."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    future = datetime.now(tz=UTC) + timedelta(days=7)
    fake_remember = AsyncMock(
        return_value=_make_entry(
            scope=MemoryScope.USER,
            slug="auto-expire",
            expires_at=future,
        ),
    )
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.remember", fake_remember),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory",
            json={
                "scope": "user",
                "body": "expires-soon",
                "slug": "auto-expire",
                "expires_at": future.isoformat(),
            },
            headers=_authed(token),
        )
    assert response.status_code == 201
    call_kwargs = fake_remember.await_args.kwargs
    assert call_kwargs["expires_at"] is not None
    assert isinstance(call_kwargs["expires_at"], datetime)


# ---------------------------------------------------------------------------
# G5.2-T2 (#624): default-TTL injection on ``memory-user`` writes
# ---------------------------------------------------------------------------
#
# The handler computes ``expires_at = now(UTC) +
# memory_user_default_ttl_days`` when the request omits ``expires_at``
# AND ``body.scope == MemoryScope.USER``. Two opt-outs:
#
# * explicit ``"expires_at": null`` in the request body → no default
#   (the CLI ``--persist`` shape). Detection uses
#   :attr:`BaseModel.model_fields_set`, not ``body.expires_at is None``.
# * non-``user`` scopes (``user-tenant`` / ``user-target`` / ``tenant``
#   / ``target``) → no default per #624's narrow scope ("``kind ==
#   'memory-user'``").
#
# Tests assert the value the handler passes to ``MemoryService.remember``
# via the ``expires_at`` kwarg -- the storage layer is the substrate's
# concern; the surface contract is what this route sends downstream.


def test_remember_user_scope_no_expires_at_injects_default_7_days(
    client: TestClient,
) -> None:
    """Omitted ``expires_at`` on ``user`` scope → default ``now + 7d``.

    Acceptance criterion: stored row has ``metadata.expires_at ~ now
    + MEMORY_USER_DEFAULT_TTL_DAYS`` (within tolerance for clock drift
    between the test invocation and the handler's ``datetime.now``).
    """
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_remember = AsyncMock(
        return_value=_make_entry(scope=MemoryScope.USER, slug="auto-default"),
    )
    before = datetime.now(UTC)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.remember", fake_remember),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory",
            json={"scope": "user", "body": "default-ttl note"},
            headers=_authed(token),
        )
    after = datetime.now(UTC)
    assert response.status_code == 201
    call_kwargs = fake_remember.await_args.kwargs
    expires_at = call_kwargs["expires_at"]
    # Default is 7 days from the ``Settings`` ``memory_user_default_ttl_days``
    # field's own default; verifying the value lies in ``[before+7d,
    # after+7d]`` covers clock-drift tolerance between the test's
    # ``datetime.now`` reads and the handler's call without coupling
    # the assertion to a fixed-second cutoff.
    assert expires_at is not None
    assert isinstance(expires_at, datetime)
    assert before + timedelta(days=7) <= expires_at <= after + timedelta(days=7)


def test_remember_user_scope_explicit_null_skips_default(
    client: TestClient,
) -> None:
    """Explicit ``"expires_at": null`` → no default; row persists forever.

    Load-bearing opt-out semantics: the discrimination must be on
    :attr:`BaseModel.model_fields_set` (pydantic v2), not
    ``body.expires_at is None`` -- otherwise the CLI ``--persist`` flag
    (which emits ``"expires_at": null``) would collapse into the default
    path and the operator's pin-forever intent would be silently
    overridden.
    """
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_remember = AsyncMock(
        return_value=_make_entry(scope=MemoryScope.USER, slug="persisted"),
    )
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.remember", fake_remember),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory",
            json={"scope": "user", "body": "pin forever", "expires_at": None},
            headers=_authed(token),
        )
    assert response.status_code == 201
    call_kwargs = fake_remember.await_args.kwargs
    assert call_kwargs["expires_at"] is None


def test_remember_user_scope_explicit_expires_at_honoured_verbatim(
    client: TestClient,
) -> None:
    """Explicit ISO-8601 ``expires_at`` is honoured (no override)."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    future = datetime(2027, 6, 1, 12, 0, 0, tzinfo=UTC)
    fake_remember = AsyncMock(
        return_value=_make_entry(scope=MemoryScope.USER, slug="explicit", expires_at=future),
    )
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.remember", fake_remember),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory",
            json={
                "scope": "user",
                "body": "expires-on-jun-1",
                "expires_at": future.isoformat(),
            },
            headers=_authed(token),
        )
    assert response.status_code == 201
    call_kwargs = fake_remember.await_args.kwargs
    assert call_kwargs["expires_at"] == future


def test_remember_user_tenant_scope_no_default_ttl(client: TestClient) -> None:
    """``user-tenant`` scope is *not* in scope for the default TTL.

    #624 narrows the default to ``kind == "memory-user"`` (i.e.
    :attr:`MemoryScope.USER`). The ``USER_TENANT`` /
    ``USER_TARGET`` / ``TENANT`` / ``TARGET`` scopes pass
    ``expires_at`` through unchanged when omitted (i.e. ``None``).
    Pinning this prevents a future refactor that widened the gate
    to ``USER_SCOPED`` from silently expiring team-shared memories.
    """
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_remember = AsyncMock(
        return_value=_make_entry(scope=MemoryScope.USER_TENANT, slug="team-note"),
    )
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.remember", fake_remember),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory",
            json={"scope": "user-tenant", "body": "team note"},
            headers=_authed(token),
        )
    assert response.status_code == 201
    call_kwargs = fake_remember.await_args.kwargs
    assert call_kwargs["expires_at"] is None


def test_remember_tenant_scope_no_default_ttl(client: TestClient) -> None:
    """``tenant`` scope (admin-only write) also bypasses the default.

    Tenant-shared memory is an explicitly operator-managed coordinate
    -- defaulting a 7-day expiry on the tenant admin's note would
    surprise the operator. The route mints a ``tenant_admin`` token
    here so the service-layer RBAC matrix doesn't 403 the call.
    """
    tenant_a = uuid.uuid4()
    key, token = _admin_token(tenant_id=tenant_a)
    fake_remember = AsyncMock(
        return_value=_make_entry(scope=MemoryScope.TENANT, slug="tenant-note"),
    )
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.remember", fake_remember),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory",
            json={"scope": "tenant", "body": "team policy"},
            headers=_authed(token),
        )
    assert response.status_code == 201
    call_kwargs = fake_remember.await_args.kwargs
    assert call_kwargs["expires_at"] is None


def test_remember_user_scope_default_ttl_honours_settings_override(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Custom ``MEMORY_USER_DEFAULT_TTL_DAYS`` env var changes the cutoff.

    Pins the env-var → ``Settings`` → handler dataflow so a future
    refactor that hardcoded the 7-day default (instead of reading
    :class:`Settings`) would surface here. The fixture clears
    ``get_settings.cache_clear()`` so the env-var change actually
    takes effect for this test.
    """
    monkeypatch.setenv("MEMORY_USER_DEFAULT_TTL_DAYS", "30")
    get_settings.cache_clear()
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_remember = AsyncMock(
        return_value=_make_entry(scope=MemoryScope.USER, slug="custom-ttl"),
    )
    before = datetime.now(UTC)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.remember", fake_remember),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory",
            json={"scope": "user", "body": "30-day note"},
            headers=_authed(token),
        )
    after = datetime.now(UTC)
    assert response.status_code == 201
    call_kwargs = fake_remember.await_args.kwargs
    expires_at = call_kwargs["expires_at"]
    assert expires_at is not None
    assert before + timedelta(days=30) <= expires_at <= after + timedelta(days=30)


# ---------------------------------------------------------------------------
# GET / -- list
# ---------------------------------------------------------------------------


def test_list_returns_envelope_with_entries(client: TestClient) -> None:
    """List wraps entries in ``{"entries": [...]}``."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_entries = [
        _make_entry(slug="a"),
        _make_entry(slug="b"),
    ]
    fake_list = AsyncMock(return_value=fake_entries)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.list_memories", fake_list),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/memory", headers=_authed(token))

    assert response.status_code == 200
    body = response.json()
    assert "entries" in body
    assert len(body["entries"]) == 2
    assert [e["slug"] for e in body["entries"]] == ["a", "b"]


def test_list_forwards_filters_to_service(client: TestClient) -> None:
    """All five query-string filters reach :meth:`MemoryService.list_memories`."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_list = AsyncMock(return_value=[])
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.list_memories", fake_list),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/memory?scope=tenant&slug_pattern=k8s&tag=infra&include_expired=true&limit=25",
            headers=_authed(token),
        )

    assert response.status_code == 200
    call_kwargs = fake_list.await_args.kwargs
    assert call_kwargs["scope"] is MemoryScope.TENANT
    assert call_kwargs["slug_pattern"] == "k8s"
    assert call_kwargs["tag"] == "infra"
    assert call_kwargs["include_expired"] is True
    assert call_kwargs["limit"] == 25


def test_list_limit_zero_returns_422(client: TestClient) -> None:
    """``limit=0`` fails the ``ge=1`` validator (Query gate)."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/memory?limit=0", headers=_authed(token))
    assert response.status_code == 422


def test_list_invalid_scope_returns_422(client: TestClient) -> None:
    """Bogus ``scope`` value rejected by the enum coercion."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/memory?scope=nonsense", headers=_authed(token))
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /{scope}/{slug} -- recall (404 collapse covers info-leak)
# ---------------------------------------------------------------------------


def test_recall_existing_returns_full_entry(client: TestClient) -> None:
    """Existing key → 200 + full body."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a, sub="alice")
    fake_recall = AsyncMock(
        return_value=_make_entry(
            scope=MemoryScope.USER,
            slug="wine-preference",
            user_sub="alice",
        ),
    )
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.recall", fake_recall),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/memory/user/wine-preference",
            headers=_authed(token),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["slug"] == "wine-preference"
    assert body["scope"] == "user"
    assert body["body"] == "Prefers Pinot Noir."


def test_recall_not_found_returns_404(client: TestClient) -> None:
    """Service returning ``None`` → 404 (not-found path)."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_recall = AsyncMock(return_value=None)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.recall", fake_recall),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/memory/user/never-existed",
            headers=_authed(token),
        )
    assert response.status_code == 404
    assert response.json()["detail"] == "memory_not_found"


def test_recall_cross_user_returns_404_not_403(client: TestClient) -> None:
    """RBAC-denied cross-user recall returns 404, NOT 403 (info-leak avoidance).

    The acceptance criterion "``GET /api/v1/memory/user/their-pref`` (another
    operator's user-scoped memory) returns 404" is the load-bearing
    info-leak guarantee from the issue body. The service folds RBAC denial
    into a ``None`` return; the route translates that to 404. A 403 here
    would let an operator enumerate the existence of other operators'
    user-scoped slugs by status-code differential.
    """
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a, sub="alice")
    # Service returns None for cross-user user-scoped recall (RBAC fold).
    fake_recall = AsyncMock(return_value=None)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.recall", fake_recall),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/memory/user/bobs-pref",
            headers=_authed(token),
        )
    assert response.status_code == 404
    assert response.json()["detail"] == "memory_not_found"


def test_recall_invalid_scope_returns_422(client: TestClient) -> None:
    """Path parameter that is not a MemoryScope value → 422."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/memory/bogus/some-slug",
            headers=_authed(token),
        )
    assert response.status_code == 422


def test_recall_target_scope_with_target_name(client: TestClient) -> None:
    """``target_name`` query param reaches the service for target-scoped recall."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_recall = AsyncMock(
        return_value=_make_entry(
            scope=MemoryScope.TARGET,
            slug="rollout-note",
            user_sub=None,
            target_name="infra-1",
        ),
    )
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.recall", fake_recall),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/memory/target/rollout-note?target_name=infra-1",
            headers=_authed(token),
        )
    assert response.status_code == 200
    call_kwargs = fake_recall.await_args.kwargs
    assert call_kwargs["target_name"] == "infra-1"
    assert call_kwargs["scope"] is MemoryScope.TARGET


# ---------------------------------------------------------------------------
# DELETE /{scope}/{slug} -- forget (idempotent)
# ---------------------------------------------------------------------------


def test_forget_existing_returns_204(client: TestClient) -> None:
    """DELETE on existing row → 204."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_forget = AsyncMock(return_value=True)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.forget", fake_forget),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.delete(
            "/api/v1/memory/user/wine-preference",
            headers=_authed(token),
        )
    assert response.status_code == 204
    assert response.content == b""


def test_forget_missing_returns_204_idempotent(client: TestClient) -> None:
    """DELETE on already-missing row → 204 (idempotent contract)."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_forget = AsyncMock(return_value=False)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.forget", fake_forget),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.delete(
            "/api/v1/memory/user/never-existed",
            headers=_authed(token),
        )
    assert response.status_code == 204


def test_forget_permission_denied_returns_403(client: TestClient) -> None:
    """Service raising :class:`PermissionDeniedError` → 403."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_forget = AsyncMock(
        side_effect=PermissionDeniedError(
            MemoryScope.TENANT, "role=operator cannot forget scope=tenant"
        ),
    )
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.forget", fake_forget),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.delete(
            "/api/v1/memory/tenant/team-runbook",
            headers=_authed(token),
        )
    assert response.status_code == 403
    assert "permission_denied" in response.json()["detail"]


def test_forget_target_scope_without_target_name_returns_422(
    client: TestClient,
) -> None:
    """Service ``ValueError`` (target_name missing) → 422."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_forget = AsyncMock(
        side_effect=ValueError("target_name is required for scope=target"),
    )
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.forget", fake_forget),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.delete(
            "/api/v1/memory/target/rollout-note",
            headers=_authed(token),
        )
    assert response.status_code == 422
    assert "target_name" in response.json()["detail"]


@pytest.mark.asyncio
async def test_forget_oversized_slug_returns_404_without_binding_slug(
    client: TestClient,
) -> None:
    """Oversized DELETE slug → 404 before ``bind_contextvars`` runs.

    The recall route guards ``len(slug) > _SLUG_MAX_LENGTH`` *before*
    ``bind_contextvars`` so the oversized slug never lands in the
    audit/broadcast payload. The forget route applies the same guard
    for parity (review finding m1, PR #643). This regression proves:

    * the route returns 404 (not 422, mirrors recall's info-leak shape)
    * :meth:`MemoryService.forget` is never invoked (the edge guard
      rejects before the service call)
    * the audit row's payload does NOT contain ``slug`` (because
      ``bind_contextvars`` never ran on this request) -- without the
      guard the oversized substring would be pessimistically bound
      into the audit payload before the service rejects it.

    The middleware always emits an audit row for the request, so the
    test asserts *what's in the payload*, not *that no row exists*.
    """
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    # 257 chars -- one over _SLUG_MAX_LENGTH (256).
    oversized_slug = "a" * 257
    fake_forget = AsyncMock(return_value=True)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.forget", fake_forget),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.delete(
            f"/api/v1/memory/user/{oversized_slug}",
            headers=_authed(token),
        )
    assert response.status_code == 404
    assert response.json()["detail"] == "memory_not_found"
    # Service must NOT be called -- edge guard rejected the request.
    fake_forget.assert_not_called()

    rows = await _audit_rows_for_path(f"/api/v1/memory/user/{oversized_slug}")
    delete_rows = [r for r in rows if r.method == "DELETE"]
    assert len(delete_rows) == 1, "expected exactly one audit row for oversized DELETE"
    payload = delete_rows[0].payload
    # Slug guard runs BEFORE bind_contextvars -- so neither op_id, scope,
    # nor slug should be present on the audit row's payload. The truthful
    # signal is that the route never classified this request as a
    # memory.forget op_id (it short-circuited at the edge).
    assert "slug" not in payload, (
        "oversized slug must NOT appear in audit payload "
        "(bind_contextvars must run *after* the length guard)"
    )
    assert payload.get("op_id") != "memory.forget", (
        "an early-rejected request must not classify as memory.forget"
    )


# ---------------------------------------------------------------------------
# Audit op_id binding contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remember_writes_audit_row_with_memory_remember_op_id(
    client: TestClient,
) -> None:
    """POST audit row carries ``memory.remember`` + slug; body NOT in payload."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_remember = AsyncMock(
        return_value=_make_entry(scope=MemoryScope.USER, slug="wine-preference"),
    )
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.remember", fake_remember),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory",
            json={
                "scope": "user",
                "body": "SECRET BODY",
                "slug": "wine-preference",
            },
            headers=_authed(token),
        )
    assert response.status_code == 201

    rows = await _audit_rows_for_path("/api/v1/memory")
    post_rows = [r for r in rows if r.method == "POST"]
    assert len(post_rows) == 1
    payload = post_rows[0].payload
    assert payload["op_id"] == "memory.remember"
    assert payload["op_class"] == "write"
    assert payload["scope"] == "user"
    assert payload["slug"] == "wine-preference"
    # Body MUST NOT appear anywhere in the audit payload.
    serialised = json.dumps(payload)
    assert "SECRET BODY" not in serialised


@pytest.mark.asyncio
async def test_list_writes_audit_row_with_memory_list_op_id(
    client: TestClient,
) -> None:
    """GET /api/v1/memory → audit row ``op_id="memory.list"`` + ``op_class="read"``."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_list = AsyncMock(return_value=[])
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.list_memories", fake_list),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/memory", headers=_authed(token))
    assert response.status_code == 200

    rows = await _audit_rows_for_path("/api/v1/memory")
    get_rows = [r for r in rows if r.method == "GET"]
    assert len(get_rows) == 1
    payload = get_rows[0].payload
    assert payload["op_id"] == "memory.list"
    assert payload["op_class"] == "read"


@pytest.mark.asyncio
async def test_recall_writes_audit_row_with_memory_recall_op_id(
    client: TestClient,
) -> None:
    """GET /api/v1/memory/{scope}/{slug} → audit ``op_id="memory.recall"`` + ``op_class="read"``."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a, sub="alice")
    fake_recall = AsyncMock(
        return_value=_make_entry(slug="wine-preference", user_sub="alice"),
    )
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.recall", fake_recall),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/memory/user/wine-preference",
            headers=_authed(token),
        )
    assert response.status_code == 200

    rows = await _audit_rows_for_path("/api/v1/memory/user/wine-preference")
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["op_id"] == "memory.recall"
    assert payload["op_class"] == "read"
    assert payload["scope"] == "user"
    assert payload["slug"] == "wine-preference"


@pytest.mark.asyncio
async def test_forget_writes_audit_row_with_existed_flag(
    client: TestClient,
) -> None:
    """DELETE /api/v1/memory/{scope}/{slug} → audit ``op_id="memory.forget"`` + ``existed``."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_forget = AsyncMock(return_value=True)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.forget", fake_forget),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.delete(
            "/api/v1/memory/user/wine-preference",
            headers=_authed(token),
        )
    assert response.status_code == 204

    rows = await _audit_rows_for_path("/api/v1/memory/user/wine-preference")
    delete_rows = [r for r in rows if r.method == "DELETE"]
    assert len(delete_rows) == 1
    payload = delete_rows[0].payload
    assert payload["op_id"] == "memory.forget"
    assert payload["op_class"] == "write"
    assert payload["scope"] == "user"
    assert payload["slug"] == "wine-preference"
    assert payload["existed"] is True


@pytest.mark.asyncio
async def test_remember_substrate_exception_still_writes_memory_remember_audit_row() -> None:
    """Substrate raising mid-remember still produces ``op_id="memory.remember"`` audit row.

    Regression guarantee for the bind-ordering rule: the audit
    contextvars must be bound *before* the service call so an
    exception during ``MemoryService.remember`` still classifies the
    row under ``memory.remember`` rather than falling back to the
    middleware's HTTP-shape default (``http.post:/api/v1/memory``)
    that would bucket as ``op_class="other"`` and defeat the
    broadcast-redaction contract for write ops.

    Uses a TestClient constructed with
    ``raise_server_exceptions=False`` so the re-raised handler
    exception surfaces as the 500 an operator would actually see in
    production, rather than failing the test with the propagated
    exception. Mirrors the kb-delete regression in test_api_v1_kb.py.
    """
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_remember = AsyncMock(side_effect=RuntimeError("simulated substrate failure"))
    raising_client = TestClient(_build_app(), raise_server_exceptions=False)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.memory.MemoryService.remember", fake_remember),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = raising_client.post(
            "/api/v1/memory",
            json={"scope": "user", "body": "x", "slug": "boom"},
            headers=_authed(token),
        )

    assert response.status_code == 500

    rows = await _audit_rows_for_path("/api/v1/memory")
    post_rows = [r for r in rows if r.method == "POST"]
    assert len(post_rows) == 1, "expected exactly one audit row for the failed POST"
    payload = post_rows[0].payload
    assert payload["op_id"] == "memory.remember"
    assert payload["op_class"] == "write"
    assert payload["scope"] == "user"
    # The slug is NOT in the payload on the exception path because
    # the post-call rebind never ran -- this is the truthful signal.
    assert "slug" not in payload
