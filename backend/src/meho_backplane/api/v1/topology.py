# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/topology*`` — REST front for the G9.1 topology graph.

G9.1-T5 (#453) of Initiative #363. Four routes that wrap the merged
T3 (#450) refresh service and T4 (#451) recursive-CTE query verbs:

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

The fifth route (``GET /api/v1/targets/discover``) lives on the
targets router (:mod:`meho_backplane.api.v1.targets`) so it sits under
the canonical ``/api/v1/targets`` prefix next to the other
target-scoped verbs.

RBAC
----

Every route requires ``operator`` minimum (``read_only`` gets 403 via
:func:`require_role`). ``refresh`` writes to ``graph_node`` /
``graph_edge`` but never to ``targets``; per the Initiative #363
acceptance criteria v0.2 keeps it ``operator`` (the curated-edge
writes that would warrant ``tenant_admin`` land in G9.2).

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

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.engine import get_session
from meho_backplane.targets.resolver import resolve_target
from meho_backplane.topology.query import (
    AmbiguousNodeError,
    find_dependencies,
    find_dependents,
    find_path,
)
from meho_backplane.topology.refresh import RefreshResult, refresh_target_topology
from meho_backplane.topology.schemas import TopologyNode, TopologyPath

__all__ = ["router"]

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/topology", tags=["topology"])

#: Module-level Depends closure — required to satisfy ruff B008 (a
#: mutable call in a default-argument position is disallowed). Same
#: pattern :mod:`meho_backplane.api.v1.targets` /
#: :mod:`meho_backplane.api.v1.retrieve` established.
_require_operator = Depends(require_role(TenantRole.OPERATOR))

#: Canonical op identifiers bound into ``audit_op_id`` per route.
#: Pinned as module constants so the contract is greppable from tests +
#: G8 dashboards and a typo surfaces at first call rather than as a
#: silent broadcast under the wrong op_id. Mirrors ``_KB_OP_IDS`` in
#: :mod:`meho_backplane.api.v1.kb`.
_OP_DEPENDENTS = "topology.dependents"
_OP_DEPENDENCIES = "topology.dependencies"
_OP_PATH = "topology.path"
_OP_REFRESH = "topology.refresh"

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
