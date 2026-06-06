# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the collection-scoped ``ask_docs`` MCP tool (G4.6-T3, #1552).

``ask_docs`` is the synthesis fast-follow to ``search_docs``: it runs the
same retrieval, then composes a grounded, cited answer over the retrieved
chunks. These tests cover the collection-scoped contract on top of the
synthesis invariants:

* ``ask_docs`` carries ``required_capability="meho-docs"`` — **absent**
  from ``tools/list`` and **403** on ``tools/call`` for a tenant without
  the base capability (the same gate as ``search_docs``).
* Strict schema: required ``[query, collection]``, product/version optional.
* Missing ``collection`` → ``-32602``; a tenant lacking
  ``meho-docs:<collection>`` → 403-class ``-32602`` (per-collection
  entitlement, #1552).
* Returns ``{answer, citations[]}`` where every citation resolves to a
  chunk the underlying retrieval returned; an answer with **zero**
  retrieved chunks returns "no grounded answer", never a hallucinated one.
* Synthesis model unconfigured / unreachable → ``-32603`` (fail-closed,
  the MCP analogue of 503), never an ungrounded answer.

Two seams are mocked so no network is touched: the corpus transport
(``meho_backplane.docs_search.backends.corpus_http.search_corpus``, the
retrieval side, reached via the collection's resolved backend) and the
synthesis client (``build_anthropic_ingest_llm_client``, the LLM side).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.corpus import CorpusChunk, CorpusSearchResponse, CorpusUnavailable
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.docs_search.synthesis import NO_GROUNDED_ANSWER
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.schemas import INTERNAL_ERROR, INVALID_PARAMS
from meho_backplane.operations.ingest.pipeline import LlmClientUnavailable
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
    seed_doc_collection,
    seeded_operator_tenant,  # noqa: F401 — pytest-discovered fixture
)

_DOCS_CAPABILITY = "meho-docs"
#: Per-collection entitlement key for the seeded ``vmware`` collection.
_VMWARE_CAP = "meho-docs:vmware"
#: A provisioned + vmware-entitled capability set.
_ENTITLED = frozenset({_DOCS_CAPABILITY, _VMWARE_CAP})

#: Where the synthesis helper resolves its default LLM client. Patching
#: here lets a test pin a deterministic stub or assert the fail-closed
#: factory propagates.
_BUILD_LLM_CLIENT = "meho_backplane.docs_search.synthesis.build_anthropic_ingest_llm_client"

#: The corpus-http backend's transport seam — the function the
#: ``corpus-http`` adapter actually calls.
_CORPUS_SEAM = "meho_backplane.docs_search.backends.corpus_http.search_corpus"


def _seed_collection_sync(**kwargs: Any) -> None:
    """Run :func:`seed_doc_collection` to completion from a sync test."""
    asyncio.run(seed_doc_collection(**kwargs))


def _operator(
    *,
    role: TenantRole = TenantRole.OPERATOR,
    capabilities: frozenset[str] = frozenset(),
) -> Operator:
    """Build a fixture operator with the requested role + capability set."""
    return Operator(
        sub="op-test",
        name="Test",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=role,
        capabilities=capabilities,
    )


@pytest.fixture
def docs_client(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[TestClient, Operator]]:
    """``TestClient`` whose operator's capability set is parametrised.

    The production ``ask_docs`` tool is registered by the lifespan's eager
    import (it lives in ``mcp/tools/docs.py`` alongside ``search_docs``);
    this fixture only pins the operator. Provision the ``meho-docs``
    capability with
    ``@pytest.mark.parametrize("docs_client", [frozenset({"meho-docs"})], indirect=True)``;
    default is the empty set (unprovisioned).
    """
    capabilities: frozenset[str] = getattr(request, "param", frozenset())
    op = _operator(role=TenantRole.OPERATOR, capabilities=capabilities)

    async def _fake_verify() -> Operator:
        return op

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client, op
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


def _fake_corpus(*chunks: CorpusChunk) -> Any:
    """An async stand-in for ``search_corpus`` capturing its call args."""
    captured: dict[str, Any] = {}

    async def _search(operator: Operator, query: str, **kwargs: Any) -> CorpusSearchResponse:
        captured["operator"] = operator
        captured["query"] = query
        captured.update(kwargs)
        return CorpusSearchResponse(chunks=list(chunks))

    _search.captured = captured  # type: ignore[attr-defined]
    return _search


class _StubLlmClient:
    """Deterministic ``LlmClient`` returning a fixed raw synthesis string.

    Captures the prompts so a test can assert the retrieved chunks were
    framed into the user prompt (the grounding evidence) without a real
    model call.
    """

    def __init__(self, raw: str) -> None:
        self._raw = raw
        self.captured: dict[str, Any] = {}

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> str:
        self.captured["system_prompt"] = system_prompt
        self.captured["user_prompt"] = user_prompt
        self.captured["max_output_tokens"] = max_output_tokens
        return self._raw


_SAMPLE_CHUNK = CorpusChunk(
    chunk_id="nsx-9.0-maximums-0007",
    document_id="nsx-9.0-config-maximums",
    content="NSX 9.0 supports up to 10,000 logical switches per manager.",
    source_url="https://docs.example.com/nsx/9.0/maximums",
    score=0.91,
)

_SECOND_CHUNK = CorpusChunk(
    chunk_id="nsx-9.0-maximums-0008",
    document_id="nsx-9.0-config-maximums",
    content="NSX 9.0 supports up to 1,000 transport nodes per manager.",
    source_url="https://docs.example.com/nsx/9.0/maximums",
    score=0.82,
)


def _ask_call(arguments: dict[str, Any], *, call_id: int = 1) -> dict[str, Any]:
    """Build a ``tools/call ask_docs`` JSON-RPC envelope."""
    return {
        "jsonrpc": "2.0",
        "id": call_id,
        "method": "tools/call",
        "params": {"name": "ask_docs", "arguments": arguments},
    }


# ---------------------------------------------------------------------------
# tools/list shape + capability gate (AC1)
# ---------------------------------------------------------------------------


def test_ask_docs_absent_from_tools_list_for_unprovisioned_tenant(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """AC1: an operator without ``meho-docs`` never sees ``ask_docs``."""
    client, _op = docs_client
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 200
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert "ask_docs" not in names


@pytest.mark.parametrize("docs_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_ask_docs_present_with_strict_collection_schema(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """The tool appears once provisioned, with a strict collection-scoped schema."""
    client, _op = docs_client
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 200
    tools_by_name = {t["name"]: t for t in response.json()["result"]["tools"]}

    assert "ask_docs" in tools_by_name
    tool = tools_by_name["ask_docs"]
    schema = tool["inputSchema"]
    assert schema["type"] == "object"
    assert schema["required"] == ["query", "collection"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"query", "collection", "product", "version", "limit"}
    assert schema["properties"]["limit"]["default"] == 10
    assert schema["properties"]["limit"]["maximum"] == 50
    # Wire shape never leaks the server-side gating fields.
    assert "required_role" not in tool
    assert "op_class" not in tool
    assert "required_capability" not in tool


@pytest.mark.parametrize("docs_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_ask_docs_description_names_collection_and_sibling(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """The description routes between the answer tool and the chunks tool."""
    client, _op = docs_client
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools_by_name = {t["name"]: t for t in response.json()["result"]["tools"]}
    desc = tools_by_name["ask_docs"]["description"]

    # Routes to the chunks-only sibling and names the other corpora.
    assert "search_docs" in desc
    assert "search_knowledge" in desc
    assert "search_memory" in desc
    # The mandatory collection scope and the no-guess posture are called out.
    assert "collection" in desc.lower()
    assert "list_doc_collections" in desc
    assert "no grounded answer" in desc.lower() or "no claim without a citation" in desc.lower()


def test_ask_docs_hidden_from_provisioned_read_only_operator() -> None:
    """The capability does not relax the role gate: read_only never sees it."""
    op = _operator(role=TenantRole.READ_ONLY, capabilities=_ENTITLED)

    async def _fake_verify() -> Operator:
        return op

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert "ask_docs" not in names


# ---------------------------------------------------------------------------
# tools/call — capability gate (AC1)
# ---------------------------------------------------------------------------


def test_tools_call_ask_docs_403_when_unprovisioned(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """AC1: naming the tool directly still 403s when the capability is absent.

    The dispatcher's capability re-check fires before the handler, so an
    unprovisioned tenant never reaches retrieval or synthesis.
    """
    client, _op = docs_client
    corpus = _fake_corpus(_SAMPLE_CHUNK)
    stub = _StubLlmClient('{"answer": "x", "cited_chunk_ids": []}')
    with (
        patch(_CORPUS_SEAM, new=corpus),
        patch(_BUILD_LLM_CLIENT, return_value=stub),
    ):
        response = post_mcp(
            client,
            _ask_call({"query": "nsx maximums", "collection": "vmware"}, call_id=2),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()
    assert "capability" in body["error"]["message"].lower()
    # Neither retrieval nor synthesis was reached.
    assert "query" not in corpus.captured  # type: ignore[attr-defined]
    assert stub.captured == {}


# ---------------------------------------------------------------------------
# tools/call — collection-scope rejection arms (#1552)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("docs_client", [_ENTITLED], indirect=True)
def test_tools_call_ask_docs_missing_collection_rejected_by_schema(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """A missing required ``collection`` fails inputSchema validation → -32602."""
    client, _op = docs_client
    response = post_mcp(client, _ask_call({"query": "x", "product": "nsx"}, call_id=3))
    assert response.status_code == 200
    assert response.json()["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("docs_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_tools_call_ask_docs_403_when_not_entitled_to_collection(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """A tenant with base ``meho-docs`` but not ``meho-docs:vmware`` → 403-class.

    The per-collection entitlement gate rejects the question before either
    retrieval or synthesis runs.
    """
    client, _op = docs_client
    _seed_collection_sync()
    corpus = _fake_corpus(_SAMPLE_CHUNK)
    stub = _StubLlmClient('{"answer": "x", "cited_chunk_ids": []}')
    with (
        patch(_CORPUS_SEAM, new=corpus),
        patch(_BUILD_LLM_CLIENT, return_value=stub),
    ):
        response = post_mcp(
            client,
            _ask_call({"query": "x", "collection": "vmware"}, call_id=4),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "entitled" in body["error"]["message"].lower()
    assert "query" not in corpus.captured  # type: ignore[attr-defined]
    assert stub.captured == {}


# ---------------------------------------------------------------------------
# tools/call — grounded happy path (AC3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("docs_client", [_ENTITLED], indirect=True)
def test_tools_call_ask_docs_returns_grounded_cited_answer(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """An entitled operator gets ``{answer, citations[]}`` over retrieved chunks.

    Pins the full round-trip: retrieval routes to the ``vmware``
    collection's backend with the optional refinements as
    ``metadata_filters``, the synthesis model composes an answer citing one
    of the two retrieved chunks, and every returned citation resolves to a
    retrieved chunk. The retrieved evidence reached the synthesis prompt.
    """
    client, op = docs_client
    _seed_collection_sync()
    corpus = _fake_corpus(_SAMPLE_CHUNK, _SECOND_CHUNK)
    stub = _StubLlmClient(
        json.dumps(
            {
                "answer": "NSX 9.0 supports up to 10,000 logical switches per manager.",
                "cited_chunk_ids": ["nsx-9.0-maximums-0007"],
            }
        )
    )
    with (
        patch(_CORPUS_SEAM, new=corpus),
        patch(_BUILD_LLM_CLIENT, return_value=stub),
    ):
        response = post_mcp(
            client,
            _ask_call(
                {
                    "query": "How many logical switches does NSX 9.0 support?",
                    "collection": "vmware",
                    "product": "nsx",
                    "version": "9.0",
                    "limit": 5,
                },
                call_id=5,
            ),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])

    assert payload["answer"].startswith("NSX 9.0 supports")
    # Only the cited chunk is returned, and it resolves to a retrieved chunk.
    assert len(payload["citations"]) == 1
    citation = payload["citations"][0]
    assert citation["chunk_id"] == "nsx-9.0-maximums-0007"
    assert citation["source_url"].endswith("/maximums")

    # The optional refinements reached the backend and the operator identity
    # was forwarded.
    assert corpus.captured["metadata_filters"] == {"product": "nsx", "version": "9.0"}  # type: ignore[attr-defined]
    assert corpus.captured["limit"] == 5  # type: ignore[attr-defined]
    assert corpus.captured["operator"].tenant_id == op.tenant_id  # type: ignore[attr-defined]
    # The retrieved evidence was framed into the synthesis prompt.
    assert "nsx-9.0-maximums-0007" in stub.captured["user_prompt"]
    assert "10,000 logical switches" in stub.captured["user_prompt"]


# ---------------------------------------------------------------------------
# tools/call — zero chunks → no grounded answer, no model call (AC3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("docs_client", [_ENTITLED], indirect=True)
def test_tools_call_ask_docs_zero_chunks_returns_no_grounded_answer(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """An empty retrieval returns "no grounded answer" without calling the model.

    The empty-evidence path is the one answer path that must NOT invoke the
    synthesis model — there is nothing to ground on, so a model call could
    only hallucinate. Citations are empty.
    """
    client, _op = docs_client
    _seed_collection_sync()
    corpus = _fake_corpus()  # zero chunks
    stub = _StubLlmClient('{"answer": "should never be used", "cited_chunk_ids": []}')
    with (
        patch(_CORPUS_SEAM, new=corpus),
        patch(_BUILD_LLM_CLIENT, return_value=stub),
    ):
        response = post_mcp(
            client,
            _ask_call(
                {"query": "obscure unanswerable thing", "collection": "vmware"},
                call_id=6,
            ),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])

    assert payload["answer"] == NO_GROUNDED_ANSWER
    assert payload["citations"] == []
    # The synthesis model was never called — no hallucinated answer.
    assert stub.captured == {}


# ---------------------------------------------------------------------------
# tools/call — fail-closed when synthesis model unconfigured (AC4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("docs_client", [_ENTITLED], indirect=True)
def test_tools_call_ask_docs_unconfigured_model_is_internal_error(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """AC4: an unconfigured synthesis model fails closed → ``-32603``.

    ``build_anthropic_ingest_llm_client`` raising ``LlmClientUnavailable``
    (no ``ANTHROPIC_API_KEY``) is the #1386 fail-closed precedent. It is
    not caught in the handler, so it bubbles to the dispatcher's generic
    catch → ``-32603`` (the MCP analogue of the route's 503). We never
    return an ungrounded answer when the model is missing.
    """
    client, _op = docs_client
    _seed_collection_sync()
    corpus = _fake_corpus(_SAMPLE_CHUNK)

    def _fail_closed() -> Any:
        raise LlmClientUnavailable("no ANTHROPIC_API_KEY configured")

    with (
        patch(_CORPUS_SEAM, new=corpus),
        patch(_BUILD_LLM_CLIENT, side_effect=_fail_closed),
    ):
        response = post_mcp(
            client,
            _ask_call({"query": "x", "collection": "vmware"}, call_id=7),
        )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == INTERNAL_ERROR


# ---------------------------------------------------------------------------
# tools/call — fabricated citation breaks the grounding contract (AC3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("docs_client", [_ENTITLED], indirect=True)
def test_tools_call_ask_docs_fabricated_citation_is_internal_error(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """A model citing a chunk_id outside the retrieved set fails closed.

    An unverifiable citation breaks the no-claim-without-a-REAL-citation
    contract, so the synthesis raises ``DocsSynthesisError`` rather than
    returning an answer with a fabricated reference. Surfaces as ``-32603``.
    """
    client, _op = docs_client
    _seed_collection_sync()
    corpus = _fake_corpus(_SAMPLE_CHUNK)
    stub = _StubLlmClient(
        json.dumps({"answer": "Fabricated.", "cited_chunk_ids": ["does-not-exist-9999"]})
    )
    with (
        patch(_CORPUS_SEAM, new=corpus),
        patch(_BUILD_LLM_CLIENT, return_value=stub),
    ):
        response = post_mcp(
            client,
            _ask_call({"query": "x", "collection": "vmware"}, call_id=8),
        )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == INTERNAL_ERROR


# ---------------------------------------------------------------------------
# tools/call — corpus unavailable surfaces as INTERNAL_ERROR (not -32602)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("docs_client", [_ENTITLED], indirect=True)
def test_tools_call_ask_docs_backend_unavailable_is_internal_error(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """A down backend is a server fault → ``-32603``, mirroring ``search_docs``."""
    client, _op = docs_client
    _seed_collection_sync()

    async def _down(_op: Operator, _query: str, **_kwargs: Any) -> CorpusSearchResponse:
        raise CorpusUnavailable("corpus_url is not configured")

    stub = _StubLlmClient('{"answer": "x", "cited_chunk_ids": []}')
    with (
        patch(_CORPUS_SEAM, new=_down),
        patch(_BUILD_LLM_CLIENT, return_value=stub),
    ):
        response = post_mcp(
            client,
            _ask_call({"query": "x", "collection": "vmware"}, call_id=9),
        )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == INTERNAL_ERROR
    # The corpus failed before synthesis was reached.
    assert stub.captured == {}


# ---------------------------------------------------------------------------
# tools/call — additionalProperties:false rejects smuggled keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("docs_client", [_ENTITLED], indirect=True)
def test_tools_call_ask_docs_rejects_extra_arguments(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """``additionalProperties: false`` rejects unknown top-level keys."""
    client, _op = docs_client
    response = post_mcp(
        client,
        _ask_call(
            {"query": "x", "collection": "vmware", "tenant_id": "smuggled"},
            call_id=10,
        ),
    )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# Uniform audit op_id across faces (G4.5-T8 #1549)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("docs_client", [_ENTITLED], indirect=True)
async def test_tools_call_ask_docs_audit_row_carries_op_id_and_collection(
    docs_client: tuple[TestClient, Operator],
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    """The MCP ``ask_docs`` audit row's ``op_id`` is ``meho.docs.ask`` + carries ``collection``.

    Mirrors ``search_docs``: the handler binds the ``audit_op_id`` and
    ``audit_collection`` contextvars so the persisted row is filterable by
    the canonical, uniform op_id and the collection across REST / CLI / MCP.
    ``op_class`` stays ``read`` (ask is a read-class compose over retrieved
    chunks) and the raw query is recorded only as ``params_hash``.
    """
    client, _op = docs_client
    await seed_doc_collection()
    corpus = _fake_corpus(_SAMPLE_CHUNK)
    stub = _StubLlmClient(
        json.dumps(
            {
                "answer": "NSX 9.0 supports up to 10,000 logical switches per manager.",
                "cited_chunk_ids": ["nsx-9.0-maximums-0007"],
            }
        )
    )
    with (
        patch(_CORPUS_SEAM, new=corpus),
        patch(_BUILD_LLM_CLIENT, return_value=stub),
    ):
        response = post_mcp(
            client,
            _ask_call(
                {"query": "logical switch maximums", "collection": "vmware"},
                call_id=11,
            ),
        )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        mcp_rows = [row for row in result.scalars().all() if row.method == "MCP"]
    assert len(mcp_rows) == 1
    payload = mcp_rows[0].payload
    assert payload["op_id"] == "meho.docs.ask"
    assert payload["op_class"] == "read"
    assert payload["collection"] == "vmware"
    assert "logical switch maximums" not in json.dumps(payload)
    assert payload["params_hash"]
