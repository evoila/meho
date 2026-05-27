# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

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
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations._lookup import connector_exists, parse_connector_id
from meho_backplane.operations._search import hybrid_search, resolve_group_id
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.targets.resolver import resolve_target

__all__ = [
    "SEARCH_LIMIT_MAX",
    "CallOperationBody",
    "OperationDescriptor",
    "OperationGroupSummary",
    "OperationSearchHit",
    "UnknownConnectorError",
    "call_operation",
    "call_operation_with_approval",
    "describe_descriptor",
    "list_operation_groups",
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
    this to ``404`` explicitly; the MCP/CLI surfaces let it propagate
    as the structured handler error they already render. Subclasses
    :class:`ValueError` to stay consistent with the other meta-tool
    domain exceptions (e.g. the missing-target ``ValueError`` mapped
    to ``400`` in :mod:`meho_backplane.api.v1.operations`).
    """


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
    """One enabled operation group as returned by :func:`list_operation_groups`.

    ``when_to_use`` is the LLM-summarised string the agent reads to pick
    which group to search within. Populated by G0.7's ingestion pipeline
    for ``source_kind='ingested'`` connectors; supplied verbatim by
    :func:`~meho_backplane.operations.register_typed_operation` callers
    for typed connectors.
    """

    model_config = ConfigDict(frozen=True)

    group_key: str
    name: str
    when_to_use: str
    operation_count: int


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
    valid value, not an unknown field. ``params`` itself is a
    free-form ``dict`` because per-op parameter shape is enforced by
    the descriptor's ``parameter_schema`` further down the dispatch
    path; only the meta-tool body's own fields are constrained here.
    """

    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(min_length=1)
    op_id: str = Field(min_length=1)
    target: str | dict[str, Any] | None = None
    params: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Meta-tool handlers
# ---------------------------------------------------------------------------


async def list_operation_groups(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Return enabled operation groups for *connector_id*.

    ``arguments`` shape: ``{"connector_id": str}``.

    Only ``review_status='enabled'`` groups are returned — staged /
    disabled groups remain hidden from the agent. Tenant scoping: the
    union of built-in (``tenant_id IS NULL``) and tenant-curated
    (``tenant_id == operator.tenant_id``) rows.

    An *unknown* ``connector_id`` (no descriptors or groups registered
    for the parsed ``(product, version, impl_id)`` triple) raises
    :class:`UnknownConnectorError` — the REST route maps that to a
    ``404``. A *known* connector with zero enabled groups still returns
    an empty ``groups`` list (``200 []``): that empty is operationally
    meaningful (the connector exists; nothing is enabled yet). The
    distinction ends the "empty catalog" trap where a mis-shaped
    ``connector_id`` looked indistinguishable from an empty connector.
    """
    connector_id = arguments["connector_id"]
    product, version, impl_id = parse_connector_id(connector_id)
    if not await connector_exists(
        tenant_id=operator.tenant_id,
        product=product,
        version=version,
        impl_id=impl_id,
    ):
        raise UnknownConnectorError(
            f"unknown connector_id {connector_id!r} — expected <impl_id>-<version> "
            f"(e.g. 'vmware-rest-9.0', 'vault-1.x'). If this id appeared in "
            f"GET /api/v1/connectors, the listing is inconsistent with the "
            f"dispatcher's resolve path — please file a bug report; otherwise "
            f"the id is mistyped or the connector is not registered on this deploy"
        )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # Count operations per group in a single pass so the response can
        # carry ``operation_count`` without an N+1 round-trip. Joined on
        # the natural key plus ``group_id`` (NULL excluded — ungrouped ops
        # don't contribute to any group's count).
        group_stmt = (
            select(OperationGroup)
            .where(
                OperationGroup.product == product,
                OperationGroup.version == version,
                OperationGroup.impl_id == impl_id,
                OperationGroup.review_status == "enabled",
                # Tenant scoping: NULL (global) OR this tenant's rows.
                # `is_(None)` and `==` chain via or_; see the resolver for
                # the same shape on Target rows.
                (OperationGroup.tenant_id.is_(None))
                | (OperationGroup.tenant_id == operator.tenant_id),
            )
            .order_by(OperationGroup.group_key)
        )
        groups = (await session.execute(group_stmt)).scalars().all()
        # Per-group operation count: filter by group_id IN (...) and
        # is_enabled, then aggregate in Python (one query, group-by in
        # the application layer keeps SQL portable across PG + SQLite).
        if groups:
            group_ids = [g.id for g in groups]
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
        else:
            counts = {}
    summaries = [
        OperationGroupSummary(
            group_key=g.group_key,
            name=g.name,
            when_to_use=g.when_to_use,
            operation_count=counts.get(g.id, 0),
        )
        for g in groups
    ]
    _log.info(
        "list_operation_groups",
        connector_id=connector_id,
        group_count=len(summaries),
        tenant_id=str(operator.tenant_id),
    )
    return {
        "connector_id": connector_id,
        "groups": [s.model_dump(mode="json") for s in summaries],
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

    Like :func:`list_operation_groups`, an *unknown* ``connector_id``
    raises :class:`UnknownConnectorError` (REST → ``404``) while a
    *known* connector with no matching ops still returns an empty
    ``hits`` list (``200 []``) — identical unknown-vs-known-empty
    semantics across both meta-tools.
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
    if not await connector_exists(
        tenant_id=operator.tenant_id,
        product=product,
        version=version,
        impl_id=impl_id,
    ):
        raise UnknownConnectorError(
            f"unknown connector_id {connector_id!r} — expected <impl_id>-<version> "
            f"(e.g. 'vmware-rest-9.0', 'vault-1.x'). If this id appeared in "
            f"GET /api/v1/connectors, the listing is inconsistent with the "
            f"dispatcher's resolve path — please file a bug report; otherwise "
            f"the id is mistyped or the connector is not registered on this deploy"
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


def _normalize_target_arg(
    target_arg: str | dict[str, Any] | None,
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
    * Any other shape (empty string, dict without ``name`` /
      with empty ``name``) → ``ValueError`` with the same message the
      pre-widening handler raised, so existing 400 callers see the
      same surface.
    """
    if target_arg is None:
        return None
    if isinstance(target_arg, str):
        if not target_arg:
            raise ValueError("target must include a 'name' field when supplied")
        return {"name": target_arg}
    # ``dict``-typed branch. The Pydantic union validates the type; we
    # only enforce the "name is set" contract here.
    name = target_arg.get("name")
    if not name:
        raise ValueError("target must include a 'name' field when supplied")
    return target_arg


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
    target_arg = _normalize_target_arg(arguments.get("target"))
    params: dict[str, Any] = arguments.get("params") or {}

    resolved_target: Any = None
    if target_arg is not None:
        # ``name`` is guaranteed present + non-empty by
        # ``_normalize_target_arg``; the cast keeps mypy happy at the
        # resolver-call site.
        name = target_arg["name"]
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            resolved_target = await resolve_target(session, operator.tenant_id, name)
            # Bind into structlog so AuditMiddleware picks up the target_id
            # on the eventual audit row. Same shape as the targets routes.
            structlog.contextvars.bind_contextvars(audit_target_id=str(resolved_target.id))
        # Per-call ``fqdn`` override -- applied in memory only. The DB row
        # is not touched; the override travels with the resolved Target
        # for the lifetime of this dispatch and is read by connectors that
        # honour vhost routing (G3.6 VCF Automation). Non-string / empty
        # values fall through silently rather than overriding with a bad
        # value; an explicit ``None`` override is not supported (use a
        # fresh ``meho targets update`` to clear the column). Bare-string
        # ``target`` callers can't reach this branch (the normaliser
        # produces a ``name``-only dict); they must switch to the dict
        # shape to opt into vhost override.
        fqdn_override = target_arg.get("fqdn")
        if isinstance(fqdn_override, str) and fqdn_override:
            resolved_target.fqdn = fqdn_override
    result = await dispatch(
        operator=operator,
        connector_id=connector_id,
        op_id=op_id,
        target=resolved_target,
        params=params,
        _approved=approved,
    )
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
    the result envelope (``status='error'`` + ``error='<code>: …'``)
    rather than as exceptions — the dispatcher contract is "always
    return a structured result". The route layer turns 4xx-class
    errors into HTTP status codes; the meta-tool handler just returns
    the envelope so the MCP transport keeps a uniform shape.
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
