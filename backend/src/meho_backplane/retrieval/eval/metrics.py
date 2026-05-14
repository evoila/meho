# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pure scoring functions for the retrieval-eval runner (G4.3-T2, #441).

Three IR-standard metrics computed per query and aggregated across the
corpus:

* **precision@k** — of the top-``k`` ranked hits, how many appear in the
  ground-truth set. Per-query value in ``[0.0, 1.0]``; the corpus
  aggregate is the mean across queries.
* **MRR (Mean Reciprocal Rank)** — for each query, the reciprocal of
  the 1-based rank of the first ground-truth hit (``0.0`` when no
  ground-truth hit appears in the ranked list). Aggregate is the mean
  across queries; bounded above by ``1.0``.
* **coverage** — fraction of queries where at least one ground-truth
  hit appeared in the top-``k`` ranked list (``recall@k`` collapsed
  to a per-query 0/1 indicator). Aggregate is the share of queries
  with ``coverage_q == 1``.

The threshold contract from Initiative #373 + Task #441 (the eval
runner's CI gate verdict):

    Green:    precision@5 >= 0.80  AND  MRR >= 0.50  AND  coverage >= 0.90
    Yellow:   any metric below green but >= 70% of green
    Red:      any metric below 70% of green threshold

Encoded as :class:`Thresholds` + :func:`verdict`. Pure, deterministic,
no I/O — every gate decision is reproducible from the same numbers.

Why pure functions instead of a metrics class
---------------------------------------------

The runner orchestrates per-query work and folds the scores into a
``SurfaceResult`` (see ``runner.py``). Splitting the math out:

* Lets unit tests pin every edge case (k > len(hits), no expected
  hits, all expected hits, etc.) without standing up the runner.
* Keeps the runner free to dispatch work concurrently — pure
  functions are trivially parallelisable.
* Mirrors the convention :mod:`meho_backplane.retrieval.retriever`
  established for ``_rrf_fuse`` (pure helper, behaviour-tested in
  isolation).

Why mean instead of micro-averaging
-----------------------------------

precision@k, MRR, and coverage all aggregate via macro-mean (one
score per query, average across queries) rather than micro-averaging
(sum hits / sum slots). The corpus is hand-curated and small (10
queries per surface in v0.2); the macro-mean weights every query
equally so a single high-cardinality query can't drown out the rest.
TREC-style retrieval evaluation has used macro-mean for the same
reason since the 1990s.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "GREEN_DEFAULTS",
    "YELLOW_FLOOR_RATIO",
    "Thresholds",
    "Verdict",
    "coverage_at_k",
    "mean_metric",
    "precision_at_k",
    "reciprocal_rank",
    "verdict",
]


#: Verdict band for a per-surface eval result. Three-state by design —
#: a binary green/red would lose the early-warning signal that lets
#: operators react before a regression hits the CI gate.
Verdict = Literal["green", "yellow", "red"]


#: Ratio of the green threshold below which a metric flips from yellow
#: to red. From the Initiative #373 / Task #441 threshold contract:
#: "any metric below 70% of green threshold" is red. Encoded as a
#: module-level constant so the verdict math is grep-able and the
#: tests can pin the exact boundary.
YELLOW_FLOOR_RATIO: Final[float] = 0.70


class Thresholds(BaseModel):
    """Per-metric green-band thresholds for a verdict computation.

    Frozen so the threshold object passed into :func:`verdict` cannot
    be mutated mid-run (a stale or rewritten threshold mid-eval would
    invalidate every subsequent verdict and silently shift the CI
    gate). The yellow floor is derived from :data:`YELLOW_FLOOR_RATIO`,
    not stored — keeping it derived means the contract "yellow is 70%
    of green" can't drift between green-threshold customisation and
    the yellow boundary.

    Defaults match the Initiative #373 / Task #441 contract; tests and
    future re-tunings construct ``Thresholds(...)`` with explicit
    values.
    """

    model_config = ConfigDict(frozen=True)

    # ge=0.0 / le=1.0 because every metric here is a [0, 1] proportion.
    # Encoded at the schema level so a typo (`precision_at_5=8.0`
    # instead of `0.8`) fails fast rather than producing a perma-red
    # verdict.
    precision_at_5: float = Field(default=0.80, ge=0.0, le=1.0)
    mrr: float = Field(default=0.50, ge=0.0, le=1.0)
    coverage: float = Field(default=0.90, ge=0.0, le=1.0)


#: Module-level singleton matching the Initiative #373 contract. Most
#: callers want this directly; ``Thresholds(precision_at_5=0.85, ...)``
#: is the escape hatch when a surface needs different bars (currently
#: unused, but the shape is locked).
GREEN_DEFAULTS: Final[Thresholds] = Thresholds()


def precision_at_k(
    ranked_hits: Sequence[str],
    expected: Iterable[str],
    *,
    k: int = 5,
) -> float:
    """Fraction of the top-``k`` ranked hits that appear in *expected*.

    Per-query metric in ``[0.0, 1.0]``. The denominator is ``min(k,
    len(ranked_hits))`` so a query with fewer than ``k`` results is
    not penalised by missing slots — the correct interpretation of
    precision@k when the system returns less than ``k`` is "of what
    it returned, how much was right". Treating absent slots as wrong
    would conflate retrieval failure (returning nothing) with
    precision failure (returning the wrong things), and the coverage
    metric below already captures the "returns nothing" case.

    Returns ``0.0`` when *ranked_hits* is empty (no slots to score).

    Parameters
    ----------
    ranked_hits
        Identifiers from the system's top-``k`` ranked response, in
        rank order (best first). For the kb surface these are
        ``documents.source_id`` slugs; for memory ``(scope, slug)``
        joined strings; for operations ``(connector_id, op_id)``
        joined strings — the runner is responsible for the per-surface
        formatting so this helper stays surface-agnostic.
    expected
        Ground-truth identifier set from the corpus. Set membership
        is what matters; order is ignored (MRR is the order-aware
        metric).
    k
        Number of top-ranked slots to score. Default 5 — matches the
        Initiative #373 threshold contract; raised to 10 / 20 only
        when reasoning about wider-recall scenarios.

    Raises
    ------
    ValueError
        ``k <= 0`` — precision@0 is undefined; raise rather than return
        ``0.0`` so a typo at the call site surfaces immediately.
    """
    if k <= 0:
        raise ValueError(f"k must be > 0; got {k}")
    if not ranked_hits:
        return 0.0

    expected_set = set(expected)
    top_k = ranked_hits[:k]
    matches = sum(1 for hit in top_k if hit in expected_set)
    # Denominator is len(top_k), not k — a system returning 3 hits and
    # getting all 3 right scores 1.0, not 0.6. See module docstring.
    return matches / len(top_k)


def reciprocal_rank(
    ranked_hits: Sequence[str],
    expected: Iterable[str],
) -> float:
    """Reciprocal of the 1-based rank of the first ground-truth hit.

    ``1.0`` when the top-1 hit is in *expected*; ``0.5`` for rank-2;
    ``0.333…`` for rank-3, etc. Returns ``0.0`` when no ground-truth
    hit appears anywhere in *ranked_hits*. The aggregate across a
    corpus is the **MRR** (mean reciprocal rank) — the runner takes
    the macro-mean.

    There is no ``k`` cap here because MRR is informative beyond k=5
    (a hit at rank 7 still scores 0.143, useful for "ranking is in
    the right neighbourhood" signal). Callers who want a capped
    version can pass ``ranked_hits[:k]`` themselves.

    Parameters
    ----------
    ranked_hits
        Same shape as :func:`precision_at_k`'s ranked_hits.
    expected
        Same shape as :func:`precision_at_k`'s expected.
    """
    expected_set = set(expected)
    for rank0, hit in enumerate(ranked_hits):
        if hit in expected_set:
            return 1.0 / (rank0 + 1)
    return 0.0


def coverage_at_k(
    ranked_hits: Sequence[str],
    expected: Iterable[str],
    *,
    k: int = 5,
) -> float:
    """1.0 when at least one expected hit lands in the top ``k``, else 0.0.

    A 0/1 per-query indicator (recall@k collapsed to "any hit at all").
    The runner aggregates across queries by mean to produce the
    surface coverage — the share of queries where the system
    surfaced *something* relevant.

    Coverage is the load-bearing "is the operator stuck with grep"
    signal: a system with high precision and high MRR but coverage
    < 0.5 is one where half of all queries return nothing useful, no
    matter how good the rest are.

    Parameters
    ----------
    ranked_hits
        Same shape as :func:`precision_at_k`'s ranked_hits.
    expected
        Same shape as :func:`precision_at_k`'s expected.
    k
        Top-``k`` cap, mirroring :func:`precision_at_k`. Default 5.

    Raises
    ------
    ValueError
        ``k <= 0`` — coverage@0 is undefined.
    """
    if k <= 0:
        raise ValueError(f"k must be > 0; got {k}")
    expected_set = set(expected)
    return 1.0 if any(hit in expected_set for hit in ranked_hits[:k]) else 0.0


def mean_metric(values: Iterable[float]) -> float:
    """Macro-mean of *values*; ``0.0`` for an empty input.

    Centralises the "0.0 instead of NaN on empty corpus" choice so the
    three per-surface aggregates use the same convention. NaN would
    propagate into the verdict computation as `<` NaN comparisons
    that always evaluate False, silently flipping every threshold
    check to a yellow / red verdict for the wrong reason.
    """
    values_list = list(values)
    if not values_list:
        return 0.0
    return sum(values_list) / len(values_list)


def verdict(
    *,
    precision: float,
    mrr: float,
    coverage: float,
    thresholds: Thresholds = GREEN_DEFAULTS,
) -> Verdict:
    """Classify a metrics tuple into the green / yellow / red bands.

    Decision tree (per the Initiative #373 / Task #441 contract):

    * Every metric ≥ its green threshold → ``"green"``.
    * Any metric below green but every metric ≥
      :data:`YELLOW_FLOOR_RATIO` * green → ``"yellow"``.
    * Any metric below :data:`YELLOW_FLOOR_RATIO` * green → ``"red"``.

    The bands are computed metric-by-metric and the worst wins (so
    one red metric is enough to flip the entire surface to red). This
    matches the operator intuition "the surface is only as healthy
    as its weakest signal" and aligns with the `meho retrieval
    eval --compare-baseline` regression detection — a single metric
    crossing the red boundary is what gates merge in CI.

    Parameters
    ----------
    precision
        Aggregated precision@5 across the corpus (macro-mean).
    mrr
        Aggregated MRR across the corpus.
    coverage
        Aggregated coverage@5 across the corpus.
    thresholds
        Per-metric green thresholds. Defaults to :data:`GREEN_DEFAULTS`.
    """
    metrics = (
        ("precision_at_5", precision, thresholds.precision_at_5),
        ("mrr", mrr, thresholds.mrr),
        ("coverage", coverage, thresholds.coverage),
    )

    # Single pass: track whether any metric is below green and whether
    # any is below the yellow floor. Two booleans is cheaper than three
    # passes and keeps the worst-wins logic local.
    any_below_green = False
    any_below_red_floor = False
    for _name, value, green in metrics:
        if value < green:
            any_below_green = True
            if value < green * YELLOW_FLOOR_RATIO:
                any_below_red_floor = True

    if any_below_red_floor:
        return "red"
    if any_below_green:
        return "yellow"
    return "green"
