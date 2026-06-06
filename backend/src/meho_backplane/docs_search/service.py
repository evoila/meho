# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The shared ``search_docs`` service: scope validation + corpus call.

Two responsibilities, both shared across the REST route (T3), the MCP
tool (T4), and the CLI verb (T5):

1. :func:`build_docs_scope` — enforce the **mandatory** product+version
   filter as a binary scope. When ``settings.corpus_require_filters`` is
   on (the default), a missing or blank ``product`` / ``version`` raises
   :class:`MissingDocsFilterError`, which the route renders HTTP 422
   (fail-closed). The filter is a scope passed verbatim to the corpus —
   never a ranking weight (#1178 / #1177). With the gate **off**, the
   filter degrades to optional: present keys still scope, absent keys
   simply widen the search (the corpus is the policy owner in that mode).

2. :func:`search_docs` — call T2's :func:`~meho_backplane.auth.corpus.search_corpus`
   with the operator's forwarded JWT and the binary scope, then project
   the corpus's chunks into MEHO's own cited-chunk surface
   (:class:`DocsChunk`). The transport's typed
   :class:`~meho_backplane.auth.corpus.CorpusUnavailable` propagates
   unchanged so the route maps it to HTTP 503 — never a silent empty 200.

The audit binding (``op_id="meho.docs.search"`` + ``op_class="read"`` +
``audit_query_hash`` + product/version/hit_count) lives in the route,
not here, because the ``audit_*`` contextvars are HTTP-request-scoped
and the chassis middleware is what writes the row. This module never
touches the raw query beyond forwarding it to the corpus.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.auth.corpus import CorpusChunk, search_corpus
from meho_backplane.auth.operator import Operator
from meho_backplane.docs_search.backends import resolve_backend
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.docs_collections import DocCollection

__all__ = [
    "DocsChunk",
    "DocsScope",
    "DocsSearchResult",
    "MissingDocsFilterError",
    "build_docs_scope",
    "search_docs",
]

_log = structlog.get_logger(__name__)

#: The two binary-scope keys forwarded to the corpus as
#: ``metadata_filters``. Both are mandatory under the REQUIRE_FILTERS
#: posture; the corpus keys its per-chunk ``metadata`` off these names.
_PRODUCT_KEY = "product"
_VERSION_KEY = "version"


class MissingDocsFilterError(ValueError):
    """Raised when a mandatory ``product`` / ``version`` filter is absent.

    The REQUIRE_FILTERS posture is fail-closed: a docs query without a
    binary product+version scope is rejected rather than forwarded as an
    unfiltered corpus query (which would scan the whole vendor corpus and
    defeat the per-product/version scoping the add-on exists to enforce).
    The route maps this to HTTP 422. ``missing`` lists which key(s) were
    absent or blank so the caller's error detail can name them without
    re-deriving the gap.
    """

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        joined = ", ".join(missing)
        super().__init__(
            f"search_docs requires a binary {joined} filter "
            "(product and version are mandatory scopes, not ranking weights)"
        )


class DocsScope(BaseModel):
    """The validated binary product+version scope for a docs query.

    Frozen value object built by :func:`build_docs_scope`. :meth:`as_filters`
    renders it into the ``{key: scalar}`` ``metadata_filters`` shape the
    corpus federation client (T2) forwards verbatim — the same containment
    contract the local retrieval substrate uses (PG ``metadata @>`` ),
    mirrored on the corpus side. Only positively-set keys are emitted, so
    the gate-off path can carry a partial scope without injecting ``None``
    values the corpus would have to special-case.
    """

    model_config = ConfigDict(frozen=True)

    product: str | None = Field(default=None)
    version: str | None = Field(default=None)

    def as_filters(self) -> dict[str, str]:
        """Render the scope as a ``{key: scalar}`` corpus filter dict."""
        filters: dict[str, str] = {}
        if self.product is not None:
            filters[_PRODUCT_KEY] = self.product
        if self.version is not None:
            filters[_VERSION_KEY] = self.version
        return filters


class DocsChunk(BaseModel):
    """One cited chunk in MEHO's ``search_docs`` response surface.

    A stable projection of the corpus's :class:`~meho_backplane.auth.corpus.CorpusChunk`
    into the shape the MCP tool (T4) and CLI (T5) render: chunk text +
    source citation + score. Decoupling MEHO's surface from the corpus's
    wire contract means a corpus field rename doesn't churn the public
    ``search_docs`` response; the projection in :func:`_project_chunk` is
    the one place that mapping lives.
    """

    model_config = ConfigDict(frozen=True)

    chunk_id: str
    document_id: str
    content: str
    source_url: str | None = None
    score: float | None = None


class DocsSearchResult(BaseModel):
    """The result of a ``search_docs`` call: the ordered cited chunks.

    ``chunks`` preserves the corpus's ranking (best first). Frozen so a
    consumer accidentally mutating the result post-construction surfaces
    as a pydantic error rather than a silently-altered response.
    """

    model_config = ConfigDict(frozen=True)

    chunks: list[DocsChunk] = Field(default_factory=list)


def build_docs_scope(product: str | None, version: str | None) -> DocsScope:
    """Validate the binary product+version scope, fail-closed under the gate.

    When ``settings.corpus_require_filters`` is on (the default), **both**
    ``product`` and ``version`` must be non-blank; either missing raises
    :class:`MissingDocsFilterError` (HTTP 422 at the route). With the gate off,
    the scope degrades to optional — whatever is provided still scopes the
    corpus query, whatever is absent simply widens it. Blank-after-strip
    values are treated as absent so a ``product=" "`` cannot smuggle past
    the mandatory gate.

    Args:
        product: The vendor product to scope to (e.g. ``"vmware"``).
        version: The product version to scope to (e.g. ``"9.0"``).

    Returns:
        A :class:`DocsScope` carrying the normalised (stripped) values.

    Raises:
        MissingDocsFilterError: when the REQUIRE_FILTERS gate is on and either
            ``product`` or ``version`` is missing or blank.
    """
    norm_product = product.strip() if product and product.strip() else None
    norm_version = version.strip() if version and version.strip() else None

    if get_settings().corpus_require_filters:
        missing: list[str] = []
        if norm_product is None:
            missing.append(_PRODUCT_KEY)
        if norm_version is None:
            missing.append(_VERSION_KEY)
        if missing:
            raise MissingDocsFilterError(missing)

    return DocsScope(product=norm_product, version=norm_version)


def _project_chunk(chunk: CorpusChunk) -> DocsChunk:
    """Project a corpus chunk into MEHO's cited-chunk surface."""
    return DocsChunk(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        content=chunk.content,
        source_url=chunk.source_url,
        score=chunk.score,
    )


async def search_docs(
    operator: Operator,
    query: str,
    *,
    scope: DocsScope,
    limit: int = 10,
    collection: DocCollection | None = None,
) -> DocsSearchResult:
    """Search a doc collection's backend for *operator* within *scope*.

    Resolves *collection* to its concrete search backend via the
    backend-agnostic router
    (:func:`~meho_backplane.docs_search.backends.resolve_backend`) and
    calls ``backend.search(...)`` — so one collection can sit on a
    managed RAG and another on the JWT-forward corpus behind this one
    entrypoint, and the agent never sees which backend answered. The
    operator JWT is forwarded by the adapter (for the JWT-forward corpus
    backend), the search is scoped to the binary product+version filter,
    and the cited chunks are projected into MEHO's surface. The query
    itself is never logged here — only its presence is implied; the route
    binds the SHA-256 hash to the audit row.

    ``collection=None`` is the **legacy single-collection** path: a
    deploy that has not yet adopted the ``doc_collections`` registry
    federates every query to the one global ``corpus_url`` via the
    re-homed :func:`~meho_backplane.auth.corpus.search_corpus` transport.
    The collection-scoped request param that makes *collection* mandatory
    is T3 (#1552); T2 ships the router with this additive, non-breaking
    seam so the legacy path keeps working unchanged.

    Args:
        operator: The verified operator whose JWT is forwarded to the
            backend.
        query: The free-text search query.
        scope: The validated binary product+version scope from
            :func:`build_docs_scope`.
        limit: Maximum number of chunks to request.
        collection: The resolved doc collection to search, or ``None`` for
            the legacy single-collection corpus path.

    Returns:
        A :class:`DocsSearchResult` carrying the backend's ranked cited
        chunks.

    Raises:
        CorpusUnavailable: when the collection routes to no registered
            backend, or the chosen backend is unconfigured, unreachable,
            or returns a non-2xx / malformed response. The route maps it
            to HTTP 503 (the backend id never appears in the 503).
    """
    filters = scope.as_filters()
    if collection is None:
        # Legacy single-collection deploy: federate to the one global
        # corpus through the module-level transport seam. Kept distinct
        # from the routed path so the pre-registry behaviour (and the
        # callers patching ``service.search_corpus``) is untouched until
        # T3 makes ``collection`` mandatory.
        response = await search_corpus(
            operator,
            query,
            metadata_filters=filters or None,
            limit=limit,
        )
    else:
        resolved = resolve_backend(collection)
        response = await resolved.backend.search(
            operator,
            query,
            backend_ref=resolved.ref,
            metadata_filters=filters or None,
            limit=limit,
        )
    chunks = [_project_chunk(c) for c in response.chunks]
    _log.info(
        "docs_search_completed",
        operator_sub=operator.sub,
        product=scope.product,
        version=scope.version,
        collection_key=collection.collection_key if collection is not None else None,
        hit_count=len(chunks),
    )
    return DocsSearchResult(chunks=chunks)
