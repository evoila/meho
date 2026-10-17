# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.retrieval.eval.passage_bench` (#3765).

No test here loads an embedding model. Searches are either scripted
(exact hits per query) or :func:`embedding_search` with a fake hashed
bag-of-words encoder and a word counter.
"""

from __future__ import annotations

import re
import uuid
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from meho_backplane.retrieval.embedding import DEFAULT_EMBEDDING_MODEL, EmbeddingService
from meho_backplane.retrieval.eval.corpus import KbPassageFixtures, load_kb_passage_fixtures
from meho_backplane.retrieval.eval.passage_bench import (
    BASELINE_CANDIDATE,
    CandidateSpec,
    PassageHit,
    PassageSearch,
    PassageSearchResult,
    PassageStratum,
    embedding_search,
    retrieve_search,
    run_passage_bench,
)
from meho_backplane.retrieval.retriever import RetrievalHit

_E5 = CandidateSpec(
    name="e5-small",
    embedding_model="intfloat/multilingual-e5-small",
    query_prefix="query: ",
    passage_prefix="passage: ",
    reembed_corpus=True,
)


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def _count(text: str) -> int:
    """Word counter that adds two special tokens, like a BERT tokenizer."""
    return len(text.split()) + 2


async def _fake_encode(texts: list[str]) -> list[list[float]]:
    vectors = []
    for text in texts:
        vec = [0.0] * 256
        for word in _words(text):
            vec[zlib.crc32(word.encode()) % 256] += 1.0
        vectors.append(vec)
    return vectors


def _ticks() -> Iterator[float]:
    t = 0.0
    while True:
        yield t
        t += 0.002


def _clock() -> Any:
    it = _ticks()
    return lambda: next(it)


def _scripted(hits_by_query: dict[str, list[PassageHit]]) -> PassageSearch:
    async def search(query: str, k: int) -> PassageSearchResult:
        return PassageSearchResult(hits_by_query.get(query, []))

    return search


def _hit(slug: str, heading: str, text: str) -> PassageHit:
    return PassageHit(slug, ("Title", heading), text)


def _perfect(fixtures: KbPassageFixtures) -> PassageSearch:
    """Return the first expected passage for every answerable query, nothing otherwise."""
    script = {
        q.query: [_hit(q.expected_slugs[0], q.expected_heading or "", " ".join(q.answer_terms))]
        for q in fixtures.queries
        if q.scenario != "no_match"
    }
    return _scripted(script)


def _stratum(strata: list[PassageStratum], scenario: str, language: str) -> PassageStratum:
    return next(s for s in strata if s.scenario == scenario and s.language == language)


# ---------------------------------------------------------------------------
# Candidate contract
# ---------------------------------------------------------------------------


def test_baseline_is_the_unchanged_default() -> None:
    assert BASELINE_CANDIDATE.is_baseline
    assert BASELINE_CANDIDATE.embedding_model == DEFAULT_EMBEDDING_MODEL
    assert not _E5.is_baseline


def test_same_dimension_model_still_requires_reembed() -> None:
    """multilingual-e5-small has 384 dimensions like the default, but its vectors differ."""
    with pytest.raises(ValidationError, match="reembed_corpus"):
        CandidateSpec(name="e5", embedding_model="intfloat/multilingual-e5-small")
    with pytest.raises(ValidationError, match="reembed_corpus"):
        CandidateSpec(name="prefixed", passage_prefix="passage: ")


def test_query_prefix_or_reranker_alone_needs_no_reembed_but_is_not_baseline() -> None:
    assert not CandidateSpec(name="q", query_prefix="query: ").is_baseline
    assert not CandidateSpec(name="r", reranker="some-reranker").is_baseline


def test_candidate_is_frozen() -> None:
    with pytest.raises(ValidationError):
        BASELINE_CANDIDATE.embedding_model = "other"


async def test_first_candidate_must_be_the_baseline() -> None:
    search = _scripted({})
    with pytest.raises(ValueError, match="baseline"):
        await run_passage_bench([(_E5, search), (BASELINE_CANDIDATE, search)])
    with pytest.raises(ValueError, match="baseline"):
        await run_passage_bench([])
    with pytest.raises(ValueError, match="unique"):
        await run_passage_bench([(BASELINE_CANDIDATE, search), (BASELINE_CANDIDATE, search)])


# ---------------------------------------------------------------------------
# Scoring and report shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize(
    "scenario", ["middle_passage", "end_passage", "duplicate", "conflict", "stale"]
)
async def test_perfect_search_scores_one_per_stratum(scenario: str, language: str) -> None:
    fixtures = load_kb_passage_fixtures()
    report = await run_passage_bench(
        [(BASELINE_CANDIDATE, _perfect(fixtures))], fixtures=fixtures, clock=_clock()
    )
    s = _stratum(report.candidates[0].strata, scenario, language)
    assert (s.source_citation, s.mrr, s.selected_reference, s.answer_support) == (1, 1, 1, 1)
    assert s.harm_rate == 0.0
    assert s.calls == s.query_count == 1
    assert s.latency_p95_ms == pytest.approx(2.0)
    assert s.bytes_mean > 0


async def test_no_match_has_no_source_metrics_and_counts_any_evidence_as_harm() -> None:
    fixtures = load_kb_passage_fixtures()
    no_match = next(q for q in fixtures.queries if q.scenario == "no_match" and q.language == "en")
    noisy = _scripted({no_match.query: [_hit("orbit-backup-retention", "Scope", "x")]})
    report = await run_passage_bench([(BASELINE_CANDIDATE, noisy)], fixtures=fixtures)
    en = _stratum(report.candidates[0].strata, "no_match", "en")
    de = _stratum(report.candidates[0].strata, "no_match", "de")
    assert en.source_citation is None and en.selected_reference is None
    assert en.harm_rate == 1.0
    assert de.harm_rate == 0.0


async def test_harmful_entry_above_correct_passage_is_harm() -> None:
    fixtures = load_kb_passage_fixtures()
    q = next(q for q in fixtures.queries if q.id == "en-stale-retention-days")
    search = _scripted(
        {
            q.query: [
                _hit(q.harmful_slugs[0], "Retention policy", "kept for 7 days"),
                _hit(q.expected_slugs[0], "Scope", "release 4"),
            ]
        }
    )
    report = await run_passage_bench([(BASELINE_CANDIDATE, search)], fixtures=fixtures)
    s = _stratum(report.candidates[0].strata, "stale", "en")
    assert s.harm_rate == 1.0
    assert s.source_citation == 1.0
    assert s.mrr == 0.5
    assert s.selected_reference == 0.0  # right entry, wrong section
    assert s.answer_support == 0.0


async def test_report_holds_no_query_or_body_text() -> None:
    fixtures = load_kb_passage_fixtures()
    baseline = await embedding_search(
        BASELINE_CANDIDATE, encode=_fake_encode, count=_count, window=40, fixtures=fixtures
    )
    report = await run_passage_bench([(BASELINE_CANDIDATE, baseline)], fixtures=fixtures)
    dumped = report.model_dump_json()
    for q in fixtures.queries:
        assert q.query not in dumped
    for entry in fixtures.entries:
        for line in entry.body.splitlines():
            if len(line.strip()) > 20:
                assert line.strip() not in dumped
    assert report.fixture_version == fixtures.version
    assert len(report.candidates[0].strata) == 12


async def test_baseline_and_candidate_run_offline_without_loading_a_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default test path never loads fastembed weights."""

    def _no_model(self: EmbeddingService) -> Any:
        raise AssertionError("a model load was attempted")

    monkeypatch.setattr(EmbeddingService, "_ensure_loaded", _no_model)
    fixtures = load_kb_passage_fixtures()
    base = await embedding_search(
        BASELINE_CANDIDATE, encode=_fake_encode, count=_count, window=40, fixtures=fixtures
    )
    cand = await embedding_search(
        _E5, encode=_fake_encode, count=_count, window=40, fixtures=fixtures
    )
    report = await run_passage_bench([(BASELINE_CANDIDATE, base), (_E5, cand)], fixtures=fixtures)
    assert [c.baseline for c in report.candidates] == [True, False]
    mid = _stratum(report.candidates[0].strata, "middle_passage", "en")
    assert mid.source_citation == 1.0


# ---------------------------------------------------------------------------
# Search builders
# ---------------------------------------------------------------------------


async def test_embedding_search_reembeds_corpus_with_matching_prefixes() -> None:
    fixtures = load_kb_passage_fixtures()
    calls: list[list[str]] = []

    async def recording(texts: list[str]) -> list[list[float]]:
        calls.append(list(texts))
        return await _fake_encode(texts)

    search = await embedding_search(
        _E5, encode=recording, count=_count, window=30, fixtures=fixtures
    )
    corpus = calls[0]
    assert len(corpus) > len(fixtures.entries)  # every entry, split into passages
    assert all(t.startswith("passage: ") for t in corpus)
    assert all(_count(t) <= 30 for t in corpus)  # the prefix still fits the window
    result = await search("renew certificate", 3)
    assert calls[1] == ["query: renew certificate"]
    assert len(result.hits) == 3 and result.calls == 1


async def test_embedding_search_refuses_reranker() -> None:
    with pytest.raises(ValueError, match="reranker"):
        await embedding_search(
            CandidateSpec(name="r", reranker="x"), encode=_fake_encode, count=_count, window=40
        )


async def test_retrieve_search_maps_passage_and_legacy_rows() -> None:
    ts = datetime(2026, 10, 1, tzinfo=UTC)
    tenant = uuid.uuid4()
    seen: dict[str, Any] = {}

    def row(source_id: str, body: str, meta: dict[str, Any]) -> RetrievalHit:
        return RetrievalHit(
            document_id=uuid.uuid4(),
            tenant_id=tenant,
            source="kb",
            source_id=source_id,
            kind="kb-passage",
            body=body,
            doc_metadata=meta,
            created_at=ts,
            updated_at=ts,
            fused_score=0.5,
            bm25_score=None,
            cosine_score=0.5,
            bm25_rank=None,
            cosine_rank=1,
        )

    async def fake_retrieve(**kwargs: Any) -> list[RetrievalHit]:
        seen.update(kwargs)
        return [
            row("opaque", "p1", {"kb_passage": {"slug": "a", "heading_path": ["A", "Ports"]}}),
            row("legacy-slug", "whole body", {}),
        ]

    search = retrieve_search(tenant_id=tenant, retrieve_fn=fake_retrieve)
    result = await search("q", 4)
    assert seen == {"tenant_id": tenant, "query": "q", "source": "kb", "limit": 4}
    assert result.hits == [
        PassageHit("a", ("A", "Ports"), "p1"),
        PassageHit("legacy-slug", (), "whole body"),
    ]
