# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared ``search_docs`` retrieval service (G4.5-T3 #1521, G4.6-T3 #1552).

The ``meho-docs`` add-on (Initiative #1518) federates vendor-document
queries through the backplane to the external corpus the ops team runs
(T2, :mod:`meho_backplane.auth.corpus`). This package is the **one**
shared entrypoint that surface — :func:`search_docs` — used by the REST
route (``POST /api/v1/search_docs``), the MCP ``search_docs`` / ``ask_docs``
tools, and the CLI verb. Keeping the scope-validation, backend call, and
cited-chunk projection in a service module (rather than inline in the
route) is what lets every surface reuse the exact same posture without
re-implementing the binary-scope gate or the citation shape.

The mandatory ``collection`` scope — the binary router / entitlement key
(G4.6-T3 #1552), never a ranking weight (the #1178 / #1177 decision) — is
enforced here: :func:`build_docs_scope` rejects a missing/blank
``collection`` with :class:`MissingDocsFilterError`, which the route
renders as HTTP 422 (fail-closed) and the MCP face as ``-32602``.
``product`` / ``version`` are optional refinements within the chosen
collection. :func:`resolve_entitled_ready_collection` is the shared gate
that turns the ``collection`` key into its registry row, enforces the
per-collection ``meho-docs:<key>`` entitlement, and checks readiness; the
central audit binding (``op_id="meho.docs.search"`` + ``audit_collection``)
stays in each surface, next to the ``audit_*`` contextvars the chassis
middleware lifts into the row.
"""

from __future__ import annotations

from meho_backplane.docs_search.backends import (
    SearchBackend,
    resolve_backend,
    resolve_backend_or_label,
)
from meho_backplane.docs_search.collection_access import (
    CollectionAccessError,
    CollectionForbiddenError,
    CollectionNotReadyError,
    NoEntitledReadyCollectionError,
    UnknownCollectionError,
    collection_capability_key,
    resolve_entitled_ready_collection,
    resolve_entitled_ready_collections,
)
from meho_backplane.docs_search.fanout import (
    CollectionScope,
    ConflictingCollectionScopeError,
    parse_collection_scope,
    rrf_merge,
    search_docs_fanout,
)
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
    "CollectionAccessError",
    "CollectionForbiddenError",
    "CollectionNotReadyError",
    "CollectionScope",
    "ConflictingCollectionScopeError",
    "DocsAnswer",
    "DocsChunk",
    "DocsScope",
    "DocsSearchResult",
    "DocsSynthesisError",
    "MissingDocsFilterError",
    "NoEntitledReadyCollectionError",
    "SearchBackend",
    "UnknownCollectionError",
    "build_docs_scope",
    "collection_capability_key",
    "parse_collection_scope",
    "resolve_backend",
    "resolve_backend_or_label",
    "resolve_entitled_ready_collection",
    "resolve_entitled_ready_collections",
    "rrf_merge",
    "search_docs",
    "search_docs_fanout",
    "synthesize_docs_answer",
]
