# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Retrieval evaluation library for the CI gate — corpus loader, metrics, runner.

This package is test tooling, not part of the product. It lives under
``backend/scripts/`` so it is not in the wheel. Two callers use it:

* ``scripts/ci/run_eval_gate.py`` (the ``eval-gate`` workflow) scores a
  stub search against the question files and compares the result with
  ``ci/eval-baseline.json``.
* The kb and memory acceptance tests score the real search.

One library covers the three retrieval surfaces (kb / memory /
operations), so a regression in the loader fails the eval for every
surface in the same place. The question files live next to this
module; :func:`load_corpus` picks the right file and Pydantic schema
for the requested surface.
"""

from scripts.retrieval_eval.corpus import (
    CorpusValidationError,
    KbCorpusQuery,
    MemoryCorpusQuery,
    OperationCorpusQuery,
    load_corpus,
    load_kb_passage_fixtures,
)
from scripts.retrieval_eval.metrics import (
    GREEN_DEFAULTS,
    Thresholds,
    Verdict,
    coverage_at_k,
    mean_metric,
    precision_at_k,
    reciprocal_rank,
    verdict,
)
from scripts.retrieval_eval.passage_bench import (
    BASELINE_CANDIDATE,
    CandidateSpec,
    PassageBenchReport,
    run_passage_bench,
)
from scripts.retrieval_eval.runner import (
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
    "BASELINE_CANDIDATE",
    "DEFAULT_K",
    "GREEN_DEFAULTS",
    "CandidateSpec",
    "CorpusValidationError",
    "EvalRequestSurface",
    "EvalResult",
    "KbCorpusQuery",
    "MemoryCorpusQuery",
    "OperationCorpusQuery",
    "PassageBenchReport",
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
    "load_kb_passage_fixtures",
    "mean_metric",
    "precision_at_k",
    "reciprocal_rank",
    "run_passage_bench",
    "save_baseline",
    "verdict",
]
