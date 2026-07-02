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
collection. :func:`resolve_entitled_ready_collection` is the **single**
shared gate that turns the ``collection`` key into its registry row,
enforces the per-collection ``meho-docs:<key>`` entitlement, and checks
readiness — branching a terminal ``disabled`` collection
(:class:`CollectionDisabledError` → 403 / ``-32602``) from the transient
``provisioning`` / ``rebuilding`` states (:class:`CollectionNotReadyError`
→ 409 / ``-32603``) so a client can tell "do not retry" from "retry
later". The central audit binding (``op_id="meho.docs.search"`` +
``audit_collection``) stays in each surface, next to the ``audit_*``
contextvars the chassis middleware lifts into the row.
"""

from __future__ import annotations

from meho_backplane.docs_search.answer_errors import (
    ANSWER_ERROR_DETAIL,
    LEG_CORPUS,
    LEG_EXPAND,
    LEG_MODEL,
    LEG_SYNTHESIS,
    AskDocsAnswerError,
    classify_answer_error,
)
from meho_backplane.docs_search.backends import (
    SearchBackend,
    resolve_backend,
    resolve_backend_or_label,
)
from meho_backplane.docs_search.citation_links import (
    CitationLink,
    citation_link_payload,
    normalize_source_ref,
    resolve_citation_link,
)
from meho_backplane.docs_search.collection_access import (
    CollectionAccessError,
    CollectionDisabledError,
    CollectionForbiddenError,
    CollectionNotReadyError,
    NoEntitledReadyCollectionError,
    UnknownCollectionError,
    collection_capability_key,
    resolve_entitled_ready_collection,
    resolve_entitled_ready_collections,
)
from meho_backplane.docs_search.expansion import (
    MAX_QUERY_VARIANTS,
    DocsQueryExpansionError,
    expand_docs_query,
)
from meho_backplane.docs_search.fanout import (
    CollectionScope,
    ConflictingCollectionScopeError,
    parse_collection_scope,
    retrieve_multi_query,
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
    "ANSWER_ERROR_DETAIL",
    "LEG_CORPUS",
    "LEG_EXPAND",
    "LEG_MODEL",
    "LEG_SYNTHESIS",
    "MAX_QUERY_VARIANTS",
    "NO_GROUNDED_ANSWER",
    "AskDocsAnswerError",
    "CitationLink",
    "CollectionAccessError",
    "CollectionDisabledError",
    "CollectionForbiddenError",
    "CollectionNotReadyError",
    "CollectionScope",
    "ConflictingCollectionScopeError",
    "DocsAnswer",
    "DocsChunk",
    "DocsQueryExpansionError",
    "DocsScope",
    "DocsSearchResult",
    "DocsSynthesisError",
    "MissingDocsFilterError",
    "NoEntitledReadyCollectionError",
    "SearchBackend",
    "UnknownCollectionError",
    "build_docs_scope",
    "citation_link_payload",
    "classify_answer_error",
    "collection_capability_key",
    "expand_docs_query",
    "normalize_source_ref",
    "parse_collection_scope",
    "resolve_backend",
    "resolve_backend_or_label",
    "resolve_citation_link",
    "resolve_entitled_ready_collection",
    "resolve_entitled_ready_collections",
    "retrieve_multi_query",
    "rrf_merge",
    "search_docs",
    "search_docs_fanout",
    "synthesize_docs_answer",
]
