# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit contract for corpus-aware query expansion + multi-query retrieve (#1916).

Exercises the two new ``ask_docs`` answer-pipeline pieces directly, without
the JSON-RPC plumbing:

* :func:`meho_backplane.docs_search.expand_docs_query`
  - expands the question into ``<= MAX_QUERY_VARIANTS`` variants;
  - is **corpus-aware** — the collection's manifest fields (vendor,
    products, description, when_to_use) appear in the expansion prompt, so
    the model can expand an acronym / product term in domain terms;
  - always leads with the operator's original question (so expansion can
    only widen recall) and deduplicates;
  - fails closed: a malformed model output raises
    :class:`DocsQueryExpansionError`, and an unconfigured client raises
    :class:`LlmClientUnavailable` (the #1386 posture) — never a silently
    un-expanded answer.

* :func:`meho_backplane.docs_search.retrieve_multi_query`
  - runs the shared single-collection retrieval once per variant and
    RRF-merges the per-variant chunk lists, deduplicating a chunk that
    several variants surface.

No network: the expansion client is a deterministic stub or the
fail-closed default factory with no key; retrieval is monkeypatched to a
per-variant recorder.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.docs_collections import DocCollection
from meho_backplane.docs_search import (
    MAX_QUERY_VARIANTS,
    DocsChunk,
    DocsQueryExpansionError,
    DocsScope,
    DocsSearchResult,
    expand_docs_query,
    retrieve_multi_query,
)
from meho_backplane.operations.ingest.pipeline import LlmClientUnavailable


class _StubLlmClient:
    """Deterministic ``LlmClient`` returning a fixed raw expansion string.

    Captures the prompts so a test can assert the manifest fields were
    framed into the user prompt (the corpus-awareness evidence) without a
    real model call.
    """

    def __init__(self, raw: str) -> None:
        self._raw = raw
        self.called = False
        self.captured: dict[str, Any] = {}

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> str:
        self.called = True
        self.captured = {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "max_output_tokens": max_output_tokens,
        }
        return self._raw


def _collection(
    *,
    collection_key: str = "vmware",
    vendor: str = "VMware by Broadcom",
    products: tuple[str, ...] = ("vsphere", "nsx"),
    description: str | None = "VMware vendor docs.",
    when_to_use: str | None = "VMware product questions.",
) -> DocCollection:
    """Build a frozen :class:`DocCollection` read shape for unit tests."""
    now = datetime(2026, 6, 19, tzinfo=UTC)
    return DocCollection(
        id=UUID("00000000-0000-0000-0000-0000000000c0"),
        tenant_id=None,
        collection_key=collection_key,
        vendor=vendor,
        products=products,
        description=description,
        when_to_use=when_to_use,
        backend={"type": "corpus-http"},
        status="ready",
        last_ingested_at=None,
        doc_count=None,
        readiness=None,
        extras={},
        created_at=now,
        updated_at=now,
    )


def _operator() -> Operator:
    return Operator(
        sub="op-test",
        name="Test",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


# ---------------------------------------------------------------------------
# expand_docs_query — corpus-awareness (AC2)
# ---------------------------------------------------------------------------


async def test_expand_is_corpus_aware_manifest_fields_in_prompt() -> None:
    """AC2: the collection's manifest fields are framed into the expand prompt.

    The vendor name, the product list, and the prose fields all reach the
    user prompt, so the model can expand acronyms / product synonyms in the
    corpus's own domain terms (here: the ``nsx`` product token + the
    ``VMware by Broadcom`` vendor).
    """
    stub = _StubLlmClient(json.dumps({"queries": ["VMware NSX configuration maximums"]}))
    variants = await expand_docs_query("nsx maximums", _collection(), llm_client=stub)

    prompt = stub.captured["user_prompt"]
    # Manifest product token + vendor + prose appear → corpus-aware expansion.
    assert "nsx" in prompt
    assert "vsphere" in prompt
    assert "VMware by Broadcom" in prompt
    assert "VMware vendor docs." in prompt
    assert "VMware product questions." in prompt
    # The acronym/product term was expanded into a domain-term variant.
    assert "VMware NSX configuration maximums" in variants


async def test_expand_includes_original_query_first() -> None:
    """The operator's original question is always the first variant.

    Expansion can only *widen* recall — the literal query is never dropped,
    so even a perfect rewrite is searched alongside the original.
    """
    stub = _StubLlmClient(json.dumps({"queries": ["vCenter snapshot tree depth limit"]}))
    variants = await expand_docs_query("snapshot depth", _collection(), llm_client=stub)
    assert variants[0] == "snapshot depth"
    assert "vCenter snapshot tree depth limit" in variants


async def test_expand_caps_variant_count() -> None:
    """The variant list is bounded by ``MAX_QUERY_VARIANTS`` (original + rewrites)."""
    many = [f"rewrite number {i}" for i in range(20)]
    stub = _StubLlmClient(json.dumps({"queries": many}))
    variants = await expand_docs_query("q", _collection(), llm_client=stub)
    assert len(variants) == MAX_QUERY_VARIANTS
    assert variants[0] == "q"


async def test_expand_dedupes_rewrites_and_echoed_original() -> None:
    """Blank / duplicate rewrites and a re-cast of the original are dropped."""
    stub = _StubLlmClient(
        json.dumps(
            {
                "queries": [
                    "  Q  ",  # case/space re-cast of the original → dropped
                    "alt phrasing",
                    "",  # blank → dropped
                    "alt phrasing",  # duplicate → dropped
                ]
            }
        )
    )
    variants = await expand_docs_query("q", _collection(), llm_client=stub)
    assert variants == ["q", "alt phrasing"]


async def test_expand_empty_rewrites_yields_original_only() -> None:
    """A model that finds no useful rewrite degrades to the original question."""
    stub = _StubLlmClient(json.dumps({"queries": []}))
    variants = await expand_docs_query("only this", _collection(), llm_client=stub)
    assert variants == ["only this"]


async def test_expand_omits_blank_optional_manifest_fields() -> None:
    """Empty ``description`` / ``when_to_use`` are not framed as bare lines."""
    stub = _StubLlmClient(json.dumps({"queries": []}))
    collection = _collection(description=None, when_to_use=None)
    await expand_docs_query("q", collection, llm_client=stub)
    prompt = stub.captured["user_prompt"]
    assert "description:" not in prompt
    assert "when_to_use:" not in prompt
    # The non-optional identity fields are still present.
    assert "vendor: VMware by Broadcom" in prompt


# ---------------------------------------------------------------------------
# expand_docs_query — fail-closed posture (AC3)
# ---------------------------------------------------------------------------


async def test_expand_non_json_output_raises_expansion_error() -> None:
    """A model that returns prose instead of JSON fails closed."""
    stub = _StubLlmClient("Here are some queries you could try...")
    with pytest.raises(DocsQueryExpansionError, match="non-JSON"):
        await expand_docs_query("q", _collection(), llm_client=stub)


async def test_expand_fenced_json_output_parses() -> None:
    """A ```json```-fenced object is tolerated and parses (#1999).

    The expand leg shared the synthesis bare-``json.loads`` bug; it must
    get the same fence tolerance or it regresses the moment the model wraps
    its ``{"queries": [...]}`` object in a markdown fence.
    """
    fenced = "```json\n" + json.dumps({"queries": ["VMware NSX configuration maximums"]}) + "\n```"
    stub = _StubLlmClient(fenced)
    variants = await expand_docs_query("NSX maximums", _collection(), llm_client=stub)
    # Original first, then the fenced rewrite — proving the fence was stripped.
    assert variants[0] == "NSX maximums"
    assert "VMware NSX configuration maximums" in variants


async def test_expand_shape_violating_output_raises_expansion_error() -> None:
    """JSON missing the required ``queries`` key fails the strict shape."""
    stub = _StubLlmClient(json.dumps({"variants": ["x"]}))
    with pytest.raises(DocsQueryExpansionError, match="shape"):
        await expand_docs_query("q", _collection(), llm_client=stub)


async def test_expand_default_client_fails_closed_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No injected client + no ``ANTHROPIC_API_KEY`` → fail-closed (the #1386 posture).

    Expansion reuses the same fail-closed Anthropic client as synthesis; an
    empty key raises ``LlmClientUnavailable``, which the MCP dispatcher maps
    to ``-32603``. Never a silently un-expanded answer.
    """
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(LlmClientUnavailable):
            await expand_docs_query("q", _collection())
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# retrieve_multi_query — per-variant retrieval + RRF merge (AC1)
# ---------------------------------------------------------------------------


def _chunk(chunk_id: str, *, content: str = "c") -> DocsChunk:
    return DocsChunk(chunk_id=chunk_id, document_id="doc-1", content=content)


async def test_retrieve_multi_query_runs_per_variant_and_merges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each variant drives one retrieval; the per-variant lists are RRF-merged.

    Variant A and variant B each return a distinct top hit plus a shared
    second hit. RRF must surface every distinct chunk exactly once, and the
    chunk both variants returned is rank-boosted (it ranks above the
    single-variant tail).
    """
    per_variant: dict[str, list[DocsChunk]] = {
        "variant a": [_chunk("a1"), _chunk("shared")],
        "variant b": [_chunk("b1"), _chunk("shared")],
    }
    seen_queries: list[str] = []

    async def _fake_search(
        _operator: Operator,
        query: str,
        *,
        scope: DocsScope,
        collection: DocCollection,
        limit: int,
    ) -> DocsSearchResult:
        seen_queries.append(query)
        return DocsSearchResult(chunks=per_variant[query])

    # Patch the name ``retrieve_multi_query`` resolved ``search_docs`` to.
    monkeypatch.setattr("meho_backplane.docs_search.fanout.search_docs", _fake_search)

    scope = DocsScope(collection_key="vmware")
    result = await retrieve_multi_query(
        _operator(),
        ["variant a", "variant b"],
        scope=scope,
        collection=_collection(),
        limit=10,
    )

    # Both variants were retrieved (one backend round-trip each).
    assert sorted(seen_queries) == ["variant a", "variant b"]
    ids = [c.chunk_id for c in result.chunks]
    # Every distinct chunk appears exactly once (the shared chunk deduped).
    assert sorted(ids) == ["a1", "b1", "shared"]
    # The chunk both variants returned is rank-boosted to the top.
    assert ids[0] == "shared"


async def test_retrieve_multi_query_single_variant_is_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-variant list degenerates to a single retrieval (the pre-expand cost)."""
    calls = 0

    async def _fake_search(
        _operator: Operator,
        query: str,
        *,
        scope: DocsScope,
        collection: DocCollection,
        limit: int,
    ) -> DocsSearchResult:
        nonlocal calls
        calls += 1
        return DocsSearchResult(chunks=[_chunk("only")])

    monkeypatch.setattr("meho_backplane.docs_search.fanout.search_docs", _fake_search)

    result = await retrieve_multi_query(
        _operator(),
        ["just one"],
        scope=DocsScope(collection_key="vmware"),
        collection=_collection(),
        limit=10,
    )
    assert calls == 1
    assert [c.chunk_id for c in result.chunks] == ["only"]
