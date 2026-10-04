# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The one ``ask_docs`` answer seam and its upstream-answer mapping (#3911).

Exercises :func:`~meho_backplane.docs_search.answer.answer_docs_question`
without any HTTP face (the faces are covered in
:mod:`tests.test_docs_upstream_answer_faces`):

* **Fixture** -- a recorded ``POST /ask?include=hits`` body (synthetic text,
  the backend's real field shape) parses and maps end to end.
* **Mapping** -- citations matched to hits by ``chunk_id`` when the hits come
  back in citation order; duplicate citations collapse; the title fallback
  order; ``[N]`` -> ``[k]`` renumbering with repeated and unknown markers; a
  citation outside the hits is ``citation_resolution``; zero hits is
  ``NO_GROUNDED_ANSWER``.
* **Routing** -- ``backend.ref["answer"] == "upstream"`` takes the upstream
  path (one answer call, no search); no opt-in, any other value, or a backend
  without ``answer()`` takes the local pipeline.
* **Endpoint resolution** -- the default ``…/search`` -> ``…/ask`` derivation
  (including a prefixed path and the legacy global URL) and an explicit
  ``answer_endpoint``.

The answer transport is replaced at the ``corpus-http`` adapter's seam
(``...backends.corpus_http.ask_corpus``), so nothing touches the network.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
import structlog.testing

from meho_backplane.auth.corpus import (
    CorpusChunk,
    CorpusSearchResponse,
    UpstreamAnswer,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.docs_collections import DocCollection
from meho_backplane.docs_search import build_docs_scope
from meho_backplane.docs_search.answer import (
    ANSWER_SOURCE_LOCAL,
    ANSWER_SOURCE_UPSTREAM,
    AskPipelineOutcome,
    answer_docs_question,
)
from meho_backplane.docs_search.answer_errors import (
    CAUSE_SYNTHESIS_CITATION_RESOLUTION,
    LEG_SYNTHESIS,
)
from meho_backplane.docs_search.backends import CorpusHttpBackend, SearchBackend
from meho_backplane.docs_search.citation_links import derive_chunk_title
from meho_backplane.docs_search.synthesis import NO_GROUNDED_ANSWER
from meho_backplane.operations.ingest import LlmJsonResult
from meho_backplane.settings import get_settings

#: The ``corpus-http`` adapter's answer transport seam.
_ASK_SEAM = "meho_backplane.docs_search.backends.corpus_http.ask_corpus"
#: The ``corpus-http`` adapter's search transport seam.
_SEARCH_SEAM = "meho_backplane.docs_search.backends.corpus_http.search_corpus"
_BUILD_EXPAND_CLIENT = "meho_backplane.docs_search.expansion.build_anthropic_ingest_llm_client"
_BUILD_SYNTH_CLIENT = "meho_backplane.docs_search.synthesis.build_anthropic_ingest_llm_client"

_FIXTURE = Path(__file__).parent / "fixtures" / "docs" / "upstream_ask_include_hits.json"
_SEARCH_URL = "https://corpus.test/search"
_UPSTREAM_REF: dict[str, Any] = {"endpoint": _SEARCH_URL, "answer": "upstream"}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env :class:`Settings` reads, with a configured global corpus URL."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("CORPUS_URL", "https://legacy-corpus.test/v1/search")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fixture_body() -> dict[str, Any]:
    body: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return body


def _operator() -> Operator:
    return Operator(
        sub="op-42",
        raw_jwt="header.payload.signature",
        tenant_id=uuid4(),
        tenant_role=TenantRole.OPERATOR,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware"}),
    )


def _collection(backend: Mapping[str, Any]) -> DocCollection:
    now = datetime.now(UTC)
    return DocCollection(
        id=uuid4(),
        tenant_id=None,
        collection_key="vmware",
        vendor="VMware by Broadcom",
        products=("vsphere",),
        description="VMware docs.",
        when_to_use="Vendor product questions.",
        backend=dict(backend),
        status="ready",
        last_ingested_at=None,
        doc_count=None,
        readiness=None,
        extras={},
        created_at=now,
        updated_at=now,
    )


class _FakeAsk:
    """A recording ``ask_corpus`` stand-in returning a fixed upstream body."""

    def __init__(self, body: Mapping[str, Any]) -> None:
        self._body = body
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, operator: Operator, query: str, **kwargs: Any) -> UpstreamAnswer:
        self.calls.append({"query": query, **kwargs})
        return UpstreamAnswer.model_validate(self._body)


class _FakeSearch:
    """A recording ``search_corpus`` stand-in returning fixed chunks."""

    def __init__(self, *chunks: CorpusChunk) -> None:
        self._chunks = list(chunks)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, operator: Operator, query: str, **kwargs: Any) -> CorpusSearchResponse:
        self.calls.append({"query": query, **kwargs})
        return CorpusSearchResponse(chunks=self._chunks)


class _StubLlm:
    """Deterministic LLM client for the local expand / synthesis legs."""

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


def _hit(chunk_id: str, *, text: str = "Synthetic chunk text.", **extra: Any) -> dict[str, Any]:
    """A minimal upstream hit (the backend's search-hit shape)."""
    return {
        "chunk_id": chunk_id,
        "document_id": "",
        "chunk_index": 0,
        "text": text,
        "source_uri": f"gs://example-corpus-bucket/docs/{chunk_id}.html",
        "score": 0.3,
        **extra,
    }


def _citation(chunk_index: int, chunk_id: str) -> dict[str, Any]:
    return {"chunk_index": chunk_index, "chunk_id": chunk_id, "quote": "q"}


def _body(answer: str, citations: list[dict[str, Any]], hits: list[dict[str, Any]]) -> dict:
    return {"query": "q", "answer": answer, "citations": citations, "hits": hits, "timing": {}}


async def _ask(
    body: Mapping[str, Any], *, backend: Mapping[str, Any] | None = None
) -> tuple[AskPipelineOutcome, _FakeAsk, _FakeSearch]:
    """Run the seam against *body* on an opted-in collection; return the fakes too."""
    fake_ask = _FakeAsk(body)
    fake_search = _FakeSearch()
    with patch(_ASK_SEAM, new=fake_ask), patch(_SEARCH_SEAM, new=fake_search):
        outcome = await answer_docs_question(
            _operator(),
            "What is new in Example Platform 2.1.1?",
            scope=build_docs_scope("vmware"),
            collection=_collection(backend or {"type": "corpus-http", "ref": _UPSTREAM_REF}),
            limit=10,
        )
    return outcome, fake_ask, fake_search


# ---------------------------------------------------------------------------
# Fixture: a recorded /ask?include=hits body parses and maps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recorded_upstream_body_parses_and_maps() -> None:
    """The recorded body maps to the backplane's answer + citation shape.

    Its hits come back in citation order with in-document ``chunk_index``
    values that differ from the answer's ``[N]`` (the trap a positional match
    would fall into); the third citation repeats the first chunk; ``[5]``
    names no citation.
    """
    outcome, fake_ask, fake_search = await _ask(_fixture_body())

    assert outcome.error is None
    assert outcome.answer_source == ANSWER_SOURCE_UPSTREAM
    assert outcome.answer is not None
    assert outcome.answer.answer == (
        "Example Platform 2.1.1 adds pooled widget storage [1]. "
        "Upgrading from 2.1.0 needs no downtime [2]. "
        "The pooling limits are listed in the release notes [1]."
    )
    citations = outcome.answer.citations
    assert [c.chunk_id for c in citations] == ["chunk-rn-2-1-1-0004", "chunk-upgrade-0011"]
    # Content comes from the hits, not the quotes.
    assert citations[0].content.startswith("Example Platform 2.1.1 adds pooled widget storage.")
    # Titles from page identity: last heading, then the breadcrumb tail.
    assert citations[0].title == "What's New"
    assert citations[1].title == "Rolling upgrades"
    # Source refs project exactly as search_docs does: never a raw gs:// path.
    assert citations[0].source_url == "meho://docs/vmware/chunk-rn-2-1-1-0004"
    retrieved = outcome.retrieved_chunks
    assert [c.chunk_id for c in retrieved] == [
        "chunk-rn-2-1-1-0004",
        "chunk-upgrade-0011",
        "chunk-kb-0001",
    ]
    assert retrieved[2].source_url == "https://knowledge.broadcom.com/external/article/100001"
    assert retrieved[2].title == "100001"
    assert all(not (c.source_url or "").startswith("gs://") for c in retrieved)
    # The backend's timing rides the outcome for the log.
    assert outcome.upstream_timing is not None
    assert outcome.upstream_timing.total_ms == 2790.6
    # One answer call, no search call.
    assert len(fake_ask.calls) == 1
    assert fake_search.calls == []


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_citations_match_hits_by_chunk_id_not_position() -> None:
    """Hits in citation order: each citation resolves to the hit with its chunk_id."""
    body = _body(
        "B is true [3]. A is true [1].",
        [_citation(3, "chunk-b"), _citation(1, "chunk-a")],
        # Citation order (b first), then an uncited hit; positions != chunk_index.
        [_hit("chunk-b", text="B text."), _hit("chunk-a", text="A text."), _hit("chunk-c")],
    )
    outcome, _ask_fake, _search_fake = await _ask(body)

    assert outcome.answer is not None
    assert [(c.chunk_id, c.content) for c in outcome.answer.citations] == [
        ("chunk-b", "B text."),
        ("chunk-a", "A text."),
    ]
    assert outcome.answer.answer == "B is true [1]. A is true [2]."


@pytest.mark.asyncio
async def test_duplicate_citations_collapse() -> None:
    """Several quotes from one chunk are one citation with one number."""
    body = _body(
        "X [0]. Y [0]. Z [2].",
        [_citation(0, "chunk-x"), _citation(0, "chunk-x"), _citation(2, "chunk-z")],
        [_hit("chunk-x"), _hit("chunk-z")],
    )
    outcome, _a, _s = await _ask(body)

    assert outcome.answer is not None
    assert [c.chunk_id for c in outcome.answer.citations] == ["chunk-x", "chunk-z"]
    assert outcome.answer.answer == "X [1]. Y [1]. Z [2]."


@pytest.mark.asyncio
async def test_markers_renumbered_repeated_and_unknown_dropped() -> None:
    """``[N]`` -> ``[k]``; a repeated N keeps its k; an unknown N is dropped."""
    body = _body(
        "First [4]. Again [4][9]. Second [7].\n\nUnknown alone [8].",
        [_citation(4, "chunk-four"), _citation(7, "chunk-seven")],
        [_hit("chunk-four"), _hit("chunk-seven")],
    )
    outcome, _a, _s = await _ask(body)

    assert outcome.answer is not None
    assert outcome.answer.answer == "First [1]. Again [1]. Second [2].\n\nUnknown alone."


@pytest.mark.asyncio
async def test_citation_outside_hits_is_citation_resolution() -> None:
    """A citation naming no returned hit breaks the grounding invariant."""
    body = _body(
        "Claim [0].",
        [_citation(0, "chunk-missing")],
        [_hit("chunk-present")],
    )
    outcome, _a, _s = await _ask(body)

    assert outcome.answer is None
    assert outcome.error is not None
    assert outcome.error.leg == LEG_SYNTHESIS
    assert outcome.error.cause == CAUSE_SYNTHESIS_CITATION_RESOLUTION
    assert outcome.answer_source == ANSWER_SOURCE_UPSTREAM
    # The hits the backend returned ride the outcome (post-retrieval failure).
    assert [c.chunk_id for c in outcome.retrieved_chunks] == ["chunk-present"]


@pytest.mark.asyncio
async def test_zero_hits_is_no_grounded_answer() -> None:
    """No hits: the deterministic no-grounded-answer, as on the local path."""
    body = _body("I could not find any relevant context to answer the question.", [], [])
    outcome, _a, _s = await _ask(body)

    assert outcome.error is None
    assert outcome.answer is not None
    assert outcome.answer.answer == NO_GROUNDED_ANSWER
    assert outcome.answer.citations == []
    assert outcome.retrieved_chunks == []


@pytest.mark.asyncio
async def test_hits_without_citations_answer_without_citations() -> None:
    """An answer the backend did not cite keeps its text, with no citations."""
    body = _body("The documents do not say [0].", [], [_hit("chunk-a")])
    outcome, _a, _s = await _ask(body)

    assert outcome.answer is not None
    assert outcome.answer.answer == "The documents do not say."
    assert outcome.answer.citations == []
    assert [c.chunk_id for c in outcome.retrieved_chunks] == ["chunk-a"]


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        (
            {
                "title": "Upstream Title",
                "heading_path": ["H1", "H2"],
                "breadcrumb": "A > B",
                "filename": "f.html",
            },
            "Upstream Title",
        ),
        ({"heading_path": ["Guide", "Planning", ""], "breadcrumb": "A > B"}, "Planning"),
        ({"heading_path": [], "breadcrumb": "Guide > Planning > Limits"}, "Limits"),
        ({"breadcrumb": "", "filename": "vsan-planning-guide.html"}, "vsan planning guide"),
        ({}, None),
    ],
)
def test_title_fallback_order(identity: dict[str, Any], expected: str | None) -> None:
    """Title: the backend's title, last heading, breadcrumb tail, humanised filename."""
    assert (
        derive_chunk_title(
            title=identity.get("title"),
            heading_path=identity.get("heading_path", []),
            breadcrumb=identity.get("breadcrumb", ""),
            filename=identity.get("filename", ""),
        )
        == expected
    )


@pytest.mark.asyncio
async def test_title_from_metadata_and_filename_reach_the_citation() -> None:
    """The backend's own title wins; a bare filename is humanised."""
    body = _body(
        "A [0]. B [1].",
        [_citation(0, "chunk-a"), _citation(1, "chunk-b")],
        [
            _hit("chunk-a", title="Backend Title", heading_path=["Ignored"]),
            _hit("chunk-b", filename="config-maximums.html"),
        ],
    )
    outcome, _a, _s = await _ask(body)

    assert outcome.answer is not None
    assert [c.title for c in outcome.answer.citations] == ["Backend Title", "config maximums"]


@pytest.mark.asyncio
async def test_completion_log_names_the_source_and_backend_timing() -> None:
    """The completion log carries answer_source, counts and the backend timing only."""
    body = _fixture_body()
    with structlog.testing.capture_logs() as logs:
        await _ask(body)
    completed = [e for e in logs if e["event"] == "docs_ask_completed"]
    assert len(completed) == 1
    event = completed[0]
    assert event["answer_source"] == ANSWER_SOURCE_UPSTREAM
    assert event["hit_count"] == 3
    assert event["citation_count"] == 2
    assert event["upstream_total_ms"] == 2790.6
    assert event["upstream_llm_ms"] == 1688.2
    # Never the question or the answer text.
    rendered = repr(logs)
    assert "Example Platform 2.1.1" not in rendered
    assert "pooled widget storage" not in rendered


# ---------------------------------------------------------------------------
# Routing: upstream only on opt-in
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opt_in_makes_one_answer_call_and_no_search() -> None:
    """``answer: "upstream"`` -> exactly one answer call, ``top_k`` = limit, no search."""
    outcome, fake_ask, fake_search = await _ask(_fixture_body())

    assert outcome.answer_source == ANSWER_SOURCE_UPSTREAM
    assert len(fake_ask.calls) == 1
    call = fake_ask.calls[0]
    assert call["limit"] == 10
    assert call["answer_url"] == "https://corpus.test/ask"
    # No product/version, no rerank flag: the transport signature has neither.
    assert set(call) == {"query", "limit", "answer_url", "audience"}
    assert fake_search.calls == []


@pytest.mark.parametrize(
    "backend",
    [
        {"type": "corpus-http"},
        {"type": "corpus-http", "ref": {"endpoint": _SEARCH_URL}},
        {"type": "corpus-http", "ref": {"endpoint": _SEARCH_URL, "answer": "local"}},
        {"type": "corpus-http", "ref": {"endpoint": _SEARCH_URL, "answer": "Upstream"}},
    ],
)
@pytest.mark.asyncio
async def test_no_opt_in_runs_the_local_pipeline(backend: dict[str, Any]) -> None:
    """Without the exact opt-in the answer comes from the local pipeline."""
    chunk = CorpusChunk(chunk_id="c-1", content="Local chunk.", source_url="https://d.test/x")
    fake_ask = _FakeAsk(_fixture_body())
    fake_search = _FakeSearch(chunk)
    synth = _StubLlm(json.dumps({"answer": "Local answer.", "cited_chunk_ids": ["c-1"]}))
    with (
        patch(_ASK_SEAM, new=fake_ask),
        patch(_SEARCH_SEAM, new=fake_search),
        patch(_BUILD_EXPAND_CLIENT, return_value=_StubLlm(json.dumps({"queries": []}))),
        patch(_BUILD_SYNTH_CLIENT, return_value=synth),
    ):
        outcome = await answer_docs_question(
            _operator(),
            "question",
            scope=build_docs_scope("vmware"),
            collection=_collection(backend),
            limit=10,
        )

    assert outcome.answer_source == ANSWER_SOURCE_LOCAL
    assert outcome.answer is not None
    assert outcome.answer.answer == "Local answer."
    assert fake_ask.calls == []
    assert len(fake_search.calls) >= 1


class _SearchOnlyBackend(SearchBackend):
    """A backend that implements only ``search`` (no answer endpoint)."""

    backend_type = "search-only-test"

    async def search(
        self,
        operator: Operator,
        query: str,
        *,
        backend_ref: Mapping[str, Any] | None = None,
        metadata_filters: dict[str, Any] | None = None,
        limit: int = 10,
    ) -> CorpusSearchResponse:
        return CorpusSearchResponse(chunks=[])


@pytest.mark.asyncio
async def test_backend_without_answer_keeps_the_local_path() -> None:
    """The base seam: ``supports_answer`` is False and ``answer`` raises."""
    backend = _SearchOnlyBackend()
    assert backend.supports_answer({"answer": "upstream"}) is False
    with pytest.raises(NotImplementedError):
        await backend.answer(_operator(), "q", backend_ref={"answer": "upstream"})


# ---------------------------------------------------------------------------
# corpus-http: opt-in + answer endpoint resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "supported"),
    [
        (None, False),
        ({}, False),
        ({"endpoint": _SEARCH_URL}, False),
        ({"answer": "upstream"}, True),
        ({"answer": "UPSTREAM"}, False),
        ({"answer": True}, False),
    ],
)
def test_corpus_http_supports_answer_only_on_exact_opt_in(
    ref: dict[str, Any] | None, supported: bool
) -> None:
    assert CorpusHttpBackend().supports_answer(ref) is supported


@pytest.mark.parametrize(
    ("ref", "expected_url"),
    [
        # Default: the search endpoint's last segment replaced by ``ask``.
        (
            {"endpoint": "https://corpus.test/search", "answer": "upstream"},
            "https://corpus.test/ask",
        ),
        # A prefixed path keeps its prefix.
        (
            {"endpoint": "https://corpus.test/v1/search", "answer": "upstream"},
            "https://corpus.test/v1/ask",
        ),
        # The ``url`` alias resolves like ``endpoint``.
        (
            {"url": "https://corpus.test/api/search", "answer": "upstream"},
            "https://corpus.test/api/ask",
        ),
        # No ref endpoint: derived from the legacy global CORPUS_URL.
        ({"answer": "upstream"}, "https://legacy-corpus.test/v1/ask"),
        # An explicit answer endpoint wins.
        (
            {
                "endpoint": "https://corpus.test/search",
                "answer": "upstream",
                "answer_endpoint": "https://answers.test/v2/answer",
            },
            "https://answers.test/v2/answer",
        ),
    ],
)
@pytest.mark.asyncio
async def test_corpus_http_answer_endpoint_resolution(
    ref: dict[str, Any], expected_url: str
) -> None:
    fake_ask = _FakeAsk(_fixture_body())
    with patch(_ASK_SEAM, new=fake_ask):
        await CorpusHttpBackend().answer(_operator(), "q", backend_ref=ref, limit=4)
    (call,) = fake_ask.calls
    assert call["answer_url"] == expected_url
    assert call["limit"] == 4
