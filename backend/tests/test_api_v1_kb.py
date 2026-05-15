# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.kb`.

Coverage matrix (G4.1-T2 / Task #416 acceptance criteria):

* **Route mounting** -- all five routes appear in the FastAPI app's
  route table; the OpenAPI document the test client builds advertises
  them at ``/api/v1/openapi.json``.
* **List / show** -- ``operator`` role can list + show; the list
  response returns the slug-sorted entries with the preview field
  truncated at 200 chars; ``show`` 404s for an unknown slug; the
  cross-tenant show probe also 404s (not 403).
* **Create** -- ``tenant_admin`` creates; ``operator`` role gets
  403; invalid slug surfaces as 422 ``invalid_slug``; the audit row
  carries ``op_id="kb.create"`` + ``op_class="write"`` + ``slug``.
* **Delete** -- ``tenant_admin`` deletes; idempotent (returns 204 on
  already-missing); the audit row carries ``op_id="kb.delete"`` +
  ``op_class="write"`` + ``existed`` boolean.
* **Ingest** -- ``tenant_admin`` ingests; ``operator`` gets 403;
  ``directory`` AND ``tarball_url`` both set returns 422; neither
  set returns 422; ``tarball_url`` set returns 501 Not Implemented
  (forward-compat for the unimplemented branch); ``dry_run=true``
  doesn't write; the audit payload carries the four KbIngestionResult
  counters (NOT the file contents).
* **Audit op_id binding** -- every route binds ``audit_op_id`` so the
  audit row's payload carries the canonical kb operation id rather
  than the HTTP-shape default; ``audit_op_class`` is bound explicitly
  so the broadcast classifier doesn't fall through to "other" for
  ``kb.show`` / ``kb.ingest``.
* **Unauthenticated** -- every route returns 401 without a token.

Tests boot the FastAPI app with the production middleware stack
(``RequestContextMiddleware`` + ``AuditMiddleware``) so audit rows
are inserted into the autouse-migrated SQLite engine. The
:class:`KbService` is patched on the route's import site for happy-
path unit tests; integration coverage against a real PG cluster
lives in ``tests/integration/test_kb_routes_pg.py``.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
import respx
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.kb import router as kb_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.kb.schemas import KbEntry, KbIngestionResult
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
# Log capture (mirrors test_api_v1_retrieve.py)
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
    """Return a :class:`FastAPI` mirroring prod with the kb router mounted.

    Includes the production middleware stack so audit payload tests see
    the same contextvar-binding flow production uses.
    """
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(kb_router)
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


def _make_entry(slug: str = "k8s-ingress", body: str = "ingress troubleshooting") -> KbEntry:
    """Build a synthetic :class:`KbEntry` for stubbed service responses."""
    return KbEntry(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        tenant_id=UUID("22222222-2222-2222-2222-222222222222"),
        slug=slug,
        body=body,
        metadata={"path": f"/tmp/{slug}.md"},
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


def test_all_five_routes_mounted_on_main_app() -> None:
    """All five routes appear in :mod:`meho_backplane.main`'s app + OpenAPI."""
    from meho_backplane.main import app

    expected_paths = {
        "/api/v1/kb",
        "/api/v1/kb/{slug}",
        "/api/v1/kb/ingest",
    }
    actual_paths = {getattr(r, "path", None) for r in app.routes}
    missing = expected_paths - actual_paths
    assert not missing, f"missing routes: {missing}"

    # Verify the OpenAPI doc enumerates every method on each path.
    openapi = app.openapi()
    paths = openapi["paths"]
    assert "get" in paths["/api/v1/kb"]
    assert "post" in paths["/api/v1/kb"]
    assert "get" in paths["/api/v1/kb/{slug}"]
    assert "delete" in paths["/api/v1/kb/{slug}"]
    assert "post" in paths["/api/v1/kb/ingest"]


# ---------------------------------------------------------------------------
# Unauthenticated (401) -- every route
# ---------------------------------------------------------------------------


def test_list_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.get("/api/v1/kb")
    assert response.status_code == 401


def test_show_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.get("/api/v1/kb/k8s-ingress")
    assert response.status_code == 401


def test_create_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.post("/api/v1/kb", json={"slug": "x", "body": "y"})
    assert response.status_code == 401


def test_delete_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.delete("/api/v1/kb/k8s-ingress")
    assert response.status_code == 401


def test_ingest_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.post("/api/v1/kb/ingest", json={"directory": "/tmp/kb"})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# RBAC -- read routes accept operator+, write routes need tenant_admin
# ---------------------------------------------------------------------------


def test_list_readonly_role_returns_403(client: TestClient) -> None:
    """``read_only`` role on GET / → 403 (operator minimum)."""
    key, token = _readonly_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/kb", headers=_authed(token))
    assert response.status_code == 403


def test_create_operator_role_returns_403(client: TestClient) -> None:
    """``operator`` role on POST / → 403 (tenant_admin only)."""
    key, token = _operator_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/kb",
            json={"slug": "k8s-ingress", "body": "x"},
            headers=_authed(token),
        )
    assert response.status_code == 403


def test_delete_operator_role_returns_403(client: TestClient) -> None:
    """``operator`` role on DELETE /{slug} → 403."""
    key, token = _operator_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.delete(
            "/api/v1/kb/k8s-ingress",
            headers=_authed(token),
        )
    assert response.status_code == 403


def test_ingest_operator_role_returns_403(client: TestClient) -> None:
    """``operator`` role on POST /ingest → 403."""
    key, token = _operator_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/kb/ingest",
            json={"directory": "/tmp/kb"},
            headers=_authed(token),
        )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# GET / -- list
# ---------------------------------------------------------------------------


def test_list_returns_slug_sorted_previews(client: TestClient) -> None:
    """Operator role + happy path returns the substrate's entries as previews."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)

    fake_entries = [
        _make_entry("k8s-ingress", "k8s ingress body"),
        _make_entry("vault-policies", "vault policy primer"),
    ]
    fake_list = AsyncMock(return_value=fake_entries)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.kb.KbService.list_entries", fake_list),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/kb",
            headers=_authed(token),
        )

    assert response.status_code == 200
    body = response.json()
    assert len(body["entries"]) == 2
    slugs = [e["slug"] for e in body["entries"]]
    assert slugs == ["k8s-ingress", "vault-policies"]
    assert body["entries"][0]["preview"] == "k8s ingress body"
    # The service was called with operator's tenant_id (tenant scoping).
    fake_list.assert_awaited_once()
    call_kwargs = fake_list.await_args.kwargs
    assert call_kwargs["tenant_id"] == tenant_a


def test_list_forwards_filter_limit_offset(client: TestClient) -> None:
    """Optional ``filter`` / ``limit`` / ``offset`` reach the substrate."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)

    fake_list = AsyncMock(return_value=[])
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.kb.KbService.list_entries", fake_list),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/kb?filter=k8s-%25&limit=25&offset=10",
            headers=_authed(token),
        )

    assert response.status_code == 200
    call_kwargs = fake_list.await_args.kwargs
    assert call_kwargs["filter_pattern"] == "k8s-%"
    assert call_kwargs["limit"] == 25
    assert call_kwargs["offset"] == 10


def test_list_truncates_long_body_to_preview(client: TestClient) -> None:
    """Bodies longer than 200 chars are truncated with a ``…`` suffix."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    long_body = "x" * 250
    fake_list = AsyncMock(return_value=[_make_entry("long-slug", long_body)])
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.kb.KbService.list_entries", fake_list),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/kb", headers=_authed(token))

    assert response.status_code == 200
    preview = response.json()["entries"][0]["preview"]
    assert preview.endswith("…")
    assert len(preview) == 201  # 200 chars + the U+2026 character


# ---------------------------------------------------------------------------
# GET /{slug} -- show
# ---------------------------------------------------------------------------


def test_show_returns_full_entry(client: TestClient) -> None:
    """Existing slug → 200 + full body."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_get = AsyncMock(return_value=_make_entry("k8s-ingress", "FULL BODY TEXT"))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.kb.KbService.get_entry", fake_get),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/kb/k8s-ingress", headers=_authed(token))

    assert response.status_code == 200
    body = response.json()
    assert body["slug"] == "k8s-ingress"
    assert body["body"] == "FULL BODY TEXT"


def test_show_unknown_slug_returns_404(client: TestClient) -> None:
    """Unknown slug → 404 ``slug_not_found``."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_get = AsyncMock(return_value=None)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.kb.KbService.get_entry", fake_get),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/kb/unknown", headers=_authed(token))

    assert response.status_code == 404
    assert response.json()["detail"] == "slug_not_found"


def test_show_cross_tenant_returns_404(client: TestClient) -> None:
    """Cross-tenant probe behaves identically to unknown-slug (404 not 403)."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    # The substrate returns None for cross-tenant probes since the WHERE
    # filter pins tenant_id. Same shape as unknown slug.
    fake_get = AsyncMock(return_value=None)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.kb.KbService.get_entry", fake_get),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/kb/other-tenant-slug",
            headers=_authed(token),
        )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST / -- create
# ---------------------------------------------------------------------------


def test_create_returns_201_and_entry(client: TestClient) -> None:
    """Tenant_admin POST → 201 + the created entry."""
    tenant_a = uuid.uuid4()
    key, token = _admin_token(tenant_id=tenant_a)
    fake_create = AsyncMock(return_value=_make_entry("k8s-ingress", "new body"))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.kb.KbService.create_entry", fake_create),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/kb",
            json={
                "slug": "k8s-ingress",
                "body": "new body",
                "metadata": {"author": "ops"},
            },
            headers=_authed(token),
        )

    assert response.status_code == 201
    body = response.json()
    assert body["slug"] == "k8s-ingress"
    assert body["body"] == "new body"
    fake_create.assert_awaited_once()
    call_kwargs = fake_create.await_args.kwargs
    assert call_kwargs["tenant_id"] == tenant_a
    assert call_kwargs["slug"] == "k8s-ingress"
    assert call_kwargs["body"] == "new body"
    assert call_kwargs["metadata"] == {"author": "ops"}


def test_create_invalid_slug_returns_422(client: TestClient) -> None:
    """Slug failing :data:`SLUG_PATTERN` surfaces as 422 from the route."""
    tenant_a = uuid.uuid4()
    key, token = _admin_token(tenant_id=tenant_a)
    # Stub the substrate to raise InvalidKbSlugError so we don't hit a
    # real DB; the route is responsible for catching + remapping.
    from meho_backplane.kb.schemas import InvalidKbSlugError

    fake_create = AsyncMock(side_effect=InvalidKbSlugError("slug 'BAD!' does not match"))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.kb.KbService.create_entry", fake_create),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/kb",
            json={"slug": "BAD!", "body": "x"},
            headers=_authed(token),
        )
    assert response.status_code == 422
    assert "does not match" in response.json()["detail"]


def test_create_empty_body_returns_422(client: TestClient) -> None:
    """Empty body field fails Pydantic min_length validation."""
    tenant_a = uuid.uuid4()
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/kb",
            json={"slug": "ok-slug", "body": ""},
            headers=_authed(token),
        )
    assert response.status_code == 422


def test_create_unknown_field_returns_422(client: TestClient) -> None:
    """Unknown field (extra="forbid") → 422 from pydantic."""
    tenant_a = uuid.uuid4()
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/kb",
            json={"slug": "ok", "body": "x", "typo_field": "oops"},
            headers=_authed(token),
        )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /{slug} -- delete (idempotent)
# ---------------------------------------------------------------------------


def test_delete_existing_returns_204(client: TestClient) -> None:
    """DELETE on existing slug → 204."""
    tenant_a = uuid.uuid4()
    key, token = _admin_token(tenant_id=tenant_a)
    fake_delete = AsyncMock(return_value=True)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.kb.KbService.delete_entry", fake_delete),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.delete("/api/v1/kb/k8s-ingress", headers=_authed(token))

    assert response.status_code == 204
    assert response.content == b""


def test_delete_missing_returns_204_idempotent(client: TestClient) -> None:
    """DELETE on already-missing slug → 204 (idempotent semantics)."""
    tenant_a = uuid.uuid4()
    key, token = _admin_token(tenant_id=tenant_a)
    fake_delete = AsyncMock(return_value=False)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.kb.KbService.delete_entry", fake_delete),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.delete("/api/v1/kb/never-existed", headers=_authed(token))

    assert response.status_code == 204


# ---------------------------------------------------------------------------
# POST /ingest -- ingest
# ---------------------------------------------------------------------------


def test_ingest_directory_returns_result(client: TestClient) -> None:
    """Tenant_admin POST /ingest with ``directory`` → 200 + counters."""
    tenant_a = uuid.uuid4()
    key, token = _admin_token(tenant_id=tenant_a)
    fake_result = KbIngestionResult(
        inserted_count=3,
        updated_count=1,
        skipped_count=5,
        error_count=0,
        errors=[],
    )
    fake_ingest = AsyncMock(return_value=fake_result)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.kb.KbService.ingest_directory", fake_ingest),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/kb/ingest",
            json={"directory": "/tmp/kb"},
            headers=_authed(token),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["inserted_count"] == 3
    assert body["updated_count"] == 1
    assert body["skipped_count"] == 5
    assert body["error_count"] == 0
    fake_ingest.assert_awaited_once()


def test_ingest_dry_run_forwards_flag(client: TestClient) -> None:
    """``dry_run=true`` reaches the substrate."""
    tenant_a = uuid.uuid4()
    key, token = _admin_token(tenant_id=tenant_a)
    fake_result = KbIngestionResult(
        inserted_count=0,
        updated_count=0,
        skipped_count=0,
        error_count=0,
        errors=[],
    )
    fake_ingest = AsyncMock(return_value=fake_result)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.kb.KbService.ingest_directory", fake_ingest),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/kb/ingest",
            json={"directory": "/tmp/kb", "dry_run": True},
            headers=_authed(token),
        )

    assert response.status_code == 200
    call_kwargs = fake_ingest.await_args.kwargs
    assert call_kwargs["dry_run"] is True


def test_ingest_both_directory_and_tarball_returns_422(client: TestClient) -> None:
    """Both ``directory`` AND ``tarball_url`` set → 422 (validator)."""
    tenant_a = uuid.uuid4()
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/kb/ingest",
            json={
                "directory": "/tmp/kb",
                "tarball_url": "https://example.com/kb.tar.gz",
            },
            headers=_authed(token),
        )
    assert response.status_code == 422


def test_ingest_neither_directory_nor_tarball_returns_422(client: TestClient) -> None:
    """Neither field set → 422 (validator)."""
    tenant_a = uuid.uuid4()
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/kb/ingest",
            json={"dry_run": False},
            headers=_authed(token),
        )
    assert response.status_code == 422


def test_ingest_tarball_url_returns_501_not_implemented(client: TestClient) -> None:
    """``tarball_url`` set (alone) → 501 (forward-compat unimplemented branch)."""
    tenant_a = uuid.uuid4()
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/kb/ingest",
            json={"tarball_url": "https://example.com/kb.tar.gz"},
            headers=_authed(token),
        )
    assert response.status_code == 501
    assert "tarball_url" in response.json()["detail"]


def test_ingest_missing_directory_returns_400(client: TestClient) -> None:
    """Substrate raising FileNotFoundError → 400 from the route."""
    tenant_a = uuid.uuid4()
    key, token = _admin_token(tenant_id=tenant_a)
    fake_ingest = AsyncMock(side_effect=FileNotFoundError("[Errno 2] No such file"))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.kb.KbService.ingest_directory", fake_ingest),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/kb/ingest",
            json={"directory": "/tmp/does-not-exist"},
            headers=_authed(token),
        )
    assert response.status_code == 400
    assert "directory_not_found" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Audit op_id binding contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_writes_audit_row_with_kb_list_op_id(
    client: TestClient,
) -> None:
    """GET /api/v1/kb → audit row carries ``op_id="kb.list"`` + ``op_class="read"``."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_list = AsyncMock(return_value=[])
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.kb.KbService.list_entries", fake_list),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/kb", headers=_authed(token))
    assert response.status_code == 200

    rows = await _audit_rows_for_path("/api/v1/kb")
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["op_id"] == "kb.list"
    assert payload["op_class"] == "read"


@pytest.mark.asyncio
async def test_show_writes_audit_row_with_kb_show_op_id(
    client: TestClient,
) -> None:
    """GET /api/v1/kb/{slug} → audit row ``op_id="kb.show"`` + ``op_class="read"``."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_get = AsyncMock(return_value=_make_entry("k8s-ingress"))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.kb.KbService.get_entry", fake_get),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/kb/k8s-ingress", headers=_authed(token))
    assert response.status_code == 200

    rows = await _audit_rows_for_path("/api/v1/kb/k8s-ingress")
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["op_id"] == "kb.show"
    assert payload["op_class"] == "read"


@pytest.mark.asyncio
async def test_create_writes_audit_row_with_kb_create_op_id_and_slug(
    client: TestClient,
) -> None:
    """POST /api/v1/kb → audit row ``op_id="kb.create"`` + ``slug``; body NOT in payload."""
    tenant_a = uuid.uuid4()
    key, token = _admin_token(tenant_id=tenant_a)
    fake_create = AsyncMock(return_value=_make_entry("k8s-ingress", "FULL BODY"))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.kb.KbService.create_entry", fake_create),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/kb",
            json={"slug": "k8s-ingress", "body": "FULL BODY"},
            headers=_authed(token),
        )
    assert response.status_code == 201

    rows = await _audit_rows_for_path("/api/v1/kb")
    # Filter to the POST row (the list test in this file also writes
    # an audit row but that runs in a different test with a fresh DB).
    post_rows = [r for r in rows if r.method == "POST"]
    assert len(post_rows) == 1
    payload = post_rows[0].payload
    assert payload["op_id"] == "kb.create"
    assert payload["op_class"] == "write"
    assert payload["slug"] == "k8s-ingress"
    # Body MUST NOT appear anywhere in the audit payload.
    serialised = json.dumps(payload)
    assert "FULL BODY" not in serialised


@pytest.mark.asyncio
async def test_delete_writes_audit_row_with_existed_flag(
    client: TestClient,
) -> None:
    """DELETE /api/v1/kb/{slug} → audit ``op_id="kb.delete"`` + ``existed`` flag."""
    tenant_a = uuid.uuid4()
    key, token = _admin_token(tenant_id=tenant_a)
    fake_delete = AsyncMock(return_value=True)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.kb.KbService.delete_entry", fake_delete),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.delete("/api/v1/kb/k8s-ingress", headers=_authed(token))
    assert response.status_code == 204

    rows = await _audit_rows_for_path("/api/v1/kb/k8s-ingress")
    delete_rows = [r for r in rows if r.method == "DELETE"]
    assert len(delete_rows) == 1
    payload = delete_rows[0].payload
    assert payload["op_id"] == "kb.delete"
    assert payload["op_class"] == "write"
    assert payload["slug"] == "k8s-ingress"
    assert payload["existed"] is True


@pytest.mark.asyncio
async def test_ingest_writes_audit_row_with_counters_not_file_contents(
    client: TestClient,
) -> None:
    """POST /api/v1/kb/ingest → audit ``op_id="kb.ingest"`` + counters; NO file contents."""
    tenant_a = uuid.uuid4()
    key, token = _admin_token(tenant_id=tenant_a)
    fake_result = KbIngestionResult(
        inserted_count=10,
        updated_count=2,
        skipped_count=32,
        error_count=0,
        errors=[],
    )
    fake_ingest = AsyncMock(return_value=fake_result)
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.kb.KbService.ingest_directory", fake_ingest),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/kb/ingest",
            json={"directory": "/tmp/kb"},
            headers=_authed(token),
        )
    assert response.status_code == 200

    rows = await _audit_rows_for_path("/api/v1/kb/ingest")
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["op_id"] == "kb.ingest"
    assert payload["op_class"] == "write"
    assert payload["inserted_count"] == 10
    assert payload["updated_count"] == 2
    assert payload["skipped_count"] == 32
    assert payload["error_count"] == 0
    # The directory path may surface as an audit_* contextvar in a future
    # iteration but the file contents NEVER do; pin the negative
    # assertion against a body-shaped value that could have leaked.
    serialised = json.dumps(payload)
    assert "body=" not in serialised
    assert "tarball" not in serialised
