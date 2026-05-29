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
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.api.v1._envelope import ENVELOPE_QUERY, EnvelopeVersion
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.engine import get_raw_session
from meho_backplane.db.models import GraphEdge, GraphEdgeKind, GraphNode
from meho_backplane.targets.resolver import resolve_target
from meho_backplane.topology.annotate import (
    AutoEdgeDeletionError,
    InvalidEdgeKindError,
    NodeRef,
    annotate_edge,
    unannotate_edge,
)
from meho_backplane.topology.bulk_import import (
    BulkImportRow,
    BulkImportValidationError,
    bulk_import_edges,
)
from meho_backplane.topology.query import (
    AmbiguousNodeError,
    find_dependencies,
    find_dependents,
    find_path,
    list_edges,
    query_diff,
    query_history,
    query_timeline,
)
from meho_backplane.topology.refresh import RefreshResult, refresh_target_topology
from meho_backplane.topology.resolvers import NodeNotFoundError
from meho_backplane.topology.schemas import (
    TopologyDiffResult,
    TopologyEdge,
    TopologyEdgeEndpoint,
    TopologyHistoryResult,
    TopologyNode,
    TopologyPath,
    TopologyTimelineResult,
)
from meho_backplane.topology.timeline_cursor import InvalidTimelineCursorError

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
_OP_BULK_IMPORT = "topology.bulk_import"
_OP_TIMELINE = "topology.timeline"
_OP_DIFF = "topology.diff"
_OP_HISTORY = "topology.history"

#: HTTP-boundary ceiling on the number of edges accepted in one
#: ``POST /edges/bulk`` body. The consumer's INVENTORY.md
#: (https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/rdc-hetzner-dc/INVENTORY.md)
#: lists ~30 curated cross-system edges at v0.2; the ceiling is sized
#: well above that so onboarding does not need to chunk the file, but
#: low enough that a stray hostile body cannot hold a single transaction
#: open for an unbounded scan. The service layer is unbounded by
#: design — the HTTP boundary is where the size guard belongs.
_BULK_IMPORT_MAX_EDGES = 1000

#: HTTP-boundary ceiling on the ``GET /edges`` ``limit`` query param.
#: Mirrors :data:`meho_backplane.topology.query._MAX_EDGE_LIMIT`; pinned
#: here so a future widening at the service layer does not silently
#: widen the HTTP boundary.
_LIST_EDGES_LIMIT_DEFAULT = 200
_LIST_EDGES_LIMIT_MAX = 1000

#: ``GET /topology/timeline`` ``limit`` ceiling at the HTTP boundary.
#: Default 50 per Task #861 acceptance criterion ("default
#: ``--limit 50``"); the substrate ceiling is 1000. Tighter HTTP cap
#: than the substrate is the same pattern :data:`_LIST_EDGES_LIMIT_MAX`
#: uses -- the boundary layer can clamp without re-issuing the
#: substrate primitive's own ceiling.
_TIMELINE_LIMIT_DEFAULT = 50
_TIMELINE_LIMIT_MAX = 1000

#: ``GET /topology/history/{name}`` ``limit`` ceiling at the HTTP
#: boundary. Mirrors the substrate ceiling
#: :data:`meho_backplane.topology.query._MAX_HISTORY_ROWS`. Per-resource
#: history is bounded by retention (default 90 days) and the operator
#: typically wants the complete walk in one response; the route default
#: is the same ceiling because a tighter cap on the HTTP boundary
#: would silently truncate the walk and the operator would think they
#: see the full history when they don't.
_HISTORY_LIMIT_MAX = 5000

#: Bounds mirror the service-layer defaults (``query._DEFAULT_DEPTH`` /
#: ``query._DEFAULT_MAX_HOPS``); the ceilings cap a pathological
#: traversal at the HTTP boundary so a hostile ``depth`` query param
#: cannot ask the recursive CTE to walk an unbounded closure (#363
#: performance discipline — depth-16 default, hard ceiling here).
_DEPTH_DEFAULT = 16
_DEPTH_MAX = 64
_MAX_HOPS_DEFAULT = 8
_MAX_HOPS_MAX = 32


@router.get("/dependents/{name}")
async def dependents(
    name: str,
    depth: int = Query(default=_DEPTH_DEFAULT, ge=1, le=_DEPTH_MAX),
    kind: str | None = Query(default=None),
    kind_filter: str | None = Query(default=None),
    envelope: EnvelopeVersion | None = ENVELOPE_QUERY,
    operator: Operator = _require_operator,
) -> list[TopologyNode] | dict[str, object]:
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

    G0.16-T6 Finding E (#1312) — opt-in to the REST↔MCP envelope
    agreement per ``docs/codebase/api-shape-conventions.md`` §4.
    Default response stays the v0.8.0 bare ``list[TopologyNode]`` so
    no client breaks; passing ``?envelope=v2`` returns the
    discriminated envelope ``{"kind": "dependents", "nodes": [...]}``
    matching the MCP ``query_topology`` tool's shape. The convention
    doc names "migration is REST-toward-MCP, not the other way";
    the v2 opt-in is the migration mechanism.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_DEPENDENTS,
        audit_op_class="read",
    )
    try:
        nodes = await find_dependents(
            operator,
            name,
            kind=kind,
            depth=depth,
            kind_filter=kind_filter,
        )
    except AmbiguousNodeError as exc:
        raise _ambiguous_node_http(exc) from exc
    if envelope is None:
        return nodes
    return {
        "kind": "dependents",
        "nodes": [n.model_dump(mode="json") for n in nodes],
    }


@router.get("/dependencies/{name}")
async def dependencies(
    name: str,
    depth: int = Query(default=_DEPTH_DEFAULT, ge=1, le=_DEPTH_MAX),
    kind: str | None = Query(default=None),
    kind_filter: str | None = Query(default=None),
    envelope: EnvelopeVersion | None = ENVELOPE_QUERY,
    operator: Operator = _require_operator,
) -> list[TopologyNode] | dict[str, object]:
    """Forward closure: everything *name* depends on.

    The mirror of :func:`dependents` — same shape, same one-row-per-
    node closure dedupe, same ``kind`` disambiguation contract, same
    tenant scoping — walking edges out of the current node rather than
    into it. Wraps
    :func:`~meho_backplane.topology.query.find_dependencies`. Honours
    the same ``?envelope=v2`` opt-in (G0.16-T6 Finding E #1312)
    returning ``{"kind": "dependencies", "nodes": [...]}``.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_DEPENDENCIES,
        audit_op_class="read",
    )
    try:
        nodes = await find_dependencies(
            operator,
            name,
            kind=kind,
            depth=depth,
            kind_filter=kind_filter,
        )
    except AmbiguousNodeError as exc:
        raise _ambiguous_node_http(exc) from exc
    if envelope is None:
        return nodes
    return {
        "kind": "dependencies",
        "nodes": [n.model_dump(mode="json") for n in nodes],
    }


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
    session: AsyncSession = Depends(get_raw_session),
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
    session: AsyncSession = Depends(get_raw_session),
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
    session: AsyncSession = Depends(get_raw_session),
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
    session: AsyncSession = Depends(get_raw_session),
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


# ---------------------------------------------------------------------------
# G9.2-T8 (#600) — bulk import
# ---------------------------------------------------------------------------


class _BulkImportEdge(BaseModel):
    """One edge in the ``POST /api/v1/topology/edges/bulk`` body.

    Same wire shape as :class:`_AnnotateEdgeRequest` (``{from, kind, to,
    note?, evidence_url?}``) — ``from`` is bound via ``alias`` because
    it is a Python keyword. ``kind`` is typed against
    :class:`GraphEdgeKind` so an unknown kind fails at the boundary
    (HTTP 422) before any service call runs, and the per-row error
    surfaces inside the standard FastAPI validation envelope with the
    row index in ``loc``. ``extra="forbid"`` rejects typo'd keys so a
    misspelled ``evidnce_url`` is caught at the boundary rather than
    silently dropped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    from_endpoint: _EdgeEndpoint = Field(alias="from")
    kind: GraphEdgeKind
    to_endpoint: _EdgeEndpoint = Field(alias="to")
    note: str | None = Field(default=None, max_length=2000)
    evidence_url: str | None = Field(default=None, max_length=2000)


class _BulkImportRequest(BaseModel):
    """Inbound body for ``POST /api/v1/topology/edges/bulk``.

    ``edges`` is a non-empty list bounded at the HTTP boundary by
    :data:`_BULK_IMPORT_MAX_EDGES`. Zero rows are rejected at 422
    rather than silently no-op'ing — an empty body is almost always a
    mistake in the operator's YAML / JSON file, and 422 surfaces it
    immediately. ``dry_run`` defaults to ``False`` per the issue
    body's "the CLI calls --dry-run explicitly" convention.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    edges: list[_BulkImportEdge] = Field(
        min_length=1,
        max_length=_BULK_IMPORT_MAX_EDGES,
    )
    dry_run: bool = Field(default=False)


class _BulkImportRowResponse(BaseModel):
    """One row's outcome in the ``POST /edges/bulk`` response.

    Mirrors :class:`~meho_backplane.topology.bulk_import.BulkEdgeResult`
    on the wire. ``edge_id`` is null in dry-run mode (no row exists)
    and on the apply-pass ``create`` rows where the service hasn't
    been called yet at validation time — the apply path always
    populates it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int
    action: str
    edge_id: str | None
    from_name: str
    from_kind: str
    to_name: str
    to_kind: str
    kind: str
    superseded: list[str]
    conflicts: list[str]


class _BulkImportResponse(BaseModel):
    """Response body for ``POST /api/v1/topology/edges/bulk``.

    ``dry_run`` echoes the call-site flag so a CLI rendering the JSON
    response can branch on it; ``created`` / ``updated`` / ``conflicts``
    are the aggregate counts the service computed; ``rows`` carries the
    per-row outcome in source order.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dry_run: bool
    created: int
    updated: int
    conflicts: int
    rows: list[_BulkImportRowResponse]


@router.post(
    "/edges/bulk",
    response_model=_BulkImportResponse,
    status_code=status.HTTP_200_OK,
)
async def bulk_import_edges_route(
    body: _BulkImportRequest,
    operator: Operator = _require_admin,
    session: AsyncSession = Depends(get_raw_session),
) -> _BulkImportResponse:
    """Batch-annotate edges from a single body in one transaction.

    Wraps
    :func:`~meho_backplane.topology.bulk_import.bulk_import_edges`
    (G9.2-T8 #600). Validation runs first against every row; a single
    invalid row rejects the entire batch (no partial apply). The
    apply pass writes every row inside one transaction so the
    "all-or-nothing per the issue body" criterion holds.

    Per-row audit + broadcast events fire one per applied row,
    mirroring the single-edge :func:`annotate_edge` flow; the
    chassis HTTP-level audit row at this route binds
    ``op_id="topology.bulk_import"`` / ``op_class="write"`` so the
    batch lives alongside the per-row audit trail under a recognisable
    parent identity.

    Error mapping:

    * **Pydantic 422** on body shape (unknown ``kind``, typo'd field,
      empty ``edges``, more than 1000 rows) — fails at the boundary
      before the service runs.
    * **422 invalid_bulk** with a per-row error list on
      :class:`BulkImportValidationError` — every row's failure is
      surfaced together so the operator fixes the file in one pass.
    * **200** on a successful apply or a successful dry-run.

    Uses HTTP 200 (not 201) for both apply and dry-run paths: the
    request is fundamentally a list mutation (create + update mix)
    where 201 is misleading for the update / dry-run path.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_BULK_IMPORT,
        audit_op_class="write",
    )
    rows = [
        BulkImportRow(
            from_ref=edge.from_endpoint.to_ref(),
            kind=edge.kind.value,
            to_ref=edge.to_endpoint.to_ref(),
            note=edge.note,
            evidence_url=edge.evidence_url,
        )
        for edge in body.edges
    ]
    try:
        result = await bulk_import_edges(session, operator, rows, dry_run=body.dry_run)
    except BulkImportValidationError as exc:
        # Surface every row's failure together. The operator's source
        # YAML / JSON is authoritative; a single 422 with the structured
        # list lets them fix every row in one pass rather than the
        # walk-the-file-N-times loop a per-row error would force.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "invalid_bulk",
                "errors": [
                    {
                        "index": e.index,
                        "error": e.error,
                        "message": e.message,
                        "name": e.name,
                        "kind": e.kind,
                        "kinds": e.kinds,
                    }
                    for e in exc.errors
                ],
            },
        ) from exc
    return _BulkImportResponse(
        dry_run=result.dry_run,
        created=result.created,
        updated=result.updated,
        conflicts=result.conflicts,
        rows=[
            _BulkImportRowResponse(
                index=row.index,
                action=row.action,
                edge_id=row.edge_id,
                from_name=row.from_name,
                from_kind=row.from_kind,
                to_name=row.to_name,
                to_kind=row.to_kind,
                kind=row.kind,
                superseded=row.superseded,
                conflicts=row.conflicts,
            )
            for row in result.rows
        ],
    )


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


# ---------------------------------------------------------------------------
# G9.3-T5 (#861) — tenant-wide timeline of graph changes
# ---------------------------------------------------------------------------


@router.get("/timeline", response_model=TopologyTimelineResult)
async def timeline_route(
    target: str | None = Query(default=None, max_length=256),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(
        default=_TIMELINE_LIMIT_DEFAULT,
        ge=1,
        le=_TIMELINE_LIMIT_MAX,
    ),
    cursor: str | None = Query(default=None, max_length=1024),
    operator: Operator = _require_operator,
    session: AsyncSession = Depends(get_raw_session),
) -> TopologyTimelineResult:
    """Tenant-wide chronological feed of graph changes.

    Wraps :func:`~meho_backplane.topology.query.query_timeline` (G9.3-T5
    #861). Walks ``graph_node_history`` + ``graph_edge_history`` in
    ``(valid_from DESC, history_id DESC)`` order, paginated via opaque
    forward-only cursor encoding ``(valid_from, history_id, source)``.
    Tenant scope is ``operator.tenant_id`` -- no surface accepts a
    tenant id on this route.

    Query parameters:

    * ``target`` -- optional target name or alias. Resolved tenant-
      scoped via :func:`~meho_backplane.targets.resolver.resolve_target`
      (alias-aware, 404 with near-misses when nothing matches). The
      resolved target id narrows the timeline to history rows for
      resources belonging to that target -- nodes whose ``target_id``
      matches, and edges whose either endpoint belongs to the target.
    * ``since`` / ``until`` -- ISO-8601 absolute datetimes; either
      bound is optional. Duration shorthand (``"24h"`` / ``"7d"``) is
      a CLI convenience that the CLI front parses; the REST API
      accepts absolute timestamps only, mirroring the audit-query
      router's split (G8.1-T2 #466).
    * ``limit`` -- page size (default ``50`` per the issue body;
      ceiling ``1000``).
    * ``cursor`` -- opaque next-page token from a prior response.

    Errors:

    * **404 ``no_target``** -- ``target`` did not resolve to a row in
      the tenant. Near-miss matches surface in ``detail.matches`` for
      operator self-correction (same shape as the ``refresh`` route).
    * **400 ``invalid_cursor``** -- ``cursor`` is not a valid opaque
      token (tampered or hand-crafted). The substrate's
      :class:`InvalidTimelineCursorError` message is echoed.

    The route binds ``audit_op_id="topology.timeline"`` /
    ``audit_op_class="audit_query"`` per [decision #3](docs/planning/v0.2-decisions.md)
    -- temporal graph queries are inspections of system state, parallel
    to G8's audit-log query surface; the broadcast event carries only
    ``{op_id, result_status, row_count}`` so the request filter (which
    may name a sensitive target) and the rows themselves never leak
    onto the SSE / Slack feed.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_TIMELINE,
        audit_op_class="audit_query",
    )
    target_id: uuid.UUID | None = None
    if target is not None:
        # Re-use the existing tenant-scoped resolver so a missing
        # target lands as the canonical 404 envelope (with near-miss
        # suggestions) the CLI already renders for refresh / show.
        resolved = await resolve_target(session, operator.tenant_id, target)
        target_id = resolved.id

    try:
        result = await query_timeline(
            operator,
            target_id=target_id,
            since=since,
            until=until,
            limit=limit,
            cursor=cursor,
        )
    except InvalidTimelineCursorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Broadcast aggregate signal: row count only, never per-row payload.
    structlog.contextvars.bind_contextvars(audit_row_count=len(result.rows))
    return result


# ---------------------------------------------------------------------------
# G9.3-T3 (#859) — per-resource history walk
# ---------------------------------------------------------------------------


@router.get(
    "/history/{name}",
    response_model=TopologyHistoryResult,
    responses={
        # 404 ``node_not_found`` / 409 ``ambiguous_node`` -- declared
        # explicitly so FastAPI's autogen OpenAPI surfaces both runtime
        # error shapes to SDK clients. The route handler below raises
        # via ``_node_not_found_http`` / ``_ambiguous_node_http`` which
        # produce ``HTTPException(404, detail={"error": "node_not_found",
        # ...})`` and ``HTTPException(409, detail={"error":
        # "ambiguous_node", ...})`` respectively. Without these
        # declarations the spec only listed 200 + 422 and clients had no
        # schema-driven signal for the recoverable per-anchor errors
        # (the operator's first-line diagnostics).
        404: {
            "description": ("Anchor node not found by ``name`` (and ``kind`` when supplied)."),
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "detail": {
                                "type": "object",
                                "properties": {
                                    "error": {
                                        "type": "string",
                                        "enum": ["node_not_found"],
                                    },
                                    "name": {"type": "string"},
                                    "kind": {"type": "string"},
                                },
                                "required": ["error", "name"],
                            },
                        },
                        "required": ["detail"],
                    },
                },
            },
        },
        409: {
            "description": (
                "Bare ``name`` resolves to multiple ``kind`` candidates; "
                "client should re-issue with an explicit ``kind``."
            ),
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "detail": {
                                "type": "object",
                                "properties": {
                                    "error": {
                                        "type": "string",
                                        "enum": ["ambiguous_node"],
                                    },
                                    "name": {"type": "string"},
                                    "kinds": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                                "required": ["error", "name", "kinds"],
                            },
                        },
                        "required": ["detail"],
                    },
                },
            },
        },
    },
)
async def history_route(
    name: str,
    kind: str | None = Query(default=None, max_length=64),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    include_edges: bool = Query(default=False),
    limit: int = Query(default=_HISTORY_LIMIT_MAX, ge=1, le=_HISTORY_LIMIT_MAX),
    operator: Operator = _require_operator,
) -> TopologyHistoryResult:
    """Chronological history walk anchored at one ``graph_node``.

    Wraps :func:`~meho_backplane.topology.query.query_history` (G9.3-T3
    #859). Companion to ``GET /topology/timeline`` (G9.3-T5): timeline
    is "what changed in the graph at all in this window"; history is
    "what changed for THIS specific resource". Resolved via the G9.1
    resolver :func:`~meho_backplane.topology.resolvers.resolve_node` so
    name + optional kind disambiguation use the same surface every
    G9.1 + G9.2 verb does.

    Query parameters:

    * ``kind`` -- optional ``graph_node.kind`` pin to disambiguate
      when a bare *name* resolves to multiple kinds in the tenant.
    * ``since`` / ``until`` -- ISO-8601 absolute datetime bounds on
      ``valid_from`` (inclusive at both ends). The CLI front resolves
      duration shorthand (``"24h"`` / ``"7d"``) to an absolute
      timestamp before crossing the wire, mirroring the timeline
      route's split.
    * ``include_edges`` -- when ``true``, also walk every history row
      for edges incident to the resolved node (joined via the inner
      subquery ``edge_id IN (SELECT id FROM graph_edge WHERE
      from_node_id = anchor OR to_node_id = anchor)``). The merged
      result still orders newest-first.
    * ``limit`` -- hard cap on returned rows (1..``_HISTORY_LIMIT_MAX``).
      Defaults to the ceiling because per-resource history is bounded
      by retention; tighter caps would silently truncate the walk.

    Errors:

    * **404 ``node_not_found``** -- *name* (and *kind*, when supplied)
      does not resolve to any node in the tenant. Cross-tenant names
      surface identically -- the tenant boundary is opaque to the
      caller.
    * **409 ``ambiguous_node``** -- bare *name* resolves to multiple
      kinds; pass ``kind`` to disambiguate.

    The route binds ``audit_op_id="topology.history"`` /
    ``audit_op_class="audit_query"`` per [decision #3](docs/planning/v0.2-decisions.md)
    -- temporal graph queries are inspections of system state,
    parallel to G8's audit-log query surface; the broadcast event
    carries only ``{op_id, result_status, row_count}`` so the
    response rows (which may carry the snapshot of a sensitive
    resource's pre/post payload) never leak onto the SSE / Slack
    feed. Same shape T5's timeline route uses.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_HISTORY,
        audit_op_class="audit_query",
    )
    try:
        result = await query_history(
            operator,
            name,
            kind=kind,
            since=since,
            until=until,
            include_edges=include_edges,
            limit=limit,
        )
    except AmbiguousNodeError as exc:
        raise _ambiguous_node_http(exc) from exc
    except NodeNotFoundError as exc:
        raise _node_not_found_http(exc) from exc

    structlog.contextvars.bind_contextvars(audit_row_count=len(result.rows))
    return result


# ---------------------------------------------------------------------------
# G9.3-T4 (#860) — graph-level diff between two timestamps
# ---------------------------------------------------------------------------


@router.get(
    "/diff",
    response_model=TopologyDiffResult,
    responses={
        # 400 ``invalid_window`` -- ``ts1 >= ts2``. Declared explicitly so
        # FastAPI's autogen OpenAPI surfaces the 400 to SDK clients; the
        # route handler below raises ``HTTPException(400, detail={"error":
        # "invalid_window", "message": ...})``. Without this declaration
        # the spec only listed 200 + 422 and clients had no schema-driven
        # signal that 400 is a documented response.
        400: {
            "description": "Invalid window (``ts1 >= ts2``).",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "detail": {
                                "type": "object",
                                "properties": {
                                    "error": {
                                        "type": "string",
                                        "enum": ["invalid_window"],
                                    },
                                    "message": {"type": "string"},
                                },
                                "required": ["error", "message"],
                            },
                        },
                        "required": ["detail"],
                    },
                },
            },
        },
    },
)
async def diff_route(
    ts1: datetime = Query(..., description="exclusive lower bound on valid_from"),
    ts2: datetime = Query(..., description="inclusive upper bound on valid_from"),
    changed_only: bool = Query(default=False),
    kind: str | None = Query(default=None, max_length=64),
    operator: Operator = _require_operator,
) -> TopologyDiffResult:
    """Tenant-scoped graph-level diff between ``ts1`` and ``ts2``.

    Wraps :func:`~meho_backplane.topology.query.query_diff` (G9.3-T4
    #860). Returns one entry per resource that mutated in
    ``(ts1, ts2]``, folded into a net ``created`` / ``updated`` /
    ``removed`` change_kind. Output is capped at 1000 entries with a
    truncation marker + remediation hint -- the diff is the heavy
    temporal query and the cap protects the API from a hostile / wide
    time window.

    Query parameters:

    * ``ts1`` -- exclusive lower bound on ``valid_from``.
    * ``ts2`` -- inclusive upper bound on ``valid_from``.
    * ``changed_only`` -- when ``true``, suppress ``updated`` entries
      whose only mutation in the window was a ``last_seen``-bump
      (refresh-service heartbeats). ``created`` / ``removed`` entries
      always surface.
    * ``kind`` -- optional resource-kind filter (node ``kind`` like
      ``vm`` or edge ``kind`` like ``runs-on``); applied after the
      fold so the cap fires on the post-filter cohort.

    Errors:

    * **400 ``invalid_window``** -- ``ts1 >= ts2``. The substrate
      raises :class:`ValueError`; the route surfaces it as a 400 with
      the canonical message so the CLI / MCP fronts render a
      consistent diagnostic.

    The route binds ``audit_op_id="topology.diff"`` /
    ``audit_op_class="audit_query"`` per the G9 audit-query convention
    (decision #3) -- temporal graph queries are inspections of system
    state, parallel to G8's audit-log query surface, so the broadcast
    event carries only ``{op_id, result_status, row_count}`` and the
    per-row payload never leaks onto the SSE / Slack feed. One
    ``audit_log`` row per call from the chassis middleware.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_DIFF,
        audit_op_class="audit_query",
    )
    try:
        result = await query_diff(
            operator,
            ts1=ts1,
            ts2=ts2,
            changed_only=changed_only,
            kind_filter=kind,
        )
    except ValueError as exc:
        # ``ts1 >= ts2`` -- empty / inverted window. The substrate's
        # message names both values; surface as 400 so the operator
        # gets the diagnostic without a 500 round trip.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_window", "message": str(exc)},
        ) from exc

    # Broadcast aggregate signal: entry count only, never per-row payload.
    structlog.contextvars.bind_contextvars(audit_row_count=len(result.entries))
    return result
