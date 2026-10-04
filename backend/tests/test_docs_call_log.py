# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-call docs log fields (#3915).

Every docs call logs the hit and cited chunk ids, never content; query text
is logged only behind ``DOCS_DEBUG_LOG_QUERY_TEXT``; a search that set a
product or version filter and found nothing warns and counts. These tests
run the real search / expand / retrieve / synthesize primitives and the
``ask_docs`` answer seam (both the local pipeline and the upstream answer
path of #3911) with the corpus transports and the model stubbed (no
network, no LLM), and capture every docs module's log records through
private loggers.

Capture shape: each module's ``_log`` (and ``call_log._query_text_log``) is
swapped for a private logger bound to one :class:`structlog.testing.LogCapture`
rather than using :func:`structlog.testing.capture_logs`, which misses the
events of a logger cached under an earlier ``configure`` on the same xdist
worker (see ``tests/test_corpus_client.py``). The private loggers keep a
DEBUG floor so a debug record that carried query text anywhere else would
be caught too.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
import structlog
import structlog.testing
from prometheus_client import REGISTRY

import meho_backplane.docs_search.answer as answer_mod
import meho_backplane.docs_search.call_log as call_log_mod
import meho_backplane.docs_search.expansion as expansion_mod
import meho_backplane.docs_search.fanout as fanout_mod
import meho_backplane.docs_search.service as service_mod
import meho_backplane.docs_search.synthesis as synthesis_mod
from meho_backplane.auth.corpus import CorpusChunk, CorpusSearchResponse, UpstreamAnswer
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.docs_collections import DocCollection
from meho_backplane.docs_search import (
    DocsChunk,
    build_docs_scope,
    expand_docs_query,
    retrieve_multi_query,
    search_docs,
    search_docs_fanout,
    synthesize_docs_answer,
)
from meho_backplane.docs_search.answer import answer_docs_question
from meho_backplane.docs_search.call_log import (
    MAX_LOGGED_IDS,
    ask_log_fields,
    hit_log_fields,
)
from meho_backplane.operations.ingest import LlmJsonResult
from meho_backplane.settings import get_settings

#: The corpus-http backend's transport seam.
_CORPUS_SEAM = "meho_backplane.docs_search.backends.corpus_http.search_corpus"
#: The corpus-http backend's answer transport seam (#3911).
_ASK_SEAM = "meho_backplane.docs_search.backends.corpus_http.ask_corpus"
_BUILD_EXPAND_CLIENT = "meho_backplane.docs_search.expansion.build_anthropic_ingest_llm_client"
_BUILD_SYNTH_CLIENT = "meho_backplane.docs_search.synthesis.build_anthropic_ingest_llm_client"
#: A collection opted in to its backend's answer endpoint.
_UPSTREAM_BACKEND: dict[str, Any] = {
    "type": "corpus-http",
    "ref": {"endpoint": "https://corpus.test/search", "answer": "upstream"},
}

# Sentinel strings: none of them may reach a log record unless the opt-in
# flag is on (and then only the query strings, only at debug level).
_QUERY = "QUERYTEXT how do snapshots quiesce the guest"
_VARIANT = "VARIANTTEXT vsphere snapshot quiescing"
_CHUNK_TEXT_A = "CHUNKTEXT-A snapshots quiesce the guest file system before capture"
_CHUNK_TEXT_B = "CHUNKTEXT-B the kb article explains quiescing failures"
_ANSWER = "ANSWERTEXT yes, a quiesced snapshot flushes guest writes first"

_CONTENT_KEYS = frozenset({"content", "text", "answer", "query", "queries", "quote"})

_ZERO_HITS_METRIC = "docs_search_scoped_zero_hits_total"

#: A docs-shelf page: no public URL is derivable, so it becomes a meho:// ref.
_CHUNK_DOCS = CorpusChunk(
    chunk_id="chunk-docs-1",
    document_id="",
    content=_CHUNK_TEXT_A,
    source_url="gs://example-bucket/docs/vsphere/snapshots.html",
)
#: A KB article: its public URL is the ref.
_CHUNK_KB = CorpusChunk(
    chunk_id="chunk-kb-2",
    document_id="",
    content=_CHUNK_TEXT_B,
    source_url="gs://example-bucket/kb/broadcom-kb/articles/31/318828.html",
)


class _StubLlmClient:
    """Deterministic expand / synthesis client returning a fixed raw string."""

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
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env ``get_settings()`` needs, with the debug flag unset."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.delenv("DOCS_DEBUG_LOG_QUERY_TEXT", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def records(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture every docs module's log records into one list."""
    capture = structlog.testing.LogCapture()
    private = structlog.wrap_logger(
        structlog.PrintLogger(),
        processors=[capture],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
    )
    for module in (service_mod, fanout_mod, expansion_mod, synthesis_mod, answer_mod, call_log_mod):
        monkeypatch.setattr(module, "_log", private)
    monkeypatch.setattr(call_log_mod, "_query_text_log", private)
    return capture.entries


def _enable_query_text_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCS_DEBUG_LOG_QUERY_TEXT", "true")
    get_settings.cache_clear()


def _fake_corpus(*chunks: CorpusChunk) -> Any:
    async def _search(operator: Any, query: str, **kwargs: Any) -> CorpusSearchResponse:
        return CorpusSearchResponse(chunks=list(chunks))

    return _search


def _operator() -> Operator:
    return Operator(
        sub="op-42",
        raw_jwt="header.payload.signature",
        tenant_id=uuid4(),
        tenant_role=TenantRole.OPERATOR,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware"}),
    )


def _collection(
    collection_key: str = "vmware", backend: dict[str, Any] | None = None
) -> DocCollection:
    now = datetime.now(UTC)
    return DocCollection(
        id=uuid4(),
        tenant_id=None,
        collection_key=collection_key,
        vendor="VMware by Broadcom",
        products=("vsphere",),
        description="VMware docs.",
        when_to_use="Vendor product questions.",
        backend=backend if backend is not None else {"type": "corpus-http"},
        status="ready",
        last_ingested_at=None,
        doc_count=None,
        readiness=None,
        extras={},
        created_at=now,
        updated_at=now,
    )


def _docs_chunk(chunk_id: str, source_url: str | None = None) -> DocsChunk:
    return DocsChunk(chunk_id=chunk_id, content=f"text of {chunk_id}", source_url=source_url)


def _events(records: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [r for r in records if r["event"] == name]


def _one(records: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matching = _events(records, name)
    assert len(matching) == 1, f"expected one {name!r}, got {[r['event'] for r in records]}"
    return matching[0]


def _assert_no_content(records: list[dict[str, Any]], *sentinels: str) -> None:
    """No record carries a content key or any sentinel string."""
    assert records
    for record in records:
        assert not (_CONTENT_KEYS & record.keys()), record
        rendered = repr(record)
        for sentinel in sentinels:
            assert sentinel not in rendered, (sentinel, record["event"])


def _zero_hits_count() -> float:
    return REGISTRY.get_sample_value(_ZERO_HITS_METRIC) or 0.0


async def _run_local_ask(chunks: tuple[CorpusChunk, ...]) -> None:
    """Expand -> retrieve -> synthesize on the local pipeline, all stubbed."""
    collection = _collection()
    expand = _StubLlmClient(json.dumps({"queries": [_VARIANT]}))
    synth = _StubLlmClient(json.dumps({"answer": _ANSWER, "cited_chunk_ids": ["chunk-kb-2"]}))
    with patch(_CORPUS_SEAM, new=_fake_corpus(*chunks)):
        variants = await expand_docs_query(_QUERY, collection, llm_client=expand)
        retrieval = await retrieve_multi_query(
            _operator(), variants, scope=build_docs_scope("vmware"), collection=collection
        )
    await synthesize_docs_answer(_QUERY, retrieval, llm_client=synth)


# ---------------------------------------------------------------------------
# The field builders
# ---------------------------------------------------------------------------


def test_hit_log_fields_keep_rank_order_and_cap_the_lists() -> None:
    chunks = [_docs_chunk(f"c{i}", f"meho://docs/vmware/c{i}") for i in range(MAX_LOGGED_IDS + 5)]

    fields = hit_log_fields(chunks)

    assert fields["hit_count"] == MAX_LOGGED_IDS + 5
    assert fields["hit_chunk_ids"] == [f"c{i}" for i in range(MAX_LOGGED_IDS)]
    assert fields["hit_source_refs"] == [f"meho://docs/vmware/c{i}" for i in range(MAX_LOGGED_IDS)]


def test_upstream_ask_fields_carry_ids_and_timings_not_content() -> None:
    """The upstream answer path's completion fields (#3911 emits them)."""
    hits = [
        _docs_chunk("h1", "https://knowledge.broadcom.com/external/article/318828"),
        _docs_chunk("h2", "meho://docs/vmware/h2"),
    ]

    fields = ask_log_fields(
        answer_source="upstream",
        hits=hits,
        citations=[hits[1]],
        upstream_total_ms=2750.0,
        upstream_llm_ms=1900.5,
    )

    assert fields == {
        "answer_source": "upstream",
        "hit_count": 2,
        "hit_chunk_ids": ["h1", "h2"],
        "hit_source_refs": [
            "https://knowledge.broadcom.com/external/article/318828",
            "meho://docs/vmware/h2",
        ],
        "citation_count": 1,
        "cited_chunk_ids": ["h2"],
        "upstream_total_ms": 2750.0,
        "upstream_llm_ms": 1900.5,
    }
    assert "text of" not in repr(fields)


def test_ask_fields_omit_timings_the_answer_did_not_report() -> None:
    fields = ask_log_fields(answer_source="upstream", hits=[], citations=[], upstream_total_ms=12.0)

    assert fields["upstream_total_ms"] == 12.0
    assert "upstream_llm_ms" not in fields


# ---------------------------------------------------------------------------
# Ids only, on search, fan-out search and local ask
# ---------------------------------------------------------------------------


async def test_search_logs_hit_ids_and_normalised_refs_only(records: list[dict[str, Any]]) -> None:
    with patch(_CORPUS_SEAM, new=_fake_corpus(_CHUNK_DOCS, _CHUNK_KB)):
        await search_docs(
            _operator(),
            _QUERY,
            scope=build_docs_scope("vmware", product="vsphere", version="8.0"),
            collection=_collection(),
        )

    completed = _one(records, "docs_search_completed")
    assert completed["hit_count"] == 2
    assert completed["hit_chunk_ids"] == ["chunk-docs-1", "chunk-kb-2"]
    assert completed["hit_source_refs"] == [
        "meho://docs/vmware/chunk-docs-1",
        "https://knowledge.broadcom.com/external/article/318828",
    ]
    assert (completed["product"], completed["version"]) == ("vsphere", "8.0")
    _assert_no_content(records, _QUERY, _CHUNK_TEXT_A, _CHUNK_TEXT_B, "gs://")


async def test_fanout_search_logs_fused_hit_ids(records: list[dict[str, Any]]) -> None:
    with patch(_CORPUS_SEAM, new=_fake_corpus(_CHUNK_DOCS)):
        await search_docs_fanout(
            _operator(), _QUERY, collections=[_collection("vmware"), _collection("other")]
        )

    completed = _one(records, "docs_search_fanout_completed")
    assert completed["hit_count"] == 2
    assert completed["hit_chunk_ids"] == ["chunk-docs-1", "chunk-docs-1"]
    # The same chunk id from two collections: hit_collections tells them
    # apart, position by position with the refs.
    assert sorted(completed["hit_collections"]) == ["other", "vmware"]
    assert [
        ref.removeprefix(f"meho://docs/{key}/")
        for key, ref in zip(completed["hit_collections"], completed["hit_source_refs"], strict=True)
    ] == ["chunk-docs-1", "chunk-docs-1"]
    _assert_no_content(records, _QUERY, _CHUNK_TEXT_A, "gs://")


async def test_single_collection_events_do_not_list_hit_collections(
    records: list[dict[str, Any]],
) -> None:
    with patch(_CORPUS_SEAM, new=_fake_corpus(_CHUNK_DOCS)):
        await search_docs(
            _operator(), _QUERY, scope=build_docs_scope("vmware"), collection=_collection()
        )

    assert "hit_collections" not in _one(records, "docs_search_completed")


async def test_local_ask_logs_hit_and_cited_ids_only(records: list[dict[str, Any]]) -> None:
    await _run_local_ask((_CHUNK_DOCS, _CHUNK_KB))

    synthesized = _one(records, "docs_ask_synthesized")
    assert synthesized["answer_source"] == "local"
    assert synthesized["hit_count"] == 2
    assert set(synthesized["hit_chunk_ids"]) == {"chunk-docs-1", "chunk-kb-2"}
    assert len(synthesized["hit_source_refs"]) == 2
    assert synthesized["citation_count"] == 1
    assert synthesized["cited_chunk_ids"] == ["chunk-kb-2"]

    merged = _one(records, "docs_search_multi_query_completed")
    assert merged["variant_count"] == 2
    assert merged["hit_chunk_ids"] == synthesized["hit_chunk_ids"]
    # One search per variant, each listing its own hits.
    assert len(_events(records, "docs_search_completed")) == 2

    _assert_no_content(records, _QUERY, _VARIANT, _CHUNK_TEXT_A, _CHUNK_TEXT_B, _ANSWER, "gs://")


async def test_local_ask_with_no_hits_logs_empty_id_lists(records: list[dict[str, Any]]) -> None:
    await _run_local_ask(())

    no_grounding = _one(records, "docs_ask_no_grounding")
    assert no_grounding["answer_source"] == "local"
    assert no_grounding["hit_chunk_ids"] == []
    assert no_grounding["cited_chunk_ids"] == []
    assert _events(records, "docs_ask_synthesized") == []


def _upstream_hit(chunk_id: str, *, text: str, source_uri: str) -> dict[str, Any]:
    """An upstream ``/ask?include=hits`` hit (the backend's search-hit shape)."""
    return {
        "chunk_id": chunk_id,
        "document_id": "",
        "chunk_index": 0,
        "text": text,
        "source_uri": source_uri,
        "score": 0.3,
        "filename": f"{chunk_id}.html",
        "heading_path": ["Snapshots"],
    }


def _upstream_body() -> dict[str, Any]:
    """An upstream answer over two hits that cites the second one.

    The hits carry raw ``gs://`` storage paths, as the backend sends them;
    the answer, the echoed query and the quote carry sentinel text.
    """
    return {
        "query": _QUERY,
        "answer": f"{_ANSWER} [0].",
        "citations": [{"chunk_index": 0, "chunk_id": "up-kb-2", "quote": _CHUNK_TEXT_B}],
        "hits": [
            _upstream_hit("up-docs-1", text=_CHUNK_TEXT_A, source_uri=_CHUNK_DOCS.source_url),
            _upstream_hit("up-kb-2", text=_CHUNK_TEXT_B, source_uri=_CHUNK_KB.source_url),
        ],
        "timing": {"total_ms": 2790.6, "llm_ms": 1688.2, "knn_ms": 912.4},
    }


def _fake_ask(body: dict[str, Any]) -> Any:
    async def _ask(operator: Any, query: str, **kwargs: Any) -> UpstreamAnswer:
        return UpstreamAnswer.model_validate(body)

    return _ask


@pytest.mark.parametrize("query_text_flag", [False, True])
async def test_upstream_ask_completion_logs_hit_and_cited_ids_only(
    records: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, query_text_flag: bool
) -> None:
    """The upstream answer path's emitted ``docs_ask_completed`` record (#3911).

    It carries the ids-only ask fields with ``answer_source="upstream"`` and
    the backend's timing, never the query, the answer or a chunk's text, and
    never a ``gs://`` path. The upstream path logs no query text even with
    the flag on: the backend does not return its rewritten query yet
    (evoila-bosnia/MEHO.Knowledge#513).
    """
    if query_text_flag:
        _enable_query_text_flag(monkeypatch)
    search_calls: list[str] = []

    async def _no_search(operator: Any, query: str, **kwargs: Any) -> CorpusSearchResponse:
        search_calls.append(query)
        return CorpusSearchResponse(chunks=[])

    with patch(_ASK_SEAM, new=_fake_ask(_upstream_body())), patch(_CORPUS_SEAM, new=_no_search):
        outcome = await answer_docs_question(
            _operator(),
            _QUERY,
            scope=build_docs_scope("vmware"),
            collection=_collection(backend=_UPSTREAM_BACKEND),
            limit=10,
        )

    assert outcome.error is None
    assert search_calls == []
    completed = _one(records, "docs_ask_completed")
    assert completed["log_level"] == "info"
    assert completed["operator_sub"] == "op-42"
    assert completed["collection_key"] == "vmware"
    assert completed["answer_source"] == "upstream"
    assert completed["hit_count"] == 2
    assert completed["hit_chunk_ids"] == ["up-docs-1", "up-kb-2"]
    assert completed["hit_source_refs"] == [
        "meho://docs/vmware/up-docs-1",
        "https://knowledge.broadcom.com/external/article/318828",
    ]
    assert completed["citation_count"] == 1
    assert completed["cited_chunk_ids"] == ["up-kb-2"]
    assert completed["upstream_total_ms"] == 2790.6
    assert completed["upstream_llm_ms"] == 1688.2
    assert _events(records, "docs_query_text") == []
    _assert_no_content(records, _QUERY, _ANSWER, _CHUNK_TEXT_A, _CHUNK_TEXT_B, "gs://")


async def test_local_ask_completion_carries_the_same_fields(
    records: list[dict[str, Any]],
) -> None:
    """``docs_ask_completed`` has the same ids-only keys on the local path, no timing."""
    expand = _StubLlmClient(json.dumps({"queries": [_VARIANT]}))
    synth = _StubLlmClient(json.dumps({"answer": _ANSWER, "cited_chunk_ids": ["chunk-kb-2"]}))
    with (
        patch(_CORPUS_SEAM, new=_fake_corpus(_CHUNK_DOCS, _CHUNK_KB)),
        patch(_BUILD_EXPAND_CLIENT, return_value=expand),
        patch(_BUILD_SYNTH_CLIENT, return_value=synth),
    ):
        outcome = await answer_docs_question(
            _operator(),
            _QUERY,
            scope=build_docs_scope("vmware"),
            collection=_collection(),
            limit=10,
        )

    assert outcome.error is None
    completed = _one(records, "docs_ask_completed")
    synthesized = _one(records, "docs_ask_synthesized")
    assert completed["answer_source"] == "local"
    for key in ("hit_count", "hit_chunk_ids", "hit_source_refs", "cited_chunk_ids"):
        assert completed[key] == synthesized[key], key
    assert completed["cited_chunk_ids"] == ["chunk-kb-2"]
    assert "upstream_total_ms" not in completed
    assert "upstream_llm_ms" not in completed
    _assert_no_content(records, _QUERY, _VARIANT, _ANSWER, _CHUNK_TEXT_A, _CHUNK_TEXT_B, "gs://")


# ---------------------------------------------------------------------------
# Query text only behind DOCS_DEBUG_LOG_QUERY_TEXT
# ---------------------------------------------------------------------------


async def test_flag_off_no_query_or_variant_string_is_logged(
    records: list[dict[str, Any]],
) -> None:
    assert get_settings().docs_debug_log_query_text is False

    await _run_local_ask((_CHUNK_DOCS, _CHUNK_KB))

    assert _events(records, "docs_query_text") == []
    _assert_no_content(records, _QUERY, _VARIANT)


async def test_flag_on_query_strings_appear_at_debug_level_only(
    records: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_query_text_flag(monkeypatch)

    await _run_local_ask((_CHUNK_DOCS, _CHUNK_KB))

    query_text = _one(records, "docs_query_text")
    assert query_text["log_level"] == "debug"
    assert query_text["source"] == "expansion"
    assert query_text["collection_key"] == "vmware"
    assert query_text["queries"] == [_QUERY, _VARIANT]
    # Every other record stays free of query text and content.
    others = [r for r in records if r is not query_text]
    _assert_no_content(others, _QUERY, _VARIANT, _CHUNK_TEXT_A, _CHUNK_TEXT_B, _ANSWER)


def test_query_text_logger_writes_under_the_info_log_floor() -> None:
    """The backplane runs at INFO; the query-text logger has its own DEBUG floor.

    A logger at the INFO floor ``configure_logging`` sets drops a ``debug``
    record, while the query-text logger writes it, so turning the flag on is
    enough to see the strings. ``capture_logs`` swaps the configured
    processors in place, so no other module's logger is disturbed.
    """
    info_floor = structlog.wrap_logger(
        None, wrapper_class=structlog.make_filtering_bound_logger(logging.INFO)
    )
    with structlog.testing.capture_logs() as entries:
        info_floor.debug("ordinary_debug")
        call_log_mod._query_text_logger().debug("docs_query_text")

    assert [(e["event"], e["log_level"]) for e in entries] == [("docs_query_text", "debug")]


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("1", True), ("no", False)])
def test_debug_flag_reads_the_env(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
) -> None:
    monkeypatch.setenv("DOCS_DEBUG_LOG_QUERY_TEXT", raw)
    get_settings.cache_clear()

    assert get_settings().docs_debug_log_query_text is expected


# ---------------------------------------------------------------------------
# Scoped zero hits: warning + counter
# ---------------------------------------------------------------------------


async def test_scoped_zero_hit_search_warns_and_counts(records: list[dict[str, Any]]) -> None:
    before = _zero_hits_count()

    with patch(_CORPUS_SEAM, new=_fake_corpus()):
        await search_docs(
            _operator(),
            _QUERY,
            scope=build_docs_scope("vmware", product="vcenter"),
            collection=_collection(),
        )

    warning = _one(records, "docs_search_scoped_zero_hits")
    assert warning["log_level"] == "warning"
    assert warning["collection_key"] == "vmware"
    assert (warning["product"], warning["version"]) == ("vcenter", None)
    assert _zero_hits_count() == before + 1
    _assert_no_content(records, _QUERY)


async def test_unscoped_zero_hit_search_does_not_warn(records: list[dict[str, Any]]) -> None:
    before = _zero_hits_count()

    with patch(_CORPUS_SEAM, new=_fake_corpus()):
        await search_docs(
            _operator(), _QUERY, scope=build_docs_scope("vmware"), collection=_collection()
        )

    assert _events(records, "docs_search_scoped_zero_hits") == []
    assert _one(records, "docs_search_completed")["hit_count"] == 0
    assert _zero_hits_count() == before


async def test_scoped_search_with_hits_does_not_warn(records: list[dict[str, Any]]) -> None:
    before = _zero_hits_count()

    with patch(_CORPUS_SEAM, new=_fake_corpus(_CHUNK_DOCS)):
        await search_docs(
            _operator(),
            _QUERY,
            scope=build_docs_scope("vmware", version="9.0"),
            collection=_collection(),
        )

    assert _events(records, "docs_search_scoped_zero_hits") == []
    assert _zero_hits_count() == before
