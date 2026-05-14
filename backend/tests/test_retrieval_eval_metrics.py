# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.retrieval.eval.metrics`.

Coverage matrix (G4.3-T2 / Task #441 acceptance criteria):

* :func:`precision_at_k` — empty hits, k > len(hits), all-correct,
  partial, none-correct, ``k <= 0`` raises.
* :func:`reciprocal_rank` — top-1, mid-rank, no hits, exact ground
  truth in / not in ranked list.
* :func:`coverage_at_k` — at-least-one-hit / no-hit / boundary
  (hit at exactly position k vs k+1).
* :func:`mean_metric` — empty input → 0.0 (NaN-safety).
* :func:`verdict` — green / yellow / red bands per the threshold
  contract; worst-metric-wins (one red metric flips the surface).
* :class:`Thresholds` — frozen + bounded validation.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from meho_backplane.retrieval.eval.metrics import (
    GREEN_DEFAULTS,
    YELLOW_FLOOR_RATIO,
    Thresholds,
    coverage_at_k,
    mean_metric,
    precision_at_k,
    reciprocal_rank,
    verdict,
)

# ---------------------------------------------------------------------------
# precision_at_k
# ---------------------------------------------------------------------------


def test_precision_at_k_empty_hits_returns_zero() -> None:
    """No ranked hits → precision is 0.0 (no slots to score)."""
    assert precision_at_k([], ["expected-a"], k=5) == 0.0


def test_precision_at_k_all_hits_in_expected_returns_one() -> None:
    """Every top-k hit in the expected set → precision = 1.0."""
    hits = ["a", "b", "c"]
    assert precision_at_k(hits, ["a", "b", "c", "d"], k=5) == 1.0


def test_precision_at_k_partial_returns_fraction_of_top_k() -> None:
    """Two of five top-k hits in expected → 0.4."""
    hits = ["good-a", "bad-1", "good-b", "bad-2", "bad-3"]
    expected = ["good-a", "good-b"]
    assert precision_at_k(hits, expected, k=5) == pytest.approx(0.4)


def test_precision_at_k_none_correct_returns_zero() -> None:
    """No top-k hit in expected → 0.0."""
    hits = ["bad-1", "bad-2", "bad-3"]
    assert precision_at_k(hits, ["expected-a"], k=5) == 0.0


def test_precision_at_k_fewer_hits_than_k_uses_actual_length_as_denominator() -> None:
    """Three hits, all correct, k=5 → 1.0 (not 0.6).

    The "what fraction of what we returned was right" semantics —
    rather than penalising the system for missing slots (a coverage
    concern, not a precision concern).
    """
    hits = ["a", "b", "c"]
    assert precision_at_k(hits, ["a", "b", "c"], k=5) == 1.0


def test_precision_at_k_k_larger_than_hits_does_not_exceed_one() -> None:
    """Two of two top-k hits correct, k=10 → 1.0 (bounded above)."""
    hits = ["a", "b"]
    assert precision_at_k(hits, ["a", "b", "c"], k=10) == 1.0


def test_precision_at_k_zero_k_raises_value_error() -> None:
    """k=0 is undefined; raise rather than silently return 0.0."""
    with pytest.raises(ValueError, match="k must be > 0"):
        precision_at_k(["a"], ["a"], k=0)


def test_precision_at_k_negative_k_raises_value_error() -> None:
    """k<0 is also rejected; symmetric with the k=0 case."""
    with pytest.raises(ValueError, match="k must be > 0"):
        precision_at_k(["a"], ["a"], k=-1)


# ---------------------------------------------------------------------------
# reciprocal_rank
# ---------------------------------------------------------------------------


def test_reciprocal_rank_top_1_hit_returns_one() -> None:
    """Top-1 hit in expected set → RR = 1.0."""
    assert reciprocal_rank(["a", "x", "y"], ["a"]) == 1.0


def test_reciprocal_rank_rank_2_returns_one_half() -> None:
    """First match at rank 2 → RR = 0.5."""
    assert reciprocal_rank(["bad", "good", "bad-2"], ["good"]) == 0.5


def test_reciprocal_rank_rank_3_returns_one_third() -> None:
    """First match at rank 3 → RR = 1/3."""
    assert reciprocal_rank(["a", "b", "c"], ["c"]) == pytest.approx(1.0 / 3.0)


def test_reciprocal_rank_no_hit_in_ranked_returns_zero() -> None:
    """No ground-truth hit anywhere → RR = 0.0."""
    assert reciprocal_rank(["x", "y", "z"], ["a", "b"]) == 0.0


def test_reciprocal_rank_only_first_hit_counts() -> None:
    """Multiple hits in ranked list — only the first counts (canonical MRR)."""
    # Hits at rank 2 and 4; result should be 0.5, not the sum.
    assert reciprocal_rank(["bad", "good-1", "bad", "good-2"], ["good-1", "good-2"]) == 0.5


def test_reciprocal_rank_empty_ranked_returns_zero() -> None:
    """Empty ranked list → 0.0 (no candidates to score)."""
    assert reciprocal_rank([], ["a"]) == 0.0


# ---------------------------------------------------------------------------
# coverage_at_k
# ---------------------------------------------------------------------------


def test_coverage_at_k_one_hit_in_top_k_returns_one() -> None:
    """At least one expected hit in top-k → 1.0."""
    assert coverage_at_k(["bad", "good", "bad"], ["good"], k=5) == 1.0


def test_coverage_at_k_no_hit_returns_zero() -> None:
    """No expected hit in top-k → 0.0."""
    assert coverage_at_k(["bad-1", "bad-2"], ["good"], k=5) == 0.0


def test_coverage_at_k_hit_at_exactly_k_counts() -> None:
    """Hit at rank k (1-indexed) is still inside the slice."""
    hits = ["bad", "bad", "bad", "bad", "good"]  # 5th = rank 5
    assert coverage_at_k(hits, ["good"], k=5) == 1.0


def test_coverage_at_k_hit_at_k_plus_one_does_not_count() -> None:
    """Hit at rank k+1 is outside the slice → 0.0."""
    hits = ["bad", "bad", "bad", "bad", "bad", "good"]  # 6th = rank 6
    assert coverage_at_k(hits, ["good"], k=5) == 0.0


def test_coverage_at_k_zero_k_raises_value_error() -> None:
    """k=0 is undefined."""
    with pytest.raises(ValueError, match="k must be > 0"):
        coverage_at_k(["a"], ["a"], k=0)


# ---------------------------------------------------------------------------
# mean_metric
# ---------------------------------------------------------------------------


def test_mean_metric_empty_returns_zero() -> None:
    """Empty corpus → 0.0 (not NaN; NaN would silently flip verdict)."""
    assert mean_metric([]) == 0.0


def test_mean_metric_simple_average() -> None:
    """Mean of [1.0, 0.0, 0.5] = 0.5."""
    assert mean_metric([1.0, 0.0, 0.5]) == 0.5


def test_mean_metric_singleton_returns_value() -> None:
    """Single-element mean is the value itself."""
    assert mean_metric([0.42]) == 0.42


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------


def test_verdict_all_at_green_threshold_is_green() -> None:
    """Every metric exactly at the green threshold → green (>= boundary)."""
    assert verdict(precision=0.80, mrr=0.50, coverage=0.90) == "green"


def test_verdict_all_above_green_is_green() -> None:
    """Every metric above green → green."""
    assert verdict(precision=0.95, mrr=0.80, coverage=1.0) == "green"


def test_verdict_one_metric_in_yellow_band_is_yellow() -> None:
    """One metric below green but above yellow floor → yellow."""
    # precision=0.70 is below green (0.80) but above 0.56 (0.80 * 0.70).
    assert verdict(precision=0.70, mrr=0.50, coverage=0.90) == "yellow"


def test_verdict_one_metric_below_yellow_floor_is_red() -> None:
    """One metric below 70% of green → red regardless of others."""
    # precision=0.50 is below 0.56 (0.80 * 0.70) → red.
    assert verdict(precision=0.50, mrr=0.50, coverage=0.90) == "red"


def test_verdict_mrr_below_red_floor_flips_to_red() -> None:
    """MRR below 70% of green floor (0.50 * 0.70 = 0.35) → red."""
    assert verdict(precision=0.95, mrr=0.30, coverage=1.0) == "red"


def test_verdict_coverage_below_red_floor_flips_to_red() -> None:
    """Coverage below 70% of green floor (0.90 * 0.70 = 0.63) → red."""
    assert verdict(precision=0.95, mrr=0.80, coverage=0.50) == "red"


def test_verdict_worst_metric_wins() -> None:
    """One red metric flips the whole verdict — others can be green."""
    # coverage=0.30 << 0.63 (red); precision/MRR are fine.
    assert verdict(precision=1.0, mrr=1.0, coverage=0.30) == "red"


def test_verdict_yellow_floor_is_inclusive() -> None:
    """Metric exactly at 70% of green is yellow, not red."""
    # precision = 0.80 * 0.70 = 0.56 — still in yellow band (>= floor).
    assert verdict(precision=0.56, mrr=0.50, coverage=0.90) == "yellow"


def test_verdict_custom_thresholds_are_honoured() -> None:
    """A stricter threshold flips a previously-green result to yellow/red."""
    strict = Thresholds(precision_at_5=0.95, mrr=0.80, coverage=0.95)
    # 0.80 was green under defaults, yellow under strict (still ≥ 0.95 * 0.70).
    assert verdict(precision=0.80, mrr=0.85, coverage=0.95, thresholds=strict) == "yellow"


def test_verdict_yellow_floor_ratio_is_seventy_percent() -> None:
    """Sanity check on the documented YELLOW_FLOOR_RATIO constant."""
    assert YELLOW_FLOOR_RATIO == 0.70


# ---------------------------------------------------------------------------
# Thresholds schema discipline
# ---------------------------------------------------------------------------


def test_thresholds_default_matches_initiative_contract() -> None:
    """Defaults match the Initiative #373 contract."""
    assert GREEN_DEFAULTS.precision_at_5 == 0.80
    assert GREEN_DEFAULTS.mrr == 0.50
    assert GREEN_DEFAULTS.coverage == 0.90


def test_thresholds_is_frozen() -> None:
    """Thresholds are immutable post-construction."""
    t = Thresholds()
    with pytest.raises(ValidationError):
        t.precision_at_5 = 0.5  # type: ignore[misc]


def test_thresholds_rejects_out_of_range_values() -> None:
    """Bounds enforced by Pydantic — 8.0 is the typo, not 0.8."""
    with pytest.raises(ValidationError):
        Thresholds(precision_at_5=8.0)


def test_thresholds_rejects_negative_values() -> None:
    """No metric can be < 0."""
    with pytest.raises(ValidationError):
        Thresholds(mrr=-0.1)
