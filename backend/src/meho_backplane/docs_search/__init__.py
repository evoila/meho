# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared ``search_docs`` retrieval service (G4.5-T3 #1521).

The ``meho-docs`` add-on (Initiative #1518) federates vendor-document
queries through the backplane to the external corpus the ops team runs
(T2, :mod:`meho_backplane.auth.corpus`). This package is the **one**
shared entrypoint that surface — :func:`search_docs` — used by both the
REST route (``POST /api/v1/search_docs``, T3) and the future MCP tool
(T4, #1523) / CLI verb (T5, #1524). Keeping the scope-validation,
corpus call, and cited-chunk projection in a service module (rather than
inline in the route) is what lets T4/T5 reuse the exact same posture
without re-implementing the REQUIRE_FILTERS gate or the citation shape.

The mandatory product+version filter — a **binary scope**, never a
ranking weight (the #1178 / #1177 decision) — is enforced here:
:func:`build_docs_scope` rejects a missing/blank ``product`` or
``version`` with :class:`MissingDocsFilterError`, which the route renders as
HTTP 422 (fail-closed). Enforcement is gated by
``settings.corpus_require_filters`` (default on); the central audit
binding (``op_id="meho.docs.search"``) stays in the route, next to the
``audit_*`` contextvars the chassis middleware lifts into the row.
"""

from __future__ import annotations

from meho_backplane.docs_search.service import (
    DocsChunk,
    DocsScope,
    DocsSearchResult,
    MissingDocsFilterError,
    build_docs_scope,
    search_docs,
)
from meho_backplane.docs_search.synthesis import (
    NO_GROUNDED_ANSWER,
    DocsAnswer,
    DocsSynthesisError,
    synthesize_docs_answer,
)

__all__ = [
    "NO_GROUNDED_ANSWER",
    "DocsAnswer",
    "DocsChunk",
    "DocsScope",
    "DocsSearchResult",
    "DocsSynthesisError",
    "MissingDocsFilterError",
    "build_docs_scope",
    "search_docs",
    "synthesize_docs_answer",
]
