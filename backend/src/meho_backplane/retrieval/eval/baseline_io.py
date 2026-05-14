# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Save / load / compare helpers for checked-in eval baselines (G4.3-T2, #441).

The CI gate workflow keeps a known-good baseline in
``ci/eval-baseline.json`` and re-runs the eval on every backend PR.
Each PR call uses :func:`load_baseline` to read the snapshot,
:func:`compare_baseline` to diff against today's run, and exits 1
when any per-metric regression exceeds the configured epsilon
(default 0.02). Operators wanting to bless a new baseline call
:func:`save_baseline` against the eval result and check the JSON
into git.

Lifted out of ``runner.py`` so the runner module stays under the
code-quality file-size limit; baseline persistence is logically
separable from eval orchestration.
"""

from __future__ import annotations

import json
from pathlib import Path

from meho_backplane.retrieval.eval.result_models import (
    EvalResult,
    RegressionEpsilon,
)

__all__ = [
    "compare_baseline",
    "load_baseline",
    "save_baseline",
]


def save_baseline(result: EvalResult, path: Path) -> None:
    """Serialise an :class:`EvalResult` to JSON on disk.

    Pretty-printed (``indent=2``) so the file diffs cleanly when
    checked into a repo (the CI gate's ``ci/eval-baseline.json`` is
    expected to live in git history; a one-line minified JSON would
    be unreadable in PR diffs).

    The parent directory is created if missing — operators saving a
    baseline to ``./.meho/eval/<date>.json`` shouldn't have to mkdir
    by hand.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Pydantic v2's model_dump_json returns a UTF-8 string; round-trip
    # through json.loads + json.dumps with indent=2 to get the pretty
    # form (model_dump_json doesn't accept indent in 2.13).
    raw = result.model_dump_json()
    pretty = json.dumps(json.loads(raw), indent=2, sort_keys=True)
    path.write_text(pretty + "\n", encoding="utf-8")


def load_baseline(path: Path) -> EvalResult:
    """Read + validate a saved baseline JSON file.

    Validation failures surface as :class:`pydantic.ValidationError`
    — same shape callers handle when constructing
    :class:`EvalResult` directly. A missing file raises the standard
    :class:`FileNotFoundError`; the CLI translates that into a clear
    operator-facing message.
    """
    raw = path.read_text(encoding="utf-8")
    return EvalResult.model_validate_json(raw)


def compare_baseline(
    today: EvalResult,
    baseline: EvalResult,
    *,
    epsilon: RegressionEpsilon | None = None,
) -> list[str]:
    """Return a list of human-readable regression strings; empty list = no regression.

    A regression is "today's metric < baseline metric - epsilon" on
    any per-surface aggregate; the function walks every shipped
    surface in *today* and compares against the same surface in
    *baseline* (skipping surfaces only present in one side — adding
    a new surface is not a regression on the existing ones).

    The returned list is suitable for printing to stderr and / or
    embedding in a JSON envelope; an empty list means the CLI's
    ``--compare-baseline`` exit-1-on-regression check passes.
    """
    eps = epsilon or RegressionEpsilon()
    regressions: list[str] = []

    today_by_surface = {r.surface: r for r in today.surfaces}
    baseline_by_surface = {r.surface: r for r in baseline.surfaces}

    for surface, today_r in today_by_surface.items():
        baseline_r = baseline_by_surface.get(surface)
        if baseline_r is None:
            continue
        # Skip surfaces with empty corpus on one side — comparing
        # zero-corpus to N-corpus is not a regression.
        if today_r.query_count == 0 or baseline_r.query_count == 0:
            continue
        regressions.extend(_diff_one_surface(surface, today_r, baseline_r, eps))

    return regressions


def _diff_one_surface(
    surface: str,
    today_r: object,
    baseline_r: object,
    eps: RegressionEpsilon,
) -> list[str]:
    """Diff one surface's three metrics; return regression strings (≥ 0)."""
    out: list[str] = []
    today_p5 = today_r.precision_at_5  # type: ignore[attr-defined]
    today_mrr = today_r.mrr  # type: ignore[attr-defined]
    today_cov = today_r.coverage  # type: ignore[attr-defined]
    base_p5 = baseline_r.precision_at_5  # type: ignore[attr-defined]
    base_mrr = baseline_r.mrr  # type: ignore[attr-defined]
    base_cov = baseline_r.coverage  # type: ignore[attr-defined]

    for metric_name, today_v, baseline_v, eps_v in (
        ("precision_at_5", today_p5, base_p5, eps.precision_at_5),
        ("mrr", today_mrr, base_mrr, eps.mrr),
        ("coverage", today_cov, base_cov, eps.coverage),
    ):
        if today_v < baseline_v - eps_v:
            out.append(
                f"{surface}.{metric_name}: today={today_v:.3f} "
                f"baseline={baseline_v:.3f} delta={today_v - baseline_v:+.3f} "
                f"epsilon={eps_v:.3f}"
            )
    return out
