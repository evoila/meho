# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``POST /api/v1/ask_docs`` -- collection-scoped grounded, cited answer.

G4.6-T2 (#1917) of Initiative #1912 (the corpus grounded-answer pipeline).
The REST face of the ``ask_docs`` answer pipeline -- the synthesis sibling
of ``POST /api/v1/search_docs`` (#1552). ``search_docs`` returns the raw
ranked cited chunks; ``ask_docs`` runs the same retrieval (with the #1916
corpus-aware **expand** step in front) and composes a single grounded,
cited answer over those chunks, returning ``{answer, citations[]}`` where
every citation carries the resolved navigable ``link`` (#1919).

Before this route ``ask_docs`` was MCP-only: ``openapi.json`` exposed
``/api/v1/search_docs`` (chunks) but a POST to ``/api/v1/ask_docs`` 404ed,
so the ``/ui/corpus`` BFF could only render raw chunks. This route is the
REST surface the OpenAPI snapshot + generated Go client now carry, and the
in-process pipeline the ``/ui/corpus`` Ask mode composes.

Single-collection only
----------------------

``ask_docs`` is **single-collection only** (#1548 decision 2, matching the
MCP ``ask_docs`` contract): cross-collection synthesis is permanently out
of scope. ``collection`` is the mandatory binary scope; there is no
``collections`` fan-out field on this route (unlike ``search_docs``), so a
grounded answer never has to reconcile chunks from divergent corpora.

Collection scope + entitlement + readiness (mirrors ``search_docs``)
--------------------------------------------------------------------

The same resolve + entitle + readiness gate ``search_docs`` runs
(:func:`~meho_backplane.docs_search.resolve_entitled_ready_collection`),
mapping each typed access failure to the same status class: a missing /
blank / unknown ``collection`` -> 422, not entitled -> 403 (the structured
``not_entitled`` body naming the missing ``meho-docs:<key>`` capability +
the identity it checked, T2 #1802), ``disabled`` -> terminal 403, and a
transiently not-ready (``provisioning`` / ``rebuilding``) collection ->
retryable 409. A cross-tenant / absent collection is unknown to the
tenant-scoped catalogue, so it resolves to the 422 unknown-collection arm
(it never reaches a backend).

Where the answer comes from (#3911)
-----------------------------------

The route composes its answer through the one answer seam,
:func:`~meho_backplane.docs_search.answer.answer_docs_question`, shared with
the ``/ui/corpus`` Ask mode and the MCP ``ask_docs`` tool. A collection whose
backend offers its own grounded-answer endpoint and that opted in
(``backend.ref["answer"] = "upstream"``) is answered by that endpoint, mapped
into the same ``{answer, citations[]}`` shape; every other collection runs the
backplane's expand -> retrieve -> synthesize pipeline. The response schema is
the same either way; the source is logged, not returned.

Answer-pipeline legs -> 5xx (the #1918 structured error model, REST-ready)
--------------------------------------------------------------------------

The three answer-pipeline legs (expand / corpus / model / synthesis) each
fail closed with their own typed exception. They are classified by the
**one** shared
:func:`~meho_backplane.docs_search.classify_answer_error` -- the same
classifier the MCP ``ask_docs`` handler uses -- so REST and MCP surface the
identical ``{detail, leg, cause, message}`` envelope (here on
``HTTPException.detail``; there on JSON-RPC ``error.data``). The route layer
chooses the HTTP status the model only names the leg:

* ``expand_failed`` / ``model_unavailable`` (no ``ANTHROPIC_API_KEY``, or a
  malformed expansion) -> **503**: a server-side config / availability
  fault, the analogue of the MCP ``-32603``. Never an un-expanded /
  ungrounded answer.
* ``corpus_unavailable`` (retrieval backend down) -> **503**: identical to
  ``search_docs``'s ``CorpusUnavailable`` -> 503, fail-closed.
* ``synthesis_malformed`` (the model ran but its output broke the grounding
  contract -- non-JSON, wrong shape, or a cited id outside the retrieved
  set) -> **502**: the upstream model returned an invalid response. Distinct
  from the 503s (model unreachable / unconfigured) so a client can tell "the
  model is missing" from "the model answered badly".
* On the upstream-answer path (#3911) the status is chosen per
  ``(leg, cause)``: a backend that **rejected** the answer call (a 4xx,
  ``corpus_unavailable`` / ``upstream_rejected``) is **502** -- a contract or
  configuration fault, not an outage -- while its other failures keep their
  leg's status. A rate-limited backend answer model
  (``model_unavailable`` / ``upstream_rate_limited``) is a 503 that forwards
  the backend's ``Retry-After`` header.

Fail-closed end to end: a leg failure is a 5xx error envelope, never a
degraded / ungrounded answer. An empty retrieval is **not** an error -- the
synthesis helper short-circuits (without a model call) to a deterministic
"no grounded answer" 200 with empty ``citations``.

RBAC + tenant scoping + audit
-----------------------------

``operator`` role minimum (mirrors ``search_docs``); the query is
tenant-scoped by construction (the forwarded JWT carries the tenant, no
body field names one). The central audit row binds the canonical
``meho.docs.ask`` op_id (the SAME token the MCP ``ask_docs`` tool binds,
G4.5-T8 #1549) + ``read`` class + the SHA-256 query hash (never the raw
query) + the collection scope, before the pipeline runs so a leg failure is
still attributable.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Any, NoReturn

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.engine import get_session
from meho_backplane.docs_collections import DocCollection
from meho_backplane.docs_search import (
    LEG_CORPUS,
    LEG_EXPAND,
    LEG_MODEL,
    LEG_SYNTHESIS,
    AskDocsAnswerError,
    CollectionDisabledError,
    CollectionForbiddenError,
    CollectionNotReadyError,
    DocsAnswer,
    DocsScope,
    MissingDocsFilterError,
    UnknownCollectionError,
    build_docs_scope,
    citation_link_payload,
    resolve_entitled_ready_collection,
)
from meho_backplane.docs_search.answer import AskPipelineOutcome, answer_docs_question
from meho_backplane.docs_search.answer_errors import CAUSE_UPSTREAM_REJECTED

__all__ = [
    "AskPipelineOutcome",
    "router",
    "run_ask_pipeline",
    "run_ask_pipeline_capturing_retrieval",
]

router = APIRouter(prefix="/api/v1", tags=["docs"])


#: Module-level ``Depends`` closure for the route's RBAC gate. Built once at
#: import time (rather than inline) to satisfy ruff's B008 rule, matching the
#: convention :mod:`meho_backplane.api.v1.search_docs` established.
_require_operator = Depends(require_role(TenantRole.OPERATOR))

#: Canonical audit op_id -- the SAME token the MCP ``ask_docs`` tool binds
#: (``meho.docs.ask``) so a who-touched / ``query_audit`` filter on
#: ``op_id="meho.docs.*"`` is transport-independent across REST / CLI / MCP
#: (G4.5-T8 #1549).
_ASK_OP_ID = "meho.docs.ask"

#: HTTP status per answer-pipeline leg. The expand / corpus / model legs are
#: server-side config / availability faults (the MCP ``-32603`` = 503
#: analogue); ``synthesis_malformed`` is a bad-gateway 502 -- the upstream
#: model returned an invalid response, distinct from it being unreachable.
_LEG_STATUS: dict[str, int] = {
    LEG_EXPAND: status.HTTP_503_SERVICE_UNAVAILABLE,
    LEG_CORPUS: status.HTTP_503_SERVICE_UNAVAILABLE,
    LEG_MODEL: status.HTTP_503_SERVICE_UNAVAILABLE,
    LEG_SYNTHESIS: status.HTTP_502_BAD_GATEWAY,
}


class AskDocsRequest(BaseModel):
    """POST body for ``/api/v1/ask_docs``.

    ``collection`` is the **mandatory binary scope** -- typed optional here
    so a missing value is rejected by the service with a route-shaped 422
    naming the absent key (carrying *why* the collection is mandatory)
    rather than Pydantic's generic ``field_required``. ``product`` /
    ``version`` are optional refinements within the chosen collection.

    There is **no** ``collections`` fan-out field: ``ask_docs`` is
    single-collection only (#1548 decision 2), matching the MCP tool.
    ``extra="forbid"`` rejects unknown fields at 422 -- so a client sending
    a ``collections`` list (or a pre-rename key) fails loud rather than
    silently fanning out, the same posture every public v1 request schema
    ships under.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    collection: str | None = Field(default=None, max_length=128)
    product: str | None = Field(default=None, max_length=128)
    version: str | None = Field(default=None, max_length=128)
    limit: int = Field(default=10, ge=1, le=50)


class AskDocsResponse(BaseModel):
    """Successful response shape for ``/api/v1/ask_docs``.

    ``answer`` is the grounded, cited answer -- composed strictly from the
    retrieved chunks (no claim without a citation) or, on an empty
    retrieval, the deterministic
    :data:`~meho_backplane.docs_search.NO_GROUNDED_ANSWER` string. Frozen so
    an accidental post-construction mutation surfaces as a pydantic error
    rather than a silently-altered response.

    ``citations`` is the subset of retrieved chunks the answer relied on,
    each serialised with its resolved navigable ``link`` (#1919) -- the
    **same** citation shape the MCP ``ask_docs`` tool returns, so a client
    consuming either face resolves citations identically. The list entries
    are the raw :class:`~meho_backplane.docs_search.DocsChunk` fields plus a
    ``link`` member; modelled as ``list[dict]`` (not a typed model) because
    the ``link`` shape is produced by the shared
    :func:`~meho_backplane.docs_search.citation_link_payload` helper, kept as
    the single source of truth for the citation-link contract.
    """

    model_config = ConfigDict(frozen=True)

    answer: str
    citations: list[dict[str, Any]]


def _compute_query_hash(query: str) -> str:
    """SHA-256 hex digest of *query* (UTF-8 encoded).

    Matches the encoding contract
    :func:`meho_backplane.api.v1.search_docs._compute_query_hash` uses so an
    analyst correlating a known query against ``audit_log`` can use a single
    hash function across both docs surfaces. The raw query is never stored --
    only this digest.
    """
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


async def _resolve_collection_or_http_error(
    session: AsyncSession,
    operator: Operator,
    collection_key: str,
) -> DocCollection:
    """Run the shared resolve + entitle + readiness gate, mapping to HTTP.

    Identical to the ``search_docs`` route's helper: an unknown collection
    -> 422 (an invalid ``collection`` argument -- the arm a cross-tenant /
    absent key also lands in, since it is unknown to the tenant-scoped
    catalogue), not entitled -> 403 (structured ``not_entitled`` naming the
    missing capability + the identity, T2 #1802), disabled -> 403 (terminal,
    *not* retryable), and transiently not ready -> 409 (retryable). Keeping
    the mapping byte-identical to ``search_docs`` is what makes the issue's
    "mirror search_docs" acceptance criterion literally true.
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
            detail={
                "error": "not_entitled",
                "collection": exc.collection_key,
                "required_capability": exc.required_capability,
                "operator_sub": exc.operator_sub,
                "tenant_id": exc.tenant_id,
                "message": str(exc),
            },
        ) from exc
    except CollectionDisabledError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "collection_disabled",
                "collection": exc.collection_key,
                "retryable": False,
            },
        ) from exc
    except CollectionNotReadyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


#: The structured (non-raising) entrypoint the ``/ui/corpus`` Ask BFF calls:
#: the one answer seam itself (#3911), kept under its established name. It
#: returns an :class:`AskPipelineOutcome` -- the grounded answer, or the
#: classified leg failure plus the chunks retrieved before it -- so the BFF can
#: fail open to those chunks instead of dropping them.
run_ask_pipeline_capturing_retrieval = answer_docs_question


async def run_ask_pipeline(
    operator: Operator,
    query: str,
    *,
    scope: DocsScope,
    collection: DocCollection,
    limit: int,
) -> DocsAnswer:
    """Answer through the one answer seam, naming the failed leg on error.

    The raising in-process entrypoint the REST route below calls. A thin
    wrapper over :func:`run_ask_pipeline_capturing_retrieval` (the answer seam,
    upstream or local, #3911) that preserves the raise-on-leg-failure
    contract: it discards the captured chunks (the REST 5xx envelope never
    carries them) and re-raises the classified
    :class:`~meho_backplane.docs_search.AskDocsAnswerError` so the route maps
    it to a 5xx ``HTTPException`` (:func:`_raise_pipeline_http_error`). The
    ``/ui/corpus`` Ask BFF calls the capturing variant directly so it can fail
    open to those chunks instead.

    Raises:
        AskDocsAnswerError: a classified leg failure (expand / corpus / model
            / synthesis). The original exception rides ``__cause__`` so the
            structlog breadcrumb keeps the traceback. An unexpected (non-leg)
            exception is re-raised unchanged.
    """
    outcome = await run_ask_pipeline_capturing_retrieval(
        operator, query, scope=scope, collection=collection, limit=limit
    )
    if outcome.error is not None:
        # Re-raise the classified leg failure; ``__cause__`` (chained by the
        # answer seam) preserves the original traceback.
        raise outcome.error
    assert outcome.answer is not None  # success outcome always carries an answer
    return outcome.answer


def _raise_pipeline_http_error(answer_error: AskDocsAnswerError) -> NoReturn:
    """Map a classified :class:`AskDocsAnswerError` to its 5xx ``HTTPException``.

    The route layer chooses the status per ``(leg, cause)``: per leg
    (:data:`_LEG_STATUS`), except that an upstream answer endpoint that
    **rejected** the call (``upstream_rejected``, #3911) is a 502 -- a
    contract / configuration fault, not the 503 "unavailable" of its leg. The
    structured envelope (``{detail, leg, cause, message}`` plus, on an
    upstream failure, ``upstream_status`` / ``retry_after``) rides
    ``HTTPException.detail`` byte-identical to the MCP ``error.data`` member,
    so a client parses the same shape on either face. A rate-limited upstream
    answer forwards its ``Retry-After`` as a header too. An unmapped leg (a
    future leg added to the model but not here) defaults to 503 -- the
    conservative fail-closed status -- rather than leaking a 500.
    """
    if answer_error.cause == CAUSE_UPSTREAM_REJECTED:
        http_status = status.HTTP_502_BAD_GATEWAY
    else:
        http_status = _LEG_STATUS.get(answer_error.leg, status.HTTP_503_SERVICE_UNAVAILABLE)
    headers = (
        {"Retry-After": str(answer_error.retry_after)}
        if answer_error.retry_after is not None
        else None
    )
    raise HTTPException(
        status_code=http_status,
        detail=answer_error.to_error_data(),
        headers=headers,
    )


@router.post(
    "/ask_docs",
    response_model=AskDocsResponse,
    responses={
        403: {
            "description": (
                "Terminal rejection of an otherwise-resolvable collection, in "
                "one of two forms (both 403, distinguished by ``detail.error``): "
                "the tenant is not entitled to the named collection "
                "(``detail.error='not_entitled'`` -- it lacks the "
                "``meho-docs:<collection>`` capability; the structured detail "
                "names the ``required_capability`` and the ``operator_sub`` / "
                "``tenant_id`` it checked) -- or the collection has been "
                "``disabled`` by an operator "
                "(``detail.error='collection_disabled'``). Mirrors "
                "``search_docs``; neither is retryable."
            ),
        },
        409: {
            "description": (
                "The named collection is known and entitled but transiently "
                "not ``ready`` (provisioning / rebuilding). Retryable once the "
                "rebuild finishes. Mirrors ``search_docs``."
            ),
        },
        422: {
            "description": (
                "The mandatory ``collection`` scope is absent / blank, or "
                "names no collection visible to the tenant (an absent / "
                "cross-tenant key lands here -- it is unknown to the "
                "tenant-scoped catalogue). A grounded answer is never composed "
                "without a routable collection."
            ),
        },
        502: {
            "description": (
                "The synthesis model ran but its output broke the grounding "
                "contract (non-JSON, wrong shape, or a citation outside the "
                "retrieved set): ``detail.leg='synthesis_malformed'``. A "
                "bad-gateway fault distinct from the model being unreachable "
                "(503). Also returned when the collection's backend answer "
                "endpoint rejected the call (``detail.cause="
                "'upstream_rejected'``, with ``detail.upstream_status``). The "
                "structured #1918 ``{detail, leg, cause, message}`` envelope "
                "rides ``detail``."
            ),
        },
        503: {
            "description": (
                "An answer-pipeline leg failed closed: the expand or synthesis "
                "model is unconfigured / produced unusable expansion "
                "(``detail.leg`` ``expand_failed`` / ``model_unavailable``), or "
                "the retrieval backend is unavailable "
                "(``detail.leg='corpus_unavailable'``). Fail-closed; never an "
                "ungrounded answer. The structured #1918 envelope rides "
                "``detail``. A rate-limited backend answer model "
                "(``detail.cause='upstream_rate_limited'``) forwards the "
                "backend's ``Retry-After`` header."
            ),
        },
    },
)
async def ask_docs_endpoint(
    body: AskDocsRequest,
    operator: Annotated[Operator, _require_operator],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AskDocsResponse:
    """Compose a grounded, cited answer over a single vendor-document collection.

    The synthesis sibling of ``POST /api/v1/search_docs``: resolve + entitle
    + readiness-gate the mandatory ``collection`` (same 403 / 409 / 422 arms
    as ``search_docs``), then answer through the one answer seam in-process
    (the collection backend's own answer endpoint when the collection opted
    in, else the #1916 expand -> retrieve-per-variant -> RRF-merge ->
    synthesize pipeline), returning ``{answer, citations[]}`` with every
    citation carrying its resolved navigable ``link`` (#1919).

    Single-collection only (no ``collections`` fan-out field). ``read_only``
    operators get 403 via :func:`require_role` before this handler. The
    central ``meho.docs.ask`` audit row is bound before the pipeline runs (so
    a leg failure is still attributable); the raw query is never bound --
    only its SHA-256 hash.
    """
    # Validate the binary scope; a missing/blank ``collection`` is the
    # mandatory-scope 422 (before any audit binding or pipeline call).
    try:
        docs_scope = build_docs_scope(body.collection, body.product, body.version)
    except MissingDocsFilterError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    # Pre-bind the canonical audit identity + scope. ``hit_count`` is bound
    # after synthesis returns; the rest are known up-front so a handler
    # exception (the entitlement / readiness / leg branches) still records the
    # query identity + scope. The raw query is never bound -- only its hash.
    structlog.contextvars.bind_contextvars(
        audit_op_id=_ASK_OP_ID,
        audit_op_class="read",
        audit_query_hash=_compute_query_hash(body.query),
        audit_collection=docs_scope.collection_key,
        audit_product=docs_scope.product,
        audit_version=docs_scope.version,
    )

    collection = await _resolve_collection_or_http_error(
        session, operator, docs_scope.collection_key
    )

    log = structlog.get_logger()
    try:
        answer = await run_ask_pipeline(
            operator,
            body.query,
            scope=docs_scope,
            collection=collection,
            limit=body.limit,
        )
    except AskDocsAnswerError as answer_error:
        # Fail-closed: a leg failure is a typed 5xx error envelope (the #1918
        # model, byte-identical to the MCP ``error.data``), never a degraded /
        # ungrounded answer. The structlog breadcrumb keeps the leg + cause.
        log.warning(
            "ask_docs_pipeline_failed",
            operator_sub=operator.sub,
            collection=docs_scope.collection_key,
            leg=answer_error.leg,
            cause=answer_error.cause,
        )
        _raise_pipeline_http_error(answer_error)

    structlog.contextvars.bind_contextvars(audit_hit_count=len(answer.citations))
    log.info(
        "ask_docs_endpoint_completed",
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
        collection=docs_scope.collection_key,
        product=docs_scope.product,
        version=docs_scope.version,
        citation_count=len(answer.citations),
    )
    return AskDocsResponse(
        answer=answer.answer,
        citations=[_citation_payload(chunk) for chunk in answer.citations],
    )


def _citation_payload(chunk: Any) -> dict[str, Any]:
    """Serialise a cited chunk with its resolved navigable ``link`` (#1919).

    The SAME shape the MCP ``ask_docs`` tool returns (its ``_citation_payload``):
    the chunk's full ``model_dump`` plus a ``link`` member resolved from the
    raw ``source_url`` via the shared
    :func:`~meho_backplane.docs_search.citation_link_payload` helper (KB ->
    ``knowledge.broadcom.com``, ``http(s)`` -> pass-through, anything else ->
    a non-clickable label -- never a broken ``gs://`` href). The raw
    ``source_url`` stays on the citation for provenance. Keeping the helper
    shared is what guarantees REST + MCP resolve citations identically.
    """
    payload: dict[str, Any] = chunk.model_dump(mode="json")
    payload["link"] = citation_link_payload(
        chunk.source_url,
        document_id=chunk.document_id,
    )
    return payload
