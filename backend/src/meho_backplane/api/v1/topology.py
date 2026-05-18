# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/topology*`` — REST front for the G9.1 + G9.2 topology graph.

G9.1-T5 (#453) of Initiative #363 mounted the four read/refresh routes
that wrap the merged T3 (#450) refresh service and T4 (#451)
recursive-CTE query verbs:

* ``GET  /api/v1/topology/dependents/{name}``   — reverse closure
  ("what depends on me"). Wraps :func:`find_dependents`.
* ``GET  /api/v1/topology/dependencies/{name}`` — forward closure
  ("what I depend on"). Wraps :func:`find_dependencies`.
* ``GET  /api/v1/topology/path``                — shortest unweighted
  path between two named nodes, or ``null`` when unreachable. Wraps
  :func:`find_path`.
* ``POST /api/v1/topology/refresh/{target_name}`` — on-demand
  rediscovery of one target's topology. Wraps
  :func:`refresh_target_topology`.

G9.2-T5 (#597) of Initiative #364 mounts the three curated-edge routes
on top — the HTTP front the CLI (T6) wraps and the integration tests
(T9) exercise:

* ``POST   /api/v1/topology/edges``             — create/upsert a
  curated edge. Wraps :func:`annotate_edge`. Requires
  :class:`TenantRole.TENANT_ADMIN`.
* ``DELETE /api/v1/topology/edges/{edge_id}``   — hard-delete a
  curated edge by id. Wraps :func:`unannotate_edge`. Requires
  :class:`TenantRole.TENANT_ADMIN`. The §3 "auto-edge deletion is a
  no-op" rule of #364 surfaces as HTTP 409.
* ``GET    /api/v1/topology/edges``             — flat filterable
  listing across the tenant. Wraps :func:`list_edges`. Requires
  :class:`TenantRole.OPERATOR`.

The fifth read route (``GET /api/v1/targets/discover``) lives on the
targets router (:mod:`meho_backplane.api.v1.targets`) so it sits under
the canonical ``/api/v1/targets`` prefix next to the other
target-scoped verbs.

RBAC
----

The read + refresh half (G9.1) requires ``operator`` minimum
(``read_only`` gets 403 via :func:`require_role`). The refresh route
writes to ``graph_node`` / ``graph_edge`` but never to ``targets``;
per the Initiative #363 acceptance criteria v0.2 keeps it ``operator``.

The curated-edge writes (G9.2 — ``POST`` / ``DELETE``) require
``tenant_admin`` instead. Annotation is a policy-layer assertion: an
operator-level member must **not** be able to add a ``depends-on`` edge
that shrinks the auto-flagged blast radius of an op they then run, so
the gate sits at the same level as ``POST /api/v1/targets`` — the
canonical G0.3 ``tenant_admin`` precedent. ``GET /edges`` remains
``operator`` (a tenant inventory view, no mutation).

Tenant scoping
--------------

There is no surface that accepts a ``tenant_id`` from the path, query
string, or body. The query verbs filter ``graph_node.tenant_id`` and
``graph_edge.tenant_id`` against ``operator.tenant_id`` in both the
anchor and the recursive term; ``refresh`` resolves the target
tenant-scoped via :func:`resolve_target` and the reconcile writes
every row under ``operator.tenant_id``. A same-named node or target in
another tenant is invisible — cross-tenant traversal *and*
cross-tenant refresh are impossible by construction.

Audit + broadcast
-----------------

Each route binds ``audit_op_id`` / ``audit_op_class`` into structlog
contextvars *before* the service call so the chassis
:class:`~meho_backplane.audit.AuditMiddleware` row (and the broadcast
event it derives) carry the canonical op identity even if the handler
raises mid-call. The read verbs bind ``topology.dependents`` /
``topology.dependencies`` / ``topology.path`` with
``op_class="read"``. The refresh route binds ``topology.refresh`` /
``op_class="read"`` for the HTTP-level row; the refresh *service*
additionally writes its own domain-level audit row + one broadcast
event with the per-target node/edge counts (Initiative #363 item 11) —
the two rows are intentional, mirroring how the operations dispatcher
writes a domain row alongside the middleware's HTTP row.

The ``.dependents`` / ``.dependencies`` / ``.path`` / ``.refresh``
suffixes are none of the broadcast classifier's read/write verb
suffixes, so the explicit ``audit_op_class="read"`` override is
load-bearing — without it the classifier would default to
``op_class="other"`` and emit the full request payload instead of a
read-shaped trace.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.engine import get_session
from meho_backplane.db.models import GraphEdge, GraphEdgeKind, GraphNode
from meho_backplane.targets.resolver import resolve_target
from meho_backplane.topology.annotate import (
    AutoEdgeDeletionError,
    InvalidEdgeKindError,
    NodeRef,
    annotate_edge,
    unannotate_edge,
)
from meho_backplane.topology.query import (
    AmbiguousNodeError,
    find_dependencies,
    find_dependents,
    find_path,
    list_edges,
)
from meho_backplane.topology.refresh import RefreshResult, refresh_target_topology
from meho_backplane.topology.resolvers import NodeNotFoundError
from meho_backplane.topology.schemas import (
    TopologyEdge,
    TopologyEdgeEndpoint,
    TopologyNode,
    TopologyPath,
)

__all__ = ["router"]

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/topology", tags=["topology"])

#: Module-level Depends closures — required to satisfy ruff B008 (a
#: mutable call in a default-argument position is disallowed). Same
#: pattern :mod:`meho_backplane.api.v1.targets` /
#: :mod:`meho_backplane.api.v1.retrieve` established. The admin closure
#: gates the G9.2 curated-edge writes (``POST`` / ``DELETE`` ``/edges``)
#: behind ``tenant_admin``; the G9.1 read + refresh routes stay at
#: ``operator``.
_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))

#: Canonical op identifiers bound into ``audit_op_id`` per route.
#: Pinned as module constants so the contract is greppable from tests +
#: G8 dashboards and a typo surfaces at first call rather than as a
#: silent broadcast under the wrong op_id. Mirrors ``_KB_OP_IDS`` in
#: :mod:`meho_backplane.api.v1.kb`.
_OP_DEPENDENTS = "topology.dependents"
_OP_DEPENDENCIES = "topology.dependencies"
_OP_PATH = "topology.path"
_OP_REFRESH = "topology.refresh"
_OP_ANNOTATE = "topology.annotate"
_OP_UNANNOTATE = "topology.unannotate"
_OP_LIST_EDGES = "topology.list_edges"

#: HTTP-boundary ceiling on the ``GET /edges`` ``limit`` query param.
#: Mirrors :data:`meho_backplane.topology.query._MAX_EDGE_LIMIT`; pinned
#: here so a future widening at the service layer does not silently
#: widen the HTTP boundary.
_LIST_EDGES_LIMIT_DEFAULT = 200
_LIST_EDGES_LIMIT_MAX = 1000

#: Bounds mirror the service-layer defaults (``query._DEFAULT_DEPTH`` /
#: ``query._DEFAULT_MAX_HOPS``); the ceilings cap a pathological
#: traversal at the HTTP boundary so a hostile ``depth`` query param
#: cannot ask the recursive CTE to walk an unbounded closure (#363
#: performance discipline — depth-16 default, hard ceiling here).
_DEPTH_DEFAULT = 16
_DEPTH_MAX = 64
_MAX_HOPS_DEFAULT = 8
_MAX_HOPS_MAX = 32


@router.get("/dependents/{name}", response_model=list[TopologyNode])
async def dependents(
    name: str,
    depth: int = Query(default=_DEPTH_DEFAULT, ge=1, le=_DEPTH_MAX),
    kind: str | None = Query(default=None),
    kind_filter: str | None = Query(default=None),
    operator: Operator = _require_operator,
) -> list[TopologyNode]:
    """Reverse closure: every node that depends on *name*.

    Wraps :func:`~meho_backplane.topology.query.find_dependents`. The
    root node is included at depth 0 so a caller can distinguish "node
    exists but has no dependents" (one-element list) from "node does
    not exist in this tenant" (empty list).

    ``kind`` pins the anchor to the ``(tenant_id, kind, name)`` unique
    row; omit it only when *name* is unique across kinds in the tenant.
    A bare *name* that resolves to multiple kinds returns 409
    (``ambiguous_node``) rather than silently merging unrelated
    closures. ``kind_filter`` restricts the walk to edges of that
    ``graph_edge.kind``.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_DEPENDENTS,
        audit_op_class="read",
    )
    try:
        return await find_dependents(
            operator,
            name,
            kind=kind,
            depth=depth,
            kind_filter=kind_filter,
        )
    except AmbiguousNodeError as exc:
        raise _ambiguous_node_http(exc) from exc


@router.get("/dependencies/{name}", response_model=list[TopologyNode])
async def dependencies(
    name: str,
    depth: int = Query(default=_DEPTH_DEFAULT, ge=1, le=_DEPTH_MAX),
    kind: str | None = Query(default=None),
    kind_filter: str | None = Query(default=None),
    operator: Operator = _require_operator,
) -> list[TopologyNode]:
    """Forward closure: everything *name* depends on.

    The mirror of :func:`dependents` — same shape, same one-row-per-
    node closure dedupe, same ``kind`` disambiguation contract, same
    tenant scoping — walking edges out of the current node rather than
    into it. Wraps
    :func:`~meho_backplane.topology.query.find_dependencies`.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_DEPENDENCIES,
        audit_op_class="read",
    )
    try:
        return await find_dependencies(
            operator,
            name,
            kind=kind,
            depth=depth,
            kind_filter=kind_filter,
        )
    except AmbiguousNodeError as exc:
        raise _ambiguous_node_http(exc) from exc


@router.get("/path", response_model=TopologyPath | None)
async def path(
    from_name: str = Query(..., alias="from"),
    to_name: str = Query(..., alias="to"),
    from_kind: str | None = Query(default=None),
    to_kind: str | None = Query(default=None),
    max_hops: int = Query(default=_MAX_HOPS_DEFAULT, ge=1, le=_MAX_HOPS_MAX),
    operator: Operator = _require_operator,
) -> TopologyPath | None:
    """Shortest unweighted path from ``from`` to ``to``, or ``null``.

    Wraps :func:`~meho_backplane.topology.query.find_path`. The query
    params are ``from`` / ``to`` (matching the Initiative #363 route
    spec ``?from=A&to=B``); the handler binds them via ``alias`` since
    ``from`` is a Python keyword. Walks edges in both directions so the
    path follows the graph's connectivity rather than only its edge
    orientation. Returns ``null`` (HTTP 200) when *to* is unreachable
    from *from* within ``max_hops`` or either endpoint does not exist
    in this tenant — unreachability is a valid answer, not an error.

    ``from_kind`` / ``to_kind`` pin each endpoint independently; an
    unpinned name resolving to multiple kinds returns 409
    (``ambiguous_node``).
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_PATH,
        audit_op_class="read",
    )
    try:
        return await find_path(
            operator,
            from_name,
            to_name,
            from_kind=from_kind,
            to_kind=to_kind,
            max_hops=max_hops,
        )
    except AmbiguousNodeError as exc:
        raise _ambiguous_node_http(exc) from exc


@router.post("/refresh/{target_name}", response_model=RefreshResult)
async def refresh(
    target_name: str,
    operator: Operator = _require_operator,
    session: AsyncSession = Depends(get_session),
) -> RefreshResult:
    """Rediscover *target_name*'s topology and reconcile it into the graph.

    Wraps :func:`~meho_backplane.topology.refresh.refresh_target_topology`.
    *target_name* is resolved tenant-scoped via
    :func:`~meho_backplane.targets.resolver.resolve_target` (alias-
    aware, 404 with near-misses when nothing matches) — so a principal
    can only ever refresh a target in their own tenant and the
    reconcile writes every row under ``operator.tenant_id``.
    Cross-tenant refresh is impossible: there is no path by which a
    target id from another tenant reaches the service.

    The refresh service writes its own domain-level ``audit_log`` row
    (``op_id="topology.refresh"``, ``op_class="read"``, the per-target
    node/edge counts in ``payload``) and publishes one fail-open
    broadcast event inside its own transaction; this route binds the
    same op identity for the chassis HTTP-level audit row so both rows
    classify consistently.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_REFRESH,
        audit_op_class="read",
    )
    target = await resolve_target(session, operator.tenant_id, target_name)
    result = await refresh_target_topology(target, operator)
    _log.info(
        "topology_refresh_route_completed",
        target_id=str(result.target_id),
        tenant_id=str(operator.tenant_id),
        added_nodes=result.added_nodes,
        added_edges=result.added_edges,
        removed_nodes=result.removed_nodes,
        removed_edges=result.removed_edges,
    )
    return result


def _ambiguous_node_http(exc: AmbiguousNodeError) -> HTTPException:
    """Map :class:`AmbiguousNodeError` to a 409 with the candidate kinds.

    The query layer raises a plain :class:`ValueError` subclass when a
    bare-name anchor resolves to more than one ``kind``. Surfacing it
    as a 409 (rather than letting it become an unhandled 500) lets the
    caller re-issue with an explicit ``kind`` — the same recovery the
    CLI/MCP fronts will offer. The ambiguous kinds are echoed in
    ``detail`` so the client can present the choice without a second
    round trip.
    """
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": "ambiguous_node",
            "name": exc.name,
            "kinds": sorted(exc.kinds),
        },
    )


# ---------------------------------------------------------------------------
# G9.2-T5 (#597) — curated-edge routes
# ---------------------------------------------------------------------------


class _EdgeEndpoint(BaseModel):
    """Inbound JSON shape for one annotation endpoint.

    Mirrors :class:`meho_backplane.topology.annotate.NodeRef` on the wire:
    ``name`` is required, ``kind`` is the optional disambiguator for a
    bare name that resolves to multiple :class:`GraphNode` rows in the
    tenant. Frozen so the route handler cannot accidentally rebind the
    field; ``extra="forbid"`` rejects typo'd keys at the boundary
    (``{from: {nme: ...}}`` is a 422, not a silently-ignored body).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    kind: str | None = Field(default=None, min_length=1)

    def to_ref(self) -> NodeRef:
        """Convert the wire model to the service-layer :class:`NodeRef`.

        The service signature takes a frozen dataclass; the route owns
        the dataclass-to-Pydantic boundary so the substrate stays
        HTTP-agnostic and the wire shape stays JSON-friendly.
        """
        return NodeRef(name=self.name, kind=self.kind)


class _AnnotateEdgeRequest(BaseModel):
    """Inbound body for ``POST /api/v1/topology/edges``.

    ``kind`` is typed against :class:`GraphEdgeKind` so an unknown kind
    fails Pydantic validation (HTTP 422) **before** the service runs —
    the service still raises :class:`InvalidEdgeKindError` for
    non-route callers, but at the HTTP boundary the operator gets the
    standard FastAPI validation error shape (with the candidate list in
    the error context) rather than a 500-shaped diagnostic.

    The keyword ``from`` is reserved in Python so the attribute name is
    ``from_endpoint``; ``alias="from"`` keeps the wire shape the issue
    body specifies (``{from, kind, to, note?, evidence_url?}``).
    ``populate_by_name`` lets the alias **and** the attribute name both
    work — handy for hand-written test fixtures that use the Python
    attribute names.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        populate_by_name=True,
    )

    from_endpoint: _EdgeEndpoint = Field(alias="from")
    kind: GraphEdgeKind
    to_endpoint: _EdgeEndpoint = Field(alias="to")
    note: str | None = Field(default=None, max_length=2000)
    evidence_url: str | None = Field(default=None, max_length=2000)


@router.post(
    "/edges",
    response_model=TopologyEdge,
    status_code=status.HTTP_201_CREATED,
)
async def annotate_edge_route(
    body: _AnnotateEdgeRequest,
    operator: Operator = _require_admin,
    session: AsyncSession = Depends(get_session),
) -> TopologyEdge:
    """Create or upsert a curated edge.

    Wraps :func:`~meho_backplane.topology.annotate.annotate_edge`
    (G9.2-T3 #595). Resolves both endpoints tenant-scoped, idempotently
    upserts the ``(tenant_id, from, to, kind)`` row, runs §6 conflict
    detection on neighbouring auto edges, and writes one ``audit_log``
    row + one broadcast event under ``op_id="topology.annotate"`` /
    ``op_class="write"``.

    Returns the resulting :class:`TopologyEdge` (after the in-service
    commit). 201 on first insert; 201 on a repeat call too — the
    idempotent upsert path is indistinguishable from a fresh insert at
    the HTTP boundary, mirroring how :func:`annotate_edge` treats the
    re-annotate case as "operator asserts the edge exists".

    Error mapping:

    * **Pydantic 422** on an unknown ``kind`` (the
      :class:`GraphEdgeKind` enum field rejects the value before the
      service runs).
    * **404** when either endpoint does not exist in the tenant
      (``NodeNotFoundError``) — same wire shape every topology route
      uses for a missing graph node.
    * **409 ambiguous_node** when a bare ``name`` endpoint resolves
      to multiple kinds in the tenant — pass ``kind`` on the
      endpoint to disambiguate.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_ANNOTATE,
        audit_op_class="write",
    )
    try:
        edge = await annotate_edge(
            session,
            operator,
            body.from_endpoint.to_ref(),
            body.kind.value,
            body.to_endpoint.to_ref(),
            note=body.note,
            evidence_url=body.evidence_url,
        )
    except InvalidEdgeKindError as exc:
        # The Pydantic ``kind: GraphEdgeKind`` field rejects unknown
        # kinds at the boundary, so this branch only ever fires for a
        # mid-flight enum widening where Pydantic accepts a value the
        # service-layer ``_validate_kind`` does not yet recognise.
        # Re-raise as 422 with the candidate list so the operator's
        # diagnostic still matches the Pydantic-rejected case.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "invalid_edge_kind",
                "kind": exc.kind,
                "kinds": sorted(k.value for k in GraphEdgeKind),
            },
        ) from exc
    except AmbiguousNodeError as exc:
        raise _ambiguous_node_http(exc) from exc
    except NodeNotFoundError as exc:
        raise _node_not_found_http(exc) from exc
    return await _edge_to_response(edge, session)


@router.delete(
    "/edges/{edge_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unannotate_edge_route(
    edge_id: uuid.UUID,
    operator: Operator = _require_admin,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Hard-delete a curated edge by id and clear its reciprocal markers.

    Wraps :func:`~meho_backplane.topology.annotate.unannotate_edge`
    (G9.2-T3 #595). Returns 204 on success.

    Error mapping (the §3 auto-edge rule of Initiative #364):

    * **409 auto_edge_deletion** when the targeted row has
      ``source='auto'``. Auto edges resurrect on the next refresh, so
      manual deletion is meaningless; the service refuses with
      :class:`AutoEdgeDeletionError` and this route surfaces the typed
      diagnostic over HTTP so the CLI / MCP layers (T6, T7) can prompt
      the operator to annotate-over-auto instead.
    * **404** when no edge with that id exists in the caller's tenant
      (the service treats cross-tenant ids and missing ids identically
      — the tenant boundary is opaque to the caller).
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_UNANNOTATE,
        audit_op_class="write",
    )
    try:
        await unannotate_edge(session, operator, edge_id=edge_id)
    except AutoEdgeDeletionError as exc:
        # §3 of Initiative #364: the curated/auto split is the
        # recoverable-mistake invariant. An auto edge is the probe's
        # current view of reality; deleting it without removing the
        # underlying relationship just lets the next refresh re-create
        # it. Surface as 409 so the front layer can offer the right
        # remediation (annotate-over-auto, then unannotate the curated
        # row, or fix the probe input) rather than the operator
        # retrying the DELETE and getting confused.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "auto_edge_deletion",
                "edge_id": str(exc.edge_id),
                "message": (
                    "graph_edge has source='auto'; auto edges resurrect on "
                    "the next refresh, so manual deletion is a no-op. "
                    "Annotate over the auto edge first, then unannotate the "
                    "curated row."
                ),
            },
        ) from exc
    except ValueError as exc:
        # The triple-form selector raises ValueError for a missing row;
        # the id-form selector raises ValueError when the row is not in
        # this tenant (cross-tenant ids are indistinguishable from
        # missing ones to the caller, per the service's tenant-boundary
        # contract). Both collapse to a 404 with the requested id —
        # leaking "exists in another tenant" would be the tenant-
        # boundary violation the service is guarding against.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "edge_not_found",
                "edge_id": str(edge_id),
                "message": str(exc),
            },
        ) from exc


@router.get("/edges", response_model=list[TopologyEdge])
async def list_edges_route(
    kind: GraphEdgeKind | None = Query(default=None),
    source: str | None = Query(default=None, pattern="^(auto|curated)$"),
    from_name: str | None = Query(default=None, alias="from"),
    to_name: str | None = Query(default=None, alias="to"),
    conflicts: bool = Query(default=False),
    limit: int = Query(
        default=_LIST_EDGES_LIMIT_DEFAULT,
        ge=1,
        le=_LIST_EDGES_LIMIT_MAX,
    ),
    offset: int = Query(default=0, ge=0),
    operator: Operator = _require_operator,
    session: AsyncSession = Depends(get_session),
) -> list[TopologyEdge]:
    """Flat filterable listing of edges in the caller's tenant.

    Wraps :func:`~meho_backplane.topology.query.list_edges` (G9.2-T4
    #596). The query params are forwarded straight through (``from`` /
    ``to`` are bound via :class:`Query` ``alias`` because ``from`` is a
    Python keyword); the tenant boundary comes from ``operator.tenant_id``
    and is non-overrideable by query string or body.

    ``kind`` is typed against :class:`GraphEdgeKind` so an unknown kind
    is rejected at the HTTP boundary (422) before the helper runs;
    ``source`` is constrained to the two ``graph_edge.source`` values
    by a regex pattern; ``conflicts=true`` forwards
    ``conflicts_only=True`` to surface the recoverability listing for
    a wrong annotation (§6 of Initiative #364).

    A bare ``from`` / ``to`` name that resolves to multiple kinds in
    the tenant surfaces as **409 ambiguous_node** (same shape as the
    traversal verbs) so the caller can re-issue with a kind-qualified
    endpoint. A name that does not resolve at all yields an empty
    list, not a 404 — consistent with the helper's "missing anchor →
    empty result" shape.

    The route binds ``audit_op_id="topology.list_edges"`` /
    ``audit_op_class="read"`` so the audit row + broadcast event carry
    the canonical identity. ``.list_edges`` is not in the broadcast
    classifier's read/write suffix tables, so the explicit override is
    load-bearing (same rationale as the traversal verbs' ``.dependents``
    / ``.dependencies`` / ``.path`` bindings).
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_LIST_EDGES,
        audit_op_class="read",
    )
    try:
        edges = await list_edges(
            session,
            operator.tenant_id,
            kind=kind.value if kind is not None else None,
            source=source,
            from_ref=from_name,
            to_ref=to_name,
            conflicts_only=conflicts,
            limit=limit,
            offset=offset,
        )
    except AmbiguousNodeError as exc:
        raise _ambiguous_node_http(exc) from exc
    return edges


def _node_not_found_http(exc: NodeNotFoundError) -> HTTPException:
    """Map :class:`NodeNotFoundError` to a 404 with the missing identifier.

    The annotate flow resolves both endpoints before the upsert; a
    missing endpoint is the operator's first-line diagnostic ("did you
    register that target / node yet?"), not a server fault. 404 with
    the name + kind echoed in ``detail`` lets the CLI / MCP fronts
    render a precise error without a second round trip.
    """
    detail: dict[str, str | None] = {
        "error": "node_not_found",
        "name": exc.name,
    }
    if exc.kind is not None:
        detail["kind"] = exc.kind
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=detail,
    )


async def _edge_to_response(edge: object, session: AsyncSession) -> TopologyEdge:
    """Coerce the ORM :class:`GraphEdge` returned by ``annotate_edge`` to
    the wire shape.

    :func:`annotate_edge` returns the SQLAlchemy ORM row; the route's
    declared ``response_model`` is the frozen Pydantic
    :class:`TopologyEdge` shape (deep-frozen ``properties``, no
    transient fields). :class:`GraphEdge` does not define ORM
    relationships to its endpoint :class:`GraphNode` rows — the model
    keeps the foreign-key columns explicit and leaves traversal to the
    recursive-CTE query layer — so this helper re-fetches the nodes
    via the session identity map (free; the service just loaded both
    inside the same transaction via :func:`resolve_node`) and assembles
    the nested :class:`TopologyEdgeEndpoint` shape the response model
    expects.
    """
    assert isinstance(edge, GraphEdge)  # narrow for mypy + runtime check
    # ``session.get`` hits the identity map for rows already loaded
    # inside the transaction — the resolver loaded both endpoints
    # before the service's upsert, so this is a no-IO lookup in the
    # common path.
    from_node = await session.get(GraphNode, edge.from_node_id)
    to_node = await session.get(GraphNode, edge.to_node_id)
    if from_node is None or to_node is None:
        # FK ``ON DELETE CASCADE`` makes this unreachable under normal
        # operation — a mid-flight node delete would have cascaded
        # away the edge before we got here. Surface as 500 (rather
        # than letting the ``NoneType`` access bubble as 500 anyway)
        # so the log line names the inconsistency rather than the
        # stack trace.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="graph_edge endpoints missing — graph in inconsistent state",
        )
    return TopologyEdge(
        id=edge.id,
        from_endpoint=TopologyEdgeEndpoint(
            id=from_node.id,
            kind=from_node.kind,
            name=from_node.name,
        ),
        to_endpoint=TopologyEdgeEndpoint(
            id=to_node.id,
            kind=to_node.kind,
            name=to_node.name,
        ),
        kind=edge.kind,
        source=edge.source,
        properties=dict(edge.properties or {}),
        last_seen=edge.last_seen,
    )
