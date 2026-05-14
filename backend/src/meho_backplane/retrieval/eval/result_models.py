# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Result models for the retrieval-eval runner (G4.3-T2, #441).

Frozen Pydantic v2 models that define the shape every consumer of
``meho retrieval eval`` (CLI, API route, regression detector, future
dashboards) sees. Lifted out of ``runner.py`` so the runner module
stays under the code-quality file-size limit; the models are
small, dependency-free, and don't change shape often, so a separate
module is the right factoring.

Hierarchy: ``EvalResult`` carries one or more ``SurfaceResult``;
each ``SurfaceResult`` carries N ``QueryResult`` rows. All three
carry per-metric MEHO numbers + optional baseline numbers + verdict
bands; the verdict computation lives in :mod:`metrics`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.retrieval.eval.metrics import Thresholds, Verdict

__all__ = [
    "EvalResult",
    "QueryResult",
    "RegressionEpsilon",
    "SurfaceResult",
]


class RegressionEpsilon(BaseModel):
    """Per-metric tolerance for ``--compare-baseline`` regression detection.

    ``today_metric < baseline_metric - epsilon`` flags a regression.
    Defaults are tight (0.02) because the v0.2 corpus is small (10
    queries) — every percentage point matters, and ranking changes
    that move precision@5 by 0.05 are worth flagging. Operators can
    pass a wider epsilon when intentionally tuning the embedding
    model (a temporary "expected regression" while the new model
    catches up).
    """

    model_config = ConfigDict(frozen=True)

    precision_at_5: float = Field(default=0.02, ge=0.0, le=1.0)
    mrr: float = Field(default=0.02, ge=0.0, le=1.0)
    coverage: float = Field(default=0.02, ge=0.0, le=1.0)


class QueryResult(BaseModel):
    """Per-query eval row — what the runner produces for each corpus entry.

    ``meho_hits`` is the surface-formatted top-``k`` slug list from
    MEHO retrieval; ``baseline_hits`` mirrors it for the baseline
    when configured. The per-query metrics (precision_at_5 / mrr /
    coverage) are computed against ``meho_hits`` + the corpus's
    expected ground truth; ``baseline_*`` mirrors are populated when
    the baseline ran. Frozen so the runner can't accidentally mutate
    a row while folding the surface aggregate.
    """

    model_config = ConfigDict(frozen=True)

    query: str
    expected_hits: list[str]
    meho_hits: list[str]
    precision_at_5: float
    reciprocal_rank: float
    coverage_at_5: float
    baseline_hits: list[str] | None = None
    baseline_precision_at_5: float | None = None
    baseline_reciprocal_rank: float | None = None
    baseline_coverage_at_5: float | None = None


class SurfaceResult(BaseModel):
    """Per-surface aggregate: corpus-wide metrics + verdict + per-query rows.

    The verdict band is computed from the macro-mean metrics against
    the configured :class:`Thresholds` (default
    :data:`~meho_backplane.retrieval.eval.metrics.GREEN_DEFAULTS`). A
    surface with ``query_count == 0`` (corpus YAML hasn't shipped
    yet) returns ``verdict="green"`` deliberately — see runner.py
    docstring.

    ``baseline_verdict`` is computed only when the baseline ran;
    ``None`` otherwise. The MEHO-≥-baseline overlay applies in the
    aggregate verdict (see runner._apply_baseline_check).
    """

    model_config = ConfigDict(frozen=True)

    surface: Literal["kb", "memory", "operations"]
    query_count: int
    precision_at_5: float
    mrr: float
    coverage: float
    verdict: Verdict
    baseline_kind: Literal["grep"] | None = None
    baseline_precision_at_5: float | None = None
    baseline_mrr: float | None = None
    baseline_coverage: float | None = None
    baseline_verdict: Verdict | None = None
    queries: list[QueryResult] = Field(default_factory=list)


class EvalResult(BaseModel):
    """Top-level eval result — the shape the API + CLI return.

    ``overall_verdict`` is the worst-of every surface (a red surface
    flips the whole result red); the CLI's exit-1-on-red CI-gate
    semantics key off this field. ``ran_at`` is the UTC timestamp
    the runner started; consumers comparing two saved baselines see
    when each was captured.
    """

    model_config = ConfigDict(frozen=True)

    ran_at: datetime
    surfaces: list[SurfaceResult]
    overall_verdict: Verdict
    thresholds: Thresholds = Field(default_factory=Thresholds)
