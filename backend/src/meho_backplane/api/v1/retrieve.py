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
kind, metadata_filters_keys, hit_count}`` -- **not the raw query
or filter values**. Retrieval queries can leak operator intent,
and ``metadata_filters`` values may carry tenant-shaped identifiers
(operator subs, target names, customer ids depending on how the
consumer wrote its keys). Storing the SHA-256 hex digest of the
query, a sorted list of filter keys (no values), and the result
count gives auditability ("operator A made 5 retrieval calls,
filtered on [source_kind, product] twice, hit-counts 12 / 7 / 0 /
0 / 3") without leaking either what they searched for or the
specific filter values they scoped to. For deeper investigation,
G8's audit-query API can correlate the timestamp with broadcast
feeds; the raw query and filter values are deliberately ephemeral.

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
from typing import Any

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator

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


#: Cap on the number of keys in ``metadata_filters``. 20 is a
#: generous upper bound on realistic operator-shaped scoping queries
#: (``source_kind`` / ``product`` / ``sidecar_kind`` / ``user_sub`` /
#: ``target_name`` cover the consumer + memory recall cases at 5 keys
#: total; the headroom absorbs unforeseen consumers). Bound here
#: rather than in the substrate because the substrate is happy to
#: accept arbitrary dict sizes -- this is a per-request denial-of-
#: service guard, not a correctness invariant.
_METADATA_FILTERS_MAX_KEYS: int = 20


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

    ``extra="forbid"`` rejects unknown fields at 422 with
    ``extra_forbidden`` detail so a v0.2.1 client still sending the
    pre-rename ``q`` / ``top_k`` keys fails loud instead of silently
    running with the defaults (Signal #2 from the 2026-05-20 RDC
    dogfood). Same posture as every public v1 request schema.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    source: str | None = Field(default=None, max_length=64)
    kind: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=10, ge=1, le=50)
    metadata_filters: dict[str, Any] | None = Field(
        default=None,
        max_length=_METADATA_FILTERS_MAX_KEYS,
        description=(
            "Optional flat {key: scalar} dict. Translated to "
            "Postgres `documents.metadata @> :jsonb` containment in "
            "both candidate SQL statements alongside `source` / "
            "`kind`. Missing keys exclude rows (PG `@>` semantics); "
            "multi-key dicts behave as an intersection. Values must "
            "be JSON scalars (str / int / float / bool / None) -- "
            "nested objects or arrays raise 422. Keys are surfaced "
            "in the audit payload (sorted, no values) so an analyst "
            "can attribute scoping without storing tenant-shaped "
            f"filter values. At most {_METADATA_FILTERS_MAX_KEYS} keys."
        ),
    )

    @field_validator("metadata_filters")
    @classmethod
    def _values_must_be_json_scalars(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """Reject nested objects / arrays in ``metadata_filters`` values.

        The substrate translates ``metadata_filters`` to a PG ``@>``
        containment predicate. Containment supports arbitrary JSONB
        shapes on both sides, but the v0.2.next contract pins the
        shape to ``{key: scalar}`` -- a flat dict whose values are
        JSON scalars (``str`` / ``int`` / ``float`` / ``bool`` /
        ``None``). Nested objects and arrays open two cans the
        substrate does not want to inherit:

        * **DSL creep.** Allowing arrays would invite "match-any-of"
          semantics (consumer ticket would file it within weeks),
          but ``@>`` of an array is "element-wise contained subset",
          which is not the OR semantics the consumer would expect.
          Better to reject than ship a confusing match shape.
        * **Index-utility erosion.** The default ``jsonb_ops`` GIN
          index on ``documents.metadata`` (migration ``0032``)
          backs containment on flat scalar pairs cleanly; nested
          paths can fall back to seq-scan depending on the planner's
          estimate of selectivity. Pinning the shape keeps the
          index meaningful.

        Refile a separate task only if a consumer surfaces a
        measured need for richer containment shapes. The substrate
        accepts arbitrary string keys -- the taxonomy is a
        consumer-side convention.
        """
        if value is None:
            return None
        for key, val in value.items():
            if not isinstance(val, (str, int, float, bool, type(None))):
                raise ValueError(
                    f"metadata_filters[{key!r}] must be a JSON scalar "
                    f"(str/int/float/bool/None); got {type(val).__name__}"
                )
        return value


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
    # Surface the *keys* (sorted, no values) of `metadata_filters` to
    # the audit payload. Values may carry tenant-shaped identifiers
    # (operator subs, target names, customer ids) -- key-only matches
    # the same privacy posture `query_hash` enforces for the query.
    # `None` when no filter dict was sent so the audit-payload
    # resolver drops the key entirely (it strips `None` values).
    metadata_filters_keys = sorted(body.metadata_filters.keys()) if body.metadata_filters else None
    log = structlog.get_logger()
    # Pre-bind everything the audit middleware can lift into
    # `audit_log.payload`. `hit_count` is bound after retrieval; the
    # others are known up-front so a handler exception still produces
    # an audit row with the query identity preserved.
    structlog.contextvars.bind_contextvars(
        audit_query_hash=query_hash,
        audit_source=body.source,
        audit_kind=body.kind,
        audit_metadata_filters_keys=metadata_filters_keys,
    )

    # `principal_sub=operator.sub` enforces the mandatory, non-overridable
    # per-principal isolation predicate at the retrieval boundary (#1797):
    # a `source="memory"` user-scoped row (`memory-user` /
    # `memory-user-tenant` / `memory-user-target`) is only returned when
    # its stored `user_sub` equals the caller's `sub`. This closes the
    # cross-principal leak where a client passed `metadata_filters`
    # straight through with no per-principal scoping; the predicate is
    # AND-enforced in the substrate, so a client-supplied
    # `metadata_filters={"user_sub": "<other>"}` cannot widen it.
    hits = await retrieve(
        tenant_id=operator.tenant_id,
        query=body.query,
        source=body.source,
        kind=body.kind,
        limit=body.limit,
        metadata_filters=body.metadata_filters,
        principal_sub=operator.sub,
    )

    structlog.contextvars.bind_contextvars(audit_hit_count=len(hits))
    duration_ms = round((time.monotonic() - start) * 1000, 2)
    log.info(
        "retrieve_endpoint_completed",
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
        source=body.source,
        kind=body.kind,
        metadata_filters_keys=metadata_filters_keys,
        hit_count=len(hits),
        duration_ms=duration_ms,
    )
    return RetrieveResponse(hits=hits, query_duration_ms=duration_ms)
