# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Eval runner — corpus-agnostic engine for `meho retrieval eval` (G4.3-T2, #441).

The runner is the load-bearing piece of the G4.3 retire-decision
contract: it consumes T1's corpus loader, calls the in-process
:func:`meho_backplane.retrieval.retriever.retrieve` helper per query,
folds the per-query hits into precision@5 / MRR / coverage via the
pure :mod:`metrics` module, and returns a typed
:class:`~meho_backplane.retrieval.eval.result_models.EvalResult`
whose ``verdict`` field gates the CI workflow.

Surfaces
--------

The same dispatch path drives all three retrieval surfaces — the
per-surface differences (slug vs ``(scope, slug)`` vs
``(connector_id, op_id)`` ground truth) are absorbed by per-surface
``_eval_<surface>`` private dispatchers that produce a uniform
``QueryResult`` shape. Surfaces whose corpus YAML hasn't shipped yet
(memory in T4 #443, operations in T3 #442) return an empty
``SurfaceResult`` with ``verdict="green"`` + ``query_count=0`` —
the "no data" green is intentional: an absent corpus must not flip
the CI gate red. The retire-checklist verb (T6 #445) is responsible
for asserting that an evaluable corpus actually shipped before
trusting the green.

Baseline integration
--------------------

When ``baseline_corpus_root`` is passed, the runner asks
:func:`meho_backplane.retrieval.eval.baseline_grep.run_grep_baseline`
for the per-query top-k slugs, computes the same three metrics
against them, and reports both side-by-side. The threshold contract
"MEHO ≥ baseline" applies per-metric; if any MEHO metric is *worse*
than baseline, the verdict downgrades to red regardless of absolute
threshold (matches Initiative #373 retire-criterion #4 in the
``retire-checklist`` body — "MEHO ranking ≥ baseline on every
metric").

Out of scope (deferred per issue body)
--------------------------------------

* **Memory + operations corpus YAML.** Land in T4 / T3.
* **LLM-judge evaluation.** v0.2 is keyword-exact ground truth only.
* **Cross-tenant eval.** v0.2 has one production tenant; the
  runner is tenant-scoped per the caller's ``tenant_id`` arg.
* **Eval-on-write.** v0.2 runs eval on-demand + in CI; sub-second
  per-ingest eval is unnecessary overhead.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

import structlog

from meho_backplane.retrieval.eval.baseline_grep import run_grep_baseline
from meho_backplane.retrieval.eval.baseline_io import (
    compare_baseline,
    load_baseline,
    save_baseline,
)
from meho_backplane.retrieval.eval.corpus import (
    OperationCorpusQuery,
    load_corpus,
)
from meho_backplane.retrieval.eval.metrics import (
    GREEN_DEFAULTS,
    Thresholds,
    Verdict,
    coverage_at_k,
    mean_metric,
    precision_at_k,
    reciprocal_rank,
    verdict,
)
from meho_backplane.retrieval.eval.result_models import (
    EvalResult,
    QueryResult,
    RegressionEpsilon,
    SurfaceResult,
)
from meho_backplane.retrieval.retriever import RetrievalHit, retrieve

__all__ = [
    "DEFAULT_K",
    "EvalRequestSurface",
    "EvalResult",
    "QueryResult",
    "RegressionEpsilon",
    "RetrieveCallable",
    "SurfaceResult",
    "compare_baseline",
    "eval_all",
    "eval_surface",
    "load_baseline",
    "save_baseline",
]

#: Per-query top-k cap. Locked at 5 to mirror the precision@5 /
#: coverage@5 thresholds in the Initiative #373 contract; raising k
#: would also require re-tuning the green thresholds (a 0.80
#: precision@5 isn't the same gate as 0.80 precision@10).
DEFAULT_K: Final[int] = 5

#: Surface label accepted by the eval runner + the API request
#: schema. ``all`` is a meta-surface that runs every shipped corpus
#: in turn; the per-surface labels mirror the corpus loader's
#: ``Literal["kb", "memory", "operations"]``.
EvalRequestSurface = Literal["kb", "memory", "operations", "all"]


#: Type of the dispatch-time ``retrieve_fn`` callable. Defaults to the
#: production :func:`meho_backplane.retrieval.retriever.retrieve` but
#: tests pass a stub that returns synthetic hits without standing up
#: PG / fastembed. Using a typed alias keeps the runner signature
#: readable.
RetrieveCallable = Callable[..., Awaitable[list[RetrievalHit]]]


# ---------------------------------------------------------------------------
# Per-surface dispatchers — turn corpus rows + retrieve hits into ranked slugs
# ---------------------------------------------------------------------------


def _kb_hits_to_slugs(hits: list[RetrievalHit]) -> list[str]:
    """Map kb retrieval hits to slug strings (the corpus's expected_hits shape).

    The kb corpus's ``expected_hits`` is a list of ``source_id``
    values (slug = filename without ``.md``); :class:`RetrievalHit`
    carries ``source_id`` directly. The mapping is a one-liner kept
    here as a named function so the per-surface symmetry with the
    memory + operations dispatchers is obvious.
    """
    return [hit.source_id for hit in hits]


def _memory_hits_to_pairs(hits: list[RetrievalHit]) -> list[str]:
    """Map memory retrieval hits to ``"<scope>/<slug>"`` strings.

    Memory ``RetrievalHit.source_id`` is the chassis-internal
    ``"<scope>:<user_sub>:<slug>"`` shape (G5.1 #421); we reduce that
    to the ``(scope, slug)`` shape the corpus YAML promises (T4 #443
    ships the YAML, but the schema is locked in T1).
    """
    pairs: list[str] = []
    for hit in hits:
        # source_id format from G5 memory layer: ``<scope>:<sub>:<slug>``
        # for user-bound scopes, ``<scope>:<slug>`` for tenant/target.
        # Joining the first + last component yields the ``(scope, slug)``
        # shape regardless of intermediate disambiguation tokens.
        parts = hit.source_id.split(":")
        if len(parts) >= 2:
            pairs.append(f"{parts[0]}/{parts[-1]}")
        else:
            pairs.append(hit.source_id)
    return pairs


def _operations_hits_to_op_ids(hits: list[RetrievalHit]) -> list[str]:
    """Map operations retrieval hits to ``op_id`` strings.

    Operation surface ``RetrievalHit.source_id`` is the connector's
    ``op_id`` (e.g. ``GET:/api/vcenter/cluster``); the corpus's
    ``expected_op_ids`` field carries the same shape, so this is the
    identity mapping. Kept as a named function for surface symmetry
    + future op_id encoding changes (e.g. when G0.7's spec ingestion
    settles a stable shape).
    """
    return [hit.source_id for hit in hits]


def _format_memory_expected(pairs: list[tuple[str, str]]) -> list[str]:
    """Format a memory corpus's expected_hits into the "scope/slug" shape."""
    return [f"{scope}/{slug}" for scope, slug in pairs]


# ---------------------------------------------------------------------------
# Per-surface eval — the load-bearing surface-agnostic shape
# ---------------------------------------------------------------------------


async def _eval_query(
    *,
    query: str,
    expected: list[str],
    retrieve_fn: RetrieveCallable,
    tenant_id: uuid.UUID,
    source: str,
    hits_to_slugs: Callable[[list[RetrievalHit]], list[str]],
    baseline_corpus_root: Path | None,
    k: int,
) -> QueryResult:
    """Run one query against MEHO retrieval (+ optional baseline grep) and score it.

    The surface-agnostic core: takes the per-surface conversion
    callable + the tenant the corpus is scoped to, runs the
    in-process :func:`retrieve` helper with ``source=<surface>``, and
    folds the resulting hits into the three metrics. Baseline grep
    runs when *baseline_corpus_root* is provided; baseline failures
    surface as ``BaselineConfigError`` upstream rather than silently
    skipping (the runner translates that into a per-surface "baseline
    skipped" flag in the result).
    """
    hits = await retrieve_fn(
        tenant_id=tenant_id,
        query=query,
        source=source,
        limit=k,
    )
    meho_hits = hits_to_slugs(hits)

    p5 = precision_at_k(meho_hits, expected, k=k)
    mrr_q = reciprocal_rank(meho_hits, expected)
    cov = coverage_at_k(meho_hits, expected, k=k)

    baseline_hits: list[str] | None = None
    baseline_p5: float | None = None
    baseline_mrr: float | None = None
    baseline_cov: float | None = None
    if baseline_corpus_root is not None:
        baseline_hits = await run_grep_baseline(query, baseline_corpus_root, k=k)
        baseline_p5 = precision_at_k(baseline_hits, expected, k=k)
        baseline_mrr = reciprocal_rank(baseline_hits, expected)
        baseline_cov = coverage_at_k(baseline_hits, expected, k=k)

    return QueryResult(
        query=query,
        expected_hits=list(expected),
        meho_hits=meho_hits,
        precision_at_5=p5,
        reciprocal_rank=mrr_q,
        coverage_at_5=cov,
        baseline_hits=baseline_hits,
        baseline_precision_at_5=baseline_p5,
        baseline_reciprocal_rank=baseline_mrr,
        baseline_coverage_at_5=baseline_cov,
    )


def _aggregate_baseline(
    queries: list[QueryResult],
    thresholds: Thresholds,
) -> tuple[float, float, float, Verdict]:
    """Aggregate baseline numbers + verdict from queries that ran the baseline."""
    rows = [q for q in queries if q.baseline_hits is not None]
    p5 = mean_metric(q.baseline_precision_at_5 or 0.0 for q in rows)
    mrr_v = mean_metric(q.baseline_reciprocal_rank or 0.0 for q in rows)
    cov = mean_metric(q.baseline_coverage_at_5 or 0.0 for q in rows)
    v = verdict(precision=p5, mrr=mrr_v, coverage=cov, thresholds=thresholds)
    return p5, mrr_v, cov, v


def _aggregate_surface(
    *,
    surface: Literal["kb", "memory", "operations"],
    queries: list[QueryResult],
    thresholds: Thresholds,
    baseline_kind: Literal["grep"] | None,
) -> SurfaceResult:
    """Fold per-query rows into the surface aggregate + verdict.

    The MEHO-≥-baseline overlay (see :func:`_apply_baseline_check`)
    runs here so the verdict reflects both the absolute thresholds
    *and* the "MEHO must beat the operator's pre-MEHO workflow"
    constraint. An empty *queries* list returns the corpus-not-yet-
    shipped green per the module docstring.
    """
    if not queries:
        return SurfaceResult(
            surface=surface,
            query_count=0,
            precision_at_5=0.0,
            mrr=0.0,
            coverage=0.0,
            verdict="green",
            queries=[],
        )

    p5 = mean_metric(q.precision_at_5 for q in queries)
    mrr_agg = mean_metric(q.reciprocal_rank for q in queries)
    cov = mean_metric(q.coverage_at_5 for q in queries)
    base_verdict = verdict(precision=p5, mrr=mrr_agg, coverage=cov, thresholds=thresholds)

    baseline_p5: float | None = None
    baseline_mrr: float | None = None
    baseline_cov: float | None = None
    baseline_v: Verdict | None = None
    final_verdict = base_verdict

    if baseline_kind is not None:
        baseline_p5, baseline_mrr, baseline_cov, baseline_v = _aggregate_baseline(
            queries, thresholds
        )
        final_verdict = _apply_baseline_check(
            base_verdict=base_verdict,
            meho=(p5, mrr_agg, cov),
            baseline=(baseline_p5, baseline_mrr, baseline_cov),
        )

    return SurfaceResult(
        surface=surface,
        query_count=len(queries),
        precision_at_5=p5,
        mrr=mrr_agg,
        coverage=cov,
        verdict=final_verdict,
        baseline_kind=baseline_kind,
        baseline_precision_at_5=baseline_p5,
        baseline_mrr=baseline_mrr,
        baseline_coverage=baseline_cov,
        baseline_verdict=baseline_v,
        queries=queries,
    )


def _apply_baseline_check(
    *,
    base_verdict: Verdict,
    meho: tuple[float, float, float],
    baseline: tuple[float, float, float],
) -> Verdict:
    """Downgrade to red when MEHO is worse than baseline on any metric.

    Per the Initiative #373 retire-checklist criterion #4: "MEHO
    ranking ≥ baseline on every metric (kb: vs ``grep -r kb/``)".
    Any per-metric loss is enough to block retire and gate CI red,
    even if absolute thresholds are otherwise green.

    A small epsilon (1e-9) avoids tripping on floating-point
    drift when MEHO and baseline are mathematically equal but
    represented as very-close floats.
    """
    if any(m < b - 1e-9 for m, b in zip(meho, baseline, strict=True)):
        return "red"
    return base_verdict


async def _eval_kb(
    *,
    retrieve_fn: RetrieveCallable,
    tenant_id: uuid.UUID,
    baseline_corpus_root: Path | None,
    thresholds: Thresholds,
    k: int,
) -> SurfaceResult:
    """Eval the kb surface — calls the kb corpus loader + the dispatcher."""
    rows = load_corpus("kb")
    queries = [
        await _eval_query(
            query=row.query,
            expected=list(row.expected_hits),
            retrieve_fn=retrieve_fn,
            tenant_id=tenant_id,
            source="kb",
            hits_to_slugs=_kb_hits_to_slugs,
            baseline_corpus_root=baseline_corpus_root,
            k=k,
        )
        for row in rows
    ]
    baseline_kind: Literal["grep"] | None = "grep" if baseline_corpus_root else None
    return _aggregate_surface(
        surface="kb", queries=queries, thresholds=thresholds, baseline_kind=baseline_kind
    )


async def _eval_memory(
    *,
    retrieve_fn: RetrieveCallable,
    tenant_id: uuid.UUID,
    thresholds: Thresholds,
    k: int,
) -> SurfaceResult:
    """Eval the memory surface — empty corpus until T4 #443 ships."""
    rows = load_corpus("memory")
    queries = [
        await _eval_query(
            query=row.query,
            expected=_format_memory_expected(list(row.expected_hits)),
            retrieve_fn=retrieve_fn,
            tenant_id=tenant_id,
            source="memory",
            hits_to_slugs=_memory_hits_to_pairs,
            baseline_corpus_root=None,
            k=k,
        )
        for row in rows
    ]
    return _aggregate_surface(
        surface="memory", queries=queries, thresholds=thresholds, baseline_kind=None
    )


async def _eval_operations(
    *,
    retrieve_fn: RetrieveCallable,
    tenant_id: uuid.UUID,
    thresholds: Thresholds,
    k: int,
) -> SurfaceResult:
    """Eval the operations surface — empty corpus until T3 #442 ships."""
    rows: list[OperationCorpusQuery] = load_corpus("operations")
    queries: list[QueryResult] = []
    for row in rows:
        # Operations corpus carries expected_op_ids per-row; the
        # connector_id filtering is the runner's responsibility (the
        # ``source="operations"`` filter narrows to that surface, but
        # not to a specific connector — that's what the corpus
        # constraint enforces).
        queries.append(
            await _eval_query(
                query=row.query,
                expected=list(row.expected_op_ids),
                retrieve_fn=retrieve_fn,
                tenant_id=tenant_id,
                source="operations",
                hits_to_slugs=_operations_hits_to_op_ids,
                baseline_corpus_root=None,
                k=k,
            )
        )
    return _aggregate_surface(
        surface="operations", queries=queries, thresholds=thresholds, baseline_kind=None
    )


# ---------------------------------------------------------------------------
# Public entrypoints
# ---------------------------------------------------------------------------


async def eval_surface(
    surface: Literal["kb", "memory", "operations"],
    *,
    tenant_id: uuid.UUID,
    retrieve_fn: RetrieveCallable | None = None,
    baseline_corpus_root: Path | None = None,
    thresholds: Thresholds = GREEN_DEFAULTS,
    k: int = DEFAULT_K,
) -> SurfaceResult:
    """Run the eval against a single surface.

    *retrieve_fn* defaults to the production
    :func:`meho_backplane.retrieval.retriever.retrieve`; tests pass a
    stub. *baseline_corpus_root* is honoured for the kb surface only
    (memory/operations baselines are deferred per module docstring) —
    pass on memory/operations and the runner silently ignores it.

    Raises
    ------
    BaselineConfigError
        Propagated from :func:`run_grep_baseline` when the baseline
        was requested for a kb surface but the corpus root is missing
        / empty / unreadable. The CLI / API translate this into an
        operator-facing error rather than a silent fallback.
    """
    rfn: RetrieveCallable = retrieve_fn if retrieve_fn is not None else retrieve

    if surface == "kb":
        return await _eval_kb(
            retrieve_fn=rfn,
            tenant_id=tenant_id,
            baseline_corpus_root=baseline_corpus_root,
            thresholds=thresholds,
            k=k,
        )
    if surface == "memory":
        return await _eval_memory(
            retrieve_fn=rfn,
            tenant_id=tenant_id,
            thresholds=thresholds,
            k=k,
        )
    return await _eval_operations(
        retrieve_fn=rfn,
        tenant_id=tenant_id,
        thresholds=thresholds,
        k=k,
    )


async def eval_all(
    *,
    tenant_id: uuid.UUID,
    retrieve_fn: RetrieveCallable | None = None,
    baseline_corpus_root: Path | None = None,
    thresholds: Thresholds = GREEN_DEFAULTS,
    k: int = DEFAULT_K,
    surfaces: list[Literal["kb", "memory", "operations"]] | None = None,
) -> EvalResult:
    """Run the eval against every requested surface; return the aggregate.

    *surfaces* defaults to all three; pass an explicit subset to scope
    the run (e.g. ``["kb"]`` for the CI gate that doesn't yet have
    memory/operations corpora).

    The overall verdict is the worst of the per-surface verdicts —
    a single red surface flips the whole eval red. Empty surfaces
    (T3/T4 corpus YAML hasn't shipped) contribute their green per
    module docstring; they don't suppress reds elsewhere.
    """
    log = structlog.get_logger()
    chosen: list[Literal["kb", "memory", "operations"]] = surfaces or ["kb", "memory", "operations"]
    started = datetime.now(UTC)

    surface_results: list[SurfaceResult] = []
    for s in chosen:
        result = await eval_surface(
            s,
            tenant_id=tenant_id,
            retrieve_fn=retrieve_fn,
            baseline_corpus_root=baseline_corpus_root,
            thresholds=thresholds,
            k=k,
        )
        surface_results.append(result)
        log.info(
            "retrieval_eval_surface_complete",
            surface=s,
            query_count=result.query_count,
            precision_at_5=result.precision_at_5,
            mrr=result.mrr,
            coverage=result.coverage,
            verdict=result.verdict,
        )

    overall = _worst_verdict([r.verdict for r in surface_results])
    return EvalResult(
        ran_at=started,
        surfaces=surface_results,
        overall_verdict=overall,
        thresholds=thresholds,
    )


def _worst_verdict(verdicts: list[Verdict]) -> Verdict:
    """Return the worst (most-red) verdict across *verdicts*; green if empty."""
    if "red" in verdicts:
        return "red"
    if "yellow" in verdicts:
        return "yellow"
    return "green"
