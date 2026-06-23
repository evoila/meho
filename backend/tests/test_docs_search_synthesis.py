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
    _SYNTHESIS_RESPONSE_FORMAT,
    NO_GROUNDED_ANSWER,
    SYNTHESIS_CAUSE_CITATION_RESOLUTION,
    SYNTHESIS_CAUSE_PARSE,
    SYNTHESIS_CAUSE_TRUNCATED,
)
from meho_backplane.operations.ingest import LlmJsonResult
from meho_backplane.operations.ingest.pipeline import LlmClientUnavailable


class _StubLlmClient:
    """Deterministic ``StructuredJsonLlmClient`` returning a fixed response.

    Synthesis calls :meth:`generate_structured_json` (text + ``stop_reason``
    + an optional forced-JSON schema); the stub records the captured kwargs
    so a test can assert the evidence reached the prompt and the structured
    schema was requested, without a real model call.
    """

    def __init__(self, raw: str, *, stop_reason: str | None = "end_turn") -> None:
        self._raw = raw
        self._stop_reason = stop_reason
        self.called = False
        self.captured: dict[str, Any] = {}

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> str:
        # Synthesis only calls generate_structured_json; this satisfies the
        # grouping-compatible half of the protocol for completeness.
        return self._raw

    async def generate_structured_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
        response_format: Any | None = None,
    ) -> LlmJsonResult:
        self.called = True
        self.captured = {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "max_output_tokens": max_output_tokens,
            "response_format": response_format,
        }
        return LlmJsonResult(text=self._raw, stop_reason=self._stop_reason)


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


async def test_synthesis_requests_structured_output_schema() -> None:
    """The synthesis call forces JSON via ``response_format`` (#1999).

    The fix replaces prompt-discipline-only JSON with a Messages-API
    structured-output schema; the stub captures the ``response_format`` so
    this asserts the schema-forced path is actually taken.
    """
    stub = _StubLlmClient(json.dumps({"answer": "ok", "cited_chunk_ids": []}))
    await synthesize_docs_answer("q", DocsSearchResult(chunks=[_CHUNK_A]), llm_client=stub)
    response_format = stub.captured["response_format"]
    assert response_format is not None
    assert response_format["type"] == "json_schema"
    # The schema is derived from the synthesis output model, so it names the
    # two contract keys the parser later validates.
    assert set(response_format["schema"]["properties"]) == {"answer", "cited_chunk_ids"}


def _collect_schema_keys(node: object) -> set[str]:
    """Flatten every object key anywhere in a (nested) JSON-Schema structure."""
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(key)
            keys |= _collect_schema_keys(value)
    elif isinstance(node, list):
        for item in node:
            keys |= _collect_schema_keys(item)
    return keys


def test_synthesis_schema_strips_unsupported_keywords() -> None:
    """The emitted wire schema carries no ``minLength``/``maxLength`` (#1999 B1).

    ``_SynthesisOutput.answer`` is declared ``Field(min_length=1)``, so the raw
    ``model_json_schema()`` emits ``"minLength": 1``. ``minLength`` (and the
    other length/range/cardinality keywords) are NOT supported by Anthropic's
    structured-outputs schema compiler; the SDK strips them only on the
    ``messages.parse()`` / ``output_format`` helper paths, not on the plain
    ``output_config`` on ``messages.create()`` this module uses. Left in, the
    keyword reaches the API verbatim and risks a schema-compilation 400 on
    every real ``claude-sonnet-4-6`` synthesis call — re-introducing the exact
    failure #1999 fixes. This pins the contract: the emitted schema must be
    free of every unsupported keyword, at any nesting depth.
    """
    emitted_keys = _collect_schema_keys(_SYNTHESIS_RESPONSE_FORMAT["schema"])
    unsupported = {
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
    }
    assert emitted_keys.isdisjoint(unsupported), emitted_keys & unsupported
    # Sanity: the strip didn't gut the schema — the contract keys survive.
    assert {"answer", "cited_chunk_ids"} <= emitted_keys


async def test_min_length_constraint_still_enforced_at_validation() -> None:
    """Stripping ``minLength`` from the *wire* schema must not weaken validation.

    The fix removes ``minLength`` only from the schema sent to the model; the
    Pydantic ``min_length=1`` constraint on ``answer`` stays in force so an
    empty-answer model response is still rejected as a parse failure rather
    than returned as a (vacuously) valid grounded answer.
    """
    stub = _StubLlmClient(json.dumps({"answer": "", "cited_chunk_ids": []}))
    with pytest.raises(DocsSynthesisError, match="shape") as excinfo:
        await synthesize_docs_answer("q", DocsSearchResult(chunks=[_CHUNK_A]), llm_client=stub)
    assert excinfo.value.cause == SYNTHESIS_CAUSE_PARSE


async def test_fenced_json_output_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ```json```-fenced object is tolerated and parses (#1999 AC).

    ``claude-sonnet-4-6`` wraps a longer answer in a markdown fence; the
    bare ``json.loads`` shipped in v0.18.0 choked on it and 502'd. The
    tolerant parser strips the fence and returns the grounded answer.
    """
    fenced = (
        "```json\n"
        + json.dumps({"answer": "The maximum is 10,000.", "cited_chunk_ids": ["chunk-a"]})
        + "\n```"
    )
    stub = _StubLlmClient(fenced)
    result = await synthesize_docs_answer(
        "what is the maximum?",
        DocsSearchResult(chunks=[_CHUNK_A, _CHUNK_B]),
        llm_client=stub,
    )
    assert result.answer == "The maximum is 10,000."
    assert [c.chunk_id for c in result.citations] == ["chunk-a"]


async def test_preamble_then_object_parses() -> None:
    """A 'Here is the answer: {…}' prose preamble is tolerated (#1999 AC)."""
    body = json.dumps({"answer": "Both facts apply.", "cited_chunk_ids": ["chunk-a", "chunk-b"]})
    stub = _StubLlmClient(f"Here is the answer: {body}\nLet me know if you need more.")
    result = await synthesize_docs_answer(
        "tell me everything",
        DocsSearchResult(chunks=[_CHUNK_A, _CHUNK_B]),
        llm_client=stub,
    )
    assert result.answer == "Both facts apply."
    assert [c.chunk_id for c in result.citations] == ["chunk-a", "chunk-b"]


async def test_truncated_output_raises_truncated_cause() -> None:
    """A ``stop_reason == "max_tokens"`` cutoff is ``cause=truncated``, not parse (#1999 AC).

    A response cut off at the token ceiling is JSON-shaped but incomplete;
    splitting it from the generic parse fault lets an operator tell a
    truncation (raise the ceiling) from a framing fault.
    """
    # A valid object truncated mid-string — unparseable, stopped on max_tokens.
    truncated = '{"answer": "The maximum is 10,'
    stub = _StubLlmClient(truncated, stop_reason="max_tokens")
    with pytest.raises(DocsSynthesisError, match="truncated") as excinfo:
        await synthesize_docs_answer("x", DocsSearchResult(chunks=[_CHUNK_A]), llm_client=stub)
    assert excinfo.value.cause == SYNTHESIS_CAUSE_TRUNCATED


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
