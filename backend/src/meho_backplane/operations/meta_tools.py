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
``vsphere.vm.list``; it sees a small fixed set of meta-tools and picks
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

Every read query filters by ``tenant_id`` in the same shape the G0.6
dispatcher's :func:`~meho_backplane.operations._lookup.lookup_descriptor`
does: built-in / global rows (``tenant_id IS NULL``) are visible to all
operators; tenant-curated rows (``tenant_id == operator.tenant_id``) are
visible only to that tenant's operators. The two row sets are unioned
before ranking so search can hit both.

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
from typing import Annotated, Any, Final

import structlog
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, WithJsonSchema
from sqlalchemy import select

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations._errors import (
    result_ambiguous_target,
    result_no_target,
    result_target_invalid_type,
    result_target_required,
)
from meho_backplane.operations._lookup import (
    connector_class_registered,
    connector_exists,
    parse_connector_id,
)
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations._search import hybrid_search, resolve_group_id
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.ingest.list_connectors import next_step_for_registered_connector
from meho_backplane.targets.resolver import (
    AmbiguousTargetError,
    TargetNotFoundError,
    resolve_target,
)

__all__ = [
    "SEARCH_LIMIT_MAX",
    "CallOperationBody",
    "ConnectorNotIngestedError",
    "OperationDescriptor",
    "OperationGroupSummary",
    "OperationSearchHit",
    "PreviewOperationBody",
    "UnknownConnectorError",
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

    Carries :attr:`connector_id` (the id the caller passed) so the MCP
    transport can thread it onto the ``error.data`` discriminator
    alongside its :class:`ConnectorNotIngestedError` sibling.
    """

    def __init__(self, message: str, *, connector_id: str) -> None:
        super().__init__(message)
        self.connector_id = connector_id


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

    Same per-signal observability shape as
    :class:`~meho_backplane.retrieval.retriever.RetrievalHit` — callers
    tuning operation description quality want to see whether the top
    candidate scored well on BM25 (lexical match) or cosine (semantic
    match) or both.
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

    * **Bare string** -- ``target: "rdc-vcenter"``. The forward-preferred
      shape; matches ``query_topology`` / ``query_audit`` so an agent
      can carry a target name across the read and the write surfaces
      without reshape. G0.13-T2 (#1132) widening of #780.
    * **Dict** -- ``target: {"name": "rdc-vcenter"}``. The original
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

    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(min_length=1)
    op_id: str = Field(min_length=1)
    target: _TargetArg = None
    params: dict[str, Any] = Field(default_factory=dict)
    work_ref: str | None = Field(default=None, min_length=1)


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
) -> dict[uuid.UUID, int]:
    """Count enabled :class:`EndpointDescriptor` rows per group in one query.

    One SQL round-trip + Python aggregation keeps the
    application-side group-by portable across PG + SQLite. Excludes
    NULL ``group_id`` — ungrouped ops don't contribute.
    """
    if not group_ids:
        return {}
    count_stmt = select(EndpointDescriptor.group_id).where(
        EndpointDescriptor.group_id.in_(group_ids),
        EndpointDescriptor.is_enabled.is_(True),
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

    Filters: matching connector triple + tenant scope (NULL global OR
    this tenant) + a *visibility* predicate; orders by ``group_key``
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
    a per-op-only (``partial``) one.
    """
    has_enabled_op = (
        select(EndpointDescriptor.id)
        .where(
            EndpointDescriptor.group_id == OperationGroup.id,
            EndpointDescriptor.is_enabled.is_(True),
        )
        .exists()
    )
    stmt = (
        select(OperationGroup)
        .where(
            OperationGroup.product == product,
            OperationGroup.version == version,
            OperationGroup.impl_id == impl_id,
            (OperationGroup.review_status == "enabled") | has_enabled_op,
            (OperationGroup.tenant_id.is_(None)) | (OperationGroup.tenant_id == tenant_id),
        )
        .order_by(OperationGroup.group_key)
        .limit(limit)
    )
    if cursor is not None:
        stmt = stmt.where(OperationGroup.group_key > cursor)
    return stmt


def _raise_unknown_connector(connector_id: str) -> None:
    """Raise :class:`UnknownConnectorError` with the long-form recovery hint."""
    raise UnknownConnectorError(
        f"unknown connector_id {connector_id!r} — expected <impl_id>-<version> "
        f"(e.g. 'vmware-rest-9.0', 'vault-1.x'). If this id appeared in "
        f"GET /api/v1/connectors, the listing is inconsistent with the "
        f"dispatcher's resolve path — please file a bug report; otherwise "
        f"the id is mistyped or the connector is not registered on this deploy",
        connector_id=connector_id,
    )


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
      existence gate is enable-agnostic.
    * **No DB rows, class registered** (:func:`connector_class_registered`
      ``True``) — "State 0.5": the connector is v2-registered but awaiting
      ingest. Raise :class:`ConnectorNotIngestedError` carrying the same
      ``next_step`` ingest hint ``GET /api/v1/connectors`` renders on the
      ``state="registered"`` row, so the agent can self-correct to the
      ``meho connector ingest …`` verb instead of receiving an opaque
      unknown-connector error (#1482).
    * **No DB rows, no class** — genuinely unknown. Raise
      :class:`UnknownConnectorError` (the long-form mistyped-id recovery
      hint).
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
    _raise_unknown_connector(connector_id)


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
    holds zero enabled ops stays hidden. Tenant scoping: the union of
    built-in (``tenant_id IS NULL``) and tenant-curated
    (``tenant_id == operator.tenant_id``) rows.

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
        counts = await _count_ops_per_group(session, [g.id for g in groups])
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


async def search_operations(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Hybrid BM25 + cosine RRF retrieval over ``endpoint_descriptor``.

    ``arguments`` shape: ``{"connector_id": str, "query": str,
    "group": str | None, "limit": int = 10}``.

    Returns ``{"hits": [...], "query_duration_ms": float}``. Tenant
    boundary enforced: every candidate query filters on
    ``(tenant_id IS NULL OR tenant_id == operator.tenant_id)``. The
    ``group`` filter (when set) narrows by ``operation_group.group_key``
    within the same connector's natural key.

    The handler embeds the query once, runs the BM25 + cosine candidate
    queries against the indexed ``endpoint_descriptor`` table, fuses the
    two ranked lists via RRF, and returns the top ``limit`` hits. Per-
    signal scores + ranks are surfaced so the agent (and operator
    debugging) can see whether a hit came from lexical match, semantic
    match, or both.

    Shares :func:`list_operation_groups`'s connector-existence taxonomy
    via :func:`_require_dispatchable_connector`: an *unknown*
    ``connector_id`` raises :class:`UnknownConnectorError` (REST →
    ``404``); a *registered-but-not-ingested* connector raises
    :class:`ConnectorNotIngestedError` with the ingest ``next_step`` hint
    (#1482); a *known* connector with no matching ops still returns an
    empty ``hits`` list (``200 []``).
    """
    connector_id = arguments["connector_id"]
    query = arguments["query"]
    group_key: str | None = arguments.get("group")
    limit = int(arguments.get("limit", 10))
    if limit < 1:
        limit = 1
    if limit > SEARCH_LIMIT_MAX:
        limit = SEARCH_LIMIT_MAX

    product, version, impl_id = parse_connector_id(connector_id)
    await _require_dispatchable_connector(
        operator=operator,
        connector_id=connector_id,
        product=product,
        version=version,
        impl_id=impl_id,
    )
    started = time.monotonic()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # Resolve group_key -> group_id (if supplied). Unknown group_key
        # short-circuits to empty hits rather than an error: same shape
        # as list_operation_groups returning [] for unknown connector_id.
        group_id: uuid.UUID | None = None
        if group_key is not None:
            group_id = await resolve_group_id(
                session,
                tenant_id=operator.tenant_id,
                product=product,
                version=version,
                impl_id=impl_id,
                group_key=group_key,
            )
            if group_id is None:
                duration_ms = (time.monotonic() - started) * 1000
                return {"hits": [], "query_duration_ms": duration_ms}
        hits = await hybrid_search(
            session,
            tenant_id=operator.tenant_id,
            product=product,
            version=version,
            impl_id=impl_id,
            group_id=group_id,
            query=query,
            limit=limit,
        )
    duration_ms = (time.monotonic() - started) * 1000
    _log.info(
        "search_operations",
        connector_id=connector_id,
        group=group_key,
        hit_count=len(hits),
        tenant_id=str(operator.tenant_id),
        duration_ms=duration_ms,
    )
    return {
        "hits": [h.model_dump(mode="json") for h in hits],
        "query_duration_ms": duration_ms,
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
    a bare string ``"rdc-vault"`` (matches ``query_topology`` /
    ``query_audit``) or the existing dict ``{"name": "rdc-vault"}``.
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

    # Per-call ``fqdn`` override -- applied in memory only. The DB row is not
    # touched; the override travels with the resolved Target for the lifetime
    # of this dispatch and is read by connectors that honour vhost routing
    # (G3.6 VCF Automation). Non-string / empty values fall through silently.
    fqdn_override = target_arg.get("fqdn")
    if isinstance(fqdn_override, str) and fqdn_override:
        resolved_target.fqdn = fqdn_override
    return resolved_target, None


async def _call_operation_impl(
    operator: Operator,
    arguments: dict[str, Any],
    *,
    approved: bool,
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
    normalised via :func:`_normalize_target_arg` (bare string ``"rdc-vcenter"``
    or partial dict ``{"name": "rdc-vcenter"}`` both reduce to the canonical
    dict form before :func:`~meho_backplane.targets.resolver.resolve_target`
    is called). Passing ``target=None`` is valid for typed operations whose
    handler does not consume a target.
    """
    connector_id = arguments["connector_id"]
    op_id = arguments["op_id"]
    params: dict[str, Any] = arguments.get("params") or {}

    # #136: target-resolution failures ride the dispatcher envelope (a single
    # ``extras.error_code`` for the consumer), not an HTTP 400/404.
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
        result = await dispatch(
            operator=operator,
            connector_id=connector_id,
            op_id=op_id,
            target=resolved_target,
            params=params,
            _approved=approved,
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
    return result.model_dump(mode="json")


async def call_operation(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Invoke :func:`~meho_backplane.operations.dispatch` for ``op_id``.

    ``arguments`` shape: ``{"connector_id": str, "op_id": str,
    "target": str | dict | None, "params": dict}``.

    The handler accepts a ``target`` value as either a bare string
    ``"rdc-vcenter"`` (matches ``query_topology`` / ``query_audit``) or
    the partial dict ``{"name": "rdc-vcenter"}``. Both are normalised by
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

    Not part of the public MCP / REST surface: only the agent layer ever
    re-dispatches on its own behalf. The REST ``POST /api/v1/approvals/{id}/approve``
    path keeps its inline ``dispatch(..., _approved=True)`` call for the
    human-driven express lane; the two coexist by design (the operator/agent
    split documented in #1117).
    """
    return await _call_operation_impl(operator, arguments, approved=True)


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
    """
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
            safety_level=descriptor.safety_level,
            requires_approval=descriptor.requires_approval,
            is_enabled=descriptor.is_enabled,
            custom_description=descriptor.custom_description,
            custom_notes=descriptor.custom_notes,
        )
