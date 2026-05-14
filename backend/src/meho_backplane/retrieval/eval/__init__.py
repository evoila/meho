# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Retrieval evaluation library — shared corpus loader + Pydantic schemas.

G4.3-T1 (#440) of Initiative #373 (G4.3 Retrieval migration tooling).
T1 ships the data layer that every later T in the Initiative builds on:

* T2 (#441) — eval runner CLI / API.
* T3 (#442) — operations corpus YAML.
* T4 (#443) — memory corpus YAML.
* T5-T7 — usage telemetry, retire-checklist, operator runbook.

The shape decision is one library across three retrieval surfaces (kb /
memory / operations) so a regression in the loader fails the eval for
every surface in the same place. Per-surface YAML files live alongside
this package; :func:`load_corpus` selects the right file + Pydantic
schema for the requested surface.

T1 ships the kb seed corpus (10 queries, hand-curated against the real
consumer ``kb/`` directory). The memory + operations corpora YAML files
land in T3/T4; :func:`load_corpus("memory")` and ``load_corpus("operations")``
return ``[]`` until then so T2's runner can iterate every surface
without crashing on a missing file.
"""

from meho_backplane.retrieval.eval.baseline_grep import (
    BaselineConfigError,
    run_grep_baseline,
)
from meho_backplane.retrieval.eval.corpus import (
    CorpusValidationError,
    KbCorpusQuery,
    MemoryCorpusQuery,
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
from meho_backplane.retrieval.eval.runner import (
    DEFAULT_K,
    EvalRequestSurface,
    EvalResult,
    QueryResult,
    RegressionEpsilon,
    RetrieveCallable,
    SurfaceResult,
    compare_baseline,
    eval_all,
    eval_surface,
    load_baseline,
    save_baseline,
)

__all__ = [
    "DEFAULT_K",
    "GREEN_DEFAULTS",
    "BaselineConfigError",
    "CorpusValidationError",
    "EvalRequestSurface",
    "EvalResult",
    "KbCorpusQuery",
    "MemoryCorpusQuery",
    "OperationCorpusQuery",
    "QueryResult",
    "RegressionEpsilon",
    "RetrieveCallable",
    "SurfaceResult",
    "Thresholds",
    "Verdict",
    "compare_baseline",
    "coverage_at_k",
    "eval_all",
    "eval_surface",
    "load_baseline",
    "load_corpus",
    "mean_metric",
    "precision_at_k",
    "reciprocal_rank",
    "run_grep_baseline",
    "save_baseline",
    "verdict",
]
