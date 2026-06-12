# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.search_docs` (G4.6-T3 #1552).

Coverage matrix (Task #1552 acceptance criteria):

* **Collection scope** -- a request missing / blanking ``collection`` is
  rejected **422** (fail-closed); ``product`` / ``version`` are now optional
  refinements (omitting them still succeeds). An unknown collection key is
  **422**.
* **Collection routing** -- the query reaches the resolved collection's
  backend; the optional product/version refinements arrive as
  ``metadata_filters`` (binary containment, never a weight). The backend id
  is absent from request + response.
* **Per-collection entitlement** -- a tenant lacking
  ``meho-docs:<collection>`` gets **403** on a known collection even though
  it can authenticate; a tenant with it succeeds.
* **Readiness** -- a *transiently* not-ready collection
  (``provisioning`` / ``rebuilding``) → **409** (retryable); a
  ``disabled`` collection → **403** with a structured
  ``detail.error='collection_disabled'`` (terminal), distinguishable from
  the 409 and from the entitlement-miss 403 (#1567).
* **RBAC** -- ``read_only`` → 403; unauthenticated → 401.
* **Central audit** -- one audit row per query, ``op_id="meho.docs.search"``,
  ``op_class="read"``, the raw query stored only as a SHA-256 hash, plus
  ``collection`` / ``product`` / ``version`` / ``hit_count``; the row is
  returned by ``query_audit`` filtered on that ``op_id``.
* **Backend-unavailable** -- a :class:`CorpusUnavailable` from the backend
  → 503 (fail-closed), never an empty 200.

The corpus call is mocked at the ``corpus-http`` backend's transport seam
(``meho_backplane.docs_search.backends.corpus_http.search_corpus``) so the
route tests don't depend on a live corpus; the DB-backed resolve uses the
autouse SQLite engine the chassis tests share, seeded with a global
``vmware`` collection.
"""

from __future__ import annotations

import asyncio
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

from meho_backplane.api.v1.search_docs import _compute_query_hash
from meho_backplane.api.v1.search_docs import router as search_docs_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.audit_query import AuditQueryFilters, query_audit
from meho_backplane.auth.corpus import CorpusChunk, CorpusSearchResponse, CorpusUnavailable
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, DocCollection
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

#: The corpus-http backend's transport seam — the function the
#: ``corpus-http`` adapter actually calls.
_CORPUS_SEAM = "meho_backplane.docs_search.backends.corpus_http.search_corpus"

#: The capabilities a token carries to search the seeded ``vmware``
#: collection: the base add-on key + the per-collection entitlement.
_ENTITLED_CAPS = ["meho-docs", "meho-docs:vmware"]


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
    monkeypatch.setenv("CORPUS_URL", "https://corpus.test/search")
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
# Doc-collection seeding
# ---------------------------------------------------------------------------


async def _seed_global_collection(*, collection_key: str = "vmware", status: str = "ready") -> None:
    """Insert a global (``tenant_id IS NULL``) collection for the route tests."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(
            DocCollection(
                tenant_id=None,
                collection_key=collection_key,
                vendor="VMware by Broadcom",
                products=["vsphere", "nsx"],
                description="VMware vendor docs.",
                when_to_use="VMware product questions.",
                backend={"type": "corpus-http"},
                status=status,
            ),
        )


def _seed_collection_sync(**kwargs: str) -> None:
    """Seed a global collection from a sync test via a one-shot loop."""
    asyncio.run(_seed_global_collection(**kwargs))


# ---------------------------------------------------------------------------
# App construction with the search_docs route + audit middleware
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """Return a :class:`FastAPI` mirroring prod with the route mounted."""
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(search_docs_router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    """``TestClient`` driving a fresh app per test."""
    yield TestClient(_build_app())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chunk(chunk_id: str = "c1", content: str = "vSAN disk groups...") -> CorpusChunk:
    """Build a :class:`CorpusChunk` for stub corpus responses."""
    return CorpusChunk(
        chunk_id=chunk_id,
        document_id="doc-7",
        content=content,
        source_url="https://docs.vendor.test/vsan#disk-groups",
        score=0.91,
        metadata={"product": "vmware", "version": "9.0"},
    )


def _mock_corpus(*chunks: CorpusChunk) -> AsyncMock:
    """An ``AsyncMock`` standing in for the backend transport returning *chunks*."""
    return AsyncMock(return_value=CorpusSearchResponse(chunks=list(chunks)))


# ---------------------------------------------------------------------------
# Pure helper coverage
# ---------------------------------------------------------------------------


def test_compute_query_hash_is_deterministic_sha256() -> None:
    """Query hash is SHA-256 of UTF-8 (64 hex chars), matching retrieve."""
    h = _compute_query_hash("vsan disk groups")
    assert len(h) == 64
    assert h == _compute_query_hash("vsan disk groups")
    int(h, 16)  # parses as hex


# ---------------------------------------------------------------------------
# Happy path + collection routing
# ---------------------------------------------------------------------------


def test_search_docs_returns_200_with_cited_chunks(client: TestClient) -> None:
    """``operator`` JWT + entitled collection → 200 with cited chunks."""
    _seed_collection_sync()
    key = _make_rsa_keypair("kid-A")
    tenant_id = UUID("33333333-3333-3333-3333-333333333333")
    token = _mint_token(
        key,
        sub="op-1",
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(tenant_id),
        capabilities=_ENTITLED_CAPS,
    )

    fake_corpus = _mock_corpus(_make_chunk("c1"), _make_chunk("c2", "another"))
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "vsan disk groups", "collection": "vmware", "limit": 5},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert len(body["chunks"]) == 2
    assert body["chunks"][0]["chunk_id"] == "c1"
    assert body["chunks"][0]["content"] == "vSAN disk groups..."
    # The backend id never appears in the response.
    assert "backend" not in json.dumps(body)

    fake_corpus.assert_awaited_once()
    call_args = fake_corpus.await_args
    operator_arg = call_args.args[0]
    assert operator_arg.tenant_id == tenant_id
    assert call_args.args[1] == "vsan disk groups"
    assert call_args.kwargs["limit"] == 5


def test_optional_refinements_forwarded_as_binary_filter_not_weight(
    client: TestClient,
) -> None:
    """Optional product+version reach the backend as ``metadata_filters``.

    The refinements are a binary containment filter (#1178 / #1177), so they
    must arrive on the backend's ``metadata_filters`` kwarg verbatim -- never
    as a ``weight`` / boost parameter. ``collection`` is the router key and
    must NOT appear in the metadata filters.
    """
    _seed_collection_sync()
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key, sub="op-2", tenant_role=TenantRole.OPERATOR.value, capabilities=_ENTITLED_CAPS
    )

    fake_corpus = _mock_corpus()
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={
                "query": "esxi upgrade",
                "collection": "vmware",
                "product": "vmware",
                "version": "8.0",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    call_kwargs = fake_corpus.await_args.kwargs
    assert call_kwargs["metadata_filters"] == {"product": "vmware", "version": "8.0"}
    # The collection routes/entitles; it is not a metadata filter.
    assert "collection" not in call_kwargs["metadata_filters"]
    assert "weight" not in call_kwargs
    assert "boost" not in call_kwargs


def test_collection_only_omits_metadata_filters(client: TestClient) -> None:
    """Product/version are optional: a collection-only query still succeeds.

    With no product/version, ``metadata_filters`` is ``None`` (the collection
    alone scopes the query).
    """
    _seed_collection_sync()
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key, sub="op-co", tenant_role=TenantRole.OPERATOR.value, capabilities=_ENTITLED_CAPS
    )
    fake_corpus = _mock_corpus(_make_chunk())
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "q", "collection": "vmware"},  # no product/version
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert fake_corpus.await_args.kwargs["metadata_filters"] is None


# ---------------------------------------------------------------------------
# Collection scope (422 fail-closed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"query": "q"},  # no collection
        {"query": "q", "collection": ""},  # blank collection
        {"query": "q", "collection": "  "},  # blank-after-strip
        {"query": "q", "product": "vmware", "version": "9.0"},  # refinements, no collection
    ],
)
def test_missing_or_blank_collection_rejects_with_422(
    client: TestClient, body: dict[str, str]
) -> None:
    """A missing / blank collection → 422. The backend must NOT be called."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key, sub="op-rf", tenant_role=TenantRole.OPERATOR.value, capabilities=_ENTITLED_CAPS
    )
    fake_corpus = _mock_corpus()
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 422
    assert "collection" in json.dumps(response.json()["detail"])
    fake_corpus.assert_not_awaited()


def test_unknown_collection_rejects_with_422(client: TestClient) -> None:
    """A collection key naming no visible collection → 422 (invalid argument)."""
    # No collection seeded.
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key, sub="op-unk", tenant_role=TenantRole.OPERATOR.value, capabilities=_ENTITLED_CAPS
    )
    fake_corpus = _mock_corpus()
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "q", "collection": "nope"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "unknown_collection"
    fake_corpus.assert_not_awaited()


def test_empty_query_rejects_with_422(client: TestClient) -> None:
    """``query=""`` fails Pydantic min_length validation."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key, sub="op-eq", tenant_role=TenantRole.OPERATOR.value, capabilities=_ENTITLED_CAPS
    )
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "", "collection": "vmware"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Per-collection entitlement (403) + readiness (409)
# ---------------------------------------------------------------------------


def test_not_entitled_to_collection_returns_403(client: TestClient) -> None:
    """A tenant lacking ``meho-docs:vmware`` → 403 on a known collection.

    The base ``meho-docs`` capability authenticates the add-on; the
    per-collection entitlement is the finer gate that rejects the query.
    The backend is never called.
    """
    _seed_collection_sync()
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key,
        sub="op-nopriv",
        tenant_role=TenantRole.OPERATOR.value,
        capabilities=["meho-docs"],  # base only, no per-collection key
    )
    fake_corpus = _mock_corpus(_make_chunk())
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "q", "collection": "vmware"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 403
    assert "entitled" in json.dumps(response.json()["detail"])
    fake_corpus.assert_not_awaited()


@pytest.mark.parametrize("status", ["provisioning", "rebuilding"])
def test_transiently_not_ready_collection_returns_409(client: TestClient, status: str) -> None:
    """A known + entitled but transiently not-ready collection → 409 (retryable)."""
    _seed_collection_sync(status=status)
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key, sub="op-nr", tenant_role=TenantRole.OPERATOR.value, capabilities=_ENTITLED_CAPS
    )
    fake_corpus = _mock_corpus(_make_chunk())
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "q", "collection": "vmware"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 409
    assert "not ready" in json.dumps(response.json()["detail"])
    fake_corpus.assert_not_awaited()


def test_disabled_collection_returns_terminal_403(client: TestClient) -> None:
    """A ``disabled`` collection → 403 (terminal), distinct from the 409 a rebuild yields.

    Asserts the operationally load-bearing terminal/retryable split (#1567):
    a disabled collection is a permanent "do not retry" 403 carrying the
    structured ``detail.error='collection_disabled'`` marker, distinguishable
    both from the retryable 409 a ``provisioning`` / ``rebuilding`` collection
    returns and from the plain-string entitlement-miss 403.
    """
    _seed_collection_sync(status="disabled")
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key, sub="op-dis", tenant_role=TenantRole.OPERATOR.value, capabilities=_ENTITLED_CAPS
    )
    fake_corpus = _mock_corpus(_make_chunk())
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "q", "collection": "vmware"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["error"] == "collection_disabled"
    assert detail["retryable"] is False
    fake_corpus.assert_not_awaited()


# ---------------------------------------------------------------------------
# Backend unavailable (503 fail-closed)
# ---------------------------------------------------------------------------


def test_backend_unavailable_maps_to_503_not_empty_200(client: TestClient) -> None:
    """A :class:`CorpusUnavailable` from the backend → 503, never empty 200."""
    _seed_collection_sync()
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key, sub="op-503", tenant_role=TenantRole.OPERATOR.value, capabilities=_ENTITLED_CAPS
    )
    fake_corpus = AsyncMock(side_effect=CorpusUnavailable("corpus unreachable: ConnectError"))
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "q", "collection": "vmware"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 503
    assert "chunks" not in response.json()


# ---------------------------------------------------------------------------
# RBAC (401 / 403)
# ---------------------------------------------------------------------------


def test_unauthenticated_request_returns_401(client: TestClient) -> None:
    """No Authorization header → 401."""
    response = client.post(
        "/api/v1/search_docs",
        json={"query": "q", "collection": "vmware"},
    )
    assert response.status_code == 401


def test_read_only_role_returns_403(client: TestClient) -> None:
    """``read_only`` JWT → 403 (route gated on OPERATOR)."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key, sub="op-ro", tenant_role=TenantRole.READ_ONLY.value, capabilities=_ENTITLED_CAPS
    )
    with respx.mock as mock_router, structlog.testing.capture_logs() as cap_logs:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "q", "collection": "vmware"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient_role"}
    insufficient = [e for e in cap_logs if e.get("event") == "insufficient_role"]
    assert len(insufficient) == 1
    assert insufficient[0]["operator_sub"] == "op-ro"


def test_admin_role_returns_200(client: TestClient) -> None:
    """``tenant_admin`` JWT passes the operator gate (admin >= operator)."""
    _seed_collection_sync()
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key,
        sub="op-admin",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        capabilities=_ENTITLED_CAPS,
    )
    fake_corpus = _mock_corpus()
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "q", "collection": "vmware"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Central audit contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_row_carries_op_id_hash_and_collection_not_raw_query(
    client: TestClient,
) -> None:
    """One audit row: ``op_id=meho.docs.search``, ``op_class=read``, hash + collection.

    The raw query MUST NOT appear anywhere in the payload -- only its
    SHA-256 digest. ``collection`` / ``product`` / ``version`` and
    ``hit_count`` are recorded.
    """
    await _seed_global_collection()
    key = _make_rsa_keypair("kid-A")
    raw_query = "how do I expand a vsan disk group"
    token = _mint_token(
        key, sub="op-audit", tenant_role=TenantRole.OPERATOR.value, capabilities=_ENTITLED_CAPS
    )
    fake_corpus = _mock_corpus(_make_chunk("c1"), _make_chunk("c2"))
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={
                "query": raw_query,
                "collection": "vmware",
                "product": "vmware",
                "version": "9.0",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.path == "/api/v1/search_docs")
        )
        rows = result.scalars().all()

    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["op_id"] == "meho.docs.search"
    assert payload["op_class"] == "read"
    assert payload["query_hash"] == _compute_query_hash(raw_query)
    assert payload["collection"] == "vmware"
    assert payload["product"] == "vmware"
    assert payload["version"] == "9.0"
    assert payload["hit_count"] == 2

    serialised = json.dumps(payload)
    assert raw_query not in serialised
    assert len(payload["query_hash"]) == 64


@pytest.mark.asyncio
async def test_query_audit_finds_row_by_op_id(client: TestClient) -> None:
    """The audit row is returned by ``query_audit`` filtered on the op_id."""
    await _seed_global_collection()
    key = _make_rsa_keypair("kid-A")
    tenant_id = UUID("44444444-4444-4444-4444-444444444444")
    token = _mint_token(
        key,
        sub="op-qa",
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(tenant_id),
        capabilities=_ENTITLED_CAPS,
    )
    fake_corpus = _mock_corpus(_make_chunk())
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "vsan", "collection": "vmware"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await query_audit(
            AuditQueryFilters(op_id="meho.docs.search"),
            tenant_id=tenant_id,
            session=session,
        )

    assert len(result.rows) == 1
    assert result.rows[0].op_id == "meho.docs.search"
    assert result.rows[0].op_class == "read"


@pytest.mark.asyncio
async def test_audit_row_written_on_backend_503(client: TestClient) -> None:
    """A 503 still produces an audit row with the query identity + collection scope.

    The contextvars are bound *before* the backend call, so the fail-closed
    path is still attributable. ``hit_count`` is absent (bound only after a
    successful return).
    """
    await _seed_global_collection()
    key = _make_rsa_keypair("kid-A")
    raw_query = "esxi boot loop"
    token = _mint_token(
        key, sub="op-503-audit", tenant_role=TenantRole.OPERATOR.value, capabilities=_ENTITLED_CAPS
    )
    fake_corpus = AsyncMock(side_effect=CorpusUnavailable("corpus returned HTTP 502", status=502))
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={
                "query": raw_query,
                "collection": "vmware",
                "product": "vmware",
                "version": "8.0",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 503

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.path == "/api/v1/search_docs")
        )
        rows = result.scalars().all()

    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["op_id"] == "meho.docs.search"
    assert payload["query_hash"] == _compute_query_hash(raw_query)
    assert payload["collection"] == "vmware"
    assert payload["product"] == "vmware"
    assert payload["version"] == "8.0"
    assert "hit_count" not in payload
    assert raw_query not in json.dumps(payload)


# ---------------------------------------------------------------------------
# Cross-collection fan-out + RRF (G4.6-T5 #1554)
# ---------------------------------------------------------------------------


#: Capabilities entitling the tenant to both fan-out collections.
_FANOUT_CAPS = ["meho-docs", "meho-docs:vmware", "meho-docs:netapp"]


def test_fanout_all_sentinel_queries_entitled_collections_and_tags_provenance(
    client: TestClient,
) -> None:
    """``collection='all'`` fans out across the entitled, ready collections.

    Both collections route to the ``corpus-http`` backend (the seam is mocked
    once), so the seam is awaited once per collection and every returned
    chunk is tagged with its source ``collection``.
    """
    _seed_collection_sync(collection_key="vmware")
    _seed_collection_sync(collection_key="netapp")
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key, sub="op-fan", tenant_role=TenantRole.OPERATOR.value, capabilities=_FANOUT_CAPS
    )
    fake_corpus = _mock_corpus(_make_chunk("c1"), _make_chunk("c2", "second"))
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "how to configure", "collection": "all"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    # Each entitled collection's backend was queried independently.
    assert fake_corpus.await_count == 2
    # Every chunk carries its source-collection provenance tag.
    seen_collections = {c["collection"] for c in body["chunks"]}
    assert seen_collections == {"vmware", "netapp"}
    assert "backend" not in json.dumps(body)


def test_fanout_explicit_list_audit_collection_is_sorted_set(client: TestClient) -> None:
    """``audit_collection`` records the sorted, comma-joined queried set."""
    _seed_collection_sync(collection_key="vmware")
    _seed_collection_sync(collection_key="netapp")
    key = _make_rsa_keypair("kid-A")
    raw_query = "shared question"
    token = _mint_token(
        key, sub="op-fan2", tenant_role=TenantRole.OPERATOR.value, capabilities=_FANOUT_CAPS
    )
    fake_corpus = _mock_corpus(_make_chunk("c1"))
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": raw_query, "collections": ["netapp", "vmware"]},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200

    sessionmaker = get_sessionmaker()

    async def _read_audit() -> AuditLog:
        async with sessionmaker() as session:
            result = await session.execute(
                select(AuditLog).where(AuditLog.path == "/api/v1/search_docs")
            )
            rows = result.scalars().all()
        assert len(rows) == 1
        return rows[0]

    payload = asyncio.run(_read_audit()).payload
    assert payload["op_id"] == "meho.docs.search"
    # Sorted, comma-joined queried set so who-touched attributes the fan-out.
    assert payload["collection"] == "netapp,vmware"
    assert payload["query_hash"] == _compute_query_hash(raw_query)
    assert raw_query not in json.dumps(payload)


def test_fanout_and_single_collection_are_mutually_exclusive(client: TestClient) -> None:
    """Supplying both ``collection`` and ``collections`` → 422 (no backend call)."""
    _seed_collection_sync(collection_key="vmware")
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key, sub="op-fan3", tenant_role=TenantRole.OPERATOR.value, capabilities=_FANOUT_CAPS
    )
    fake_corpus = _mock_corpus(_make_chunk("c1"))
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "q", "collection": "vmware", "collections": ["netapp"]},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 422
    fake_corpus.assert_not_awaited()


def test_fanout_drops_non_entitled_collection_from_all(client: TestClient) -> None:
    """``all`` never contributes a non-entitled collection.

    Two collections are seeded but the tenant is entitled to only ``vmware``;
    ``netapp`` is dropped silently-but-logged, so the fan-out queries exactly
    one backend.
    """
    _seed_collection_sync(collection_key="vmware")
    _seed_collection_sync(collection_key="netapp")
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key,
        sub="op-fan4",
        tenant_role=TenantRole.OPERATOR.value,
        capabilities=["meho-docs", "meho-docs:vmware"],  # netapp NOT entitled
    )
    fake_corpus = _mock_corpus(_make_chunk("c1"))
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "q", "collection": "all"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    # Only the entitled collection's backend was queried.
    assert fake_corpus.await_count == 1
    body = response.json()
    assert {c["collection"] for c in body["chunks"]} == {"vmware"}
