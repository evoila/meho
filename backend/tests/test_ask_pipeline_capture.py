# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ``ask_docs`` capturing pipeline (#1939).

:func:`~meho_backplane.api.v1.ask_docs.run_ask_pipeline_capturing_retrieval`
is the structured, non-raising sibling of
:func:`~meho_backplane.api.v1.ask_docs.run_ask_pipeline`. It runs the same
expand -> retrieve -> synthesize legs but, instead of raising a classified
:class:`~meho_backplane.docs_search.AskDocsAnswerError` and discarding the
retrieval, returns an
:class:`~meho_backplane.api.v1.ask_docs.AskPipelineOutcome` carrying the
chunks retrieval returned alongside the classified error. The ``/ui/corpus``
Ask BFF uses it to **fail open** to those chunks on a **post-retrieval** leg
failure (the #1939 fix); the pre-retrieval legs carry no chunks.

These exercise the real pipeline primitives end to end with the three model /
transport seams stubbed (no network, no LLM), so the capture contract is
proven against the actual leg sequencing rather than a mock of the function.
The raising ``run_ask_pipeline`` wrapper is covered behaviourally through the
REST route in ``tests/test_ask_docs_route.py``; here a focused test confirms
it still raises (the REST 5xx contract) after the refactor.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from meho_backplane.api.v1.ask_docs import (
    AskPipelineOutcome,
    run_ask_pipeline,
    run_ask_pipeline_capturing_retrieval,
)
from meho_backplane.auth.corpus import CorpusChunk, CorpusSearchResponse, CorpusUnavailable
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.docs_collections import DocCollection
from meho_backplane.docs_search import build_docs_scope
from meho_backplane.docs_search.answer_errors import (
    CAUSE_CLIENT_UNAVAILABLE,
    CAUSE_CORPUS_UNAVAILABLE,
    CAUSE_EXPANSION_INVALID,
    CAUSE_SYNTHESIS_PARSE,
    LEG_CORPUS,
    LEG_EXPAND,
    LEG_MODEL,
    LEG_SYNTHESIS,
    AskDocsAnswerError,
)
from meho_backplane.operations.ingest import LlmJsonResult
from meho_backplane.operations.ingest.pipeline import LlmClientUnavailable

#: The corpus-http backend's transport seam — the retrieval side.
_CORPUS_SEAM = "meho_backplane.docs_search.backends.corpus_http.search_corpus"
#: Where synthesis resolves its default LLM client (the answer side).
_BUILD_LLM_CLIENT = "meho_backplane.docs_search.synthesis.build_anthropic_ingest_llm_client"
#: Where the corpus-aware expand step (#1916) resolves its default LLM client.
_BUILD_EXPAND_CLIENT = "meho_backplane.docs_search.expansion.build_anthropic_ingest_llm_client"


class _StubLlmClient:
    """Deterministic ``LlmClient`` returning a fixed raw JSON string."""

    def __init__(self, raw: str) -> None:
        self._raw = raw

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
        return LlmJsonResult(text=self._raw, stop_reason="end_turn")


@pytest.fixture(autouse=True)
def _default_expand_client() -> Iterator[None]:
    """Pin a working expand client so a test reaches retrieval (#1916).

    The default expand client fails closed with no ``ANTHROPIC_API_KEY``
    (never set in the test env); without this every retrieval-reaching test
    would fail on the expand leg. A test asserting expand behaviour
    re-patches ``_BUILD_EXPAND_CLIENT`` inside its own ``with`` block.
    """
    stub = _StubLlmClient(json.dumps({"queries": ["snapshot quiesce"]}))
    with patch(_BUILD_EXPAND_CLIENT, return_value=stub):
        yield


def _fake_corpus(*chunks: CorpusChunk) -> Any:
    """An async ``search_corpus`` stand-in returning *chunks*."""

    async def _search(operator: Any, query: str, **kwargs: Any) -> CorpusSearchResponse:
        return CorpusSearchResponse(chunks=list(chunks))

    return _search


def _down_corpus() -> Any:
    """An async ``search_corpus`` stand-in that fails closed (backend down)."""

    async def _search(operator: Any, query: str, **kwargs: Any) -> CorpusSearchResponse:
        raise CorpusUnavailable("corpus backend unreachable", status=503)

    return _search


_SAMPLE_CHUNK = CorpusChunk(
    chunk_id="vsphere-snapshots-0001",
    document_id="vsphere-snapshots",
    content="Snapshots quiesce the guest file system before capture.",
    source_url="https://docs.vmware.test/snapshots",
    score=0.91,
)


def _operator() -> Operator:
    return Operator(
        sub="op-42",
        raw_jwt="header.payload.signature",
        tenant_id=uuid4(),
        tenant_role=TenantRole.OPERATOR,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware"}),
    )


def _collection() -> DocCollection:
    """A ready, corpus-http-backed collection resolvable without a DB."""
    now = datetime.now(UTC)
    return DocCollection(
        id=uuid4(),
        tenant_id=None,
        collection_key="vmware",
        vendor="VMware by Broadcom",
        products=("vsphere",),
        description="VMware docs.",
        when_to_use="Vendor product questions.",
        backend={"type": "corpus-http"},
        status="ready",
        last_ingested_at=None,
        doc_count=None,
        readiness=None,
        extras={},
        created_at=now,
        updated_at=now,
    )


async def _run_capture() -> AskPipelineOutcome:
    return await run_ask_pipeline_capturing_retrieval(
        _operator(),
        "do snapshots quiesce the guest",
        scope=build_docs_scope("vmware"),
        collection=_collection(),
        limit=10,
    )


# ---------------------------------------------------------------------------
# Success: the outcome carries the answer + the retrieved chunks
# ---------------------------------------------------------------------------


async def test_success_outcome_carries_answer_and_chunks() -> None:
    """A clean run returns the grounded answer + the retrieved chunks, no error."""
    synth = _StubLlmClient(
        json.dumps(
            {
                "answer": "Yes, snapshots quiesce the guest.",
                "cited_chunk_ids": ["vsphere-snapshots-0001"],
            }
        )
    )
    with (
        patch(_CORPUS_SEAM, new=_fake_corpus(_SAMPLE_CHUNK)),
        patch(_BUILD_LLM_CLIENT, return_value=synth),
    ):
        outcome = await _run_capture()

    assert outcome.error is None
    assert outcome.answer is not None
    assert "quiesce" in outcome.answer.answer
    # The retrieved chunks ride the outcome (the same set synthesis grounded on).
    assert [c.chunk_id for c in outcome.retrieved_chunks] == ["vsphere-snapshots-0001"]


# ---------------------------------------------------------------------------
# Post-retrieval legs: the outcome carries the REAL retrieved chunks (#1939)
# ---------------------------------------------------------------------------


async def test_synthesis_malformed_captures_retrieved_chunks() -> None:
    """``synthesis_malformed`` is post-retrieval: the real chunks ride the outcome."""
    # Non-JSON synthesis output -> DocsSynthesisError(parse) -> synthesis_malformed.
    synth = _StubLlmClient("this is not json")
    with (
        patch(_CORPUS_SEAM, new=_fake_corpus(_SAMPLE_CHUNK)),
        patch(_BUILD_LLM_CLIENT, return_value=synth),
    ):
        outcome = await _run_capture()

    assert outcome.answer is None
    assert isinstance(outcome.error, AskDocsAnswerError)
    assert outcome.error.leg == LEG_SYNTHESIS
    assert outcome.error.cause == CAUSE_SYNTHESIS_PARSE
    # The #1939 fix: retrieval succeeded, so its chunks are preserved for the
    # UI to fail open to (rather than dropped).
    assert [c.chunk_id for c in outcome.retrieved_chunks] == ["vsphere-snapshots-0001"]
    # The original exception is chained for the traceback breadcrumb.
    assert outcome.error.__cause__ is not None


async def test_model_unavailable_captures_retrieved_chunks() -> None:
    """A synthesis-stage ``LlmClientUnavailable`` is post-retrieval: chunks ride along."""

    def _fail_closed() -> Any:
        raise LlmClientUnavailable("no ANTHROPIC_API_KEY configured")

    with (
        patch(_CORPUS_SEAM, new=_fake_corpus(_SAMPLE_CHUNK)),
        patch(_BUILD_LLM_CLIENT, side_effect=_fail_closed),
    ):
        outcome = await _run_capture()

    assert outcome.answer is None
    assert isinstance(outcome.error, AskDocsAnswerError)
    assert outcome.error.leg == LEG_MODEL
    assert outcome.error.cause == CAUSE_CLIENT_UNAVAILABLE
    # Retrieval succeeded before the missing model; chunks are preserved.
    assert [c.chunk_id for c in outcome.retrieved_chunks] == ["vsphere-snapshots-0001"]


# ---------------------------------------------------------------------------
# Pre-retrieval legs: the outcome carries NO chunks (banner-only on the UI)
# ---------------------------------------------------------------------------


async def test_corpus_unavailable_captures_no_chunks() -> None:
    """``corpus_unavailable`` is pre-retrieval: retrieval failed, so no chunks."""
    synth = _StubLlmClient("unused")
    with (
        patch(_CORPUS_SEAM, new=_down_corpus()),
        patch(_BUILD_LLM_CLIENT, return_value=synth),
    ):
        outcome = await _run_capture()

    assert outcome.answer is None
    assert isinstance(outcome.error, AskDocsAnswerError)
    assert outcome.error.leg == LEG_CORPUS
    assert outcome.error.cause == CAUSE_CORPUS_UNAVAILABLE
    assert outcome.retrieved_chunks == []


async def test_expand_failed_captures_no_chunks() -> None:
    """``expand_failed`` is pre-retrieval: it fails before retrieval runs."""
    # A malformed expansion (wrong shape) -> DocsQueryExpansionError.
    bad_expand = _StubLlmClient(json.dumps({"not_queries": []}))
    synth = _StubLlmClient("unused")
    with (
        patch(_BUILD_EXPAND_CLIENT, return_value=bad_expand),
        patch(_CORPUS_SEAM, new=_fake_corpus(_SAMPLE_CHUNK)),
        patch(_BUILD_LLM_CLIENT, return_value=synth),
    ):
        outcome = await _run_capture()

    assert outcome.answer is None
    assert isinstance(outcome.error, AskDocsAnswerError)
    assert outcome.error.leg == LEG_EXPAND
    assert outcome.error.cause == CAUSE_EXPANSION_INVALID
    assert outcome.retrieved_chunks == []


# ---------------------------------------------------------------------------
# The raising wrapper still raises (the REST 5xx contract is preserved)
# ---------------------------------------------------------------------------


async def test_run_ask_pipeline_still_raises_on_leg_failure() -> None:
    """``run_ask_pipeline`` re-raises the classified leg error (REST contract)."""
    synth = _StubLlmClient("this is not json")
    with (
        patch(_CORPUS_SEAM, new=_fake_corpus(_SAMPLE_CHUNK)),
        patch(_BUILD_LLM_CLIENT, return_value=synth),
        pytest.raises(AskDocsAnswerError) as excinfo,
    ):
        await run_ask_pipeline(
            _operator(),
            "do snapshots quiesce the guest",
            scope=build_docs_scope("vmware"),
            collection=_collection(),
            limit=10,
        )

    assert excinfo.value.leg == LEG_SYNTHESIS
    assert excinfo.value.cause == CAUSE_SYNTHESIS_PARSE
    # Traceback breadcrumb preserved via __cause__.
    assert excinfo.value.__cause__ is not None


async def test_run_ask_pipeline_returns_answer_on_success() -> None:
    """``run_ask_pipeline`` returns the grounded answer unchanged on success."""
    synth = _StubLlmClient(
        json.dumps(
            {
                "answer": "Yes, snapshots quiesce the guest.",
                "cited_chunk_ids": ["vsphere-snapshots-0001"],
            }
        )
    )
    with (
        patch(_CORPUS_SEAM, new=_fake_corpus(_SAMPLE_CHUNK)),
        patch(_BUILD_LLM_CLIENT, return_value=synth),
    ):
        answer = await run_ask_pipeline(
            _operator(),
            "do snapshots quiesce the guest",
            scope=build_docs_scope("vmware"),
            collection=_collection(),
            limit=10,
        )

    assert "quiesce" in answer.answer
    assert [c.chunk_id for c in answer.citations] == ["vsphere-snapshots-0001"]
