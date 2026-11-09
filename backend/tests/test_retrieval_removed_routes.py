# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The retrieval quality check and the retire checklist are not product routes.

The quality check runs only as a CI script (``scripts/ci/run_eval_gate.py``
with ``backend/scripts/retrieval_eval/``). The usage report stays.
"""

from __future__ import annotations

from fastapi.routing import iter_route_contexts

from meho_backplane.main import app as production_app


def _routes() -> set[tuple[str, str]]:
    return {
        (method, context.path_format)
        for context in iter_route_contexts(production_app.routes)
        for method in (getattr(context, "methods", None) or set())
    }


def test_eval_and_retire_routes_are_gone() -> None:
    paths = {path for _, path in _routes()}
    for gone in (
        "/api/v1/retrieve/eval",
        "/api/v1/retrieve/retire-checklist",
        "/ui/retrieval/eval",
        "/ui/retrieval/retire-checklist",
    ):
        assert gone not in paths


def test_usage_routes_stay() -> None:
    routes = _routes()
    assert ("GET", "/api/v1/retrieve/usage") in routes
    assert ("POST", "/ui/retrieval/usage") in routes
