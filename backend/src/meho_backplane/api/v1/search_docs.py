# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``POST /api/v1/search_docs`` -- collection-scoped vendor-document retrieval.

G4.6-T3 (#1552) of Initiative #1548 (the doc-collection catalogue),
building on G4.5-T3 (#1521). The route is the REST face of the shared
:func:`~meho_backplane.docs_search.search_docs` service: it routes a
free-text query to the named collection's backend (T2 #1551) rather than
hitting any one corpus directly. Routing through the backplane is what
lands every query in the audit trail, forwards the operator JWT once,
enforces per-collection entitlement, and records the collection scope
centrally.

Collection scope (the binary scope)
-----------------------------------

``collection`` is **mandatory** -- a request missing or blanking it is
rejected **422** (fail-closed) by
:func:`~meho_backplane.docs_search.build_docs_scope` rather than
forwarded as an unscoped query. ``collection`` is the binary scope: it
routes the query to a backend and gates entitlement, but it is a
router / entitlement key, NOT a metadata filter (it never reaches the
backend's per-chunk ``metadata`` containment query). ``product`` /
``version`` demote to **optional refinements** within the chosen
collection -- present, they ride ``metadata_filters`` (binary
containment, the #1178 / #1177 decision, never a ranking weight); absent,
the collection alone scopes the query.

Per-collection entitlement + readiness
--------------------------------------

After the scope parses, :func:`~meho_backplane.docs_search.resolve_entitled_ready_collection`
runs the shared gate: it resolves ``collection`` to its registry row
(tenant-first), enforces the ``meho-docs:<collection>`` capability
(403 on a miss -- the collection exists but the tenant is not entitled),
and checks the registry ``status`` (409 when the collection is not
``ready``). An unknown collection is 422 (an invalid argument).

RBAC + tenant scoping
---------------------

``operator`` role minimum (mirrors :mod:`meho_backplane.api.v1.retrieve`)
-- ``read_only`` operators get 403 via :func:`require_role` before the
handler runs. The query is tenant-scoped by construction: there is no
surface that accepts a tenant id from the body; the forwarded JWT (and
the corpus's own audit) carries ``operator.tenant_id``.

Central audit contract
----------------------

The handler binds the ``audit_*`` contextvars **before** the corpus
call so a handler exception still produces an audit row with the partial
payload:

* ``audit_op_id = "meho.docs.search"`` -- the canonical op_id every
  ``search_docs`` audit row carries, so ``query_audit`` / who-touched
  filter on ``payload->>'op_id' = 'meho.docs.search'``.
* ``audit_op_class = "read"`` -- this is a read operation. The broadcast
  payload for ``read`` is full-detail, which is safe here because the
  bound payload is *only* the hash + scope + hit count -- the **raw query
  is never bound** (only its SHA-256 digest), so nothing operator-sensitive
  can leak through the feed.
* ``audit_query_hash`` -- SHA-256 hex of the UTF-8 query; the raw query
  is never stored.
* ``audit_collection`` -- the collection scope (the operator-chosen
  collection key, the binary router / entitlement scope, recorded in the
  clear for who-touched attribution; builds on #1549 so a row is
  filterable by both op_id and collection).
* ``audit_product`` / ``audit_version`` -- the optional refinements, when
  present (operator-chosen, not tenant-shaped identifiers).
* ``audit_hit_count`` -- bound after the backend returns.

Corpus-unavailable contract
---------------------------

The transport's typed
:class:`~meho_backplane.auth.corpus.CorpusUnavailable` (corpus
unconfigured, unreachable, or non-2xx / malformed) is mapped to HTTP
**503** -- never a silent empty 200. The exception's message is the
only thing surfaced; the corpus response body is never attached (the
transport already guarantees that), so a corpus error page cannot leak
through the 503 detail.

Out of scope (per the Initiative body)
--------------------------------------

* MCP tool registration + capability gating (T4, #1523) and the
  ``meho docs search`` CLI verb (T5, #1524) -- both reuse the shared
  :func:`~meho_backplane.docs_search.search_docs` service this route
  fronts.
* ``ask_docs`` (synthesized answer) -- fast-follow (T7, #1526).
* Local indexing of the corpus -- federation only.
"""

from __future__ import annotations

import hashlib
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.corpus import CorpusUnavailable
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.engine import get_session
from meho_backplane.docs_collections import DocCollection
from meho_backplane.docs_search import (
    CollectionForbiddenError,
    CollectionNotReadyError,
    CollectionScope,
    ConflictingCollectionScopeError,
    DocsChunk,
    DocsSearchResult,
    MissingDocsFilterError,
    NoEntitledReadyCollectionError,
    UnknownCollectionError,
    build_docs_scope,
    parse_collection_scope,
    resolve_entitled_ready_collection,
    resolve_entitled_ready_collections,
    search_docs,
    search_docs_fanout,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1", tags=["docs"])


#: Module-level ``Depends`` closure for the route's RBAC gate. Built
#: once at import time (rather than inline) to satisfy ruff's B008 rule,
#: matching the convention :mod:`meho_backplane.api.v1.retrieve`
#: established.
_require_operator = Depends(require_role(TenantRole.OPERATOR))


class SearchDocsRequest(BaseModel):
    """POST body for ``/api/v1/search_docs``.

    ``collection`` is the **mandatory binary scope** (G4.6-T3 #1552) -- it
    is typed optional here so a missing value is rejected by the service
    with a route-shaped 422 naming the absent key (carrying *why* the
    collection is mandatory), rather than Pydantic's generic
    ``field_required``. ``product`` / ``version`` are **optional
    refinements** within the chosen collection.

    ``collections`` is the opt-in **cross-collection fan-out** scope (G4.6-T5
    #1554): an explicit list of collection keys to query and RRF-merge.
    ``collection="all"`` is the equivalent sentinel for *every* entitled,
    ready collection. The fan-out scope is **mutually exclusive** with a
    single ``collection`` -- supplying both is a 422. ``product`` /
    ``version`` refinements do not apply to a fan-out (each collection is a
    pre-scoped corpus); they are ignored on the fan-out path.

    ``extra="forbid"`` rejects unknown fields at 422 so a client sending
    a pre-rename key fails loud rather than running with the defaults --
    the same posture every public v1 request schema ships under.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    collection: str | None = Field(default=None, max_length=128)
    collections: list[str] | None = Field(default=None, max_length=64)
    product: str | None = Field(default=None, max_length=128)
    version: str | None = Field(default=None, max_length=128)
    limit: int = Field(default=10, ge=1, le=50)


class SearchDocsResponse(BaseModel):
    """Successful response shape for ``/api/v1/search_docs``.

    ``chunks`` is the corpus's ranked cited-chunk list (best first),
    projected into MEHO's :class:`~meho_backplane.docs_search.DocsChunk`
    surface so the wire contract is decoupled from the corpus's. Frozen
    so an accidental post-construction mutation surfaces as a pydantic
    error rather than a silently-altered response.
    """

    model_config = ConfigDict(frozen=True)

    chunks: list[DocsChunk]


def _compute_query_hash(query: str) -> str:
    """SHA-256 hex digest of *query* (UTF-8 encoded).

    Matches the encoding contract :func:`meho_backplane.api.v1.retrieve._compute_query_hash`
    uses so an analyst correlating a known query against ``audit_log``
    can use a single hash function across both retrieval surfaces. The
    raw query is never stored -- only this digest.
    """
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


async def _resolve_collection_or_http_error(
    session: AsyncSession,
    operator: Operator,
    collection_key: str,
) -> DocCollection:
    """Run the shared resolve + entitle + readiness gate, mapping to HTTP.

    Each typed access failure maps to its own status: an unknown collection
    → 422 (an invalid ``collection`` argument, carrying the catalogue of
    visible keys), not entitled → 403 (the collection exists; the tenant
    lacks ``meho-docs:<collection>``), not ready → 409 (the backend is not
    answerable yet).
    """
    try:
        return await resolve_entitled_ready_collection(session, operator, collection_key)
    except UnknownCollectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": "unknown_collection",
                "collection": exc.collection_key,
                "known_collections": exc.known_keys,
            },
        ) from exc
    except CollectionForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except CollectionNotReadyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


async def _resolve_fanout_or_http_error(
    session: AsyncSession,
    operator: Operator,
    requested_keys: list[str] | None,
) -> list[DocCollection]:
    """Resolve a cross-collection fan-out scope, mapping the empty-set error.

    The fan-out resolver drops non-entitled / not-ready members
    (logged, not raised) and only raises when the *whole* set collapses to
    empty -- mapped to 403 here (the tenant has no answerable collection in
    the requested scope), the same 403-class the single-collection
    not-entitled branch uses.
    """
    try:
        return await resolve_entitled_ready_collections(
            session, operator, requested_keys=requested_keys
        )
    except NoEntitledReadyCollectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc


async def _run_fanout_or_http_error(
    operator: Operator,
    query: str,
    *,
    collections: list[DocCollection],
    limit: int,
    log: structlog.stdlib.BoundLogger,
) -> DocsSearchResult:
    """Run the fan-out search, mapping a backend outage to the shared 503.

    A fan-out is fail-closed on *any* collection's backend: one unavailable
    backend is a 503 for the whole query rather than a partial fused list
    that silently omits a collection the operator asked for -- the same
    posture the single-collection path takes.
    """
    try:
        return await search_docs_fanout(operator, query, collections=collections, limit=limit)
    except CorpusUnavailable as exc:
        log.warning(
            "search_docs_fanout_backend_unavailable",
            operator_sub=operator.sub,
            collections=[c.collection_key for c in collections],
            corpus_status=exc.status,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.post(
    "/search_docs",
    response_model=SearchDocsResponse,
    responses={
        403: {
            "description": (
                "The tenant is not entitled to the named collection -- it "
                "lacks the ``meho-docs:<collection>`` capability even "
                "though it can see the add-on. The collection exists; the "
                "principal just cannot search it."
            ),
        },
        409: {
            "description": (
                "The named collection is known and entitled but not "
                "``ready`` (provisioning / rebuilding / disabled) -- its "
                "backend is not answerable yet."
            ),
        },
        422: {
            "description": (
                "The mandatory ``collection`` scope is absent / blank, names "
                "no collection visible to the tenant, or both a single "
                "``collection`` and a fan-out ``collections`` / "
                "``collection='all'`` scope were supplied (they are mutually "
                "exclusive). A docs query without a routable collection is "
                "rejected rather than forwarded as an unscoped query."
            ),
        },
        503: {
            "description": (
                "The collection's backend is unavailable -- unconfigured, "
                "unreachable, or returned a non-2xx / malformed response. "
                "Fail-closed; never an empty 200."
            ),
        },
    },
)
async def search_docs_endpoint(
    body: SearchDocsRequest,
    operator: Annotated[Operator, _require_operator],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SearchDocsResponse:
    """Route a vendor-document query to one or more collections, returning cited chunks.

    Two scopes, mutually exclusive (a 422 when both are supplied):

    * **Single** (``collection``) -- the T3 path: resolve to one backend,
      enforce the per-collection ``meho-docs:<collection>`` entitlement (403)
      and readiness (409), and search that one collection.
    * **Fan-out** (``collections=[…]`` or ``collection="all"``, G4.6-T5
      #1554) -- query every entitled, ready collection in scope on its own
      backend and RRF-merge the ranked lists. Non-entitled / not-ready
      collections are dropped (logged); an empty resolved set is a 403.

    Both bind the central ``meho.docs.search`` audit row, with
    ``audit_collection`` carrying the queried collection (single) or the
    sorted, comma-joined queried set (fan-out) so who-touched attributes the
    query either way. ``read_only`` operators get 403 via
    :func:`require_role` before reaching this handler. The raw query is never
    bound -- only its SHA-256 hash.
    """
    # Parse the collection scope first; a conflicting (both single + fan-out)
    # or missing scope is a 422 that must NOT bind an audit row implying a
    # backend call happened.
    try:
        scope = parse_collection_scope(body.collection, body.collections)
    except ConflictingCollectionScopeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    log = structlog.get_logger()
    if scope.is_fanout():
        return await _handle_fanout(body, operator, session, scope, log)
    return await _handle_single(body, operator, session, log)


async def _handle_single(
    body: SearchDocsRequest,
    operator: Operator,
    session: AsyncSession,
    log: structlog.stdlib.BoundLogger,
) -> SearchDocsResponse:
    """The single-collection path (T3 #1552): one backend, one collection."""
    # Validate the binary scope; a missing/blank ``collection`` is the
    # mandatory-scope 422 (before any audit binding or backend call).
    try:
        docs_scope = build_docs_scope(body.collection, body.product, body.version)
    except MissingDocsFilterError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    # Pre-bind everything the audit middleware lifts into
    # ``audit_log.payload``. ``hit_count`` is bound after the backend
    # returns; the rest are known up-front so a handler exception (the
    # entitlement / readiness / backend branches) still records the query
    # identity + scope. The raw query is never bound -- only its SHA-256
    # hash.
    structlog.contextvars.bind_contextvars(
        audit_op_id="meho.docs.search",
        audit_op_class="read",
        audit_query_hash=_compute_query_hash(body.query),
        audit_collection=docs_scope.collection_key,
        audit_product=docs_scope.product,
        audit_version=docs_scope.version,
    )

    # Resolve + entitle + readiness gate (each typed failure → its own HTTP
    # status; see :func:`_resolve_collection_or_http_error`).
    collection = await _resolve_collection_or_http_error(
        session, operator, docs_scope.collection_key
    )

    try:
        result = await search_docs(
            operator,
            body.query,
            scope=docs_scope,
            collection=collection,
            limit=body.limit,
        )
    except CorpusUnavailable as exc:
        # Fail-closed: an unconfigured / unreachable / non-2xx backend is
        # 503, never an empty 200. The transport guarantees the backend
        # response body is never on the exception, so nothing leaks
        # through the detail (and the backend id stays server-side).
        log.warning(
            "search_docs_backend_unavailable",
            operator_sub=operator.sub,
            collection=docs_scope.collection_key,
            product=docs_scope.product,
            version=docs_scope.version,
            corpus_status=exc.status,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    structlog.contextvars.bind_contextvars(audit_hit_count=len(result.chunks))
    log.info(
        "search_docs_endpoint_completed",
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
        collection=docs_scope.collection_key,
        product=docs_scope.product,
        version=docs_scope.version,
        hit_count=len(result.chunks),
    )
    return SearchDocsResponse(chunks=result.chunks)


async def _handle_fanout(
    body: SearchDocsRequest,
    operator: Operator,
    session: AsyncSession,
    scope: CollectionScope,
    log: structlog.stdlib.BoundLogger,
) -> SearchDocsResponse:
    """The cross-collection fan-out path (T5 #1554): RRF over entitled set.

    Resolves the requested keys (or the ``all`` sentinel) to the entitled,
    ready set, binds ``audit_collection`` to the **sorted, comma-joined**
    queried set, then fans out + RRF-merges. ``product`` / ``version`` do
    not apply to a fan-out (each collection is a pre-scoped corpus) and are
    not bound.
    """
    # Resolve the entitled, ready set first (drops non-entitled / not-ready
    # members, logged); an empty set is a 403 before any audit binding.
    collections = await _resolve_fanout_or_http_error(session, operator, scope.requested_keys())

    # Bind the canonical audit identity. ``audit_collection`` is the sorted,
    # comma-joined queried set so who-touched attributes a fan-out to every
    # collection it actually touched (the resolver returns them sorted).
    queried = [c.collection_key for c in collections]
    structlog.contextvars.bind_contextvars(
        audit_op_id="meho.docs.search",
        audit_op_class="read",
        audit_query_hash=_compute_query_hash(body.query),
        audit_collection=",".join(queried),
    )

    result = await _run_fanout_or_http_error(
        operator, body.query, collections=collections, limit=body.limit, log=log
    )

    structlog.contextvars.bind_contextvars(audit_hit_count=len(result.chunks))
    log.info(
        "search_docs_fanout_endpoint_completed",
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
        collections=queried,
        hit_count=len(result.chunks),
    )
    return SearchDocsResponse(chunks=result.chunks)
