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
    DocsChunk,
    MissingDocsFilterError,
    UnknownCollectionError,
    build_docs_scope,
    resolve_entitled_ready_collection,
    search_docs,
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

    ``extra="forbid"`` rejects unknown fields at 422 so a client sending
    a pre-rename key fails loud rather than running with the defaults --
    the same posture every public v1 request schema ships under.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    collection: str | None = Field(default=None, max_length=128)
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
                "The mandatory ``collection`` scope is absent / blank, or "
                "names no collection visible to the tenant. A docs query "
                "without a routable collection is rejected rather than "
                "forwarded as an unscoped query."
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
    """Route a vendor-document query to a collection's backend, returning cited chunks.

    Enforces the mandatory ``collection`` binary scope (422 when absent),
    resolves it to its backend via the shared
    :func:`~meho_backplane.docs_search.search_docs` service, enforces the
    per-collection ``meho-docs:<collection>`` entitlement (403) and
    readiness (409), and binds the central ``meho.docs.search`` audit row.
    ``read_only`` operators get 403 via :func:`require_role` before
    reaching this handler.

    The audit contextvars are bound **before** the entitlement / backend
    call so a handler exception (the entitlement 403, readiness 409, or
    :class:`CorpusUnavailable` 503 branch) still produces an audit row
    with the query identity + collection scope preserved. The raw query is
    never bound -- only its SHA-256 hash.
    """
    # Validate the binary scope first; a 422 here must NOT bind an audit
    # row implying a backend call happened. ``collection`` is mandatory.
    try:
        scope = build_docs_scope(body.collection, body.product, body.version)
    except MissingDocsFilterError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    log = structlog.get_logger()
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
        audit_collection=scope.collection_key,
        audit_product=scope.product,
        audit_version=scope.version,
    )

    # Resolve + entitle + readiness gate (each typed failure → its own HTTP
    # status; see :func:`_resolve_collection_or_http_error`).
    collection = await _resolve_collection_or_http_error(session, operator, scope.collection_key)

    try:
        result = await search_docs(
            operator,
            body.query,
            scope=scope,
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
            collection=scope.collection_key,
            product=scope.product,
            version=scope.version,
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
        collection=scope.collection_key,
        product=scope.product,
        version=scope.version,
        hit_count=len(result.chunks),
    )
    return SearchDocsResponse(chunks=result.chunks)
