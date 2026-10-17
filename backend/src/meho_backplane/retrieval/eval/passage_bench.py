# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Offline benchmark for KB passage retrieval candidates (#3765).

This module answers one question before anyone changes a production
default: does a retrieval candidate (another embedding model, query
and passage prefixes, a reranker) find better cited passages than the
current default on the synthetic fixtures in
``kb_passage_fixtures.yaml``?

It reuses the retrieval eval package: the fixture loader in
:mod:`corpus`, the rank metrics in :mod:`metrics`, ``DEFAULT_K`` and
the ``RetrieveCallable`` seam from :mod:`runner`. It adds no route,
index, model, migration or setting. A candidate here is only a
description; nothing in this module can make it the production default.

How a run works
---------------

1. The caller passes a list of ``(CandidateSpec, PassageSearch)``
   pairs. The first one must be the default baseline.
2. For each candidate, every fixture query runs once through its
   search. The run times each call and counts bytes and calls.
3. Each query is scored: correct source, correct cited passage, answer
   terms in that passage, and harm (a stale or conflicting entry ranked
   above the first correct passage, or any evidence for a ``no_match``
   query).
4. Scores are grouped by ``(scenario, language)``. The report holds
   only labels and numbers, never a query or a body.

Two search builders are included:

* :func:`retrieve_search` runs the production
  :func:`~meho_backplane.retrieval.retriever.retrieve` with
  ``source="kb"``. It needs the fixtures ingested into a tenant.
* :func:`embedding_search` re-embeds the whole fixture corpus in memory
  with the candidate's model and prefixes, then ranks by cosine. It
  needs only an ``encode`` function, so tests pass a fake one and no
  model is downloaded.
"""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, model_validator

from meho_backplane.kb.passages import chunk_markdown
from meho_backplane.retrieval.embedding import DEFAULT_EMBEDDING_MODEL
from meho_backplane.retrieval.eval.corpus import (
    KbPassageFixtures,
    KbPassageQuery,
    load_kb_passage_fixtures,
)
from meho_backplane.retrieval.eval.metrics import (
    coverage_at_k,
    mean_metric,
    percentile,
    reciprocal_rank,
)
from meho_backplane.retrieval.eval.runner import DEFAULT_K, RetrieveCallable
from meho_backplane.retrieval.retriever import retrieve

__all__ = [
    "BASELINE_CANDIDATE",
    "CandidateReport",
    "CandidateSpec",
    "PassageBenchReport",
    "PassageHit",
    "PassageSearch",
    "PassageSearchResult",
    "PassageStratum",
    "embedding_search",
    "retrieve_search",
    "run_passage_bench",
]


@dataclass(frozen=True)
class PassageHit:
    """One ranked passage: its entry slug, heading path and text."""

    slug: str
    heading_path: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class PassageSearchResult:
    """Ranked hits for one query, and how many backend calls it took."""

    hits: list[PassageHit]
    calls: int = 1


#: A search under test: ``(query, k) -> ranked passages``.
PassageSearch = Callable[[str, int], Awaitable[PassageSearchResult]]

Encode = Callable[[list[str]], Awaitable[list[list[float]]]]


class CandidateSpec(BaseModel):
    """What a candidate changes compared with the default retrieval.

    ``reembed_corpus`` must be ``True`` whenever the model or the
    passage prefix differs from the default. This holds even when the
    new model has the same vector size: vectors from two models are not
    comparable, so stored passages must be embedded again.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    query_prefix: str = ""
    passage_prefix: str = ""
    reranker: str | None = None
    reembed_corpus: bool = False

    @model_validator(mode="after")
    def _require_reembed(self) -> CandidateSpec:
        changes_vectors = (
            self.embedding_model != DEFAULT_EMBEDDING_MODEL or self.passage_prefix != ""
        )
        if changes_vectors and not self.reembed_corpus:
            raise ValueError(
                f"candidate {self.name!r} changes passage vectors; set reembed_corpus=True"
            )
        return self

    @property
    def is_baseline(self) -> bool:
        """``True`` when this is the unchanged default retrieval."""
        return (
            self.embedding_model == DEFAULT_EMBEDDING_MODEL
            and self.query_prefix == ""
            and self.passage_prefix == ""
            and self.reranker is None
        )


#: The default retrieval, unchanged. Every report starts with it.
BASELINE_CANDIDATE = CandidateSpec(name="default")


class PassageStratum(BaseModel):
    """Metrics for one ``(scenario, language)`` group.

    Source and passage metrics are ``None`` for ``no_match`` groups,
    which have no correct answer.
    """

    model_config = ConfigDict(frozen=True)

    scenario: str
    language: str
    query_count: int
    source_citation: float | None
    mrr: float | None
    selected_reference: float | None
    answer_support: float | None
    harm_rate: float
    latency_p95_ms: float
    bytes_mean: float
    bytes_p95: float
    calls: int


class CandidateReport(BaseModel):
    """All groups for one candidate."""

    model_config = ConfigDict(frozen=True)

    candidate: CandidateSpec
    baseline: bool
    strata: list[PassageStratum]


class PassageBenchReport(BaseModel):
    """The benchmark result. Holds labels and numbers only."""

    model_config = ConfigDict(frozen=True)

    fixture_version: int
    k: int
    candidates: list[CandidateReport]


@dataclass(frozen=True)
class _QueryScore:
    source: float | None
    rr: float | None
    selected: float | None
    answer: float | None
    harm: float
    latency_ms: float
    bytes: int
    calls: int


def _unique_slugs(hits: Sequence[PassageHit]) -> list[str]:
    slugs: list[str] = []
    for hit in hits:
        if hit.slug not in slugs:
            slugs.append(hit.slug)
    return slugs


def _score(
    query: KbPassageQuery, hits: list[PassageHit], latency_ms: float, calls: int, k: int
) -> _QueryScore:
    size = sum(len(h.text.encode("utf-8")) for h in hits)
    if query.scenario == "no_match":
        harm = 1.0 if hits else 0.0
        return _QueryScore(None, None, None, None, harm, latency_ms, size, calls)
    expected = set(query.expected_slugs)
    harmful = set(query.harmful_slugs)
    first = next((i for i, h in enumerate(hits) if h.slug in expected), None)
    slugs = _unique_slugs(hits)
    source = coverage_at_k(slugs, query.expected_slugs, k=k)
    rr = reciprocal_rank(slugs, query.expected_slugs)
    if first is None:
        harm = 1.0 if any(h.slug in harmful for h in hits) else 0.0
        return _QueryScore(source, rr, 0.0, 0.0, harm, latency_ms, size, calls)
    cited = hits[first]
    selected = 1.0 if cited.heading_path[-1:] == (query.expected_heading,) else 0.0
    text = cited.text.casefold()
    answer = mean_metric(1.0 if t.casefold() in text else 0.0 for t in query.answer_terms)
    harm = 1.0 if any(h.slug in harmful for h in hits[:first]) else 0.0
    return _QueryScore(source, rr, selected, answer, harm, latency_ms, size, calls)


def _optional_mean(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return mean_metric(present) if present else None


def _stratum(scenario: str, language: str, scores: list[_QueryScore]) -> PassageStratum:
    return PassageStratum(
        scenario=scenario,
        language=language,
        query_count=len(scores),
        source_citation=_optional_mean([s.source for s in scores]),
        mrr=_optional_mean([s.rr for s in scores]),
        selected_reference=_optional_mean([s.selected for s in scores]),
        answer_support=_optional_mean([s.answer for s in scores]),
        harm_rate=mean_metric(s.harm for s in scores),
        latency_p95_ms=percentile((s.latency_ms for s in scores), 95),
        bytes_mean=mean_metric(float(s.bytes) for s in scores),
        bytes_p95=percentile((float(s.bytes) for s in scores), 95),
        calls=sum(s.calls for s in scores),
    )


async def _run_candidate(
    spec: CandidateSpec,
    search: PassageSearch,
    fixtures: KbPassageFixtures,
    k: int,
    clock: Callable[[], float],
) -> CandidateReport:
    groups: dict[tuple[str, str], list[_QueryScore]] = {}
    for query in fixtures.queries:
        started = clock()
        result = await search(query.query, k)
        latency_ms = (clock() - started) * 1000.0
        score = _score(query, result.hits[:k], latency_ms, result.calls, k)
        groups.setdefault((query.scenario, query.language), []).append(score)
    strata = [_stratum(sc, lang, scores) for (sc, lang), scores in groups.items()]
    return CandidateReport(candidate=spec, baseline=spec.is_baseline, strata=strata)


async def run_passage_bench(
    candidates: Sequence[tuple[CandidateSpec, PassageSearch]],
    *,
    fixtures: KbPassageFixtures | None = None,
    k: int = DEFAULT_K,
    clock: Callable[[], float] = time.perf_counter,
) -> PassageBenchReport:
    """Run every candidate over the fixtures and report per group.

    Raises
    ------
    ValueError
        The list is empty, the first candidate is not the default
        baseline, or two candidates share a name.
    """
    if not candidates or not candidates[0][0].is_baseline:
        raise ValueError("the first candidate must be the default baseline")
    names = [spec.name for spec, _ in candidates]
    if len(set(names)) != len(names):
        raise ValueError("candidate names must be unique")
    fx = fixtures if fixtures is not None else load_kb_passage_fixtures()
    reports = [await _run_candidate(spec, search, fx, k, clock) for spec, search in candidates]
    return PassageBenchReport(fixture_version=fx.version, k=k, candidates=reports)


def retrieve_search(
    *, tenant_id: uuid.UUID, retrieve_fn: RetrieveCallable | None = None
) -> PassageSearch:
    """Search through the production ``retrieve(source="kb")`` path.

    Passage rows carry their slug and heading path in the
    ``kb_passage`` metadata (#3761). A legacy whole-entry row has its
    slug as ``source_id`` and no heading path.
    """
    rfn: RetrieveCallable = retrieve_fn if retrieve_fn is not None else retrieve

    async def search(query: str, k: int) -> PassageSearchResult:
        hits = await rfn(tenant_id=tenant_id, query=query, source="kb", limit=k)
        out: list[PassageHit] = []
        for hit in hits:
            loc = hit.doc_metadata.get("kb_passage")
            if isinstance(loc, dict):
                out.append(PassageHit(str(loc["slug"]), tuple(loc["heading_path"]), hit.body))
            else:
                out.append(PassageHit(hit.source_id, (), hit.body))
        return PassageSearchResult(out)

    return search


async def embedding_search(
    candidate: CandidateSpec,
    *,
    encode: Encode,
    count: Callable[[str], int],
    window: int,
    fixtures: KbPassageFixtures | None = None,
) -> PassageSearch:
    """Re-embed the whole fixture corpus for *candidate*; return a cosine search.

    Every entry is split with the production chunker. The window is
    made smaller by the passage prefix's tokens, so ``prefix + passage``
    still fits the model. Passages get ``passage_prefix`` and queries get
    ``query_prefix``, so both sides use the preprocessing the model
    expects. Pass ``EmbeddingService(model, cache_dir).encode`` and its
    ``passage_tokenizer()`` for a real run.

    Raises
    ------
    ValueError
        The candidate names a reranker; this search does not run one.
    """
    if candidate.reranker is not None:
        raise ValueError("embedding_search does not run a reranker; pass your own search")
    fx = fixtures if fixtures is not None else load_kb_passage_fixtures()
    prefix_tokens = count(candidate.passage_prefix) - count("")
    passages = [
        (entry.slug, p)
        for entry in fx.entries
        for p in chunk_markdown(entry.body, count, window - prefix_tokens)
    ]
    vectors = await encode([candidate.passage_prefix + p.embed_text for _, p in passages])

    async def search(query: str, k: int) -> PassageSearchResult:
        (qv,) = await encode([candidate.query_prefix + query])
        ranked = sorted(range(len(passages)), key=lambda i: (-_cosine(qv, vectors[i]), i))[:k]
        hits = [
            PassageHit(passages[i][0], passages[i][1].heading_path, passages[i][1].text)
            for i in ranked
        ]
        return PassageSearchResult(hits)

    return search


def _cosine(a: list[float], b: list[float]) -> float:
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / norm if norm else 0.0
