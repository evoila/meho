# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``POST /api/v1/search_docs`` -- federated vendor-document retrieval.

G4.5-T3 (#1521) of Initiative #1518 (the ``meho-docs`` add-on). The
route is the REST face of the shared
:func:`~meho_backplane.docs_search.search_docs` service: it federates a
free-text query through the backplane to the external vendor-document
corpus the ops team runs (T2, :mod:`meho_backplane.auth.corpus`) rather
than hitting the corpus directly. Routing through the backplane is what
lands every query in the audit trail, forwards the operator JWT once,
and enforces the mandatory product/version posture centrally.

REQUIRE_FILTERS (the binary scope)
----------------------------------

``product`` AND ``version`` are **mandatory** -- a request missing
either is rejected **422** (fail-closed) by
:func:`~meho_backplane.docs_search.build_docs_scope` rather than
forwarded as an unfiltered corpus query. The filter is a **binary
scope** passed verbatim to the corpus (the #1178 / #1177 decision:
containment, not RRF weighting) -- it is never expressed as a ranking
weight. Enforcement is gated by ``settings.corpus_require_filters``
(default on); with the gate off the filter degrades to optional and the
corpus owns the policy.

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
  bound payload is *only* the hash + binary scope + hit count -- the
  **raw query is never bound** (only its SHA-256 digest), so nothing
  operator-sensitive can leak through the feed.
* ``audit_query_hash`` -- SHA-256 hex of the UTF-8 query; the raw query
  is never stored.
* ``audit_product`` / ``audit_version`` -- the binary scope (these are
  the operator-chosen product/version, not tenant-shaped identifiers, so
  they are recorded in the clear for who-touched attribution).
* ``audit_hit_count`` -- bound after the corpus returns.

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

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.auth.corpus import CorpusUnavailable
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.docs_search import (
    DocsChunk,
    MissingDocsFilterError,
    build_docs_scope,
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

    ``product`` / ``version`` are the **mandatory binary scope** under
    the REQUIRE_FILTERS posture -- they are typed optional here so a
    missing value is rejected by the service with a route-shaped 422
    naming the absent key(s), rather than Pydantic's generic
    ``field_required`` (which would not say *why* the scope is mandatory
    or honour the ``corpus_require_filters`` gate-off path).

    ``extra="forbid"`` rejects unknown fields at 422 so a client sending
    a pre-rename key fails loud rather than running with the defaults --
    the same posture every public v1 request schema ships under.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
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


@router.post(
    "/search_docs",
    response_model=SearchDocsResponse,
    responses={
        422: {
            "description": (
                "The mandatory binary product+version scope is absent "
                "(REQUIRE_FILTERS). A docs query without both filters is "
                "rejected rather than forwarded as an unfiltered corpus "
                "query."
            ),
        },
        503: {
            "description": (
                "The external corpus is unavailable -- unconfigured, "
                "unreachable, or returned a non-2xx / malformed response. "
                "Fail-closed; never an empty 200."
            ),
        },
    },
)
async def search_docs_endpoint(
    body: SearchDocsRequest,
    operator: Operator = _require_operator,
) -> SearchDocsResponse:
    """Federate a vendor-document query to the corpus, returning cited chunks.

    Enforces the mandatory binary product+version scope (422 when the
    REQUIRE_FILTERS gate is on and either is absent), forwards the
    operator JWT to the corpus via the shared
    :func:`~meho_backplane.docs_search.search_docs` service, and binds
    the central ``meho.docs.search`` audit row. ``read_only`` operators
    get 403 via :func:`require_role` before reaching this handler.

    The audit contextvars are bound **before** the corpus call so a
    handler exception (including the :class:`CorpusUnavailable` → 503
    branch) still produces an audit row with the query identity + binary
    scope preserved. The raw query is never bound -- only its SHA-256
    hash.
    """
    # Validate the binary scope first; a 422 here must NOT bind an audit
    # row implying a corpus call happened. The scope build is the
    # REQUIRE_FILTERS gate.
    try:
        scope = build_docs_scope(body.product, body.version)
    except MissingDocsFilterError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    log = structlog.get_logger()
    # Pre-bind everything the audit middleware lifts into
    # ``audit_log.payload``. ``hit_count`` is bound after the corpus
    # returns; the rest are known up-front so a handler exception (e.g.
    # the corpus 503 branch) still records the query identity + scope.
    # The raw query is never bound -- only its SHA-256 hash.
    structlog.contextvars.bind_contextvars(
        audit_op_id="meho.docs.search",
        audit_op_class="read",
        audit_query_hash=_compute_query_hash(body.query),
        audit_product=scope.product,
        audit_version=scope.version,
    )

    try:
        result = await search_docs(
            operator,
            body.query,
            scope=scope,
            limit=body.limit,
        )
    except CorpusUnavailable as exc:
        # Fail-closed: an unconfigured / unreachable / non-2xx corpus is
        # 503, never an empty 200. The transport guarantees the corpus
        # response body is never on the exception, so nothing leaks
        # through the detail.
        log.warning(
            "search_docs_corpus_unavailable",
            operator_sub=operator.sub,
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
        product=scope.product,
        version=scope.version,
        hit_count=len(result.chunks),
    )
    return SearchDocsResponse(chunks=result.chunks)
