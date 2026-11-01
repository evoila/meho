# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: file-size — pre-existing >600-line shared meta-tool
# module (915 lines before this change; it co-locates the four agent
# meta-tool handlers + their Pydantic wire models, shared verbatim across
# the MCP, REST, and CLI surfaces — that wire identity is the point). The
# #1648 change is a localised list_operation_groups visibility fix
# (partial-marker fields + one query predicate); a module split would
# touch every surface and is explicitly out of scope here. Tracked
# separately if it lands.

"""Three operation meta-tools backing the agent's working surface.

G0.6-T8 (#399) of Initiative #388. The three operation meta-tools form
the agent-facing surface over the G0.6 substrate; each is registered as
both an MCP tool (see :mod:`meho_backplane.mcp.tools.operations`) and a
REST route (see :mod:`meho_backplane.api.v1.operations`). The CLI
``meho operation ...`` verbs mirror the same surface. All four surfaces
share these handlers verbatim — the wire-shape unification is the point.

Per CLAUDE.md postulate 5, the agent never sees per-op MCP tools like
``k8s.pod.list``; it sees a small fixed set of meta-tools and picks
operations through them. This module ships three of the ~17 meta-tools
that comprise that surface:

* :func:`list_operation_groups` — enumerate enabled operation groups
  for a connector. The agent uses this to decide *which group* to
  search within before issuing a query.
* :func:`search_operations` — hybrid BM25 + cosine RRF retrieval over
  ``endpoint_descriptor`` rows scoped to a connector and (optionally)
  a group. Same RRF algorithm the G0.4 retrieval substrate uses for
  documents; the retrieval ranks the agent's best candidate operations.
* :func:`call_operation` — invokes :func:`~meho_backplane.operations.dispatch`
  after resolving the optional target descriptor via
  :func:`~meho_backplane.targets.resolver.resolve_target`. Returns the
  full :class:`~meho_backplane.connectors.schemas.OperationResult`
  shape: status / result / error / extras / duration_ms.

Tenant scoping
==============

Every read query filters by ``tenant_id`` with the same predicate the G0.6
dispatcher's :func:`~meho_backplane.operations._lookup.lookup_descriptor`
uses (:func:`~meho_backplane.operations._lookup.visible_scope_clause`).
When a tenant has its own row for a connector, only that row is visible to
the tenant's operators; the built-in row for the same connector is not
used (#3870). Otherwise the built-in rows (``tenant_id IS NULL``) are
visible. Another tenant's rows are never visible.

Hybrid retrieval over endpoint_descriptor
=========================================

The same BM25 + cosine RRF algorithm
:mod:`meho_backplane.retrieval.retriever` ships for the ``documents``
table runs here against ``endpoint_descriptor``. The two implementations
are intentionally parallel — same RRF fusion math, same per-signal
candidate-pull limit, same embedding service — but the table schema is
different so the SQL diverges. Migration ``0005`` provisioned the
BM25 GIN expression index (``endpoint_descriptor_bm25_idx``) over
``coalesce(summary, '') || ' ' || coalesce(description, '')`` and the
IVFFlat cosine index (``endpoint_descriptor_embedding_idx``) over
``embedding vector_cosine_ops``. Both are PG-only; SQLite tests
fall back to in-process ranking by re-running the candidate
SELECT without the FTS / vector operators (the SQLAlchemy ``select``
form filters by ``tenant_id`` + ``is_enabled`` and orders by ``op_id``
so the deterministic ordering matches what the integration test
needs to assert).

Output schemas
==============

The three handlers' return shapes are pinned to JSON-Schema 2020-12
fragments inside :mod:`meho_backplane.mcp.tools.operations` (the MCP
``outputSchema`` field on each tool) and re-used by the REST router
(:mod:`meho_backplane.api.v1.operations`) as Pydantic response models.
Wire identity across MCP and REST is the contract the CLI relies on.
"""

from __future__ import annotations

import time
import uuid
from contextlib import AsyncExitStack
from typing import Annotated, Any, Final, Literal, get_args

import structlog
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, WithJsonSchema
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup, PermissionVerdict
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations._contract import UnknownOperationError, build_operation_contract
from meho_backplane.operations._errors import (
    result_ambiguous_target,
    result_no_target,
    result_target_invalid_type,
    result_target_required,
    unknown_connector_message,
)
from meho_backplane.operations._lookup import (
    close_connector_ids,
    connector_class_registered,
    connector_exists,
    enabled_for_tenant_clause,
    group_visible_for_tenant_clause,
    installed_owner_clause,
    parse_connector_id,
    visible_scope_clause,
)
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations._search import hybrid_search, resolve_group_id
from meho_backplane.operations.addon_orchestration import (
    bound_parent_linkage,
    resolve_or_open_orchestration_run,
)
from meho_backplane.operations.delivery_budget import (
    bound_complete_response,
    refuse_undeliverable_receipt,
)
from meho_backplane.operations.dispatch_limits import rate_limited_read_envelope
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.ingest.list_connectors import next_step_for_registered_connector
from meho_backplane.operations.op_policy import OpKey, effective_policy_map
from meho_backplane.retrieval.retriever import RRF_K
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import (
    AmbiguousTargetError,
    TargetNotFoundError,
    resolve_target,
)

__all__ = [
    "SEARCH_CLASSIFY_MIN_HITS",
    "SEARCH_LIMIT_MAX",
    "SEARCH_NARROWING_MAX",
    "SEARCH_NEAR_TIE_BAND",
    "SEARCH_OUTCOMES",
    "CallOperationBody",
    "ConnectorNotIngestedError",
    "OperationDescriptor",
    "OperationGroupSummary",
    "OperationSearchHit",
    "PreviewOperationBody",
    "SearchOutcome",
    "UnknownConnectorError",
    "UnknownOperationError",
    "apply_target_fqdn_override",
    "call_operation",
    "call_operation_with_approval",
    "describe_descriptor",
    "list_operation_groups",
    "preview_operation",
    "search_operations",
]


class UnknownConnectorError(ValueError):
    """Raised when a ``connector_id`` names no registered connector.

    A *domain* exception, not a transport one. The shared meta-tool
    handlers (REST route, MCP transport, CLI) must not couple to
    :class:`fastapi.HTTPException` — the MCP server's generic
    ``except Exception`` would otherwise mistranslate a clean
    "unknown connector" into a JSON-RPC ``INTERNAL_ERROR`` (a worse
    trap than the empty-200 this task removes). The REST route maps
    this to ``404`` explicitly; the MCP transport maps it to
    ``INVALID_PARAMS`` (``-32602``) with a typed ``data`` payload (see
    :mod:`meho_backplane.mcp.tools.operations`). Subclasses
    :class:`ValueError` to stay consistent with the other meta-tool
    domain exceptions (e.g. the missing-target ``ValueError`` mapped
    to ``400`` in :mod:`meho_backplane.api.v1.operations`).

    Carries :attr:`connector_id` (the id the caller passed) and
    :attr:`did_you_mean` (up to 5 close ids the caller can see, #4136) so
    the MCP transport can thread them onto the ``error.data``
    discriminator alongside its :class:`ConnectorNotIngestedError` sibling.
    """

    def __init__(
        self,
        message: str,
        *,
        connector_id: str,
        did_you_mean: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.connector_id = connector_id
        self.did_you_mean = did_you_mean or []

    def as_error_data(self) -> dict[str, Any]:
        """Render the structured ``error.data`` payload for the MCP surface.

        Shape: ``{"reason": "unknown_connector", "connector_id": ...,
        "did_you_mean": [...]}``.
        """
        return {
            "reason": "unknown_connector",
            "connector_id": self.connector_id,
            "did_you_mean": self.did_you_mean,
        }

    @classmethod
    async def for_caller(cls, *, tenant_id: uuid.UUID, connector_id: str) -> UnknownConnectorError:
        """Build the error with the close ids *tenant_id* can see (#4136)."""
        did_you_mean = await close_connector_ids(tenant_id=tenant_id, connector_id=connector_id)
        return cls(
            unknown_connector_message(connector_id, did_you_mean),
            connector_id=connector_id,
            did_you_mean=did_you_mean,
        )


class ConnectorNotIngestedError(ValueError):
    """Raised when a ``connector_id`` names a *registered-but-not-ingested* connector.

    The connector's class is registered in the v2 registry (via
    :func:`~meho_backplane.connectors.registry.register_connector_v2`) but
    has no ``endpoint_descriptor`` / ``operation_group`` rows yet — the
    "State 0.5" the ``GET /api/v1/connectors`` listing already surfaces as
    ``state="registered"`` with a ``next_step`` ingest hint. The meta-tools
    raise this *instead of* :class:`UnknownConnectorError` so the caller can
    tell "exists, run ingest" apart from "no such connector" rather than
    receiving an opaque ``-32603 UnknownConnectorError`` over MCP (#1482).

    Carries the structured detail the surfaces self-correct against:

    * :attr:`connector_id` — the id the caller passed.
    * :attr:`next_step` — the ``{"verb", "rationale"}`` ingest hint built
      from the same connector-spec-catalog lookup the listing uses
      (:func:`~meho_backplane.operations.ingest.list_connectors._next_step_for_registered`),
      or ``None`` when the catalog/registry could not produce one.

    Subclasses :class:`ValueError` like its sibling so existing
    ``except ValueError`` call sites keep their behaviour; the MCP handler
    threads :attr:`as_error_data` onto the JSON-RPC ``error.data`` member
    and the REST route maps it to ``404`` with the hint in ``detail``.
    """

    def __init__(
        self,
        message: str,
        *,
        connector_id: str,
        next_step: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.connector_id = connector_id
        self.next_step = next_step

    def as_error_data(self) -> dict[str, Any]:
        """Render the structured ``error.data`` payload for the MCP surface.

        Shape: ``{"reason": "connector_not_ingested", "connector_id": ...,
        "next_step": {"verb", "rationale"} | None}``. ``reason`` is the
        machine-readable discriminator an agent keys on to self-correct to
        the ``next_step.verb`` ingest command.
        """
        return {
            "reason": "connector_not_ingested",
            "connector_id": self.connector_id,
            "next_step": self.next_step,
        }


_log = structlog.get_logger(__name__)


#: Hard cap on ``search_operations`` ``limit``. 50 matches the per-signal
#: candidate-pull in :mod:`~meho_backplane.retrieval.retriever`; pulling
#: more than that returns a strict subset of the same fused list with no
#: extra signal. Clamping keeps the API surface from issuing pathological
#: ranking queries against a corpus that will eventually grow to ~10k
#: descriptors per connector.
SEARCH_LIMIT_MAX: int = 50

#: The ``outcome`` values every ``search_operations`` result carries (#3670).
#: An unknown or not-yet-ingested ``connector_id`` is not in this list: those
#: two cases stay errors (``-32602`` on MCP, ``404`` on REST).
#:
#: * ``matched`` — the top hit matched the query's words and is clearly
#:   ahead of the next hit.
#: * ``ambiguous`` — hits came back, but they are weak: the top hit did not
#:   match the query's words (it is only close in meaning), or the top two
#:   hits are nearly tied. Narrow the search before calling anything.
#: * ``no_match`` — the connector has enabled operations, but none matched.
#: * ``unknown_group`` — the ``group`` filter names no visible group.
#: * ``zero_enabled_operations`` — the connector is ingested, but none of
#:   its operations is enabled for this caller. Nothing can be called.
#: * ``package_not_installed`` — the connector's enabled operations all come
#:   from a connector package that is not installed in this MEHO (#5560).
#:   The advisory names the package. Nothing can be called until the
#:   package is installed again.
SearchOutcome = Literal[
    "matched",
    "ambiguous",
    "no_match",
    "unknown_group",
    "zero_enabled_operations",
    "package_not_installed",
]
SEARCH_OUTCOMES: Final[tuple[str, ...]] = get_args(SearchOutcome)

#: Most entries a ``search_operations`` ``narrowing`` list holds.
SEARCH_NARROWING_MAX: Final[int] = 20

#: Fewest hits :func:`_classify_hits` needs to tell ``matched`` from a
#: near tie. ``search_operations`` always reads at least this many from the
#: retriever, even when the caller asked for ``limit=1``, and cuts the list
#: down to the caller's ``limit`` afterwards. Otherwise a tied second hit
#: would be hidden and the result would read as ``matched``.
SEARCH_CLASSIFY_MIN_HITS: Final[int] = 2

#: Near-tie band for the ``ambiguous`` outcome. When the top hit's
#: ``fused_score`` beats the second hit's by less than this, the two are
#: treated as tied. The value is one rank step at the top of a single RRF
#: signal: a top hit only wins clearly when it is ahead by more than one
#: list position. This reads the scores the retriever already produced; it
#: never changes hit order. Tuning belongs to the routing evaluation.
SEARCH_NEAR_TIE_BAND: Final[float] = 1.0 / (RRF_K + 1) - 1.0 / (RRF_K + 2)


# ---------------------------------------------------------------------------
# Pydantic response models (shared with the REST router + MCP outputSchema)
# ---------------------------------------------------------------------------


class OperationGroupSummary(BaseModel):
    """One operation group as returned by :func:`list_operation_groups`.

    ``when_to_use`` is the LLM-summarised string the agent reads to pick
    which group to search within. Populated by G0.7's ingestion pipeline
    for ``source_kind='ingested'`` connectors; supplied verbatim by
    :func:`~meho_backplane.operations.register_typed_operation` callers
    for typed connectors.

    A group surfaces here when it is either fully enabled
    (``review_status='enabled'``) **or** still ``staged``/``disabled`` at
    the group level yet holding at least one per-op-enabled descriptor
    (``edit_op is_enabled=true``). The latter is flagged ``partial=True``
    so groups-first discovery isn't blind to per-op enablement — an agent
    that called ``list_operation_groups`` FIRST (as the tool description
    instructs) still sees the group whose ops ``search_operations`` and
    dispatch already treat as live (claude-rdc-hetzner-dc#1136).

    * ``operation_count`` / ``enabled_op_count`` — both count the group's
      ``is_enabled=True`` descriptors. They are equal by construction;
      ``enabled_op_count`` names the semantic explicitly so a ``partial``
      group's live-op count reads unambiguously, and it stays stable if
      ``operation_count`` is ever redefined to a total.
    * ``partial`` — ``True`` exactly when the group's own
      ``review_status`` is not ``'enabled'`` (so its visibility here is
      owed solely to per-op enablement, which always implies
      ``enabled_op_count >= 1``); ``False`` for a fully-enabled group.
    """

    model_config = ConfigDict(frozen=True)

    group_key: str
    name: str
    when_to_use: str
    operation_count: int
    enabled_op_count: int
    partial: bool


class OperationSearchHit(BaseModel):
    """One ranked hit from :func:`search_operations`.

    The three score fields are the same per-signal shape as
    :class:`~meho_backplane.retrieval.retriever.RetrievalHit`. They help
    people who tune operation descriptions see whether a hit matched on
    words (BM25), on meaning (cosine), or both. They are not on the wire
    by default (#3668): :func:`search_operations` drops them unless a
    ``tenant_admin`` caller asks for them with ``include_scores``.
    """

    model_config = ConfigDict(frozen=True)

    op_id: str
    summary: str | None
    description: str | None
    group_key: str | None
    safety_level: str
    requires_approval: bool
    fused_score: float
    bm25_score: float | None
    cosine_score: float | None


#: Ranking fields kept off a search hit unless a ``tenant_admin`` caller
#: opts in with ``include_scores`` (#3668).
_SCORE_FIELDS: Final[frozenset[str]] = frozenset({"fused_score", "bm25_score", "cosine_score"})


class OperationDescriptor(BaseModel):
    """Full :class:`~meho_backplane.db.models.EndpointDescriptor` read shape.

    Returned by :func:`describe_descriptor` (and the
    ``GET /api/v1/operations/{descriptor_id}`` route). ``embedding`` is
    deliberately omitted — it's a 384-dim float vector that adds wire
    bulk with no operator value at the descriptor-inspection layer.
    ``llm_instructions`` IS included; the route is gated on
    ``tenant_admin`` so the per-op agent prompt stays out of read-only
    operators' hands.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID | None
    product: str
    version: str
    impl_id: str
    op_id: str
    source_kind: str
    method: str | None
    path: str | None
    handler_ref: str | None
    summary: str | None
    description: str | None
    group_id: uuid.UUID | None
    group_key: str | None
    tags: list[str]
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    llm_instructions: dict[str, Any] | None
    safety_level: str
    requires_approval: bool
    is_enabled: bool
    custom_description: str | None
    custom_notes: str | None


#: The *documented* ``target`` shape — what a well-formed request sends and
#: what codegen'd clients (the CLI's oapi-codegen union type) build against.
_TARGET_DECLARED_SCHEMA: Final[dict[str, Any]] = TypeAdapter(
    str | dict[str, Any] | None
).json_schema()

#: ``target`` field annotation for :class:`CallOperationBody` /
#: :class:`PreviewOperationBody` (#2110). The runtime type is ``Any`` so a
#: wrong-JSON-typed ``target`` (``12345``, ``true``, ``[...]``) passes body
#: validation and reaches the meta-tool seam, where
#: :func:`_normalize_target_arg` classifies it and
#: :func:`_resolve_target_or_error` returns the ``target_invalid_type``
#: dispatcher envelope — HTTP 200, one ``extras.error_code`` switch, no
#: FastAPI 422 ``detail[]`` array left for consumers to special-case (the
#: issue #2110 Option-A decision, superseding the #136 schema-vs-resolution
#: boundary). :class:`~pydantic.WithJsonSchema` pins the *published* schema to
#: the pre-#2110 ``anyOf [string, object, null]`` union — byte-identical
#: OpenAPI, so generated clients keep the honest "what you should send" shape
#: and the CLI's Go union type does not churn.
_TargetArg = Annotated[Any, WithJsonSchema(_TARGET_DECLARED_SCHEMA)]


class CallOperationBody(BaseModel):
    """Request body for the ``POST /api/v1/operations/call`` route.

    Mirrors the :func:`call_operation` ``arguments`` shape so the route
    and the MCP handler share validation. ``target`` accepts either
    shape and ``None``:

    * **Bare string** -- ``target: "prod-k8s"``. The forward-preferred
      shape; matches ``query_topology`` / ``query_audit`` so an agent
      can carry a target name across the read and the write surfaces
      without reshape. G0.13-T2 (#1132) widening of #780.
    * **Dict** -- ``target: {"name": "prod-k8s"}``. The original
      shape; the handler resolves the ``name`` field and (optionally)
      reads ``fqdn`` for vhost routing. Still accepted unchanged.
    * **None** -- the operation does not need a target (typed handlers
      that don't read it; composite handlers that do their own
      resolution).

    Both shapes normalise to the same dict before dispatch, so the
    resolver and the connectors see one canonical form. A bare-string
    ``target`` is equivalent to ``{"name": <string>}`` -- no ``fqdn``
    override can be passed via the string shape; callers that need
    the override stay on the dict.

    Recognised keys on the ``target`` dict:

    * ``name`` (required when ``target`` is supplied as a dict) -- the
      slug or alias the resolver looks up in the targets registry.
    * ``fqdn`` (optional) -- per-call override for the resolved
      target's ``fqdn`` column. Honoured by connectors that read
      ``target.fqdn`` for vhost routing (G3.6 VCF Automation:
      the appliance enforces strict ``Host:`` matching and returns
      404 with empty body when reached by IP without the correct
      vhost set). The override mutates the resolved Target in
      memory only; the database row is not modified. Any other key
      in the dict is silently ignored, mirroring the documented
      forward-compatibility posture in the MCP tool schema.

    ``extra="forbid"`` (G0.9-T2 / #729) rejects unknown *body* fields
    with 422 ``extra_forbidden`` -- a typo in ``connector_id`` or an
    unknown sibling field still fails loud. The ``target`` field's
    own union widening is orthogonal: bare-string is now a first-class
    valid value, not an unknown field. A ``target`` of any *other* JSON
    type (``12345``, ``true``, ``[...]``) is NOT a 422 -- it rides the
    dispatcher envelope as ``target_invalid_type`` (#2110; see
    :data:`_TargetArg`), so every target-failure mode is HTTP 200 +
    ``extras.error_code``. ``params`` itself is a
    free-form ``dict`` because per-op parameter shape is enforced by
    the descriptor's ``parameter_schema`` further down the dispatch
    path; only the meta-tool body's own fields are constrained here.

    ``work_ref`` (work_ref I1-T2 #1657) is the optional external
    change-ticket reference -- an opaque typed-URI string
    (``"gh:evoila/meho#7"``) correlating this dispatch to the
    out-of-band change record that authorised it. When supplied, it is
    the per-op override the Goal #1651 design calls for:
    :func:`_call_operation_impl` binds it onto
    :data:`~meho_backplane.operations._audit.work_ref_var` for the
    duration of the dispatch, so the DISPATCH ``audit_log`` row carries
    it ahead of any ambient ``Meho-Work-Ref`` header bound at the
    chassis boundary. ``None`` leaves the ambient binding (or NULL)
    untouched. The field stores an opaque string -- no validation, no
    tracker API call (Goal #1651 ships the field only).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    connector_id: str = Field(min_length=1)
    op_id: str = Field(min_length=1)
    target: _TargetArg = None
    params: dict[str, Any] = Field(default_factory=dict)
    work_ref: str | None = Field(default=None, min_length=1)
    preview_hash: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Preview-result-hash binding for the destructive tier (#3197). "
            "The hash a prior preview_operation of the identical "
            "(connector_id, op_id, target, params) returned. REQUIRED for a "
            "safety_level='destructive' op — the dispatcher refuses to park "
            "the approval fail-closed unless it matches the server-recomputed "
            "preview hash. Ignored for every non-destructive op. None default "
            "keeps a bare call_operation byte-identical to pre-#3197."
        ),
    )
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=(
            "Optional retry key for a call that waits for approval (#3486). "
            "If you send the same key again with the identical call, MEHO "
            "returns the approval request it already made (same "
            "approval_request_id, extras.approval_request_reused=true) "
            "instead of a second one. The key belongs to you: another "
            "principal's key never matches. It is free again once that "
            "request is rejected or expires. The same key with a different "
            "call is refused with idempotency_key_conflict. Ignored when the "
            "call does not wait for approval."
        ),
    )
    async_: bool = Field(
        default=False,
        alias="async",
        description=(
            "Async governed dispatch (#3079). When true, ``/call`` returns a "
            "durable run handle (HTTP 202) immediately instead of holding the "
            "connection for the op's full duration; execution proceeds "
            "server-side and the caller polls / cancels via "
            "``/api/v1/operations/runs/{handle}``. The completed "
            "``OperationResult`` envelope is persisted on the run, so a "
            "dropped response never loses the outcome. Default false — sync "
            "mode is byte-identical to the pre-#3079 behaviour."
        ),
    )


class PreviewOperationBody(BaseModel):
    """Request body for the ``POST /api/v1/operations/preview`` route.

    Same field shape as :class:`CallOperationBody` (#1683) -- a preview
    resolves the *same* op + target + params a real ``call_operation``
    would, then returns the literal would-be HTTP request instead of
    sending it. Keeping the body identical means an operator who hit a
    write 4xx can re-issue the exact arguments against ``/preview`` to see
    what was put on the wire (the audit row persists only a hashed
    ``params_hash``, so the request shape is otherwise unrecoverable).

    ``target`` accepts the same three shapes as ``call_operation`` (bare
    string, dict ``{"name": ...}``, or ``None``); see
    :class:`CallOperationBody` for the convention. A wrong-JSON-typed
    ``target`` rides the ``target_invalid_type`` envelope instead of a 422
    (#2110; see :data:`_TargetArg`), identical to ``/call``.
    ``extra="forbid"`` keeps a typo in ``connector_id`` failing loud rather
    than silently previewing nothing.
    """

    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(min_length=1)
    op_id: str = Field(min_length=1)
    target: _TargetArg = None
    params: dict[str, Any] = Field(default_factory=dict)


class PreviewOperationResponse(BaseModel):
    """Response envelope of ``POST /api/v1/operations/preview`` (#4923).

    Types the envelope :func:`preview_operation` returns so the OpenAPI
    document and the generated clients know its fields. The route serialises
    only the keys the envelope carries (``response_model_exclude_unset``):
    an ``error`` envelope has no ``method``, an ingested ``ok`` preview has no
    ``plan_shown``, and so on. ``extra="allow"`` keeps the keys a
    target-resolution or rate-limit envelope adds (``duration_ms``,
    ``retry_after_seconds``, ...) on the wire unchanged.
    """

    model_config = ConfigDict(extra="allow")

    status: Literal["ok", "error", "unavailable", "rate_limited"] = Field(
        description=(
            "ok: the would-be request (or the synthetic preview) resolved. "
            "error: a structured fault, see `error` and `extras.error_code`. "
            "unavailable: a typed/composite op outside the approval tiers has "
            "no request to preview. rate_limited: over the per-minute dispatch "
            "cap for this principal."
        )
    )
    op_id: str | None = Field(default=None, description="Echo of the requested op id.")
    connector_id: str | None = Field(
        default=None,
        description=(
            "The connector the preview resolved against. May differ from the "
            "requested id when a sibling catalog serves the target."
        ),
    )
    source_kind: str | None = Field(
        default=None,
        description="`ingested`, `typed` or `composite`; absent when the op is unknown.",
    )
    method: str | None = Field(
        default=None,
        description="Resolved HTTP verb (status=ok); `COMPOSITE` for a synthetic preview.",
    )
    resolved_path: str | None = Field(
        default=None,
        description=(
            "Fully resolved request path, placeholders substituted and mount prefix "
            "applied (status=ok); the op id for a synthetic preview."
        ),
    )
    query: dict[str, Any] | None = Field(
        default=None, description="Query-string params, or null when there are none (status=ok)."
    )
    redacted_body: Any = Field(
        default=None,
        description=(
            "The would-be request body after the connector-boundary redaction "
            "pipeline; null when the op declares no body (status=ok)."
        ),
    )
    preview_hash: str | None = Field(
        default=None,
        description=(
            "Stable SHA-256 over the resolved request (status=ok). Present it as "
            "`preview_hash` on the governed call of a destructive-tier op."
        ),
    )
    safety_level: str | None = Field(
        default=None,
        description=(
            "The op's risk level: safe, caution, dangerous or destructive. Present on "
            "every preview whose op resolved (every status except unknown_op / "
            "unknown_connector)."
        ),
    )
    requires_approval: bool | None = Field(
        default=None,
        description=(
            "True when the op is marked as needing a human approval before it runs. "
            "Present whenever `safety_level` is."
        ),
    )
    policy_verdict: PermissionVerdict | None = Field(
        default=None,
        description=(
            "What MEHO would do with this call for this caller and target: run it "
            "at once (auto-execute), wait for a second person's approval "
            "(needs-approval) or refuse it (deny). Decided the same way the call "
            "is decided; nothing is written. Present whenever `safety_level` is."
        ),
    )
    policy_reason: str | None = Field(
        default=None,
        description=(
            "The policy gate's reason for the verdict; null when it gives none "
            "(a person's call that runs at once)."
        ),
    )
    proposed_effect: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The plan of what the op will change, the same block the approval "
            "request shows. Present only on a synthetic preview whose plan can be "
            "built without contacting the target."
        ),
    )
    plan_shown: bool | None = Field(
        default=None,
        description=(
            "Synthetic preview only: true when `proposed_effect` is present. False "
            "means the plan needs the target and is shown on the approval request."
        ),
    )
    plan_note: str | None = Field(
        default=None, description="Plain reason the plan is not shown (when `plan_shown` is false)."
    )
    error: str | None = Field(
        default=None,
        description="Human-readable fault (status=error / unavailable / rate_limited).",
    )
    extras: dict[str, Any] | None = Field(
        default=None,
        description="Structured fault details, for example `error_code` or `validation_errors`.",
    )


# ---------------------------------------------------------------------------
# Meta-tool handlers
# ---------------------------------------------------------------------------


#: Default page size for :func:`list_operation_groups` keyset
#: pagination. Operation-group surfaces are usually small (O(10) per
#: connector); 100 is a comfortable ceiling that keeps every real
#: connector on one page while still bounding the response size for a
#: pathological connector with thousands of groups (G0.18-T5 #1358 —
#: parity with `list_targets` paging).
_LIST_OPERATION_GROUPS_LIMIT_DEFAULT: Final[int] = 100

#: Hard ceiling on :func:`list_operation_groups` ``limit``. Matches
#: :data:`meho_backplane.mcp.tools.topology._LIST_TARGETS_LIMIT_MAX`'s
#: 500 ceiling so sibling list tools share one upper bound.
_LIST_OPERATION_GROUPS_LIMIT_MAX: Final[int] = 500


def _coerce_list_operation_groups_pagination(
    arguments: dict[str, Any],
) -> tuple[int, str | None]:
    """Extract + bounds-check the ``(limit, cursor)`` pagination pair.

    Defaults `limit` to :data:`_LIST_OPERATION_GROUPS_LIMIT_DEFAULT`;
    clamps to ``[1, _LIST_OPERATION_GROUPS_LIMIT_MAX]``; coerces an
    empty / non-string `cursor` to ``None`` so the substrate query's
    `where(group_key > cursor)` branch is skipped on the first page.
    """
    limit = int(arguments.get("limit") or _LIST_OPERATION_GROUPS_LIMIT_DEFAULT)
    if limit < 1 or limit > _LIST_OPERATION_GROUPS_LIMIT_MAX:
        raise ValueError(
            f"limit out of range: {limit} (must be in [1, {_LIST_OPERATION_GROUPS_LIMIT_MAX}])",
        )
    cursor_arg = arguments.get("cursor")
    cursor = cursor_arg if isinstance(cursor_arg, str) and cursor_arg else None
    return limit, cursor


async def _count_ops_per_group(
    session: Any,
    group_ids: list[uuid.UUID],
    *,
    tenant_id: uuid.UUID,
) -> dict[uuid.UUID, int]:
    """Count the ops that are on for *tenant_id*, per group, in one query.

    "On" is :func:`enabled_for_tenant_clause` (the tenant's own setting
    first, then the install default, #5029). One SQL round-trip + Python
    aggregation keeps the application-side group-by portable across PG +
    SQLite. Excludes NULL ``group_id`` — ungrouped ops don't contribute.
    """
    if not group_ids:
        return {}
    count_stmt = select(EndpointDescriptor.group_id).where(
        EndpointDescriptor.group_id.in_(group_ids),
        enabled_for_tenant_clause(tenant_id=tenant_id),
        installed_owner_clause(),
    )
    count_rows = (await session.execute(count_stmt)).all()
    counts: dict[uuid.UUID, int] = {}
    for row in count_rows:
        gid = row[0]
        if gid is not None:
            counts[gid] = counts.get(gid, 0) + 1
    return counts


def _build_operation_groups_query(
    *,
    product: str,
    version: str,
    impl_id: str,
    tenant_id: uuid.UUID,
    limit: int,
    cursor: str | None,
) -> Any:
    """Build the keyset-paginated :class:`OperationGroup` SELECT.

    Filters: matching connector triple + tenant scope
    (:func:`~meho_backplane.operations._lookup.visible_scope_clause`) + a
    *visibility* predicate; orders by ``group_key``
    ASC; capped at ``limit``. ``cursor`` (when given) advances past
    the prior page's last ``group_key`` via strict ``>`` — keyset
    pagination with no offset and no overlap.

    Visibility predicate (claude-rdc-hetzner-dc#1136): a group surfaces
    when it is fully enabled (``review_status='enabled'``) **or** still
    ``staged``/``disabled`` at the group level yet holding ≥1 per-op-
    enabled descriptor. The per-op branch is a correlated ``EXISTS`` over
    ``endpoint_descriptor`` tied by ``group_id``, so the connector triple
    and tenant scope are inherited from the matched group row (the same
    transitive scoping :func:`_count_ops_per_group` relies on) without
    re-stating them on the descriptor. This keeps groups-first discovery
    in sync with ``search_operations`` + dispatch, which already key off
    per-op ``is_enabled``; the marker fields on
    :class:`OperationGroupSummary` tell a fully-enabled group apart from
    a per-op-only (``partial``) one. The predicate lives in
    :func:`~meho_backplane.operations._lookup.group_visible_for_tenant_clause`
    so it also reads the tenant's own on/off settings (#5029) and hides a
    group whose rows all belong to a connector package that is not
    installed (#5037).
    """
    stmt = (
        select(OperationGroup)
        .where(
            OperationGroup.product == product,
            OperationGroup.version == version,
            OperationGroup.impl_id == impl_id,
            group_visible_for_tenant_clause(tenant_id=tenant_id),
            visible_scope_clause(
                OperationGroup,
                tenant_id=tenant_id,
                product=product,
                version=version,
                impl_id=impl_id,
            ),
        )
        .order_by(OperationGroup.group_key)
        .limit(limit)
    )
    if cursor is not None:
        stmt = stmt.where(OperationGroup.group_key > cursor)
    return stmt


async def _require_dispatchable_connector(
    *,
    operator: Operator,
    connector_id: str,
    product: str,
    version: str,
    impl_id: str,
) -> None:
    """Gate the discovery meta-tools on a *dispatchable* connector.

    Shared by :func:`list_operation_groups` and :func:`search_operations`
    so the two enforce the identical unknown / not-ingested / known
    taxonomy. Three outcomes for the parsed *(product, version, impl_id)*
    triple:

    * **DB rows exist** (:func:`connector_exists` ``True``) — the connector
      is ingested; return so the caller proceeds to its (possibly empty)
      data query. An ingested connector with zero *enabled* groups still
      falls here and yields the operationally-meaningful empty list — the
      existence gate is enable-agnostic. ``search_operations`` labels that
      case ``outcome="zero_enabled_operations"`` (#3670).
    * **No DB rows, class registered** (:func:`connector_class_registered`
      ``True``) — "State 0.5": the connector is v2-registered but awaiting
      ingest. Raise :class:`ConnectorNotIngestedError` carrying the same
      ``next_step`` ingest hint ``GET /api/v1/connectors`` renders on the
      ``state="registered"`` row, so the agent can self-correct to the
      ``meho connector ingest …`` verb instead of receiving an opaque
      unknown-connector error (#1482).
    * **No DB rows, no class** — genuinely unknown. Raise
      :class:`UnknownConnectorError`, which names up to 5 close ids the
      caller can see (#4136).
    """
    if await connector_exists(
        tenant_id=operator.tenant_id,
        product=product,
        version=version,
        impl_id=impl_id,
    ):
        return
    if connector_class_registered(product=product, version=version, impl_id=impl_id):
        next_step = next_step_for_registered_connector(
            product=product,
            version=version,
            impl_id=impl_id,
        )
        verb = next_step.verb if next_step is not None else "meho connector ingest …"
        raise ConnectorNotIngestedError(
            f"connector {connector_id!r} is registered but not yet ingested "
            f"(no operations available). Run `{verb}` to populate its "
            f"operations, then retry. This is distinct from an unknown "
            f"connector_id: the connector exists, it just has nothing to "
            f"dispatch yet.",
            connector_id=connector_id,
            next_step=next_step.model_dump(mode="json") if next_step is not None else None,
        )
    raise await UnknownConnectorError.for_caller(
        tenant_id=operator.tenant_id, connector_id=connector_id
    )


async def list_operation_groups(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Return the operation groups for *connector_id* visible to the agent.

    ``arguments`` shape: ``{"connector_id": str, "limit"?: int,
    "cursor"?: str}``.

    A group is returned when it is fully enabled
    (``review_status='enabled'``) **or** still ``staged``/``disabled`` at
    the group level yet holding ≥1 per-op-enabled descriptor (set via
    ``edit_op is_enabled=true``); the latter carries ``partial=True`` with
    a non-zero ``enabled_op_count`` so groups-first discovery isn't blind
    to per-op enablement that ``search_operations`` + dispatch already
    honour (claude-rdc-hetzner-dc#1136). A group that is not enabled and
    holds zero enabled ops stays hidden. Tenant scoping:
    :func:`~meho_backplane.operations._lookup.visible_scope_clause` — the
    tenant's own rows when it has a row for the connector, the built-in
    rows otherwise.

    Per-group enablement note: a service-layer
    :meth:`~meho_backplane.operations.ingest.service.ReviewService.enable_group`
    already exists for scope-minimal per-group enablement but is not yet
    exposed as an MCP tool (out of scope for #1648); ``edit_op`` per-op
    stays the agent-surface escape hatch in the meantime.

    Three connector-existence outcomes (gated by
    :func:`_require_dispatchable_connector`):

    * *Unknown* ``connector_id`` (no DB rows AND no registered class for
      the parsed ``(product, version, impl_id)`` triple) raises
      :class:`UnknownConnectorError` — the REST route maps that to a
      ``404``, the MCP transport to ``-32602``.
    * *Registered but not ingested* (the class is in the v2 registry but
      has zero DB rows — "State 0.5") raises
      :class:`ConnectorNotIngestedError` carrying the ``meho connector
      ingest …`` ``next_step`` hint, so the caller self-corrects rather
      than treating it as unknown (#1482).
    * *Known/ingested* connector with zero enabled groups still returns
      an empty ``groups`` list (``200 []``): that empty is operationally
      meaningful (the connector exists; nothing is enabled yet). The
      distinction ends the "empty catalog" trap where a mis-shaped
      ``connector_id`` looked indistinguishable from an empty connector.

    Pagination (G0.18-T5 #1358)
    ---------------------------

    Keyset pagination on the lexicographically-sorted ``group_key``.
    The substrate query orders by ``group_key`` ASC; ``cursor`` is the
    last ``group_key`` from the prior page (results are strictly past
    the cursor). A full page implies more rows may exist and the
    response carries ``next_cursor`` set to the last returned
    ``group_key``; a short page is the end of the listing and
    ``next_cursor`` is ``null``. Mirrors ``list_targets``'s
    keyset-pagination shape so a sibling agent that paginates targets
    re-uses the same idiom for operation groups.
    """
    connector_id = arguments["connector_id"]
    limit, cursor = _coerce_list_operation_groups_pagination(arguments)
    product, version, impl_id = parse_connector_id(connector_id)
    await _require_dispatchable_connector(
        operator=operator,
        connector_id=connector_id,
        product=product,
        version=version,
        impl_id=impl_id,
    )
    sessionmaker = get_sessionmaker()
    group_stmt = _build_operation_groups_query(
        product=product,
        version=version,
        impl_id=impl_id,
        tenant_id=operator.tenant_id,
        limit=limit,
        cursor=cursor,
    )
    async with sessionmaker() as session:
        groups = (await session.execute(group_stmt)).scalars().all()
        counts = await _count_ops_per_group(
            session, [g.id for g in groups], tenant_id=operator.tenant_id
        )
    summaries = [
        OperationGroupSummary(
            group_key=g.group_key,
            name=g.name,
            when_to_use=g.when_to_use,
            operation_count=counts.get(g.id, 0),
            enabled_op_count=counts.get(g.id, 0),
            # A group reaches this list either fully enabled or — when
            # still staged/disabled — solely because it holds ≥1 per-op-
            # enabled descriptor (the query's EXISTS branch). Flag the
            # latter so groups-first discovery can tell the two apart;
            # ``enabled_op_count >= 1`` is guaranteed in the partial case.
            partial=g.review_status != "enabled",
        )
        for g in groups
    ]
    # Keyset cursor: when the page is full there *may* be more groups
    # past the last returned ``group_key``; surface it as the next
    # call's ``cursor``. A short page is definitively the end of the
    # listing and ``next_cursor`` is ``null`` so the caller stops.
    next_cursor = groups[-1].group_key if len(groups) == limit else None
    _log.info(
        "list_operation_groups",
        connector_id=connector_id,
        group_count=len(summaries),
        tenant_id=str(operator.tenant_id),
        limit=limit,
        cursor=cursor,
        next_cursor=next_cursor,
    )
    return {
        "connector_id": connector_id,
        "groups": [s.model_dump(mode="json") for s in summaries],
        "next_cursor": next_cursor,
    }


async def _has_enabled_operation(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    product: str,
    version: str,
    impl_id: str,
) -> bool:
    """Return ``True`` when the caller can see at least one enabled op.

    One bounded ``EXISTS`` over ``endpoint_descriptor``. It uses the same
    tenant scope as dispatch (:func:`visible_scope_clause`) and counts
    enabled rows only, so it never reveals a disabled or foreign-tenant op.
    """
    stmt = select(
        select(EndpointDescriptor.id)
        .where(
            visible_scope_clause(
                EndpointDescriptor,
                tenant_id=tenant_id,
                product=product,
                version=version,
                impl_id=impl_id,
            ),
            EndpointDescriptor.product == product,
            EndpointDescriptor.version == version,
            EndpointDescriptor.impl_id == impl_id,
            enabled_for_tenant_clause(tenant_id=tenant_id),
            installed_owner_clause(),
        )
        .exists()
    )
    return bool((await session.execute(stmt)).scalar())


async def _missing_package_of_connector(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    product: str,
    version: str,
    impl_id: str,
) -> str | None:
    """Return a missing connector package that owns an enabled op, else ``None``.

    The twin of :func:`_has_enabled_operation` for the rows that
    :func:`installed_owner_clause` hides (#5560). Same tenant scope, enabled
    rows only. It runs only when the caller can see no enabled op, so its
    answer tells ``package_not_installed`` from ``zero_enabled_operations``.
    """
    stmt = (
        select(EndpointDescriptor.owner_package)
        .where(
            visible_scope_clause(
                EndpointDescriptor,
                tenant_id=tenant_id,
                product=product,
                version=version,
                impl_id=impl_id,
            ),
            EndpointDescriptor.product == product,
            EndpointDescriptor.version == version,
            EndpointDescriptor.impl_id == impl_id,
            enabled_for_tenant_clause(tenant_id=tenant_id),
            ~installed_owner_clause(),
        )
        .order_by(EndpointDescriptor.owner_package)
        .limit(1)
    )
    package: str | None = (await session.execute(stmt)).scalar_one_or_none()
    return package


async def _visible_group_keys(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    product: str,
    version: str,
    impl_id: str,
) -> list[str]:
    """Return the group keys ``list_operation_groups`` would show, capped.

    Reuses :func:`_build_operation_groups_query`, so the tenant scope and
    the enabled-only visibility rule are the same as that tool's. The
    agent can already see these keys; nothing hidden is revealed.
    """
    stmt = _build_operation_groups_query(
        product=product,
        version=version,
        impl_id=impl_id,
        tenant_id=tenant_id,
        limit=SEARCH_NARROWING_MAX,
        cursor=None,
    )
    groups = (await session.execute(stmt)).scalars().all()
    return [g.group_key for g in groups]


def _classify_hits(hits: list[OperationSearchHit]) -> SearchOutcome:
    """Decide ``matched`` or ``ambiguous`` for a non-empty hit list.

    Reads the scores the retriever already returned; never re-ranks.
    ``ambiguous`` when the top hit has no lexical (BM25) score — it is only
    close in meaning, the usual shape of a misleading near-match — or when
    the top two ``fused_score`` values sit inside
    :data:`SEARCH_NEAR_TIE_BAND`.
    """
    top = hits[0]
    if top.bm25_score is None:
        return "ambiguous"
    if len(hits) > 1 and top.fused_score - hits[1].fused_score < SEARCH_NEAR_TIE_BAND:
        return "ambiguous"
    return "matched"


def _search_guidance(
    outcome: SearchOutcome,
    *,
    group_key: str | None,
    group_keys: list[str] | None = None,
    package: str | None = None,
) -> tuple[str | None, list[str]]:
    """Return the ``(advisory, narrowing)`` pair for *outcome*.

    The text names no disabled op and no count of hidden rows. For
    ``package_not_installed`` it names the missing *package*.
    """
    if outcome == "matched":
        return None, []
    if outcome == "package_not_installed":
        return (
            f"The operations of this connector come from the connector package "
            f"{package!r}, which is not installed in this MEHO. Do not call any "
            f"operation on this connector.",
            [f"Ask an operator to install the connector package {package!r} again."],
        )
    if outcome == "unknown_group":
        return (
            f"Group {group_key!r} does not exist on this connector or has no "
            "enabled operations. Use one of the group keys in `narrowing`, "
            "or search without a group.",
            (group_keys or [])[:SEARCH_NARROWING_MAX],
        )
    if outcome == "zero_enabled_operations":
        return (
            "This connector has no enabled operations you can use. An "
            "operator must enable operations first. Do not call any "
            "operation on this connector.",
            ["Ask an operator to enable the operations you need on this connector."],
        )
    narrowing = ["Use more specific words that describe the action you need."]
    if group_key is None:
        narrowing.append("Call `list_operation_groups` and search again with a `group` filter.")
    else:
        narrowing.append("Search again without the `group` filter.")
    if outcome == "no_match":
        return (
            "No enabled operation matched this query. Do not call an "
            "operation that this search did not return.",
            narrowing,
        )
    return (
        "The hits are weak or nearly tied. Do not call any of them. Narrow the search first.",
        narrowing,
    )


async def _run_search(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    product: str,
    version: str,
    impl_id: str,
    group_key: str | None,
    query: str,
    limit: int,
) -> tuple[list[OperationSearchHit], SearchOutcome, list[str] | None]:
    """Run the search and pick its outcome.

    Returns ``(hits, outcome, detail)``. ``detail`` is the visible group
    keys for ``unknown_group`` and ``[package]`` for
    ``package_not_installed``, else ``None``. The enabled-op ``EXISTS``
    runs only when the search found nothing, to tell ``no_match`` from
    ``zero_enabled_operations``; then the missing-package probe tells
    ``package_not_installed`` from ``zero_enabled_operations`` (#5560).

    The retriever is asked for at least :data:`SEARCH_CLASSIFY_MIN_HITS`
    hits so the near-tie check can see the second hit. The outcome is read
    from that list; the returned ``hits`` are then cut to *limit* without
    changing their order.
    """
    group_id: uuid.UUID | None = None
    if group_key is not None:
        # An unknown group_key is not an error: it returns no hits and the
        # ``unknown_group`` outcome, with the visible group keys to retry with.
        group_id = await resolve_group_id(
            session,
            tenant_id=tenant_id,
            product=product,
            version=version,
            impl_id=impl_id,
            group_key=group_key,
        )
        if group_id is None:
            group_keys = await _visible_group_keys(
                session,
                tenant_id=tenant_id,
                product=product,
                version=version,
                impl_id=impl_id,
            )
            # No visible group at all: when a missing package is the
            # reason, say so rather than "unknown group" (#5560).
            package = (
                None
                if group_keys
                else await _missing_package_of_connector(
                    session,
                    tenant_id=tenant_id,
                    product=product,
                    version=version,
                    impl_id=impl_id,
                )
            )
            if package is not None:
                return [], "package_not_installed", [package]
            return [], "unknown_group", group_keys
    hits = await hybrid_search(
        session,
        tenant_id=tenant_id,
        product=product,
        version=version,
        impl_id=impl_id,
        group_id=group_id,
        query=query,
        limit=max(limit, SEARCH_CLASSIFY_MIN_HITS),
    )
    if hits:
        return hits[:limit], _classify_hits(hits), None
    has_enabled = await _has_enabled_operation(
        session,
        tenant_id=tenant_id,
        product=product,
        version=version,
        impl_id=impl_id,
    )
    if has_enabled:
        return [], "no_match", None
    package = await _missing_package_of_connector(
        session, tenant_id=tenant_id, product=product, version=version, impl_id=impl_id
    )
    if package is not None:
        return [], "package_not_installed", [package]
    return [], "zero_enabled_operations", None


async def search_operations(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Hybrid BM25 + cosine RRF retrieval over ``endpoint_descriptor``.

    ``arguments`` shape: ``{"connector_id": str, "query"?: str,
    "op_id"?: str, "group": str | None, "limit": int = 10,
    "include_scores"?: bool}``. One of ``query`` / ``op_id`` is needed.

    **Exact-op mode (#3668).** When ``op_id`` is set, there is no
    ranking: the handler looks up that one operation and returns
    ``{"operation": <OperationContract>}`` -- the stored
    ``parameter_schema`` as-is, small examples, the target rule, the
    result shape, the pagination hint and retry guidance (see
    :mod:`meho_backplane.operations._contract`). ``query`` / ``group`` /
    ``limit`` are ignored in this mode. An op that is not visible and
    enabled raises :class:`UnknownOperationError`.

    **Ranked mode.** Returns ``{"hits": [...], "query_duration_ms":
    float, "outcome": str, "advisory": str | None, "narrowing":
    [str]}``. Each hit carries ``op_id`` / ``summary`` / ``description``
    / ``group_key`` / ``safety_level`` / ``requires_approval``. The
    ranking scores are added only when ``include_scores`` is true and
    the caller is ``tenant_admin``; anyone else gets the slim hit. Tenant
    boundary enforced: every candidate query filters on the same
    :func:`~meho_backplane.operations._lookup.visible_scope_clause`
    dispatch uses, so a hit is always an op ``call_operation`` can run.

    The handler embeds the query once, runs the BM25 + cosine candidate
    queries against the indexed ``endpoint_descriptor`` table, fuses the
    two ranked lists via RRF, and returns the top ``limit`` hits. Per-
    signal scores + ranks are surfaced so the agent (and operator
    debugging) can see whether a hit came from lexical match, semantic
    match, or both -- behind the admin opt-in above.

    No-suitable-match taxonomy (#3670). Two cases are errors, raised by
    :func:`_require_dispatchable_connector`: an *unknown* ``connector_id``
    (:class:`UnknownConnectorError`) and a *registered-but-not-ingested*
    one (:class:`ConnectorNotIngestedError`, #1482). Every other result
    carries one ``outcome`` from :data:`SEARCH_OUTCOMES`: ``matched``,
    ``ambiguous`` (:func:`_classify_hits`), ``no_match``,
    ``unknown_group`` (``narrowing`` lists the visible group keys),
    ``zero_enabled_operations`` (an abstention advisory, no op ids), or
    ``package_not_installed`` (the advisory names the missing connector
    package, #5560).
    ``advisory`` explains a non-``matched`` outcome in one sentence;
    ``narrowing`` holds at most :data:`SEARCH_NARROWING_MAX` next steps.
    Hit order is the retriever's; the outcome never re-ranks.

    #3500: per-principal dispatch rate limit. A burst of searches from one
    principal over its per-minute cap is rejected with a structured
    ``rate_limited`` envelope before any embedding / candidate query runs.
    """
    rate_limited = await rate_limited_read_envelope(operator)
    if rate_limited is not None:
        return rate_limited
    connector_id = arguments["connector_id"]
    op_id: str | None = arguments.get("op_id")
    product, version, impl_id = parse_connector_id(connector_id)
    await _require_dispatchable_connector(
        operator=operator,
        connector_id=connector_id,
        product=product,
        version=version,
        impl_id=impl_id,
    )
    if op_id is not None:
        contract = await build_operation_contract(
            tenant_id=operator.tenant_id,
            connector_id=connector_id,
            product=product,
            version=version,
            impl_id=impl_id,
            op_id=op_id,
        )
        _log.info(
            "search_operations_exact_op",
            connector_id=connector_id,
            op_id=op_id,
            tenant_id=str(operator.tenant_id),
        )
        return {"operation": contract.model_dump(mode="json")}

    query = arguments["query"]
    group_key: str | None = arguments.get("group")
    limit = min(max(int(arguments.get("limit", 10)), 1), SEARCH_LIMIT_MAX)
    show_scores = (
        bool(arguments.get("include_scores")) and operator.tenant_role == TenantRole.TENANT_ADMIN
    )
    started = time.monotonic()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        hits, outcome, detail = await _run_search(
            session,
            tenant_id=operator.tenant_id,
            product=product,
            version=version,
            impl_id=impl_id,
            group_key=group_key,
            query=query,
            limit=limit,
        )
    duration_ms = (time.monotonic() - started) * 1000
    advisory, narrowing = _search_guidance(
        outcome,
        group_key=group_key,
        group_keys=detail if outcome == "unknown_group" else None,
        package=detail[0] if outcome == "package_not_installed" and detail else None,
    )
    _log.info(
        "search_operations",
        connector_id=connector_id,
        group=group_key,
        hit_count=len(hits),
        outcome=outcome,
        tenant_id=str(operator.tenant_id),
        duration_ms=duration_ms,
    )
    exclude = None if show_scores else set(_SCORE_FIELDS)
    return {
        "hits": [h.model_dump(mode="json", exclude=exclude) for h in hits],
        "query_duration_ms": duration_ms,
        "outcome": outcome,
        "advisory": advisory,
        "narrowing": narrowing,
    }


def _json_type_name(value: Any) -> str:
    """Name *value*'s JSON type for the ``target_invalid_type`` diagnostic.

    JSON-vocabulary names (``"integer"``, not ``"int"``) so the envelope
    speaks the same language as the request body the consumer wrote.
    ``bool`` is checked before ``int`` — Python's ``bool`` subclasses
    ``int`` and would otherwise report as ``"integer"``.
    """
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


class _InvalidTargetTypeError(Exception):
    """``target`` is not a string, a dict, or ``None`` (#2110).

    Deliberately NOT a :class:`ValueError` subclass so
    :func:`_resolve_target_or_error`'s ``except ValueError`` arm
    (``target_required``) cannot swallow it — the two failure modes carry
    distinct ``error_code``\\ s.
    """

    def __init__(self, received_type: str) -> None:
        super().__init__(
            f"target must be a string name, an object with a 'name' field, "
            f"or null; got {received_type}"
        )
        self.received_type = received_type


def _normalize_target_arg(
    target_arg: Any,
) -> dict[str, Any] | None:
    """Normalise ``call_operation``'s ``target`` to the canonical dict shape.

    G0.13-T2 (#1132) additive widening: ``call_operation`` accepts either
    a bare string ``"prod-vault"`` (matches ``query_topology`` /
    ``query_audit``) or the existing dict ``{"name": "prod-vault"}``.
    Both reduce to the same dict before dispatch so downstream code
    (resolver, connectors, audit) sees one canonical form.

    Validation rules:

    * ``None`` → ``None`` (operation does not need a target).
    * ``str`` (non-empty) → ``{"name": <string>}``. No ``fqdn`` override
      can be supplied via the string shape; callers that need the
      override stay on the dict.
    * ``dict`` with a non-empty ``name`` key → returned as-is.
    * Empty string / dict without ``name`` / dict with empty ``name`` →
      ``ValueError`` (the ``target_required`` envelope upstream).
    * Any other JSON type (``12345``, ``true``, ``[...]``) →
      :class:`_InvalidTargetTypeError` (the ``target_invalid_type``
      envelope upstream, #2110). The REST body models pass such values
      through deliberately (see :data:`_TargetArg`); the raw MCP
      ``arguments`` dict always could — this arm also closes the
      ``12345.get("name")`` :class:`AttributeError` that used to escape
      the MCP transport as an unstructured internal error.
    """
    if target_arg is None:
        return None
    if isinstance(target_arg, str):
        if not target_arg:
            raise ValueError("target must include a 'name' field when supplied")
        return {"name": target_arg}
    if not isinstance(target_arg, dict):
        raise _InvalidTargetTypeError(_json_type_name(target_arg))
    name = target_arg.get("name")
    if not name:
        raise ValueError("target must include a 'name' field when supplied")
    return target_arg


async def _resolve_target_or_error(
    operator: Operator,
    op_id: str,
    target_raw: Any,
    *,
    bind_audit_target: bool,
) -> tuple[Any, dict[str, Any] | None]:
    """Resolve ``call``/``preview``'s ``target`` arg, or return an error envelope (#136).

    Returns ``(resolved_target, None)`` on success (``resolved_target`` is
    ``None`` for a target-less op), or ``(None, envelope)`` when the target
    fails. Every failure class rides the dispatcher envelope
    (``status="error"`` + ``extras.error_code``) so a ``/operations/call`` (and
    ``/preview``) consumer switches on one ``error_code`` instead of parsing a
    400/404/422 body:

    * a missing / empty / ``name``-less ``target`` (the
      :func:`_normalize_target_arg` ``ValueError``) → ``target_required``;
    * a ``target`` of a wrong JSON type — not a string, an object, or null
      (:class:`_InvalidTargetTypeError`) → ``target_invalid_type``
      (#2110, closing the last mode that escaped as a FastAPI 422);
    * a supplied name that resolves to no live target (the resolver's
      :exc:`~meho_backplane.targets.resolver.TargetNotFoundError`) → ``no_target``;
    * a supplied name that matches more than one target — an alias collision
      (the resolver's :exc:`~meho_backplane.targets.resolver.AmbiguousTargetError`)
      → ``ambiguous_target``.

    ``bind_audit_target`` binds the resolved
    ``target_id`` into structlog for the eventual audit row (``call`` does;
    ``preview`` writes no row, so it does not).
    """
    try:
        target_arg = _normalize_target_arg(target_raw)
    except ValueError:
        return None, result_target_required(op_id, 0.0).model_dump(mode="json")
    except _InvalidTargetTypeError as exc:
        return None, result_target_invalid_type(op_id, exc.received_type, 0.0).model_dump(
            mode="json"
        )
    if target_arg is None:
        return None, None

    name = target_arg["name"]
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            resolved_target = await resolve_target(session, operator.tenant_id, name)
        except TargetNotFoundError as exc:
            detail: dict[str, Any] = exc.detail if isinstance(exc.detail, dict) else {}
            return None, result_no_target(
                op_id,
                str(detail.get("query", name)),
                detail.get("matches", []),
                0.0,
            ).model_dump(mode="json")
        except AmbiguousTargetError as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            return None, result_ambiguous_target(
                op_id,
                str(detail.get("query", name)),
                detail.get("matches", []),
                0.0,
            ).model_dump(mode="json")
        if bind_audit_target:
            # Bind into structlog so AuditMiddleware picks up the target_id
            # on the eventual audit row. Same shape as the targets routes.
            structlog.contextvars.bind_contextvars(audit_target_id=str(resolved_target.id))

    apply_target_fqdn_override(resolved_target, target_arg)
    return resolved_target, None


def apply_target_fqdn_override(resolved_target: Any, target_arg: Any) -> None:
    """Apply the per-call ``fqdn`` of a dict-shaped *target_arg* to *resolved_target*.

    In memory only: the database row is not touched. The override travels
    with the resolved target for the lifetime of one dispatch and is read by
    connectors that route by virtual host (G3.6 VCF Automation: the HTTP
    ``Host`` header and the TLS server name). The bare-string ``target``
    shape carries no override; a non-string or empty value falls through
    silently, and so does a missing target.

    Shared by the name lookup of every call (:func:`_resolve_target_or_error`)
    and by the agent waiter's resume after approval, which loads the target
    by the id the approval row saved and then applies the override of the
    agent's original call again, so the approved call reaches the same
    virtual host as the call that waited for approval.
    """
    if resolved_target is None or not isinstance(target_arg, dict):
        return
    fqdn_override = target_arg.get("fqdn")
    if isinstance(fqdn_override, str) and fqdn_override:
        resolved_target.fqdn = fqdn_override


async def _call_operation_impl(
    operator: Operator,
    arguments: dict[str, Any],
    *,
    approved: bool,
    resolved_target: Any = None,
) -> dict[str, Any]:
    """Resolve target + dispatch; shared body for the approved/unapproved entries.

    Two public wrappers thread different ``approved`` values into the same
    body: :func:`call_operation` (the default) calls the dispatcher's policy
    gate; :func:`call_operation_with_approval` skips it because the durable
    approval-decision row is the authorization (G11.1-T9 #1117 / G11.2-T4 #817).
    Both share the same target-resolution + fqdn-override + structlog-audit
    wiring so the audit row shapes stay symmetric.

    ``arguments`` shape: ``{"connector_id": str, "op_id": str,
    "target": str | dict | None, "params": dict}``. The ``target`` value is
    normalised via :func:`_normalize_target_arg` (bare string ``"prod-k8s"``
    or partial dict ``{"name": "prod-k8s"}`` both reduce to the canonical
    dict form before :func:`~meho_backplane.targets.resolver.resolve_target`
    is called). Passing ``target=None`` is valid for typed operations whose
    handler does not consume a target.

    A caller that already holds the live target row passes it as
    *resolved_target*; then ``arguments["target"]`` is not read at all. The
    agent waiter uses this on every resume: it loads the target
    the approval row saved by id, so the name the agent used when it parked
    cannot point at a different target by the time the approval lands.
    """
    connector_id = arguments["connector_id"]
    op_id = arguments["op_id"]
    params: dict[str, Any] = arguments.get("params") or {}

    # #3641: a completed op must deliver its receipt inside the wire
    # ceiling. The receipt grows with ``op_id``, so an ``op_id`` whose
    # receipt could never fit is refused here, before the target is
    # resolved and before anything runs.
    wire_max_bytes = get_settings().result_complete_response_wire_max_bytes
    refusal = refuse_undeliverable_receipt(op_id, wire_max_bytes)
    if refusal is not None:
        return refusal.model_dump(mode="json")

    if resolved_target is not None:
        structlog.contextvars.bind_contextvars(audit_target_id=str(resolved_target.id))
    else:
        # #136: target-resolution failures ride the dispatcher envelope (a
        # single ``extras.error_code`` for the consumer), not an HTTP 400/404.
        resolved_target, target_error = await _resolve_target_or_error(
            operator, op_id, arguments.get("target"), bind_audit_target=True
        )
        if target_error is not None:
            return target_error

    # work_ref I1-T2 (#1657): bind an explicit per-op ``work_ref`` (the
    # MCP ``call_operation`` tool arg / the REST ``CallOperationBody``
    # field) onto :data:`work_ref_var` for the dispatch, so the DISPATCH
    # ``audit_log`` row the dispatcher writes carries it. Same token +
    # ``try/finally`` / ``reset(token)`` discipline as the runbook
    # engine's ``run_id_var`` / ``step_id_var`` bind-around-dispatch
    # (``run_service._operation_call``) -- the binding never leaks past
    # this call. It is the per-op OVERRIDE the Goal #1651 design calls
    # for: gated on a non-empty arg, so a bare ``call_operation`` does
    # NOT clobber an ambient ``Meho-Work-Ref`` header bound at the
    # chassis -- ``reset`` restores exactly the prior value, never a
    # hardcoded default.
    work_ref_arg = arguments.get("work_ref")
    work_ref_token = None
    if isinstance(work_ref_arg, str) and work_ref_arg:
        work_ref_token = work_ref_var.set(work_ref_arg)
    try:
        async with AsyncExitStack() as linkage_stack:
            # #3028 / #3446 out-of-process parent-linkage: when a
            # client-credential service principal (paired add-on or not)
            # dispatches under a ``work_ref``, group its per-step dispatches
            # into one audit-replay subtree by binding the orchestration run's
            # synthesized ``session_id`` + anchor ``parent_audit_id`` around the
            # dispatch. Resolution reads the *effective* work_ref (the arg above
            # or an ambient ``Meho-Work-Ref`` header) and returns ``None`` for
            # users and agents, so their behaviour (an independent audit row)
            # is untouched.
            effective_work_ref = work_ref_var.get()
            if effective_work_ref:
                linkage_run = await resolve_or_open_orchestration_run(operator, effective_work_ref)
                if linkage_run is not None:
                    await linkage_stack.enter_async_context(bound_parent_linkage(linkage_run))
            result = await dispatch(
                operator=operator,
                connector_id=connector_id,
                op_id=op_id,
                target=resolved_target,
                params=params,
                _approved=approved,
                # #3197: the destructive-tier preview-result-hash binding.
                # A MEHO dispatch control (like ``work_ref`` above), not a
                # connector op param — threaded top-level, never into
                # ``params``, so it stays out of ``params_hash`` and the op's
                # parameter_schema. ``None`` for a non-destructive call.
                preview_hash=arguments.get("preview_hash"),
                # #3486: retry key for a parked call, a MEHO dispatch control
                # like ``preview_hash`` -- never part of ``params``.
                idempotency_key=arguments.get("idempotency_key"),
            )
    finally:
        if work_ref_token is not None:
            work_ref_var.reset(work_ref_token)

    _log.info(
        "call_operation",
        connector_id=connector_id,
        op_id=op_id,
        status=result.status,
        duration_ms=result.duration_ms,
        tenant_id=str(operator.tenant_id),
        approved=approved,
    )
    # #3641: terminal wire ceiling, measured on the real MCP encoding. A
    # completed op that is too large to deliver keeps ``status="ok"`` and
    # gets ``delivery="partial"`` / ``"unavailable"`` instead.
    result = bound_complete_response(result, wire_max_bytes)
    return result.model_dump(mode="json")


async def call_operation(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Invoke :func:`~meho_backplane.operations.dispatch` for ``op_id``.

    ``arguments`` shape: ``{"connector_id": str, "op_id": str,
    "target": str | dict | None, "params": dict}``.

    The handler accepts a ``target`` value as either a bare string
    ``"prod-k8s"`` (matches ``query_topology`` / ``query_audit``) or
    the partial dict ``{"name": "prod-k8s"}``. Both are normalised by
    :func:`_normalize_target_arg` to the canonical dict form before
    :func:`~meho_backplane.targets.resolver.resolve_target` is called.
    Passing ``target=None`` is valid for typed operations whose handler
    does not consume a target (the dispatcher accepts ``None`` through
    to the branch handler).

    Returns the :class:`~meho_backplane.connectors.schemas.OperationResult`
    serialised via ``model_dump(mode="json")``. Errors surface inside
    the result envelope (``status='error'`` + ``extras.error_code``)
    rather than as exceptions — the dispatcher contract is "always
    return a structured result". Target-resolution failures are enveloped
    too (#136): a missing / empty / ``name``-less target →
    ``target_required``, a supplied-but-unresolvable name → ``no_target``
    (see :func:`_resolve_target_or_error`). So the meta-tool never raises
    for operator-input faults and the MCP transport keeps a uniform shape.
    """
    return await _call_operation_impl(operator, arguments, approved=False)


async def call_operation_with_approval(
    operator: Operator,
    arguments: dict[str, Any],
    *,
    resolved_target: Any = None,
) -> dict[str, Any]:
    """Re-dispatch a previously-parked ``call_operation`` after operator approval.

    The agent runtime's approval-resume entry point (G11.1-T9 #1117). Called
    exclusively from
    :func:`~meho_backplane.agent.approval_wait.resume_or_surface_awaiting_approval`
    after the broadcast feed reported ``approval.approved`` for an in-flight
    agent run's pending request. Same body as :func:`call_operation` but
    threads ``_approved=True`` into the dispatcher, which skips the policy
    gate — the durable approval-decision row is the authorization the gate
    bypass relies on.

    Not part of the public MCP / REST surface: only the agent layer calls
    this. The operator surfaces (REST ``/approve`` + ``/decide``, MCP by-id
    approve, UI approve) re-dispatch through the shared
    :func:`~meho_backplane.operations.approval_queue.resume_dispatch_after_approval`.
    For a run-bound request both this in-process path (invoked by the agent
    waiter once the ``approval.approved`` broadcast lands) and an operator
    surface can race to resume, so the operator/agent split is **enforced**
    by the exactly-one-resumer claim (#2293), not merely assumed: the agent
    waiter wins :func:`~meho_backplane.operations.approval_queue.claim_resume`
    before calling this function, and ``resume_dispatch_after_approval``
    wins the same claim before it dispatches, so exactly one side executes
    the approved op (a single conditional ``UPDATE ... WHERE resumed_at IS
    NULL``; the loser no-ops). This is what closes the double-dispatch the
    unenforced "coexist by design" note (#1117) had left open.

    *resolved_target*: the live target row to dispatch against, when the
    caller already loaded it. The waiter passes the target the approval row
    saved by id, for a composite re-entry and for a direct
    re-dispatch, so ``arguments["target"]`` is ignored and no name lookup
    runs. The waiter also sets ``arguments["target"]`` to ``None``, so a
    tenant-wide op (no saved target) stays without a target.
    """
    return await _call_operation_impl(
        operator, arguments, approved=True, resolved_target=resolved_target
    )


async def preview_operation(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Resolve an op + params to the literal would-be HTTP request, without sending.

    The read-only diagnosis sibling of :func:`call_operation` (#1683). Takes
    the same ``arguments`` shape -- ``{"connector_id": str, "op_id": str,
    "target": str | dict | None, "params": dict}`` -- resolves the target the
    same way, then delegates to
    :func:`~meho_backplane.operations._request_preview.preview_dispatch`,
    which returns ``{method, resolved_path, query, redacted_body}`` for an
    ``source_kind='ingested'`` op **instead of dispatching it**. The body is
    redacted through the same connector-boundary pipeline the response path
    uses, so the preview is not a new raw-secret surface; nothing is written
    to the audit row (the ``params_hash`` privacy design is untouched).

    Use this to diagnose a write 4xx (the gh-rest 422 of #1656, a 403 of
    #1649) from the inside -- re-issue the exact arguments here to read back
    what meho would put on the wire, rather than bisecting payload shapes
    from outside. Inspection only: it never sends the request and never
    re-dispatches a past one (replay is a separate concern, Goal #1651).

    Returns the structured envelope verbatim (``status`` of ``"ok"`` /
    ``"error"`` / ``"unavailable"``); operator-input faults (unknown op,
    invalid params, unresolvable connector) come back inside the envelope
    rather than as exceptions, mirroring ``call_operation``. Target-resolution
    failures are enveloped identically (#136): ``target_required`` for a
    missing / empty / ``name``-less target, ``no_target`` for a
    supplied-but-unresolvable name (see :func:`_resolve_target_or_error`) —
    so ``call`` and ``preview`` behave the same for target resolution.

    #3500: shares ``call_operation``'s per-principal dispatch rate limit —
    a burst over the per-minute cap is rejected with a structured
    ``rate_limited`` envelope before any target resolution.
    """
    rate_limited = await rate_limited_read_envelope(operator)
    if rate_limited is not None:
        return rate_limited
    connector_id = arguments["connector_id"]
    op_id = arguments["op_id"]
    params: dict[str, Any] = arguments.get("params") or {}

    # #136: same target-resolution-rides-the-envelope contract as ``call``.
    # ``bind_audit_target=False`` — a preview writes no audit row, so there is
    # nothing to enrich.
    resolved_target, target_error = await _resolve_target_or_error(
        operator, op_id, arguments.get("target"), bind_audit_target=False
    )
    if target_error is not None:
        return target_error

    envelope = await preview_dispatch(
        operator=operator,
        connector_id=connector_id,
        op_id=op_id,
        target=resolved_target,
        params=params,
    )
    _log.info(
        "preview_operation",
        connector_id=connector_id,
        op_id=op_id,
        status=envelope.get("status"),
        tenant_id=str(operator.tenant_id),
    )
    return envelope


async def describe_descriptor(
    operator: Operator,
    descriptor_id: uuid.UUID,
) -> OperationDescriptor | None:
    """Return the full descriptor row for *descriptor_id*, tenant-scoped.

    Used by the ``GET /api/v1/operations/{descriptor_id}`` route (which
    is gated on ``tenant_admin`` because ``llm_instructions`` is the
    per-op agent prompt and reading it amounts to a prompt leak). Returns
    ``None`` when the row doesn't exist OR when it exists but belongs to
    a different tenant — the two cases are indistinguishable to the
    caller to avoid an existence-oracle on cross-tenant rows.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor, OperationGroup.group_key)
            .outerjoin(OperationGroup, EndpointDescriptor.group_id == OperationGroup.id)
            .where(EndpointDescriptor.id == descriptor_id)
        )
        row = result.first()
        if row is None:
            return None
        descriptor, group_key = row
        # Tenant gate: built-in (None) is visible to everyone;
        # tenant-scoped rows visible only to their own tenant. A row in
        # another tenant collapses to "not found".
        if descriptor.tenant_id is not None and descriptor.tenant_id != operator.tenant_id:
            return None
        # #4711: show the risk level and approval that apply to this
        # tenant, with operator overrides, so the console run form and the
        # REST read match what a call does.
        policy = (
            await effective_policy_map(session, tenant_id=operator.tenant_id, rows=[descriptor])
        )[OpKey.of(descriptor)]
        return OperationDescriptor(
            id=descriptor.id,
            tenant_id=descriptor.tenant_id,
            product=descriptor.product,
            version=descriptor.version,
            impl_id=descriptor.impl_id,
            op_id=descriptor.op_id,
            source_kind=descriptor.source_kind,
            method=descriptor.method,
            path=descriptor.path,
            handler_ref=descriptor.handler_ref,
            summary=descriptor.summary,
            description=descriptor.description,
            group_id=descriptor.group_id,
            group_key=group_key,
            tags=list(descriptor.tags or []),
            parameter_schema=dict(descriptor.parameter_schema or {}),
            response_schema=(
                dict(descriptor.response_schema) if descriptor.response_schema else None
            ),
            llm_instructions=(
                dict(descriptor.llm_instructions) if descriptor.llm_instructions else None
            ),
            safety_level=policy.safety_level,
            requires_approval=policy.requires_approval,
            is_enabled=descriptor.is_enabled,
            custom_description=descriptor.custom_description,
            custom_notes=descriptor.custom_notes,
        )
