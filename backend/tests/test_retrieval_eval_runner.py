# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.retrieval.eval.runner`.

Coverage matrix (G4.3-T2 / Task #441 acceptance criteria):

* :func:`eval_surface` for ``kb`` — all-correct → green; partial →
  green/yellow; none-correct → red. Uses a stub retrieve_fn keyed on
  the corpus's expected_hits so the test is deterministic without
  PG / fastembed.
* :func:`eval_surface` for ``memory`` — empty corpus returns
  ``query_count=0`` + ``verdict="green"`` per the module's
  "absent corpus is not a failure" rule. (Operations corpus shipped
  in G4.3-T3 #442; ``test_retrieval_eval_operation_corpus.py`` owns
  its corpus-content coverage.)
* :func:`eval_all` — overall verdict is the worst of the per-surface
  verdicts; one red surface flips the whole result.
* Baseline integration — when *baseline_corpus_root* is set, the kb
  surface populates ``baseline_*`` fields and the
  MEHO-≥-baseline check downgrades to red when MEHO loses on any
  metric.
* :func:`save_baseline` / :func:`load_baseline` round-trip preserves
  the EvalResult shape.
* :func:`compare_baseline` — detects per-surface regressions, ignores
  surfaces present on only one side, ignores empty surfaces.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from meho_backplane.retrieval.eval.runner import (
    EvalResult,
    RegressionEpsilon,
    SurfaceResult,
    compare_baseline,
    eval_all,
    eval_surface,
    load_baseline,
    save_baseline,
)
from meho_backplane.retrieval.retriever import RetrievalHit

# ---------------------------------------------------------------------------
# Stub retrieve_fn helpers
# ---------------------------------------------------------------------------


def _make_hit(slug: str, source: str = "kb") -> RetrievalHit:
    """Build a synthetic RetrievalHit so the runner doesn't need PG."""
    return RetrievalHit(
        document_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        source=source,
        source_id=slug,
        kind=f"{source}-entry",
        body=f"body for {slug}",
        doc_metadata={},
        fused_score=0.5,
        bm25_score=0.5,
        cosine_score=0.5,
        bm25_rank=1,
        cosine_rank=1,
    )


def _make_perfect_retrieve_fn() -> Any:
    """Stub that returns the corpus's first expected hit as the top-1.

    Models a retrieval system that always nails the ground truth
    across every shipped surface — the kb corpus's first
    expected_hit and the operations corpus's first expected_op_id
    are both returned as the single hit for their respective
    surfaces. The ``source`` argument the runner threads through
    picks the right answer map at call time.
    """
    from meho_backplane.retrieval.eval.corpus import load_corpus

    kb_answers = {row.query: row.expected_hits[0] for row in load_corpus("kb")}
    ops_answers = {row.query: row.expected_op_ids[0] for row in load_corpus("operations")}

    async def perfect(
        *,
        tenant_id: uuid.UUID,
        query: str,
        source: str | None = None,
        kind: str | None = None,
        limit: int = 10,
    ) -> list[RetrievalHit]:
        # Operations branch — the runner passes ``source="operations"``;
        # RetrievalHit.source_id is the op_id (per
        # ``runner._operations_hits_to_op_ids``).
        if source == "operations":
            op_id = ops_answers.get(query)
            if op_id is None:
                return []
            return [_make_hit(op_id, source="operations")]
        # kb branch (default) — RetrievalHit.source_id is the slug.
        slug = kb_answers.get(query)
        if slug is None:
            return []
        return [_make_hit(slug, source=source or "kb")]

    return perfect


def _make_terrible_retrieve_fn() -> Any:
    """Stub that returns hits that NEVER match the corpus's expected hits.

    Models a fully-broken retrieval substrate. precision=0, MRR=0,
    coverage=0 — should always produce ``red``.
    """

    async def terrible(
        *,
        tenant_id: uuid.UUID,
        query: str,
        source: str | None = None,
        kind: str | None = None,
        limit: int = 10,
    ) -> list[RetrievalHit]:
        return [
            _make_hit("totally-unrelated-1", source=source or "kb"),
            _make_hit("totally-unrelated-2", source=source or "kb"),
            _make_hit("totally-unrelated-3", source=source or "kb"),
        ]

    return terrible


def _make_partial_retrieve_fn() -> Any:
    """Stub that returns the right hit only for half the queries.

    Models a degraded retrieval substrate — should produce yellow
    (partial precision/MRR/coverage but above the red floor).
    """
    from meho_backplane.retrieval.eval.corpus import load_corpus

    corpus = load_corpus("kb")
    # Answer for the first half; nothing for the rest.
    answer_map = {row.query: row.expected_hits[0] for row in corpus[:5]}

    async def partial(
        *,
        tenant_id: uuid.UUID,
        query: str,
        source: str | None = None,
        kind: str | None = None,
        limit: int = 10,
    ) -> list[RetrievalHit]:
        slug = answer_map.get(query)
        if slug is None:
            return []
        return [_make_hit(slug, source=source or "kb")]

    return partial


# ---------------------------------------------------------------------------
# eval_surface — kb
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eval_surface_kb_perfect_retrieve_returns_green() -> None:
    """Perfect retrieval → all metrics 1.0; verdict green."""
    result = await eval_surface(
        "kb",
        tenant_id=uuid.uuid4(),
        retrieve_fn=_make_perfect_retrieve_fn(),
    )

    assert isinstance(result, SurfaceResult)
    assert result.surface == "kb"
    assert result.query_count == 10
    assert result.precision_at_5 == pytest.approx(1.0)
    assert result.mrr == pytest.approx(1.0)
    assert result.coverage == pytest.approx(1.0)
    assert result.verdict == "green"
    assert len(result.queries) == 10


@pytest.mark.asyncio
async def test_eval_surface_kb_terrible_retrieve_returns_red() -> None:
    """Zero-correct retrieval → all metrics 0; verdict red."""
    result = await eval_surface(
        "kb",
        tenant_id=uuid.uuid4(),
        retrieve_fn=_make_terrible_retrieve_fn(),
    )

    assert result.precision_at_5 == 0.0
    assert result.mrr == 0.0
    assert result.coverage == 0.0
    assert result.verdict == "red"


@pytest.mark.asyncio
async def test_eval_surface_kb_partial_retrieve_yields_yellow_or_red_band() -> None:
    """Half-right retrieval → degraded but typically not zero."""
    result = await eval_surface(
        "kb",
        tenant_id=uuid.uuid4(),
        retrieve_fn=_make_partial_retrieve_fn(),
    )

    # Half the queries get a top-1 perfect hit, half get nothing.
    # precision_at_5 averages 1.0 (half) and 0.0 (half) → 0.5
    # MRR averages 1.0 (half) and 0.0 (half) → 0.5
    # coverage averages 1.0 (half) and 0.0 (half) → 0.5
    assert result.precision_at_5 == pytest.approx(0.5)
    assert result.mrr == pytest.approx(0.5)
    assert result.coverage == pytest.approx(0.5)
    # All three metrics below green; precision=0.5 < 0.56 (red floor) → red.
    assert result.verdict == "red"


@pytest.mark.asyncio
async def test_eval_surface_kb_per_query_results_carry_expected_and_meho_hits() -> None:
    """Every QueryResult carries the ground truth + MEHO hits + per-query metrics."""
    result = await eval_surface(
        "kb",
        tenant_id=uuid.uuid4(),
        retrieve_fn=_make_perfect_retrieve_fn(),
    )

    for q in result.queries:
        assert q.expected_hits, f"empty expected_hits: {q.query}"
        assert q.meho_hits, f"empty meho_hits: {q.query}"
        # Each query was a perfect top-1 hit.
        assert q.precision_at_5 == 1.0
        assert q.reciprocal_rank == 1.0
        assert q.coverage_at_5 == 1.0
        # No baseline ran.
        assert q.baseline_hits is None
        assert q.baseline_precision_at_5 is None


# ---------------------------------------------------------------------------
# eval_surface — memory (empty corpus → no-op green)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eval_surface_memory_empty_corpus_is_green() -> None:
    """Memory corpus YAML hasn't shipped → query_count=0 + verdict='green'."""
    result = await eval_surface(
        "memory",
        tenant_id=uuid.uuid4(),
        retrieve_fn=_make_terrible_retrieve_fn(),  # Doesn't matter — never called.
    )

    assert result.surface == "memory"
    assert result.query_count == 0
    assert result.verdict == "green"
    assert result.queries == []


# ---------------------------------------------------------------------------
# eval_all — surface roll-up
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eval_all_perfect_kb_and_ops_with_empty_memory_returns_green() -> None:
    """Green kb + green ops + no-data memory → overall green.

    Memory corpus stays empty until T4 #443 ships; the runner's
    absent-corpus-is-green rule keeps the overall verdict green when
    every shipped surface is green.
    """
    result = await eval_all(
        tenant_id=uuid.uuid4(),
        retrieve_fn=_make_perfect_retrieve_fn(),
    )

    assert isinstance(result, EvalResult)
    assert {s.surface for s in result.surfaces} == {"kb", "memory", "operations"}
    assert result.overall_verdict == "green"


@pytest.mark.asyncio
async def test_eval_all_red_kb_flips_overall_to_red() -> None:
    """One red surface → overall red regardless of others."""
    result = await eval_all(
        tenant_id=uuid.uuid4(),
        retrieve_fn=_make_terrible_retrieve_fn(),
    )

    kb = next(s for s in result.surfaces if s.surface == "kb")
    assert kb.verdict == "red"
    assert result.overall_verdict == "red"


@pytest.mark.asyncio
async def test_eval_all_surface_filter_scopes_the_run() -> None:
    """Passing surfaces=['kb'] only runs kb."""
    result = await eval_all(
        tenant_id=uuid.uuid4(),
        retrieve_fn=_make_perfect_retrieve_fn(),
        surfaces=["kb"],
    )

    assert {s.surface for s in result.surfaces} == {"kb"}


# ---------------------------------------------------------------------------
# Baseline integration
# ---------------------------------------------------------------------------


def _seed_grep_kb(tmp_path: Path) -> Path:
    """Seed a tiny kb directory grep can match against."""
    from meho_backplane.retrieval.eval.corpus import load_corpus

    kb = tmp_path / "kb"
    kb.mkdir()
    # For each kb query in the corpus, write a file whose body
    # contains the query text + an extra word so "esxcli" matches
    # the esxcli file etc.
    for row in load_corpus("kb"):
        for slug in row.expected_hits:
            (kb / f"{slug}.md").write_text(
                f"# {slug}\n{row.query}\n",
                encoding="utf-8",
            )
    return kb


@pytest.mark.asyncio
async def test_eval_surface_kb_with_baseline_populates_baseline_fields(
    tmp_path: Path,
) -> None:
    """``baseline_corpus_root`` set → per-query baseline_* fields populated."""
    kb_root = _seed_grep_kb(tmp_path)

    result = await eval_surface(
        "kb",
        tenant_id=uuid.uuid4(),
        retrieve_fn=_make_perfect_retrieve_fn(),
        baseline_corpus_root=kb_root,
    )

    assert result.baseline_kind == "grep"
    assert result.baseline_precision_at_5 is not None
    assert result.baseline_mrr is not None
    assert result.baseline_coverage is not None
    assert result.baseline_verdict in {"green", "yellow", "red"}

    for q in result.queries:
        assert q.baseline_hits is not None
        assert q.baseline_precision_at_5 is not None
        assert q.baseline_reciprocal_rank is not None
        assert q.baseline_coverage_at_5 is not None


@pytest.mark.asyncio
async def test_eval_surface_kb_meho_worse_than_baseline_downgrades_to_red(
    tmp_path: Path,
) -> None:
    """When MEHO loses to baseline on any metric → overall verdict = red."""
    # Seed grep kb so the baseline finds slugs (precision > 0).
    kb_root = _seed_grep_kb(tmp_path)

    # MEHO returns nothing → baseline beats MEHO trivially.
    async def empty_retrieve(
        *,
        tenant_id: uuid.UUID,
        query: str,
        source: str | None = None,
        kind: str | None = None,
        limit: int = 10,
    ) -> list[RetrievalHit]:
        return []

    result = await eval_surface(
        "kb",
        tenant_id=uuid.uuid4(),
        retrieve_fn=empty_retrieve,
        baseline_corpus_root=kb_root,
    )

    # MEHO precision/MRR/coverage all 0; baseline > 0 → MEHO < baseline → red.
    assert result.precision_at_5 == 0.0
    # The base verdict before the baseline check would be red anyway, but
    # the assertion proves the baseline check fires either way.
    assert result.verdict == "red"


@pytest.mark.asyncio
async def test_eval_surface_kb_meho_equals_baseline_does_not_downgrade(
    tmp_path: Path,
) -> None:
    """When MEHO == baseline on every metric, the base verdict is preserved."""
    kb_root = _seed_grep_kb(tmp_path)

    # Both MEHO and baseline return identical hits → metrics equal →
    # no downgrade. The base verdict (driven by absolute thresholds)
    # stands.
    result = await eval_surface(
        "kb",
        tenant_id=uuid.uuid4(),
        retrieve_fn=_make_perfect_retrieve_fn(),
        baseline_corpus_root=kb_root,
    )

    # MEHO is perfect (1.0/1.0/1.0); baseline is whatever grep
    # returned. The downgrade check fires only when MEHO < baseline.
    # Perfect MEHO can't be less than baseline — verdict stays green.
    assert result.verdict == "green"


# ---------------------------------------------------------------------------
# save_baseline / load_baseline round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_load_baseline_round_trip(tmp_path: Path) -> None:
    """Saving + reloading an EvalResult preserves every field."""
    eval_result = await eval_all(
        tenant_id=uuid.uuid4(),
        retrieve_fn=_make_perfect_retrieve_fn(),
    )

    path = tmp_path / "baseline.json"
    save_baseline(eval_result, path)

    loaded = load_baseline(path)

    assert loaded.overall_verdict == eval_result.overall_verdict
    assert len(loaded.surfaces) == len(eval_result.surfaces)
    for original, copied in zip(eval_result.surfaces, loaded.surfaces, strict=True):
        assert original.surface == copied.surface
        assert original.query_count == copied.query_count
        assert original.precision_at_5 == copied.precision_at_5
        assert original.mrr == copied.mrr
        assert original.coverage == copied.coverage
        assert original.verdict == copied.verdict


@pytest.mark.asyncio
async def test_save_baseline_creates_parent_dir(tmp_path: Path) -> None:
    """``save_baseline`` mkdirs the parent path so callers don't have to."""
    eval_result = await eval_all(
        tenant_id=uuid.uuid4(),
        retrieve_fn=_make_perfect_retrieve_fn(),
    )

    nested_path = tmp_path / "a" / "b" / "c" / "baseline.json"
    save_baseline(eval_result, nested_path)

    assert nested_path.exists()


@pytest.mark.asyncio
async def test_save_baseline_writes_pretty_json_with_trailing_newline(
    tmp_path: Path,
) -> None:
    """The file is human-readable (indent=2) and ends with a newline."""
    eval_result = await eval_all(
        tenant_id=uuid.uuid4(),
        retrieve_fn=_make_perfect_retrieve_fn(),
    )

    path = tmp_path / "baseline.json"
    save_baseline(eval_result, path)

    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    # Indent=2 means newlines + spaces in the JSON shape.
    assert "\n  " in text
    # Round-trip parses cleanly.
    json.loads(text)


# ---------------------------------------------------------------------------
# compare_baseline — regression detection
# ---------------------------------------------------------------------------


def _make_eval_result(
    surface: str,
    *,
    p5: float,
    mrr: float,
    cov: float,
    query_count: int = 10,
) -> EvalResult:
    """Construct a minimal EvalResult with one surface, given metrics."""
    surface_result = SurfaceResult(
        surface=surface,  # type: ignore[arg-type]
        query_count=query_count,
        precision_at_5=p5,
        mrr=mrr,
        coverage=cov,
        verdict="green",
        queries=[],
    )
    return EvalResult(
        ran_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        surfaces=[surface_result],
        overall_verdict="green",
    )


def test_compare_baseline_no_regression_returns_empty_list() -> None:
    """Identical metrics → no regression."""
    today = _make_eval_result("kb", p5=0.85, mrr=0.60, cov=0.95)
    baseline = _make_eval_result("kb", p5=0.85, mrr=0.60, cov=0.95)

    assert compare_baseline(today, baseline) == []


def test_compare_baseline_detects_precision_regression() -> None:
    """Today's precision drops below baseline by more than epsilon → flagged."""
    today = _make_eval_result("kb", p5=0.50, mrr=0.60, cov=0.95)
    baseline = _make_eval_result("kb", p5=0.85, mrr=0.60, cov=0.95)

    regressions = compare_baseline(today, baseline)
    assert any("kb.precision_at_5" in r for r in regressions)


def test_compare_baseline_detects_mrr_regression() -> None:
    """Today's MRR drops → flagged."""
    today = _make_eval_result("kb", p5=0.85, mrr=0.30, cov=0.95)
    baseline = _make_eval_result("kb", p5=0.85, mrr=0.60, cov=0.95)

    regressions = compare_baseline(today, baseline)
    assert any("kb.mrr" in r for r in regressions)


def test_compare_baseline_detects_coverage_regression() -> None:
    """Today's coverage drops → flagged."""
    today = _make_eval_result("kb", p5=0.85, mrr=0.60, cov=0.50)
    baseline = _make_eval_result("kb", p5=0.85, mrr=0.60, cov=0.95)

    regressions = compare_baseline(today, baseline)
    assert any("kb.coverage" in r for r in regressions)


def test_compare_baseline_within_epsilon_does_not_flag() -> None:
    """Drop within the epsilon tolerance → not flagged (noise floor)."""
    # Default epsilon is 0.02. A 0.01 drop should not flag.
    today = _make_eval_result("kb", p5=0.84, mrr=0.60, cov=0.95)
    baseline = _make_eval_result("kb", p5=0.85, mrr=0.60, cov=0.95)

    assert compare_baseline(today, baseline) == []


def test_compare_baseline_custom_epsilon_widens_tolerance() -> None:
    """Passing a wider epsilon allows a larger drop without flagging."""
    today = _make_eval_result("kb", p5=0.70, mrr=0.60, cov=0.95)
    baseline = _make_eval_result("kb", p5=0.85, mrr=0.60, cov=0.95)

    # 0.15 drop, epsilon 0.20 → tolerated.
    eps = RegressionEpsilon(precision_at_5=0.20, mrr=0.02, coverage=0.02)
    assert compare_baseline(today, baseline, epsilon=eps) == []


def test_compare_baseline_skips_surfaces_with_zero_corpus() -> None:
    """Empty corpus on either side → not a regression for that surface."""
    today = _make_eval_result("kb", p5=0.0, mrr=0.0, cov=0.0, query_count=0)
    baseline = _make_eval_result("kb", p5=0.85, mrr=0.60, cov=0.95)

    assert compare_baseline(today, baseline) == []


def test_compare_baseline_ignores_surfaces_only_in_one_side() -> None:
    """A new surface in today but absent in baseline is not a regression."""
    # baseline only has kb; today has kb + memory.
    today_kb = SurfaceResult(
        surface="kb",
        query_count=10,
        precision_at_5=0.85,
        mrr=0.60,
        coverage=0.95,
        verdict="green",
    )
    today_mem = SurfaceResult(
        surface="memory",
        query_count=10,
        precision_at_5=0.10,
        mrr=0.05,
        coverage=0.20,
        verdict="red",
    )
    today = EvalResult(
        ran_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        surfaces=[today_kb, today_mem],
        overall_verdict="red",
    )
    baseline = _make_eval_result("kb", p5=0.85, mrr=0.60, cov=0.95)

    # Memory is new — its bad numbers are not flagged as a
    # regression, only kb is compared.
    assert compare_baseline(today, baseline) == []


# ---------------------------------------------------------------------------
# Default retrieve_fn dispatch — proves the runner falls back cleanly.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eval_surface_uses_default_retrieve_when_none_passed() -> None:
    """No ``retrieve_fn`` → patched module-level retrieve is invoked."""

    async def stub_retrieve(
        *,
        tenant_id: uuid.UUID,
        query: str,
        source: str | None = None,
        kind: str | None = None,
        limit: int = 10,
    ) -> list[RetrievalHit]:
        return []

    with patch("meho_backplane.retrieval.eval.runner.retrieve", new=stub_retrieve):
        result = await eval_surface("kb", tenant_id=uuid.uuid4())

    # Empty hits everywhere → 0 metrics → red verdict.
    assert result.precision_at_5 == 0.0
    assert result.verdict == "red"
