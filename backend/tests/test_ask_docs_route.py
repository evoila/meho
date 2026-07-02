# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.ask_docs` (G4.6-T2 #1917).

The REST face of the ``ask_docs`` grounded-answer pipeline -- the synthesis
sibling of ``POST /api/v1/search_docs``. Coverage matrix (Task #1917
acceptance criteria):

* **Grounded answer** -- an entitled operator gets ``{answer, citations[]}``
  where every citation resolves to a retrieved chunk and carries the #1919
  resolved navigable ``link``; the answer is grounded strictly in the
  retrieved chunks.
* **Empty retrieval** -- zero chunks returns the deterministic "no grounded
  answer" 200 (no model call), not an error.
* **Collection scope mirrors ``search_docs``** -- missing / blank
  ``collection`` -> 422; a tenant lacking ``meho-docs:<collection>`` -> 403
  ``not_entitled`` (structured); a ``disabled`` collection -> terminal 403;
  a transiently not-ready collection -> 409.
* **Cross-tenant / absent collection -> 422** -- a collection key not
  visible to the tenant-scoped catalogue is the same ``unknown_collection``
  422 ``search_docs`` returns (see the cross-tenant test's docstring for the
  reconciliation of the issue's "cross-tenant/absent -> 404" wording against
  its dominant "mirror ``search_docs``" requirement).
* **Single-collection only** -- a ``collections`` fan-out field is rejected
  at 422 (``extra="forbid"``); ``ask_docs`` never fans out.
* **Answer-pipeline legs -> 5xx (#1918)** -- ``expand_failed`` /
  ``model_unavailable`` / ``corpus_unavailable`` -> 503,
  ``synthesis_malformed`` -> 502, each carrying the structured
  ``{detail, leg, cause, message}`` envelope on ``detail`` -- the SAME shape
  the MCP ``ask_docs`` tool returns on ``error.data``.
* **RBAC** -- ``read_only`` -> 403; unauthenticated -> 401.
* **Central audit** -- one row, ``op_id=meho.docs.ask``, ``op_class=read``,
  the raw query stored only as a SHA-256 hash.

Three seams are mocked so no network / model is touched: the corpus
transport (``...backends.corpus_http.search_corpus``, retrieval), the
**expand** LLM client (``...docs_search.expansion.build_anthropic_ingest_llm_client``),
and the **synthesis** LLM client (``...docs_search.synthesis.build_anthropic_ingest_llm_client``).
An autouse fixture pins a working expand client (mirroring the MCP test
suite) so every pipeline-reaching test does not 503 on the expand leg.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.ask_docs import _compute_query_hash
from meho_backplane.api.v1.ask_docs import router as ask_docs_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.corpus import CorpusChunk, CorpusSearchResponse, CorpusUnavailable
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, DocCollection
from meho_backplane.docs_search.answer_errors import (
    ANSWER_ERROR_DETAIL,
    CAUSE_CLIENT_UNAVAILABLE,
    CAUSE_CORPUS_UNAVAILABLE,
    CAUSE_EXPANSION_INVALID,
    CAUSE_SYNTHESIS_CITATION_RESOLUTION,
    CAUSE_SYNTHESIS_PARSE,
    LEG_CORPUS,
    LEG_EXPAND,
    LEG_MODEL,
    LEG_SYNTHESIS,
)
from meho_backplane.docs_search.synthesis import NO_GROUNDED_ANSWER
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.operations.ingest import LlmJsonResult
from meho_backplane.operations.ingest.pipeline import LlmClientUnavailable
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

#: The corpus-http backend's transport seam — the retrieval side.
_CORPUS_SEAM = "meho_backplane.docs_search.backends.corpus_http.search_corpus"

#: Where synthesis resolves its default LLM client (the answer side).
_BUILD_LLM_CLIENT = "meho_backplane.docs_search.synthesis.build_anthropic_ingest_llm_client"

#: Where the corpus-aware expand step (#1916) resolves its default LLM client.
#: Distinct from the synthesis seam so a test can pin the expand client
#: independently (or assert its fail-closed factory propagates).
_BUILD_EXPAND_CLIENT = "meho_backplane.docs_search.expansion.build_anthropic_ingest_llm_client"

#: Capabilities entitling the tenant to the seeded ``vmware`` collection.
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


async def _seed_tenant_collection(
    *, collection_key: str, tenant_id: UUID, status: str = "ready"
) -> None:
    """Insert a collection owned by *tenant_id* (the cross-tenant fixture)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(
            DocCollection(
                tenant_id=tenant_id,
                collection_key=collection_key,
                vendor="Other Tenant Vendor",
                products=["thing"],
                description="A collection owned by another tenant.",
                when_to_use="Never, from a different tenant.",
                backend={"type": "corpus-http"},
                status=status,
            ),
        )


def _seed_collection_sync(**kwargs: Any) -> None:
    """Seed a global collection from a sync test via a one-shot loop."""
    asyncio.run(_seed_global_collection(**kwargs))


# ---------------------------------------------------------------------------
# App construction + LLM stubs
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """Return a :class:`FastAPI` mirroring prod with the route mounted."""
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(ask_docs_router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    """``TestClient`` driving a fresh app per test."""
    yield TestClient(_build_app())


class _StubLlmClient:
    """Deterministic ``LlmClient`` returning a fixed raw JSON string.

    Captures the prompts so a test can assert the retrieved chunks were
    framed into the synthesis prompt (the grounding evidence) without a real
    model call.
    """

    def __init__(self, raw: str) -> None:
        self._raw = raw
        self.captured: dict[str, Any] = {}

    async def generate_json(
        self, *, system_prompt: str, user_prompt: str, max_output_tokens: int
    ) -> str:
        return self._raw

    async def generate_structured_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
        response_format: Any | None = None,
    ) -> LlmJsonResult:
        self.captured["system_prompt"] = system_prompt
        self.captured["user_prompt"] = user_prompt
        self.captured["max_output_tokens"] = max_output_tokens
        self.captured["response_format"] = response_format
        return LlmJsonResult(text=self._raw, stop_reason="end_turn")


@pytest.fixture(autouse=True)
def _default_expand_client() -> Iterator[None]:
    """Pin a working expand client for every test (#1916), like the MCP suite.

    ``ask_docs`` runs the corpus-aware expand step before retrieval; the
    default expand client fails closed with no ``ANTHROPIC_API_KEY`` (never
    set in the test env), so without this every pipeline-reaching test would
    503 on the expand leg. A test asserting expand behaviour / its
    fail-closed posture re-patches ``_BUILD_EXPAND_CLIENT`` inside its own
    ``with`` block (applied later, so it wins).
    """
    stub = _StubLlmClient(json.dumps({"queries": ["VMware NSX configuration maximums"]}))
    with patch(_BUILD_EXPAND_CLIENT, return_value=stub):
        yield


def _fake_corpus(*chunks: CorpusChunk) -> Any:
    """An async ``search_corpus`` stand-in returning *chunks*, capturing args."""
    captured: dict[str, Any] = {}

    async def _search(operator: Any, query: str, **kwargs: Any) -> CorpusSearchResponse:
        captured["operator"] = operator
        captured["query"] = query
        captured.update(kwargs)
        return CorpusSearchResponse(chunks=list(chunks))

    _search.captured = captured  # type: ignore[attr-defined]
    return _search


_SAMPLE_CHUNK = CorpusChunk(
    chunk_id="nsx-9.0-maximums-0007",
    document_id="nsx-9.0-config-maximums",
    content="NSX 9.0 supports up to 10,000 logical switches per manager.",
    source_url="https://docs.example.com/nsx/9.0/maximums",
    score=0.91,
)


def _token(key: Any, *, sub: str, role: str = TenantRole.OPERATOR.value, **kw: Any) -> str:
    """Mint a JWT with the entitled caps unless overridden."""
    kw.setdefault("capabilities", _ENTITLED_CAPS)
    return _mint_token(key, sub=sub, tenant_role=role, **kw)


# ---------------------------------------------------------------------------
# Pure helper coverage
# ---------------------------------------------------------------------------


def test_compute_query_hash_matches_search_docs_contract() -> None:
    """Query hash is SHA-256 of UTF-8 (64 hex chars), shared with search_docs."""
    from meho_backplane.api.v1.search_docs import _compute_query_hash as search_hash

    h = _compute_query_hash("vsan disk groups")
    assert len(h) == 64
    assert h == search_hash("vsan disk groups")  # one hash function across faces
    int(h, 16)


# ---------------------------------------------------------------------------
# Happy path (AC: returns {answer, citations[]} for an entitled collection)
# ---------------------------------------------------------------------------


def test_ask_docs_returns_grounded_cited_answer(client: TestClient) -> None:
    """An entitled operator gets ``{answer, citations[]}`` with a resolved link."""
    _seed_collection_sync()
    key = _make_rsa_keypair("kid-A")
    tenant_id = UUID("33333333-3333-3333-3333-333333333333")
    token = _token(key, sub="op-1", tenant_id=str(tenant_id))

    corpus = _fake_corpus(_SAMPLE_CHUNK)
    synth = _StubLlmClient(
        json.dumps(
            {
                "answer": "NSX 9.0 supports up to 10,000 logical switches per manager.",
                "cited_chunk_ids": ["nsx-9.0-maximums-0007"],
            }
        )
    )
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=corpus),
        patch(_BUILD_LLM_CLIENT, return_value=synth),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/ask_docs",
            json={
                "query": "How many logical switches does NSX 9.0 support?",
                "collection": "vmware",
                "product": "nsx",
                "version": "9.0",
                "limit": 5,
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"].startswith("NSX 9.0 supports")
    assert len(body["citations"]) == 1
    citation = body["citations"][0]
    assert citation["chunk_id"] == "nsx-9.0-maximums-0007"
    # #1919: an https source passes through as the clickable href (SAME shape
    # the MCP ask_docs tool returns).
    assert citation["link"]["clickable"] is True
    assert citation["link"]["href"] == "https://docs.example.com/nsx/9.0/maximums"
    # The refinements reached the backend; the operator identity was forwarded.
    assert corpus.captured["metadata_filters"] == {"product": "nsx", "version": "9.0"}  # type: ignore[attr-defined]
    assert corpus.captured["operator"].tenant_id == tenant_id  # type: ignore[attr-defined]
    # The retrieved evidence framed into the synthesis prompt.
    assert "10,000 logical switches" in synth.captured["user_prompt"]


def test_ask_docs_kb_gs_citation_resolves_to_canonical_link(client: TestClient) -> None:
    """A KB ``gs://`` citation is normalized to the canonical KB URL (#1919, #132).

    Post-#132 the raw ``gs://`` object path never reaches the wire: the
    citation's ``source_url`` is normalized to the canonical Broadcom KB URL
    (backend-agnostic, still clickable). The re-derived ``link`` therefore
    resolves via the ``external`` pass-through arm (same ``href``, only the
    ``kind`` tag differs from the pre-#132 ``broadcom_kb``).
    """
    _seed_collection_sync()
    key = _make_rsa_keypair("kid-A")
    token = _token(key, sub="op-kb")
    kb_chunk = CorpusChunk(
        chunk_id="kb-414551-0001",
        document_id="broadcom-kb-414551",
        content="vCenter Server scaling maximums for vSphere 9.0.",
        source_url="gs://meho-knowledge-vmware-corpus/kb/broadcom-kb/articles/41/414551.html",
        score=0.95,
    )
    corpus = _fake_corpus(kb_chunk)
    synth = _StubLlmClient(
        json.dumps({"answer": "See KB 414551.", "cited_chunk_ids": ["kb-414551-0001"]})
    )
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=corpus),
        patch(_BUILD_LLM_CLIENT, return_value=synth),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/ask_docs",
            json={"query": "vCenter scaling maximums?", "collection": "vmware"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    citation = response.json()["citations"][0]
    # #132: the raw gs:// object path never reaches the wire — source_url is
    # normalized to the canonical KB URL.
    assert not citation["source_url"].startswith("gs://")
    assert citation["source_url"] == "https://knowledge.broadcom.com/external/article/414551"
    link = citation["link"]
    assert link["kind"] == "external"  # re-derived from the canonical https URL
    assert link["clickable"] is True
    assert link["href"] == "https://knowledge.broadcom.com/external/article/414551"


def test_ask_docs_zero_chunks_returns_no_grounded_answer(client: TestClient) -> None:
    """An empty retrieval returns "no grounded answer" 200 without a model call."""
    _seed_collection_sync()
    key = _make_rsa_keypair("kid-A")
    token = _token(key, sub="op-empty")
    corpus = _fake_corpus()  # zero chunks
    synth = _StubLlmClient('{"answer": "never used", "cited_chunk_ids": []}')
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=corpus),
        patch(_BUILD_LLM_CLIENT, return_value=synth),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/ask_docs",
            json={"query": "obscure unanswerable thing", "collection": "vmware"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == NO_GROUNDED_ANSWER
    assert body["citations"] == []
    assert synth.captured == {}  # the model was never called


# ---------------------------------------------------------------------------
# Collection scope (422) — mirrors search_docs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"query": "q"},  # no collection
        {"query": "q", "collection": ""},  # blank
        {"query": "q", "collection": "  "},  # blank-after-strip
    ],
)
def test_missing_or_blank_collection_rejects_with_422(
    client: TestClient, body: dict[str, str]
) -> None:
    """A missing / blank collection -> 422 (mirrors search_docs). No backend call."""
    key = _make_rsa_keypair("kid-A")
    token = _token(key, sub="op-422")
    corpus = _fake_corpus(_SAMPLE_CHUNK)
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/ask_docs", json=body, headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 422
    assert "collection" in json.dumps(response.json()["detail"])
    assert "query" not in corpus.captured  # type: ignore[attr-defined]


def test_unknown_collection_rejects_with_422(client: TestClient) -> None:
    """A key naming no visible collection -> 422 (mirrors search_docs)."""
    key = _make_rsa_keypair("kid-A")  # no collection seeded
    token = _token(key, sub="op-unk")
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/ask_docs",
            json={"query": "q", "collection": "nope"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "unknown_collection"


def test_cross_tenant_collection_is_invisible_and_returns_422(client: TestClient) -> None:
    """A collection owned by ANOTHER tenant is invisible -> 422 unknown_collection.

    The issue's acceptance criterion reads "cross-tenant/absent collection ->
    404", but its dominant requirement (stated twice) is that ``ask_docs``
    **mirror ``search_docs``**. The shared
    :func:`~meho_backplane.docs_search.resolve_entitled_ready_collection` gate
    resolves tenant-first-then-global, so a collection belonging to a
    different tenant is simply *not visible* to this tenant's catalogue and
    raises :class:`UnknownCollectionError` -> 422 (``unknown_collection``) --
    exactly as ``search_docs`` does, with no 404 path anywhere in the shared
    resolver. Implementing a divergent 404 would break the dual-surface
    parity the issue's primary instruction requires, so this route follows
    ``search_docs``: cross-tenant / absent is the 422 unknown-collection arm.
    """
    key = _make_rsa_keypair("kid-A")
    other_tenant = UUID("99999999-9999-9999-9999-999999999999")
    asyncio.run(_seed_tenant_collection(collection_key="secret", tenant_id=other_tenant))
    # This operator is on a DIFFERENT tenant (the minter's default tenant).
    token = _mint_token(
        key,
        sub="op-xt",
        tenant_role=TenantRole.OPERATOR.value,
        capabilities=["meho-docs", "meho-docs:secret"],  # entitled, but wrong tenant
    )
    corpus = _fake_corpus(_SAMPLE_CHUNK)
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/ask_docs",
            json={"query": "q", "collection": "secret"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "unknown_collection"
    assert "query" not in corpus.captured  # type: ignore[attr-defined]


def test_collections_fanout_field_rejected_by_schema(client: TestClient) -> None:
    """``ask_docs`` is single-collection only: a ``collections`` list -> 422.

    ``extra="forbid"`` on the request model rejects the fan-out field at the
    schema boundary, so a client cannot smuggle a cross-collection fan-out
    into the grounded-answer route (matching the MCP tool's single-collection
    contract).
    """
    _seed_collection_sync()
    key = _make_rsa_keypair("kid-A")
    token = _token(key, sub="op-fan")
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/ask_docs",
            json={"query": "q", "collection": "vmware", "collections": ["netapp"]},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Entitlement (403) + readiness (409/403) — mirrors search_docs
# ---------------------------------------------------------------------------


def test_not_entitled_to_collection_returns_structured_403(client: TestClient) -> None:
    """A tenant lacking ``meho-docs:vmware`` -> 403 ``not_entitled`` (structured)."""
    _seed_collection_sync()
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key,
        sub="op-nopriv",
        tenant_role=TenantRole.OPERATOR.value,
        capabilities=["meho-docs"],  # base only, no per-collection key
    )
    corpus = _fake_corpus(_SAMPLE_CHUNK)
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/ask_docs",
            json={"query": "q", "collection": "vmware"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["error"] == "not_entitled"
    assert detail["collection"] == "vmware"
    assert detail["required_capability"] == "meho-docs:vmware"
    assert detail["operator_sub"] == "op-nopriv"
    assert "meho-docs:vmware" in detail["message"]
    assert "query" not in corpus.captured  # type: ignore[attr-defined]


@pytest.mark.parametrize("status", ["provisioning", "rebuilding"])
def test_transiently_not_ready_collection_returns_409(client: TestClient, status: str) -> None:
    """A known + entitled but transiently not-ready collection -> 409 (retryable)."""
    _seed_collection_sync(status=status)
    key = _make_rsa_keypair("kid-A")
    token = _token(key, sub="op-nr")
    corpus = _fake_corpus(_SAMPLE_CHUNK)
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/ask_docs",
            json={"query": "q", "collection": "vmware"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 409
    assert "query" not in corpus.captured  # type: ignore[attr-defined]


def test_disabled_collection_returns_terminal_403(client: TestClient) -> None:
    """A ``disabled`` collection -> terminal 403 (mirrors search_docs)."""
    _seed_collection_sync(status="disabled")
    key = _make_rsa_keypair("kid-A")
    token = _token(key, sub="op-dis")
    corpus = _fake_corpus(_SAMPLE_CHUNK)
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=corpus),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/ask_docs",
            json={"query": "q", "collection": "vmware"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["error"] == "collection_disabled"
    assert detail["retryable"] is False


# ---------------------------------------------------------------------------
# Answer-pipeline legs -> 5xx with the #1918 structured envelope
# ---------------------------------------------------------------------------


def test_unconfigured_synthesis_model_is_503_model_unavailable(client: TestClient) -> None:
    """No synthesis model -> 503 with ``leg=model_unavailable`` (#1918, fail-closed)."""
    _seed_collection_sync()
    key = _make_rsa_keypair("kid-A")
    token = _token(key, sub="op-nomodel")
    corpus = _fake_corpus(_SAMPLE_CHUNK)

    def _fail_closed() -> Any:
        raise LlmClientUnavailable("no ANTHROPIC_API_KEY configured")

    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=corpus),
        patch(_BUILD_LLM_CLIENT, side_effect=_fail_closed),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/ask_docs",
            json={"query": "q", "collection": "vmware"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["detail"] == ANSWER_ERROR_DETAIL
    assert detail["leg"] == LEG_MODEL
    assert detail["cause"] == CAUSE_CLIENT_UNAVAILABLE


def test_unconfigured_expand_model_is_503_expand_failed(client: TestClient) -> None:
    """No expand model -> 503 with ``leg=expand_failed`` before retrieval (#1918)."""
    _seed_collection_sync()
    key = _make_rsa_keypair("kid-A")
    token = _token(key, sub="op-noexpand")
    corpus = _fake_corpus(_SAMPLE_CHUNK)
    synth = _StubLlmClient('{"answer": "unused", "cited_chunk_ids": []}')

    def _fail_closed() -> Any:
        raise LlmClientUnavailable("no ANTHROPIC_API_KEY configured")

    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=corpus),
        patch(_BUILD_EXPAND_CLIENT, side_effect=_fail_closed),
        patch(_BUILD_LLM_CLIENT, return_value=synth),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/ask_docs",
            json={"query": "q", "collection": "vmware"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 503
    detail = response.json()["detail"]
    # The SHARED LlmClientUnavailable is attributed to the EXPAND leg here,
    # not model_unavailable — the per-leg wrap disambiguates the same type.
    assert detail["leg"] == LEG_EXPAND
    assert detail["cause"] == CAUSE_CLIENT_UNAVAILABLE
    # Expansion failed before retrieval or synthesis ran.
    assert "query" not in corpus.captured  # type: ignore[attr-defined]
    assert synth.captured == {}


def test_malformed_expansion_is_503_expand_failed(client: TestClient) -> None:
    """A non-JSON expansion -> 503 ``expand_failed`` / ``expansion_invalid`` (#1918)."""
    _seed_collection_sync()
    key = _make_rsa_keypair("kid-A")
    token = _token(key, sub="op-badexpand")
    corpus = _fake_corpus(_SAMPLE_CHUNK)
    expand = _StubLlmClient("not valid json at all")
    synth = _StubLlmClient('{"answer": "unused", "cited_chunk_ids": []}')
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=corpus),
        patch(_BUILD_EXPAND_CLIENT, return_value=expand),
        patch(_BUILD_LLM_CLIENT, return_value=synth),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/ask_docs",
            json={"query": "q", "collection": "vmware"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["leg"] == LEG_EXPAND
    assert detail["cause"] == CAUSE_EXPANSION_INVALID
    assert synth.captured == {}


def test_corpus_unavailable_is_503_corpus_unavailable(client: TestClient) -> None:
    """A down backend -> 503 with ``leg=corpus_unavailable`` (mirrors search_docs)."""
    _seed_collection_sync()
    key = _make_rsa_keypair("kid-A")
    token = _token(key, sub="op-corpusdown")

    async def _down(_op: Any, _query: str, **_kwargs: Any) -> CorpusSearchResponse:
        raise CorpusUnavailable("corpus_url is not configured")

    synth = _StubLlmClient('{"answer": "unused", "cited_chunk_ids": []}')
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=_down),
        patch(_BUILD_LLM_CLIENT, return_value=synth),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/ask_docs",
            json={"query": "q", "collection": "vmware"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["leg"] == LEG_CORPUS
    assert detail["cause"] == CAUSE_CORPUS_UNAVAILABLE
    assert synth.captured == {}  # the corpus failed before synthesis


def test_fabricated_citation_is_502_synthesis_malformed(client: TestClient) -> None:
    """A model citing an absent chunk_id -> 502 ``synthesis_malformed`` / citation_resolution.

    The model's output broke the grounding contract -- a bad-gateway 502
    (the upstream model answered, badly), distinct from the 503s (model
    unreachable / unconfigured).
    """
    _seed_collection_sync()
    key = _make_rsa_keypair("kid-A")
    token = _token(key, sub="op-fab")
    corpus = _fake_corpus(_SAMPLE_CHUNK)
    synth = _StubLlmClient(
        json.dumps({"answer": "Fabricated.", "cited_chunk_ids": ["does-not-exist-9999"]})
    )
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=corpus),
        patch(_BUILD_LLM_CLIENT, return_value=synth),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/ask_docs",
            json={"query": "q", "collection": "vmware"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["detail"] == ANSWER_ERROR_DETAIL
    assert detail["leg"] == LEG_SYNTHESIS
    assert detail["cause"] == CAUSE_SYNTHESIS_CITATION_RESOLUTION


def test_non_json_synthesis_is_502_synthesis_malformed_parse(client: TestClient) -> None:
    """A non-JSON synthesis output -> 502 ``synthesis_malformed`` / parse (#1918)."""
    _seed_collection_sync()
    key = _make_rsa_keypair("kid-A")
    token = _token(key, sub="op-synthparse")
    corpus = _fake_corpus(_SAMPLE_CHUNK)
    synth = _StubLlmClient("I'm sorry, I cannot answer that.")  # non-JSON
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=corpus),
        patch(_BUILD_LLM_CLIENT, return_value=synth),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/ask_docs",
            json={"query": "q", "collection": "vmware"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["leg"] == LEG_SYNTHESIS
    assert detail["cause"] == CAUSE_SYNTHESIS_PARSE


# ---------------------------------------------------------------------------
# RBAC (401 / 403)
# ---------------------------------------------------------------------------


def test_unauthenticated_request_returns_401(client: TestClient) -> None:
    """No Authorization header -> 401."""
    response = client.post("/api/v1/ask_docs", json={"query": "q", "collection": "vmware"})
    assert response.status_code == 401


def test_read_only_role_returns_403(client: TestClient) -> None:
    """``read_only`` JWT -> 403 (route gated on OPERATOR, like search_docs)."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key, sub="op-ro", tenant_role=TenantRole.READ_ONLY.value, capabilities=_ENTITLED_CAPS
    )
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/ask_docs",
            json={"query": "q", "collection": "vmware"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient_role"}


def test_admin_role_returns_200(client: TestClient) -> None:
    """``tenant_admin`` JWT passes the operator gate (admin >= operator)."""
    _seed_collection_sync()
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key, sub="op-admin", tenant_role=TenantRole.TENANT_ADMIN.value, capabilities=_ENTITLED_CAPS
    )
    corpus = _fake_corpus(_SAMPLE_CHUNK)
    synth = _StubLlmClient(
        json.dumps({"answer": "ok", "cited_chunk_ids": ["nsx-9.0-maximums-0007"]})
    )
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=corpus),
        patch(_BUILD_LLM_CLIENT, return_value=synth),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/ask_docs",
            json={"query": "q", "collection": "vmware"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Central audit contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_row_carries_ask_op_id_and_hash_not_raw_query(client: TestClient) -> None:
    """One audit row: ``op_id=meho.docs.ask``, ``op_class=read``, hash not raw query."""
    await _seed_global_collection()
    key = _make_rsa_keypair("kid-A")
    raw_query = "how do I expand a vsan disk group"
    token = _token(key, sub="op-audit")
    corpus = _fake_corpus(_SAMPLE_CHUNK)
    synth = _StubLlmClient(
        json.dumps({"answer": "ok", "cited_chunk_ids": ["nsx-9.0-maximums-0007"]})
    )
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=corpus),
        patch(_BUILD_LLM_CLIENT, return_value=synth),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/ask_docs",
            json={"query": raw_query, "collection": "vmware", "product": "nsx", "version": "9.0"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == "/api/v1/ask_docs"))
        rows = result.scalars().all()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["op_id"] == "meho.docs.ask"
    assert payload["op_class"] == "read"
    assert payload["query_hash"] == _compute_query_hash(raw_query)
    assert payload["collection"] == "vmware"
    assert payload["product"] == "nsx"
    assert raw_query not in json.dumps(payload)


@pytest.mark.asyncio
async def test_audit_row_written_on_pipeline_503(client: TestClient) -> None:
    """A pipeline 503 still produces an audit row with the query identity + scope.

    The contextvars bind *before* the pipeline runs, so the fail-closed path
    is attributable. ``hit_count`` is absent (bound only after a successful
    return).
    """
    await _seed_global_collection()
    key = _make_rsa_keypair("kid-A")
    raw_query = "esxi boot loop"
    token = _token(key, sub="op-503-audit")

    async def _down(_op: Any, _query: str, **_kwargs: Any) -> CorpusSearchResponse:
        raise CorpusUnavailable("corpus returned HTTP 502", status=502)

    synth = _StubLlmClient('{"answer": "unused", "cited_chunk_ids": []}')
    with (
        respx.mock as mock_router,
        patch(_CORPUS_SEAM, new=_down),
        patch(_BUILD_LLM_CLIENT, return_value=synth),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/ask_docs",
            json={"query": raw_query, "collection": "vmware"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 503

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == "/api/v1/ask_docs"))
        rows = result.scalars().all()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["op_id"] == "meho.docs.ask"
    assert payload["query_hash"] == _compute_query_hash(raw_query)
    assert payload["collection"] == "vmware"
    assert "hit_count" not in payload
    assert raw_query not in json.dumps(payload)
