# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``POST /api/v1/retrieve`` -- operator-facing diagnostic retrieval surface.

G0.4-T5 (#262) of Initiative #225. The primary callers of the
retrieval substrate (G4 ``meho kb search`` and G5 ``meho recall``)
invoke :func:`~meho_backplane.retrieval.retriever.retrieve` directly
in-process; the HTTP route serves three purposes that the in-process
helper cannot:

* **Operator diagnostics.** "Is retrieval returning what I expect
  against the current kb corpus?" -- an operator can curl the route
  with a representative query and read back the per-signal scores +
  ranks without spinning up a Python REPL inside the cluster.
* **Cross-language consumers.** Future kb / memory CLIs in other
  language stacks consume the HTTP surface; the in-process helper
  is Python-only.
* **MCP tool surface.** G4 may register ``meho.kb.search`` as an
  MCP tool that wraps this route (G0.5 deferral -- the MCP
  resource ``meho://retrieve/{query}`` is filed as a v0.2.next
  follow-up).

RBAC
----

``operator`` role minimum. ``read_only`` operators get 403 --
diagnostic retrieval is operator-level (the same gate that protects
``/api/v1/rbac-test/operator``); ``read_only`` users typically use
G4 ``meho kb search`` as their entry point, which v0.2.next can
authorise differently.

Audit privacy contract
----------------------

The audit_log row's ``payload`` carries ``{query_hash, source,
kind, hit_count}`` -- **not the raw query**. Per the v0.2 sensitivity
defaults (decision #3 in ``docs/planning/v0.2-decisions.md``),
retrieval queries can leak operator intent. Storing the SHA-256 hex
digest + count gives auditability ("operator A made 5 retrieval
calls, hit-counts 12 / 7 / 0 / 0 / 3") without storing what they
searched. For deeper investigation, G8's audit-query API can
correlate the timestamp with broadcast feeds; the raw query is
deliberately ephemeral.

The route binds the four ``audit_*`` contextvars BEFORE calling
:func:`retrieve` so a handler exception still produces an audit row
with the partial payload it bound (``audit_hit_count`` would be
absent in that case -- the partial signal is itself useful for
incident postmortem).

Tenant scoping
--------------

The route passes ``operator.tenant_id`` to ``retrieve``; the JWT
verifier (G0.1-T2) is what made it a UUID. There is no API surface
that accepts a different tenant_id from the body / query string --
cross-tenant retrieval is impossible by construction.

Out of scope (deferred per Initiative body)
-------------------------------------------

* MCP resource ``meho://retrieve/{query}`` -- v0.2.next G0.5
  follow-up.
* Streaming hits (SSE) -- single batch response only.
* Cursor pagination -- ``limit`` parameter only; max 50 (the
  per-signal candidate ceiling).
* Per-tenant rate limiting -- v0.2 has no rate limiter.
* Embedding the query client-side -- operators don't have fastembed
  locally; the backplane embeds.
* Storing the raw query in audit_log -- explicitly out per privacy
  decision.
"""

from __future__ import annotations

import hashlib
import time

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.retrieval.retriever import RetrievalHit, retrieve

__all__ = ["router"]

router = APIRouter(prefix="/api/v1", tags=["retrieval"])


#: Module-level Depends closure for the route's RBAC gate. Building
#: it once at import time (rather than inline as
#: ``operator: Operator = Depends(require_role(TenantRole.OPERATOR))``)
#: keeps the FastAPI parameterised-dependency pattern compatible with
#: ruff's B008 rule, matching the convention
#: :mod:`meho_backplane.api.v1.rbac_test` established.
_require_operator = Depends(require_role(TenantRole.OPERATOR))


class RetrieveRequest(BaseModel):
    """POST body for ``/api/v1/retrieve``.

    Field validation is the load-bearing first gate: empty queries
    short-circuit at the framework (422 Unprocessable Entity) so the
    embedding pipeline never sees a zero-token input, and oversized
    queries are rejected before they hit the model. The
    ``max_length=2000`` cap is a generous upper bound on realistic
    free-form operator queries; operators with longer payloads
    should use the embedding pipeline directly via G4 / G5 ingestion
    paths.
    """

    model_config = ConfigDict(frozen=True)

    query: str = Field(min_length=1, max_length=2000)
    source: str | None = Field(default=None, max_length=64)
    kind: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=10, ge=1, le=50)


class RetrieveResponse(BaseModel):
    """Successful response shape for ``/api/v1/retrieve``.

    ``query_duration_ms`` is the wall-clock time from request entry
    to response build (covers both the embedding compute and the two
    SQL round-trips); operators tuning embedding-model choice or
    debugging retrieval latency consume this value. Frozen so a
    handler accidentally mutating it post-construction surfaces as a
    pydantic error rather than a silently-modified response.
    """

    model_config = ConfigDict(frozen=True)

    hits: list[RetrievalHit]
    query_duration_ms: float


def _compute_query_hash(query: str) -> str:
    """SHA-256 hex digest of *query* (UTF-8 encoded).

    Matches the same encoding contract
    :func:`~meho_backplane.retrieval.indexer.compute_body_hash`
    uses for document bodies, so an analyst correlating
    audit_log.payload.query_hash with a known query string can use a
    single hash function across both surfaces. SHA-256 is overkill
    for a per-request audit token (a 32-bit CRC would functionally
    suffice for collision purposes) but matches the rest of the
    chassis's hash discipline.
    """
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


@router.post("/retrieve", response_model=RetrieveResponse)
async def retrieve_endpoint(
    body: RetrieveRequest,
    operator: Operator = _require_operator,
) -> RetrieveResponse:
    """Run hybrid retrieval against the requesting tenant's corpus.

    Tenant-scoped to ``operator.tenant_id`` -- there is no surface
    that accepts a tenant id in the body. ``read_only`` operators get
    403 via :func:`require_role` before reaching this handler.

    Binds four ``audit_*`` contextvars **before** the retrieval call
    so the audit_log row's ``payload`` JSONB carries the privacy-
    preserving query trace even if the handler raises mid-retrieval
    (the partial payload is itself a useful incident-postmortem
    signal). The raw query is never bound -- only its SHA-256 hash.
    """
    start = time.monotonic()

    query_hash = _compute_query_hash(body.query)
    log = structlog.get_logger()
    # Pre-bind everything the audit middleware can lift into
    # `audit_log.payload`. `hit_count` is bound after retrieval; the
    # other three are known up-front so a handler exception still
    # produces an audit row with the query identity preserved.
    structlog.contextvars.bind_contextvars(
        audit_query_hash=query_hash,
        audit_source=body.source,
        audit_kind=body.kind,
    )

    hits = await retrieve(
        tenant_id=operator.tenant_id,
        query=body.query,
        source=body.source,
        kind=body.kind,
        limit=body.limit,
    )

    structlog.contextvars.bind_contextvars(audit_hit_count=len(hits))
    duration_ms = round((time.monotonic() - start) * 1000, 2)
    log.info(
        "retrieve_endpoint_completed",
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
        source=body.source,
        kind=body.kind,
        hit_count=len(hits),
        duration_ms=duration_ms,
    )
    return RetrieveResponse(hits=hits, query_duration_ms=duration_ms)
