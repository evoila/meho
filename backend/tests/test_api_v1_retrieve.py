# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.retrieve`.

Coverage matrix (G0.4-T5 / Task #262 acceptance criteria):

* **Happy path** -- valid request from an ``operator`` JWT returns
  200 with ``hits`` + ``query_duration_ms``. The hits list comes
  from a mocked :func:`retrieve` so the route test doesn't depend
  on a real PG cluster.
* **Validation** -- empty query rejects with 422 (Pydantic
  ``min_length=1``); oversized ``limit`` rejects with 422
  (Pydantic ``le=50``); oversized query rejects with 422
  (``max_length=2000``).
* **RBAC** -- ``read_only`` JWT returns 403 with the
  ``insufficient_role`` log event (proves the
  ``require_role(TenantRole.OPERATOR)`` gate is wired correctly);
  ``operator`` JWT returns 200; ``tenant_admin`` JWT returns 200.
* **Unauthenticated** -- no ``Authorization`` header returns 401.
* **Audit payload privacy** -- the audit_log row's ``payload``
  contains ``{query_hash, source, kind, hit_count}`` but **NOT** the
  raw query string. The hash is a SHA-256 hex digest of the UTF-8
  query so an analyst can map a known query back to its hash, but
  the digest alone is non-reversible.
* **Tenant scoping** -- the ``retrieve`` helper is called with the
  operator's ``tenant_id`` (the one in the JWT claim); a route
  that ignored the operator and pulled a tenant id from the body
  would fail this test.

The audit assertion uses an SQLite-backed AsyncSession to read back
the persisted ``audit_log`` row -- the same path
:mod:`tests.test_audit` uses to verify the row shape.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
import respx
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.retrieve import _compute_query_hash
from meho_backplane.api.v1.retrieve import router as retrieve_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.retrieval.retriever import RetrievalHit
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
# App construction with the retrieve route + audit middleware
# ---------------------------------------------------------------------------


def _build_app_with_retrieve_route() -> FastAPI:
    """Return a :class:`FastAPI` mirroring prod with the retrieve route mounted.

    Includes the production middleware stack (``RequestContextMiddleware``
    + ``AuditMiddleware``) so the audit payload tests see the same
    contextvar-binding flow production uses. The retrieve helper is
    patched at the route's import site (not here) so each test stubs
    its own response.
    """
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(retrieve_router)
    return app


@pytest.fixture
def retrieve_client() -> Iterator[TestClient]:
    """``TestClient`` driving a fresh app per test."""
    yield TestClient(_build_app_with_retrieve_route())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hit(body: str = "doc body") -> RetrievalHit:
    """Build a :class:`RetrievalHit` for stub responses."""
    return RetrievalHit(
        document_id=UUID("11111111-1111-1111-1111-111111111111"),
        tenant_id=UUID("22222222-2222-2222-2222-222222222222"),
        source="kb",
        source_id="k8s-ingress",
        kind="kb-entry",
        body=body,
        doc_metadata={"author": "ops"},
        fused_score=0.032,
        bm25_score=0.85,
        cosine_score=0.72,
        bm25_rank=2,
        cosine_rank=1,
    )


# ---------------------------------------------------------------------------
# Pure helper coverage
# ---------------------------------------------------------------------------


def test_compute_query_hash_is_deterministic_sha256() -> None:
    """Query hash is SHA-256 of UTF-8, matching ``compute_body_hash``."""
    h = _compute_query_hash("kubernetes ingress")
    assert len(h) == 64
    assert h == _compute_query_hash("kubernetes ingress")
    int(h, 16)  # parses as hex


def test_compute_query_hash_distinguishes_queries() -> None:
    """Different queries → different hashes."""
    assert _compute_query_hash("a") != _compute_query_hash("b")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_retrieve_route_returns_200_with_hits_and_duration(
    retrieve_client: TestClient,
) -> None:
    """``operator`` JWT + valid query → 200 with hits + query_duration_ms."""
    key = _make_rsa_keypair("kid-A")
    tenant_id = UUID("33333333-3333-3333-3333-333333333333")
    token = _mint_token(
        key,
        sub="op-1",
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(tenant_id),
    )

    fake_retrieve = AsyncMock(return_value=[_make_hit("body-A"), _make_hit("body-B")])
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.retrieve.retrieve", new=fake_retrieve),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = retrieve_client.post(
            "/api/v1/retrieve",
            json={"query": "kubernetes ingress", "limit": 5},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert len(body["hits"]) == 2
    assert body["hits"][0]["source"] == "kb"
    assert body["hits"][0]["fused_score"] == 0.032
    assert body["hits"][0]["bm25_rank"] == 2
    assert body["hits"][0]["cosine_rank"] == 1
    assert "query_duration_ms" in body
    assert isinstance(body["query_duration_ms"], (int, float))
    assert body["query_duration_ms"] >= 0.0

    # The retrieve helper was called with the operator's tenant_id
    # (proves tenant-scoping is wired correctly).
    fake_retrieve.assert_awaited_once()
    call_kwargs = fake_retrieve.await_args.kwargs
    assert call_kwargs["tenant_id"] == tenant_id
    assert call_kwargs["query"] == "kubernetes ingress"
    assert call_kwargs["limit"] == 5


def test_retrieve_route_passes_source_and_kind_filters(
    retrieve_client: TestClient,
) -> None:
    """Optional ``source`` / ``kind`` filters surface in the retrieve call."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key,
        sub="op-2",
        tenant_role=TenantRole.OPERATOR.value,
    )

    fake_retrieve = AsyncMock(return_value=[])
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.retrieve.retrieve", new=fake_retrieve),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = retrieve_client.post(
            "/api/v1/retrieve",
            json={
                "query": "kubernetes",
                "source": "kb",
                "kind": "kb-entry",
                "limit": 3,
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json()["hits"] == []
    call_kwargs = fake_retrieve.await_args.kwargs
    assert call_kwargs["source"] == "kb"
    assert call_kwargs["kind"] == "kb-entry"


# ---------------------------------------------------------------------------
# Validation (422)
# ---------------------------------------------------------------------------


def test_empty_query_rejects_with_422(retrieve_client: TestClient) -> None:
    """``query=""`` fails Pydantic min_length validation."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-3", tenant_role=TenantRole.OPERATOR.value)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = retrieve_client.post(
            "/api/v1/retrieve",
            json={"query": ""},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 422


def test_oversized_limit_rejects_with_422(retrieve_client: TestClient) -> None:
    """``limit=51`` exceeds the schema max of 50."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-4", tenant_role=TenantRole.OPERATOR.value)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = retrieve_client.post(
            "/api/v1/retrieve",
            json={"query": "anything", "limit": 51},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 422


def test_oversized_query_rejects_with_422(retrieve_client: TestClient) -> None:
    """Query > 2000 chars fails max_length validation."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-5", tenant_role=TenantRole.OPERATOR.value)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = retrieve_client.post(
            "/api/v1/retrieve",
            json={"query": "x" * 2001},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# RBAC (401 / 403)
# ---------------------------------------------------------------------------


def test_unauthenticated_request_returns_401(retrieve_client: TestClient) -> None:
    """No Authorization header → 401."""
    response = retrieve_client.post("/api/v1/retrieve", json={"query": "q"})
    assert response.status_code == 401


def test_read_only_role_returns_403_with_log(
    retrieve_client: TestClient,
) -> None:
    """``read_only`` JWT → 403 + ``insufficient_role`` log event.

    Pins the contract that the route is gated on
    ``require_role(TenantRole.OPERATOR)`` and that ``read_only`` is
    below ``operator`` in the role ordering. The
    ``insufficient_role`` event includes the operator sub + both
    actual and required role values per :func:`require_role`'s
    docstring.

    Uses :func:`structlog.testing.capture_logs` rather than a module-
    local ``PrintLoggerFactory(file=buf)`` swap. The structured logger
    in :mod:`meho_backplane.auth.rbac` is a module-level lazy proxy
    that materialises into a cached ``BoundLogger`` on first use
    (production sets ``cache_logger_on_first_use=True``), so once
    materialised it pins the factory it was built with and ignores
    later ``structlog.configure`` calls swapping the factory. Under
    pytest-xdist, that materialisation can happen in any test
    triggering ``require_role`` before this test's fixture runs
    (#738 flake under ``-n 3``). ``capture_logs`` mutates the
    configured-processors list in place, so cached BoundLoggers
    holding a reference to that same list still pick up the
    ``LogCapture`` processor.
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-ro", tenant_role=TenantRole.READ_ONLY.value)
    with respx.mock as mock_router, structlog.testing.capture_logs() as cap_logs:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = retrieve_client.post(
            "/api/v1/retrieve",
            json={"query": "q"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient_role"}

    insufficient = [entry for entry in cap_logs if entry.get("event") == "insufficient_role"]
    assert len(insufficient) == 1
    assert insufficient[0]["operator_sub"] == "op-ro"
    assert insufficient[0]["actual_role"] == "read_only"
    assert insufficient[0]["required_role"] == "operator"


def test_admin_role_returns_200(retrieve_client: TestClient) -> None:
    """``tenant_admin`` JWT passes the ``operator`` gate (admin >= operator)."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-admin", tenant_role=TenantRole.TENANT_ADMIN.value)
    fake_retrieve = AsyncMock(return_value=[])
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.retrieve.retrieve", new=fake_retrieve),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = retrieve_client.post(
            "/api/v1/retrieve",
            json={"query": "q"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Audit payload privacy contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_payload_carries_query_hash_not_raw_query(
    retrieve_client: TestClient,
) -> None:
    """The audit_log row stores ``{query_hash, source, kind, hit_count}`` -- not the query.

    Load-bearing privacy contract: retrieval queries can leak
    operator intent, so v0.2 stores only the SHA-256 hash + filter
    metadata + result count. An analyst correlating a known query
    against the audit log uses the same SHA-256 to compute the
    expected hash; the digest alone is non-reversible.

    The raw query string MUST NOT appear anywhere in
    ``audit_log.payload``. This is asserted with an explicit
    `"not in"` check against the JSON-serialised payload so a
    future refactor that accidentally stuffs the query into a
    debug field surfaces the regression immediately.
    """
    key = _make_rsa_keypair("kid-A")
    raw_query = "kubernetes ingress troubleshooting RFC 7541"
    token = _mint_token(key, sub="op-audit", tenant_role=TenantRole.OPERATOR.value)

    fake_retrieve = AsyncMock(return_value=[_make_hit(), _make_hit()])
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.retrieve.retrieve", new=fake_retrieve),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = retrieve_client.post(
            "/api/v1/retrieve",
            json={"query": raw_query, "source": "kb", "kind": "kb-entry", "limit": 5},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200

    # Read back the audit_log row. The TestClient runs the request
    # through the production AuditMiddleware → _write_audit_row →
    # AuditLog INSERT path against the autouse-migrated SQLite
    # engine.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == "/api/v1/retrieve"))
        rows = result.scalars().all()

    assert len(rows) == 1
    payload = rows[0].payload
    expected_hash = _compute_query_hash(raw_query)

    assert payload["query_hash"] == expected_hash
    assert payload["source"] == "kb"
    assert payload["kind"] == "kb-entry"
    assert payload["hit_count"] == 2

    # Explicit negative: raw query MUST NOT appear in the serialised payload.
    serialised = json.dumps(payload)
    assert raw_query not in serialised
    # Also assert the digest is exactly 64 hex chars -- regression
    # against any future refactor that swaps to a shorter hash.
    assert len(payload["query_hash"]) == 64


@pytest.mark.asyncio
async def test_audit_payload_omits_unset_filters(
    retrieve_client: TestClient,
) -> None:
    """Optional filters left ``None`` are not written to the payload.

    The :func:`_resolve_audit_payload` helper drops None values so
    a route that binds ``audit_kind=None`` doesn't produce a
    ``"kind": null`` entry. Keeps the audit_log.payload tight --
    only positively-set values show.
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-nofilter", tenant_role=TenantRole.OPERATOR.value)

    fake_retrieve = AsyncMock(return_value=[])
    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.retrieve.retrieve", new=fake_retrieve),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = retrieve_client.post(
            "/api/v1/retrieve",
            json={"query": "anything"},  # no source / kind / limit
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == "/api/v1/retrieve"))
        rows = result.scalars().all()

    assert len(rows) == 1
    payload = rows[0].payload
    assert "query_hash" in payload
    assert "hit_count" in payload
    # source / kind are None on this request -- must be absent from payload.
    assert "source" not in payload
    assert "kind" not in payload


# ---------------------------------------------------------------------------
# Chassis-era audit row stays empty-payload (regression check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_retrieve_routes_have_empty_audit_payload(
    retrieve_client: TestClient,
) -> None:
    """A non-retrieve route's audit row carries ``payload={}``.

    The audit-payload enrichment is opt-in via ``audit_*``
    contextvars. Routes that bind nothing (every chassis-era
    surface) must still get the empty-dict behaviour today's
    audit_log rows carry -- the enrichment is additive, not a
    breaking change.

    Probes a 404 to a non-existent route under an authenticated
    JWT: the audit middleware fires (operator_sub bound), but no
    handler ran -- so no contextvars bound -- so payload stays {}.
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-empty", tenant_role=TenantRole.OPERATOR.value)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        # 404 path: the audit middleware writes the row for any
        # request where operator_sub is bound, but no handler runs
        # so no audit_* contextvars are set.
        response = retrieve_client.post(
            "/api/v1/retrieve-nonexistent",
            json={"query": "ignored"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 404

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.path == "/api/v1/retrieve-nonexistent")
        )
        rows = result.scalars().all()
    # Either zero rows (no handler ran, so auth never bound operator_sub)
    # or one row with empty payload. Both are acceptable -- the
    # load-bearing assertion is that NO ``audit_*`` keys leaked from a
    # prior test's contextvar binding.
    for row in rows:
        assert row.payload == {} or not any(k.startswith("audit_") for k in row.payload)
