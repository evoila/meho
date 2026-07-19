# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The shared ``search_docs`` service: scope validation + corpus call.

Two responsibilities, both shared across the REST route (T3), the MCP
tool (T4), and the CLI verb (T5):

1. :func:`build_docs_scope` — enforce the **mandatory** ``collection``
   binary scope (T3 #1552). ``collection`` is the single hard-required
   scope: a missing or blank value raises :class:`MissingDocsFilterError`,
   which the route renders HTTP 422 (fail-closed) and the MCP face renders
   ``-32602``. ``product`` / ``version`` demote to **optional refinements**
   within the chosen collection — present, they ride
   :meth:`DocsScope.as_filters` as ``metadata_filters``; absent, the
   collection alone scopes the query. The filters are scopes passed
   verbatim to the backend, never ranking weights (#1178 / #1177). The
   ``collection`` key is a **router / entitlement key**, not a metadata
   filter — it is kept out of :meth:`DocsScope.as_filters` so it never
   leaks into the backend's per-chunk ``metadata`` containment query.

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

from collections.abc import Sequence
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.auth.corpus import CorpusChunk
from meho_backplane.auth.operator import Operator
from meho_backplane.docs_search.backends import resolve_backend
from meho_backplane.docs_search.citation_links import normalize_source_ref

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

#: The optional refinement keys forwarded to the backend as
#: ``metadata_filters`` within the chosen collection. Both are optional
#: under the collection-scoped posture (T3 #1552); the backend keys its
#: per-chunk ``metadata`` off these names.
_PRODUCT_KEY = "product"
_VERSION_KEY = "version"

#: The mandatory binary-scope key. ``collection`` is the router /
#: entitlement key (T3 #1552) — required on every query, but **not** a
#: metadata filter (it never reaches :meth:`DocsScope.as_filters`).
_COLLECTION_KEY = "collection"


class MissingDocsFilterError(ValueError):
    """Raised when the mandatory ``collection`` scope is absent or blank.

    The collection-scoped posture is fail-closed: a docs query without a
    ``collection`` is rejected rather than forwarded as an unscoped query
    (which would have no backend to route to and defeat the per-collection
    entitlement the catalogue exists to enforce). The route maps this to
    HTTP 422; the MCP face maps it to ``-32602``. ``missing`` lists which
    key(s) were absent or blank so the caller's error detail can name them
    without re-deriving the gap.
    """

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        joined = ", ".join(missing)
        super().__init__(
            f"search_docs requires a {joined} scope "
            "(collection is the mandatory binary scope, not a ranking weight)"
        )


class DocsScope(BaseModel):
    """The validated binary scope for a docs query.

    Frozen value object built by :func:`build_docs_scope`. ``collection_key``
    is the mandatory router / entitlement key; ``product`` / ``version`` are
    the optional refinements within that collection. :meth:`as_filters`
    renders **only** the optional refinements into the ``{key: scalar}``
    ``metadata_filters`` shape the backend forwards verbatim — the same
    containment contract the local retrieval substrate uses (PG
    ``metadata @>`` ), mirrored on the backend side. ``collection_key`` is
    deliberately **excluded** from :meth:`as_filters`: it routes and gates
    the query, it is not a per-chunk metadata field. Only positively-set
    refinement keys are emitted, so a query with no product/version carries
    no spurious ``None`` values the backend would have to special-case.
    """

    model_config = ConfigDict(frozen=True)

    collection_key: str
    product: str | None = Field(default=None)
    version: str | None = Field(default=None)

    def as_filters(self) -> dict[str, str]:
        """Render the optional refinements as a ``{key: scalar}`` filter dict.

        ``collection_key`` is intentionally not included — it is a router /
        entitlement key, not a metadata filter.
        """
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

    ``collection`` is the **provenance** tag (G4.6-T5 #1554): which
    collection the chunk came from. It is ``None`` on the single-collection
    path (the collection is already implied by the request scope) and set
    to the source collection key on the cross-collection fan-out path so an
    agent fusing hits from several collections can attribute each chunk.

    ``document_id`` is ``str | None`` (#2004): it mirrors the corpus's own
    optional owning-document id (``None`` when the corpus has no document
    concept for a chunk) and is only read as a citation-label fallback.

    ``title`` is the **optional** human-legible chunk title (#2475), passed
    through from the corpus (``CorpusChunk.title``). It is the *preferred*
    citation label — every citation face (``ask_docs``, ``/ui/corpus``)
    feeds it to the ``title -> document_id -> filename -> URL`` label chain
    — and is ``None`` until the upstream corpus supplies one, so today's
    corpus (which sends no title) sees no behaviour change.

    ``source_url`` is the **backend-agnostic** citation reference (#132): a
    canonical public URL where one is derivable, else an opaque
    ``meho://docs/<collection>/<chunk_id>`` ref. It is **never** the corpus's
    raw ``gs://`` object path — the projection in :func:`_project_chunk`
    normalizes it via
    :func:`~meho_backplane.docs_search.citation_links.normalize_source_ref`
    so no storage-backend scheme or internal bucket/layout reaches the wire.
    """

    model_config = ConfigDict(frozen=True)

    chunk_id: str
    document_id: str | None = None
    title: str | None = None
    content: str
    source_url: str | None = None
    score: float | None = None
    collection: str | None = None


class DocsSearchResult(BaseModel):
    """The result of a ``search_docs`` call: the ordered cited chunks.

    ``chunks`` preserves the corpus's ranking (best first). Frozen so a
    consumer accidentally mutating the result post-construction surfaces
    as a pydantic error rather than a silently-altered response.
    """

    model_config = ConfigDict(frozen=True)

    chunks: list[DocsChunk] = Field(default_factory=list)


def retrieval_is_grounded(chunks: Sequence[DocsChunk]) -> bool:
    """Return whether a retrieval has anything to ground an answer on (#133).

    The **single source of truth** for the groundedness verdict both
    retrieval surfaces expose: ``search_docs`` reports it as the
    :attr:`SearchDocsResponse.grounded` flag, and ``ask_docs`` uses the same
    check to short-circuit to
    :data:`~meho_backplane.docs_search.synthesis.NO_GROUNDED_ANSWER` — so the
    two never diverge on what "grounded" means (a second, drifting threshold
    is exactly what #133 forbids).

    The verdict is **presence-based and deterministic**: grounded ⇔ retrieval
    returned at least one chunk. This is `ask_docs`'s existing empty-evidence
    determination (it answers ``NO_GROUNDED_ANSWER`` *without calling the
    model* when retrieval is empty), lifted verbatim; it makes no model call
    and reads no score, so ``search_docs`` stays pure retrieval.

    What it deliberately does **not** do: judge whether *present* chunks are
    topically relevant. A query that retrieves noise chunks at deceptively
    high scores (the corpus ``score`` is an opaque, undocumented scale, and
    out-of-corpus scores have been observed *higher* than in-corpus ones — so
    no absolute floor separates them) still reports ``grounded=True`` here.
    Distinguishing that case needs a calibrated, per-collection score floor —
    deferred as the Option-A follow-on in #133 — and this function is the one
    seam that refinement lands in, for both surfaces at once.
    """
    return len(chunks) > 0


def build_docs_scope(
    collection: str | None,
    product: str | None = None,
    version: str | None = None,
) -> DocsScope:
    """Validate the binary ``collection`` scope, fail-closed.

    ``collection`` is the **mandatory** binary scope (T3 #1552): a missing
    or blank value raises :class:`MissingDocsFilterError` (HTTP 422 at the
    route, ``-32602`` at the MCP face), unconditionally — this is not gated
    by ``settings.corpus_require_filters`` (the legacy product+version gate),
    because every collection-scoped query *must* name a collection to route
    and entitle on. ``product`` / ``version`` are **optional refinements**
    within the chosen collection: whatever is provided rides
    :meth:`DocsScope.as_filters` as ``metadata_filters``, whatever is absent
    simply widens the search inside the collection. Blank-after-strip values
    are treated as absent so a ``collection=" "`` cannot smuggle past the
    mandatory gate.

    Args:
        collection: The mandatory collection key to route + entitle on
            (e.g. ``"vmware"``).
        product: The optional vendor product refinement (e.g. ``"vsphere"``).
        version: The optional product version refinement (e.g. ``"9.0"``).

    Returns:
        A :class:`DocsScope` carrying the normalised (stripped) values.

    Raises:
        MissingDocsFilterError: when ``collection`` is missing or blank.
    """
    norm_collection = collection.strip() if collection and collection.strip() else None
    norm_product = product.strip() if product and product.strip() else None
    norm_version = version.strip() if version and version.strip() else None

    if norm_collection is None:
        raise MissingDocsFilterError([_COLLECTION_KEY])

    return DocsScope(
        collection_key=norm_collection,
        product=norm_product,
        version=norm_version,
    )


def _project_chunk(
    chunk: CorpusChunk,
    *,
    collection: str | None = None,
    collection_key: str | None = None,
) -> DocsChunk:
    """Project a corpus chunk into MEHO's cited-chunk surface.

    *collection* tags the chunk's provenance on the cross-collection
    fan-out path (T5 #1554); it is ``None`` on the single-collection path,
    where the source collection is already implied by the request scope.

    ``source_url`` is normalized through
    :func:`~meho_backplane.docs_search.citation_links.normalize_source_ref`
    (#132) so the wire never carries the corpus's raw ``gs://`` object path
    (which would leak the storage backend + internal bucket/layout) — it
    becomes a canonical public URL where one is derivable, else an opaque
    ``meho://docs/<collection>/<chunk_id>`` ref. This is the single seam both
    ``search_docs`` and ``ask_docs`` citations (every :class:`DocsChunk` is
    born here) normalize through. *collection_key* is the routing collection
    that namespaces the opaque ref; it falls back to the *collection*
    provenance tag (set on the fan-out path).
    """
    return DocsChunk(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        title=chunk.title,
        content=chunk.content,
        source_url=normalize_source_ref(
            chunk.source_url,
            collection_key=collection_key or collection,
            chunk_id=chunk.chunk_id,
            title=chunk.title,
            document_id=chunk.document_id,
        ),
        score=chunk.score,
        collection=collection,
    )


async def search_docs(
    operator: Operator,
    query: str,
    *,
    scope: DocsScope,
    collection: DocCollection,
    limit: int = 10,
) -> DocsSearchResult:
    """Search *collection*'s backend for *operator* within *scope*.

    Resolves *collection* to its concrete search backend via the
    backend-agnostic router
    (:func:`~meho_backplane.docs_search.backends.resolve_backend`) and
    calls ``backend.search(...)`` — so one collection can sit on a
    managed RAG and another on the JWT-forward corpus behind this one
    entrypoint, and the agent never sees which backend answered. The
    operator JWT is forwarded by the adapter (for the JWT-forward corpus
    backend), the search is scoped by the collection (and the optional
    product/version refinements), and the cited chunks are projected into
    MEHO's surface. The query itself is never logged here — only its
    presence is implied; the route binds the SHA-256 hash to the audit
    row.

    *collection* is the **required** binary scope (T3 #1552): the caller
    (the REST route / MCP handler) has already resolved the
    operator-supplied ``collection`` key to its registry row via
    :func:`~meho_backplane.docs_collections.resolve_doc_collection`,
    enforced per-collection entitlement, and checked readiness — so by the
    time the query reaches here, the backend binding is authoritative.

    Args:
        operator: The verified operator whose JWT is forwarded to the
            backend.
        query: The free-text search query.
        scope: The validated binary scope from :func:`build_docs_scope`
            (carries ``collection_key`` plus the optional product/version
            refinements).
        collection: The resolved doc collection whose ``backend`` record
            the router selects the search backend from.
        limit: Maximum number of chunks to request.

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
    resolved = resolve_backend(collection)
    response = await resolved.backend.search(
        operator,
        query,
        backend_ref=resolved.ref,
        metadata_filters=filters or None,
        limit=limit,
    )
    chunks = [_project_chunk(c, collection_key=scope.collection_key) for c in response.chunks]
    _log.info(
        "docs_search_completed",
        operator_sub=operator.sub,
        collection_key=scope.collection_key,
        product=scope.product,
        version=scope.version,
        hit_count=len(chunks),
    )
    return DocsSearchResult(chunks=chunks)
