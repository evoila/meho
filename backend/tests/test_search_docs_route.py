# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.search_docs` (G4.5-T3 #1521).

Coverage matrix (Task #1521 acceptance criteria):

* **REQUIRE_FILTERS** -- a request missing ``product`` or ``version`` (or
  carrying a blank one) is rejected **422** when the
  ``corpus_require_filters`` gate is on; with both present the corpus is
  called and cited chunks are returned. With the gate off, a partial /
  absent scope is accepted and forwarded as-is.
* **Binary scope, not a weight** -- the product+version scope reaches the
  T2 federation client as ``metadata_filters`` (a binary containment
  scope), verified via the mock; it is never expressed as a ranking
  weight.
* **RBAC + tenant** -- ``read_only`` → 403; ``operator`` → 200; the
  forwarded operator carries the JWT's ``tenant_id`` (tenant-scoped by
  construction). Unauthenticated → 401.
* **Central audit** -- one audit row per query, ``op_id="meho.docs.search"``,
  ``op_class="read"``, the raw query stored only as a SHA-256 hash, plus
  ``product`` / ``version`` / ``hit_count``; the row is returned by
  :func:`~meho_backplane.audit_query.query.query_audit` filtered on that
  ``op_id``.
* **Corpus-unavailable** -- a :class:`CorpusUnavailable` from the
  federation client → 503 (fail-closed), never an empty 200.

The corpus call is mocked at :func:`meho_backplane.docs_search.service.search_corpus`
(the service's import site) so the route tests don't depend on a live
corpus; the audit read-back uses the autouse SQLite engine the chassis
audit tests share.
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

from meho_backplane.api.v1.search_docs import _compute_query_hash
from meho_backplane.api.v1.search_docs import router as search_docs_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.audit_query import AuditQueryFilters, query_audit
from meho_backplane.auth.corpus import CorpusChunk, CorpusSearchResponse, CorpusUnavailable
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
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
    """Pin every env var :class:`Settings` reads, around every test.

    ``CORPUS_URL`` is set so the corpus is "configured" (the federation
    client is mocked at the service import site regardless, so the URL is
    never dialled); ``CORPUS_REQUIRE_FILTERS`` defaults on. Tests that
    exercise the gate-off path override it via :func:`_settings_env`'s
    monkeypatch + ``cache_clear``.
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
    monkeypatch.setenv("CORPUS_URL", "https://corpus.test/search")
    monkeypatch.setenv("CORPUS_REQUIRE_FILTERS", "true")
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
# App construction with the search_docs route + audit middleware
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """Return a :class:`FastAPI` mirroring prod with the route mounted.

    Includes the production middleware stack so the audit-payload tests
    see the same contextvar-binding flow production uses. The corpus
    client is patched at the service import site per-test.
    """
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
    """An ``AsyncMock`` standing in for ``search_corpus`` returning *chunks*."""
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
# Happy path + binary scope forwarding
# ---------------------------------------------------------------------------


def test_search_docs_returns_200_with_cited_chunks(client: TestClient) -> None:
    """``operator`` JWT + product+version → 200 with cited chunks."""
    key = _make_rsa_keypair("kid-A")
    tenant_id = UUID("33333333-3333-3333-3333-333333333333")
    token = _mint_token(
        key,
        sub="op-1",
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(tenant_id),
    )

    fake_corpus = _mock_corpus(_make_chunk("c1"), _make_chunk("c2", "another"))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.docs_search.service.search_corpus", new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "vsan disk groups", "product": "vmware", "version": "9.0", "limit": 5},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert len(body["chunks"]) == 2
    assert body["chunks"][0]["chunk_id"] == "c1"
    assert body["chunks"][0]["content"] == "vSAN disk groups..."
    assert body["chunks"][0]["source_url"] == "https://docs.vendor.test/vsan#disk-groups"
    assert body["chunks"][0]["score"] == 0.91

    # The corpus client was called as the operator (forwarded JWT) with
    # the query + limit; the operator object carries the JWT tenant_id.
    fake_corpus.assert_awaited_once()
    call_args = fake_corpus.await_args
    operator_arg = call_args.args[0]
    assert operator_arg.tenant_id == tenant_id
    assert call_args.args[1] == "vsan disk groups"
    assert call_args.kwargs["limit"] == 5


def test_product_version_forwarded_as_binary_filter_not_weight(client: TestClient) -> None:
    """The product+version scope reaches the corpus as ``metadata_filters``.

    The scope is a binary containment filter (#1178 / #1177), so it must
    arrive on the T2 client's ``metadata_filters`` kwarg verbatim -- never
    as a ``weight`` / boost parameter.
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-2", tenant_role=TenantRole.OPERATOR.value)

    fake_corpus = _mock_corpus()
    with (
        respx.mock as mock_router,
        patch("meho_backplane.docs_search.service.search_corpus", new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "esxi upgrade", "product": "vmware", "version": "8.0"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    call_kwargs = fake_corpus.await_args.kwargs
    assert call_kwargs["metadata_filters"] == {"product": "vmware", "version": "8.0"}
    # The scope is a filter, not a weight: no weighting kwarg is passed.
    assert "weight" not in call_kwargs
    assert "weights" not in call_kwargs
    assert "boost" not in call_kwargs


# ---------------------------------------------------------------------------
# REQUIRE_FILTERS (422 fail-closed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "missing"),
    [
        ({"query": "q", "version": "9.0"}, "product"),
        ({"query": "q", "product": "vmware"}, "version"),
        ({"query": "q"}, "product"),
        ({"query": "q", "product": "  ", "version": "9.0"}, "product"),
        ({"query": "q", "product": "vmware", "version": ""}, "version"),
    ],
)
def test_missing_or_blank_filter_rejects_with_422(
    client: TestClient, body: dict[str, str], missing: str
) -> None:
    """A missing / blank product or version → 422 (REQUIRE_FILTERS).

    The corpus client must NOT be called when the mandatory scope is
    absent -- an unfiltered corpus query is exactly what fail-closed
    prevents.
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-rf", tenant_role=TenantRole.OPERATOR.value)
    fake_corpus = _mock_corpus()
    with (
        respx.mock as mock_router,
        patch("meho_backplane.docs_search.service.search_corpus", new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 422
    assert missing in json.dumps(response.json()["detail"])
    fake_corpus.assert_not_awaited()


def test_gate_off_accepts_partial_scope_and_forwards_present_keys(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``corpus_require_filters`` off, a partial scope is accepted.

    Whatever filter is present still scopes the corpus query; absent
    keys simply widen it (the corpus owns the policy in this mode). The
    enforcement is gated, per the Initiative -- not hard-wired.
    """
    monkeypatch.setenv("CORPUS_REQUIRE_FILTERS", "false")
    get_settings.cache_clear()

    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-gate-off", tenant_role=TenantRole.OPERATOR.value)
    fake_corpus = _mock_corpus(_make_chunk())
    with (
        respx.mock as mock_router,
        patch("meho_backplane.docs_search.service.search_corpus", new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "q", "product": "vmware"},  # no version
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    # Only the present key is forwarded; no `version: None` is injected.
    assert fake_corpus.await_args.kwargs["metadata_filters"] == {"product": "vmware"}


def test_empty_query_rejects_with_422(client: TestClient) -> None:
    """``query=""`` fails Pydantic min_length validation."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-eq", tenant_role=TenantRole.OPERATOR.value)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "", "product": "vmware", "version": "9.0"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Corpus unavailable (503 fail-closed)
# ---------------------------------------------------------------------------


def test_corpus_unavailable_maps_to_503_not_empty_200(client: TestClient) -> None:
    """A :class:`CorpusUnavailable` from the client → 503, never empty 200."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-503", tenant_role=TenantRole.OPERATOR.value)
    fake_corpus = AsyncMock(side_effect=CorpusUnavailable("corpus unreachable: ConnectError"))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.docs_search.service.search_corpus", new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "q", "product": "vmware", "version": "9.0"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 503
    assert "chunks" not in response.json()


def test_unconfigured_corpus_maps_to_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unset ``CORPUS_URL`` → the real client raises → 503.

    Uses the *real* :func:`search_corpus` (no patch) with ``CORPUS_URL``
    cleared, proving the unconfigured branch fails closed end-to-end
    rather than via the mock.
    """
    monkeypatch.delenv("CORPUS_URL", raising=False)
    get_settings.cache_clear()

    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-unconfig", tenant_role=TenantRole.OPERATOR.value)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "q", "product": "vmware", "version": "9.0"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 503


# ---------------------------------------------------------------------------
# RBAC (401 / 403)
# ---------------------------------------------------------------------------


def test_unauthenticated_request_returns_401(client: TestClient) -> None:
    """No Authorization header → 401."""
    response = client.post(
        "/api/v1/search_docs",
        json={"query": "q", "product": "vmware", "version": "9.0"},
    )
    assert response.status_code == 401


def test_read_only_role_returns_403(client: TestClient) -> None:
    """``read_only`` JWT → 403 (route gated on OPERATOR)."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-ro", tenant_role=TenantRole.READ_ONLY.value)
    with respx.mock as mock_router, structlog.testing.capture_logs() as cap_logs:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "q", "product": "vmware", "version": "9.0"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient_role"}
    insufficient = [e for e in cap_logs if e.get("event") == "insufficient_role"]
    assert len(insufficient) == 1
    assert insufficient[0]["operator_sub"] == "op-ro"


def test_admin_role_returns_200(client: TestClient) -> None:
    """``tenant_admin`` JWT passes the operator gate (admin >= operator)."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-admin", tenant_role=TenantRole.TENANT_ADMIN.value)
    fake_corpus = _mock_corpus()
    with (
        respx.mock as mock_router,
        patch("meho_backplane.docs_search.service.search_corpus", new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "q", "product": "vmware", "version": "9.0"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Central audit contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_row_carries_op_id_hash_and_scope_not_raw_query(client: TestClient) -> None:
    """One audit row: ``op_id=meho.docs.search``, ``op_class=read``, hash + scope.

    The raw query MUST NOT appear anywhere in the payload -- only its
    SHA-256 digest. ``product`` / ``version`` (operator-chosen scopes,
    not tenant-shaped identifiers) and ``hit_count`` are recorded.
    """
    key = _make_rsa_keypair("kid-A")
    raw_query = "how do I expand a vsan disk group"
    token = _mint_token(key, sub="op-audit", tenant_role=TenantRole.OPERATOR.value)
    fake_corpus = _mock_corpus(_make_chunk("c1"), _make_chunk("c2"))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.docs_search.service.search_corpus", new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": raw_query, "product": "vmware", "version": "9.0"},
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
    assert payload["product"] == "vmware"
    assert payload["version"] == "9.0"
    assert payload["hit_count"] == 2

    serialised = json.dumps(payload)
    assert raw_query not in serialised
    assert len(payload["query_hash"]) == 64


@pytest.mark.asyncio
async def test_query_audit_finds_row_by_op_id(client: TestClient) -> None:
    """The audit row is returned by ``query_audit`` filtered on the op_id.

    Closes the loop that who-touched / ``query_audit`` surface every
    docs query under the named op -- the reason the route binds a canonical
    ``op_id`` rather than the path-derived default.
    """
    key = _make_rsa_keypair("kid-A")
    tenant_id = UUID("44444444-4444-4444-4444-444444444444")
    token = _mint_token(
        key,
        sub="op-qa",
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(tenant_id),
    )
    fake_corpus = _mock_corpus(_make_chunk())
    with (
        respx.mock as mock_router,
        patch("meho_backplane.docs_search.service.search_corpus", new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": "vsan", "product": "vmware", "version": "9.0"},
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
async def test_audit_row_written_on_corpus_503(client: TestClient) -> None:
    """A 503 still produces an audit row with the query identity + scope.

    The contextvars are bound *before* the corpus call, so the
    fail-closed path is still attributable: who searched for what
    (hashed), scoped to which product/version, even though it failed.
    ``hit_count`` is absent (bound only after a successful return).
    """
    key = _make_rsa_keypair("kid-A")
    raw_query = "esxi boot loop"
    token = _mint_token(key, sub="op-503-audit", tenant_role=TenantRole.OPERATOR.value)
    fake_corpus = AsyncMock(side_effect=CorpusUnavailable("corpus returned HTTP 502", status=502))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.docs_search.service.search_corpus", new=fake_corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/search_docs",
            json={"query": raw_query, "product": "vmware", "version": "8.0"},
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
    assert payload["product"] == "vmware"
    assert payload["version"] == "8.0"
    assert "hit_count" not in payload
    assert raw_query not in json.dumps(payload)
