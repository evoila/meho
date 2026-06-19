# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit contract for grounded docs-answer synthesis (G4.5-T7, #1526).

Exercises :func:`meho_backplane.docs_search.synthesize_docs_answer`
directly — the grounding invariants that the MCP tool relies on, without
the JSON-RPC plumbing:

* zero retrieved chunks → :data:`NO_GROUNDED_ANSWER`, **no model call**;
* a grounded answer cites only chunks the retrieval returned;
* a fabricated / malformed citation set fails closed
  (:class:`DocsSynthesisError`);
* the default (unconfigured) synthesis client fails closed with
  :class:`LlmClientUnavailable`.

No network: the synthesis client is a deterministic stub or the
fail-closed default factory with no key.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from meho_backplane.docs_search import (
    DocsChunk,
    DocsSearchResult,
    DocsSynthesisError,
    synthesize_docs_answer,
)
from meho_backplane.docs_search.synthesis import (
    NO_GROUNDED_ANSWER,
    SYNTHESIS_CAUSE_CITATION_RESOLUTION,
    SYNTHESIS_CAUSE_PARSE,
)
from meho_backplane.operations.ingest.pipeline import LlmClientUnavailable


class _StubLlmClient:
    """Deterministic ``LlmClient`` returning a fixed raw synthesis string."""

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


_CHUNK_A = DocsChunk(
    chunk_id="chunk-a",
    document_id="doc-1",
    content="Fact A: the maximum is 10,000.",
    source_url="https://docs.example.com/a",
    score=0.9,
)
_CHUNK_B = DocsChunk(
    chunk_id="chunk-b",
    document_id="doc-1",
    content="Fact B: the default is 1,000.",
    source_url="https://docs.example.com/b",
    score=0.8,
)


async def test_zero_chunks_returns_no_grounded_answer_without_model_call() -> None:
    """An empty retrieval short-circuits to a deterministic non-answer."""
    stub = _StubLlmClient('{"answer": "unused", "cited_chunk_ids": []}')
    result = await synthesize_docs_answer("anything", DocsSearchResult(chunks=[]), llm_client=stub)
    assert result.answer == NO_GROUNDED_ANSWER
    assert result.citations == []
    assert stub.called is False


async def test_grounded_answer_cites_only_retrieved_chunks() -> None:
    """The returned citations are the retrieved subset the model relied on."""
    stub = _StubLlmClient(
        json.dumps({"answer": "The maximum is 10,000.", "cited_chunk_ids": ["chunk-a"]})
    )
    result = await synthesize_docs_answer(
        "what is the maximum?",
        DocsSearchResult(chunks=[_CHUNK_A, _CHUNK_B]),
        llm_client=stub,
    )
    assert result.answer == "The maximum is 10,000."
    assert [c.chunk_id for c in result.citations] == ["chunk-a"]
    # The retrieved evidence reached the synthesis prompt.
    assert "chunk-a" in stub.captured["user_prompt"]
    assert "10,000" in stub.captured["user_prompt"]


async def test_citations_follow_retrieval_order_and_dedupe() -> None:
    """Citations preserve retrieval ranking and a doubly-cited chunk appears once."""
    stub = _StubLlmClient(
        json.dumps(
            {
                "answer": "Both facts apply.",
                # Out of order + duplicate; result must be ranked + unique.
                "cited_chunk_ids": ["chunk-b", "chunk-a", "chunk-a"],
            }
        )
    )
    result = await synthesize_docs_answer(
        "tell me everything",
        DocsSearchResult(chunks=[_CHUNK_A, _CHUNK_B]),
        llm_client=stub,
    )
    assert [c.chunk_id for c in result.citations] == ["chunk-a", "chunk-b"]


async def test_fabricated_citation_raises_synthesis_error() -> None:
    """A cited id outside the retrieved set breaks the grounding contract.

    The sub-cause is ``citation_resolution`` (#1918): the output parsed, but
    a cited id did not resolve to a retrieved chunk — distinct from a
    structurally-unparseable output.
    """
    stub = _StubLlmClient(json.dumps({"answer": "Fabricated.", "cited_chunk_ids": ["chunk-z"]}))
    with pytest.raises(DocsSynthesisError, match="not in the retrieved set") as excinfo:
        await synthesize_docs_answer("x", DocsSearchResult(chunks=[_CHUNK_A]), llm_client=stub)
    assert excinfo.value.cause == SYNTHESIS_CAUSE_CITATION_RESOLUTION


async def test_non_json_output_raises_synthesis_error() -> None:
    """A model that returns prose instead of JSON fails closed (cause=parse)."""
    stub = _StubLlmClient("Sorry, I can't help with that.")
    with pytest.raises(DocsSynthesisError, match="non-JSON") as excinfo:
        await synthesize_docs_answer("x", DocsSearchResult(chunks=[_CHUNK_A]), llm_client=stub)
    assert excinfo.value.cause == SYNTHESIS_CAUSE_PARSE


async def test_shape_violating_output_raises_synthesis_error() -> None:
    """JSON missing the required ``answer`` key fails the strict shape (cause=parse)."""
    stub = _StubLlmClient(json.dumps({"cited_chunk_ids": ["chunk-a"]}))
    with pytest.raises(DocsSynthesisError, match="shape") as excinfo:
        await synthesize_docs_answer("x", DocsSearchResult(chunks=[_CHUNK_A]), llm_client=stub)
    assert excinfo.value.cause == SYNTHESIS_CAUSE_PARSE


async def test_default_client_fails_closed_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No injected client + no ``ANTHROPIC_API_KEY`` → fail-closed (the #1386 posture).

    The default synthesis client is built from settings; an empty key
    raises ``LlmClientUnavailable``, which the MCP dispatcher maps to
    ``-32603``. Never an ungrounded answer.
    """
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(LlmClientUnavailable):
            await synthesize_docs_answer("x", DocsSearchResult(chunks=[_CHUNK_A]))
    finally:
        get_settings.cache_clear()
