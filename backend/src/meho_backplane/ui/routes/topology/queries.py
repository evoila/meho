# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""UI-facing topology subgraph query helpers for the graph overlays.

Initiative #342 (G10.5 Topology UI), Task #882 (G10.5-T3). The graph
view (T2 / #881) renders the full tenant inventory; the overlays
here render *focused* subgraphs an operator drives via query params:

* ``?from=<name>[&kind=<kind>][&direction=dependents|dependencies][&depth=N]``
  -- the dependents (reverse) or dependencies (forward) subgraph
  rooted at ``<name>``. Same edge-direction semantics as the G9.1
  traversal verbs (:func:`meho_backplane.topology.query.find_dependents`
  / :func:`~meho_backplane.topology.query.find_dependencies`): an
  edge ``from_node --kind--> to_node`` reads "from_node depends on
  to_node", so the dependents subgraph walks *into* the root (every
  node that depends on it transitively) and the dependencies
  subgraph walks *out of* it (everything the root depends on
  transitively).

The shortest-path overlay (``?from=A&to=B``) lives in the sibling
:mod:`meho_backplane.ui.routes.topology.path_queries` module so each
overlay flavour fits inside the code-quality file-size budget.

Why a UI-facing variant of the G9.1 substrate?
==============================================

The substrate verbs in :mod:`meho_backplane.topology.query`
(:func:`find_dependents`, :func:`find_dependencies`, :func:`find_path`)
are tuned for the REST + MCP + CLI surfaces: they take an
:class:`~meho_backplane.auth.operator.Operator`, open their own
:class:`AsyncSession`, and use a PostgreSQL ``WITH RECURSIVE ... CYCLE``
CTE for the closure walk. The UI surface needs three different things:

1. **Dialect portability.** The chassis unit-test fixture uses
   SQLite (PG-only CYCLE-recursive isn't available there); the
   substrate verbs are tested against PostgreSQL in
   ``backend/tests/integration/``. The UI controller layer wants
   coverage in the unit suite so a route-shape regression is caught
   on every PR, not only on the integration sweep. The BFS helpers
   here use plain SQLAlchemy ORM ``select(...)`` so they execute on
   both dialects.

2. **Tenant scoping via the session context, not an Operator.** The
   UI surface flows ``tenant_id`` from the encrypted session cookie
   (:class:`meho_backplane.ui.auth.middleware.UISessionContext`),
   not from a JWT-derived :class:`Operator`. Direct ``tenant_id`` +
   ``db_session`` arguments match the shape
   :func:`meho_backplane.topology.query.list_nodes` already uses for
   the same UI surface.

3. **Subgraph emission instead of a flat closure.** The substrate
   returns a flattened list of nodes with depth markers; Cytoscape
   needs the **edges between them** as separate elements. BFS
   produces the visited-node set, then a second pass keeps only
   edges with both endpoints in that set. Mirrors the
   ``_fetch_edges_for_nodes`` pattern :mod:`...graph` uses for the
   full graph view.

Tenant-scoping defense in depth
===============================

Every edge query enforces the tenant boundary at three points: the
edge row itself + both endpoint joins. Same posture
:mod:`...graph._fetch_edges_for_nodes` and
:mod:`...detail._fetch_edges` established.

BFS depth-bound + visited semantics
===================================

The BFS visits the root at depth 0, its immediate neighbours at
depth 1, and so on. The ``depth`` argument caps the recursion.
Cycles are naturally broken by the visited-set guard. The
implementation does not need the CYCLE bookkeeping the PG recursive
CTE carries because the visited check runs in Python.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy import JSON, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from meho_backplane.db.models import GraphEdge, GraphNode

__all__ = [
    "DEFAULT_OVERLAY_DEPTH",
    "DEFAULT_PATH_MAX_HOPS",
    "MAX_OVERLAY_DEPTH",
    "MAX_PATH_MAX_HOPS",
    "AmbiguousNodeError",
    "NodeNotFoundError",
    "PathSubgraphResult",
    "SubgraphEdgeRow",
    "SubgraphNodeRow",
    "SubgraphResult",
    "fetch_dependencies_subgraph",
    "fetch_dependents_subgraph",
    "resolve_anchor",
]


#: Default depth for the dependents / dependencies overlay. ``3`` is
#: the operator-friendly v0.2 value.
DEFAULT_OVERLAY_DEPTH: Final[int] = 3

#: Hard ceiling on the overlay's traversal depth. Mirrors the
#: substrate ``find_dependents`` ``_DEFAULT_DEPTH``.
MAX_OVERLAY_DEPTH: Final[int] = 16

#: Default hop ceiling for the path query. ``8`` matches the substrate
#: :data:`~meho_backplane.topology.query._DEFAULT_MAX_HOPS`.
DEFAULT_PATH_MAX_HOPS: Final[int] = 8

#: Hard ceiling on the path search.
MAX_PATH_MAX_HOPS: Final[int] = 16


class AmbiguousNodeError(ValueError):
    """Raised when a bare-name lookup resolves to multiple kinds.

    A node name like ``app`` may legitimately exist under two kinds
    (``target`` and ``vm``); resolving by ``name`` alone would
    anchor on both and traverse a merged closure of two unrelated
    objects. The error surfaces the candidate kinds so the caller
    (the route handler) can return an HTTP 409 with a kind-
    disambiguation hint.

    Mirrors the substrate
    :class:`meho_backplane.topology.resolvers.AmbiguousNodeError`
    contract without re-importing it.
    """

    def __init__(self, name: str, kinds: list[str]) -> None:
        self.name = name
        self.kinds = kinds
        super().__init__(
            f"node name {name!r} resolves to multiple kinds in this tenant: "
            f"{sorted(kinds)!r}; pass ``kind=<one>`` to disambiguate"
        )


class NodeNotFoundError(ValueError):
    """Raised when a name (+ optional kind) resolves to no row in this tenant.

    The route handler maps this to HTTP 404. Cross-tenant ids and
    truly-absent names surface identically -- the tenant boundary is
    opaque.
    """

    def __init__(self, name: str, *, kind: str | None = None) -> None:
        self.name = name
        self.kind = kind
        if kind is not None:
            super().__init__(f"node ({kind=!r}, {name=!r}) not found in this tenant")
        else:
            super().__init__(f"node ({name=!r}) not found in this tenant")


@dataclass(frozen=True)
class SubgraphNodeRow:
    """One node in a subgraph overlay."""

    id: uuid.UUID
    kind: str
    name: str


@dataclass(frozen=True)
class SubgraphEdgeRow:
    """One edge in a subgraph overlay."""

    id: uuid.UUID
    kind: str
    source: str
    from_id: uuid.UUID
    to_id: uuid.UUID


@dataclass(frozen=True)
class SubgraphResult:
    """The nodes + edges a dependents / dependencies overlay renders."""

    root_id: uuid.UUID
    root_name: str
    root_kind: str
    nodes: list[SubgraphNodeRow]
    edges: list[SubgraphEdgeRow]
    truncated: bool


@dataclass(frozen=True)
class PathSubgraphResult:
    """The nodes + edges + highlighted-edge set a path overlay renders.

    ``highlighted_edge_ids`` is the subset of ``edges`` Cytoscape
    applies the ``highlight`` class to (the path's own edges).
    ``path_node_ids`` carries the path sequence in order so the UI
    can also highlight the path's nodes.
    """

    path_node_ids: tuple[uuid.UUID, ...]
    nodes: list[SubgraphNodeRow]
    edges: list[SubgraphEdgeRow]
    highlighted_edge_ids: frozenset[uuid.UUID]
    total_hops: int | None
    truncated: bool


async def resolve_anchor(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    kind: str | None,
) -> GraphNode:
    """Resolve ``(name [, kind])`` to a single ``graph_node`` row.

    * ``(tenant_id, kind, name)`` pinned -> at most one row by the
      unique index; raise :class:`NodeNotFoundError` on no match.
    * ``(tenant_id, name)`` bare -> 0 rows raises
      :class:`NodeNotFoundError`; 1 row returns it; >1 rows raises
      :class:`AmbiguousNodeError` with the candidate kinds.

    Soft-deleted nodes (``last_seen IS NULL``) are included -- the
    overlay mirrors the substrate traversal verbs, which do not filter
    ``last_seen`` (a soft-deleted node stays reachable, last-refresh-
    wins; #584).

    Exposed (not name-mangled with a leading underscore) because the
    sibling :mod:`...path_queries` module needs to resolve both
    endpoints independently against the same contract.
    """
    stmt = select(GraphNode).where(
        GraphNode.tenant_id == tenant_id,
        GraphNode.name == name,
    )
    if kind is not None:
        stmt = stmt.where(GraphNode.kind == kind)
    result = await db_session.execute(stmt)
    rows = list(result.scalars().all())
    if not rows:
        raise NodeNotFoundError(name, kind=kind)
    if len(rows) == 1:
        return rows[0]
    kinds = sorted({row.kind for row in rows})
    raise AmbiguousNodeError(name, kinds)


async def _bfs_neighbours(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    frontier_ids: list[uuid.UUID],
    direction: Literal["reverse", "forward"],
) -> list[tuple[uuid.UUID, GraphNode, GraphEdge]]:
    """One BFS expansion: every edge out of (or into) the frontier set.

    ``direction="reverse"`` walks edges *into* the frontier and
    returns the source endpoint -- the "dependents" semantic.
    ``direction="forward"`` walks edges *out of* the frontier and
    returns the destination endpoint -- the "dependencies" semantic.

    The triple tenant-scoping (edge + both endpoints) is the
    defense-in-depth posture. Soft-deleted edges (``last_seen IS
    NULL``) are included -- the overlay mirrors the substrate traversal
    verbs, which do not filter ``last_seen`` (last-refresh-wins; #584).

    Superseded edges (``properties->>'superseded_by' IS NOT NULL``) are
    excluded to mirror the G9.1 substrate traversal verbs
    (:func:`meho_backplane.topology.query.find_dependents` /
    :func:`~meho_backplane.topology.query.find_dependencies`, see
    Initiative #364 §6 / Task #595). A tenant that curates supersede
    annotations would otherwise see edges in the UI overlay that the
    substrate REST/CLI API hides -- a substrate-vs-UI divergence the
    operator cannot reconcile.
    """
    if not frontier_ids:
        return []

    from_alias = aliased(GraphNode)
    to_alias = aliased(GraphNode)
    stmt = (
        select(from_alias, to_alias, GraphEdge)
        .join(from_alias, from_alias.id == GraphEdge.from_node_id)
        .join(to_alias, to_alias.id == GraphEdge.to_node_id)
        .where(
            GraphEdge.tenant_id == tenant_id,
            from_alias.tenant_id == tenant_id,
            to_alias.tenant_id == tenant_id,
            # Mirror the substrate's superseded-edge exclusion -- the
            # PG raw-SQL ``e.properties->>'superseded_by' IS NULL``
            # idiom translated to dialect-portable ORM. PG returns SQL
            # NULL on a missing JSON key (``.is_(None)``); SQLite's
            # ``JSON1`` returns the JSON NULL token, which SQLAlchemy
            # exposes via the :data:`JSON.NULL` sentinel. The ``or_``
            # accepts both so the predicate fires identically on both
            # dialects.
            or_(
                GraphEdge.properties["superseded_by"].is_(None),
                GraphEdge.properties["superseded_by"] == JSON.NULL,
            ),
        )
        .order_by(GraphEdge.id)
    )
    if direction == "reverse":
        stmt = stmt.where(GraphEdge.to_node_id.in_(frontier_ids))
    else:
        stmt = stmt.where(GraphEdge.from_node_id.in_(frontier_ids))

    result = await db_session.execute(stmt)
    rows: list[tuple[uuid.UUID, GraphNode, GraphEdge]] = []
    for from_node, to_node, edge in result.all():
        if direction == "reverse":
            anchor_id = to_node.id
            neighbour = from_node
        else:
            anchor_id = from_node.id
            neighbour = to_node
        rows.append((anchor_id, neighbour, edge))
    return rows


def _expand_frontier(
    rows: list[tuple[uuid.UUID, GraphNode, GraphEdge]],
    *,
    visited_ids: dict[uuid.UUID, GraphNode],
    edges_by_id: dict[uuid.UUID, GraphEdge],
    max_nodes: int,
) -> tuple[list[uuid.UUID], bool]:
    """Fold one BFS hop's rows into the visited / edge maps.

    Returns ``(next_frontier_ids, truncated)``. ``truncated`` flips
    when promoting a candidate node would push the visited count
    over the configured ceiling.
    """
    next_frontier: list[uuid.UUID] = []
    truncated = False
    for _anchor_id, neighbour, edge in rows:
        edges_by_id.setdefault(edge.id, edge)
        if neighbour.id in visited_ids:
            continue
        if len(visited_ids) >= max_nodes:
            truncated = True
            continue
        visited_ids[neighbour.id] = neighbour
        next_frontier.append(neighbour.id)
    return next_frontier, truncated


async def _walk_subgraph(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    root: GraphNode,
    depth: int,
    direction: Literal["reverse", "forward"],
    max_nodes: int,
) -> SubgraphResult:
    """Run the bounded BFS that builds the dependents/dependencies subgraph.

    Visited-set semantics: a node is added once at its minimum depth
    and never revisited; cycles break naturally. ``max_nodes`` is
    the safety hatch matching the full graph view's 500-node
    frontend cap (#881).

    Returns a :class:`SubgraphResult` with the visited nodes + every
    edge whose **both endpoints** landed in the visited set.
    """
    visited_ids: dict[uuid.UUID, GraphNode] = {root.id: root}
    edges_by_id: dict[uuid.UUID, GraphEdge] = {}
    truncated = False
    current_frontier: list[uuid.UUID] = [root.id]

    for _ in range(depth):
        if not current_frontier:
            break
        rows = await _bfs_neighbours(
            db_session,
            tenant_id=tenant_id,
            frontier_ids=current_frontier,
            direction=direction,
        )
        next_frontier, hop_truncated = _expand_frontier(
            rows,
            visited_ids=visited_ids,
            edges_by_id=edges_by_id,
            max_nodes=max_nodes,
        )
        if hop_truncated:
            truncated = True
        current_frontier = next_frontier
        if truncated:
            break

    nodes = [
        SubgraphNodeRow(id=node.id, kind=node.kind, name=node.name) for node in visited_ids.values()
    ]
    edges = [
        SubgraphEdgeRow(
            id=edge.id,
            kind=edge.kind,
            source=edge.source,
            from_id=edge.from_node_id,
            to_id=edge.to_node_id,
        )
        for edge in edges_by_id.values()
        if edge.from_node_id in visited_ids and edge.to_node_id in visited_ids
    ]

    return SubgraphResult(
        root_id=root.id,
        root_name=root.name,
        root_kind=root.kind,
        nodes=nodes,
        edges=edges,
        truncated=truncated,
    )


async def fetch_dependents_subgraph(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    kind: str | None = None,
    depth: int = DEFAULT_OVERLAY_DEPTH,
    max_nodes: int = 500,
) -> SubgraphResult:
    """Return the dependents subgraph rooted at ``name``.

    Edge-direction: "what depends on me" -- reverse traversal.
    Raises :class:`NodeNotFoundError` on an unknown root,
    :class:`AmbiguousNodeError` on a bare-name root that resolves to
    multiple kinds in the tenant.
    """
    root = await resolve_anchor(db_session, tenant_id=tenant_id, name=name, kind=kind)
    return await _walk_subgraph(
        db_session,
        tenant_id=tenant_id,
        root=root,
        depth=depth,
        direction="reverse",
        max_nodes=max_nodes,
    )


async def fetch_dependencies_subgraph(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    kind: str | None = None,
    depth: int = DEFAULT_OVERLAY_DEPTH,
    max_nodes: int = 500,
) -> SubgraphResult:
    """Return the dependencies subgraph rooted at ``name``.

    Edge-direction: "what I depend on" -- forward traversal.
    """
    root = await resolve_anchor(db_session, tenant_id=tenant_id, name=name, kind=kind)
    return await _walk_subgraph(
        db_session,
        tenant_id=tenant_id,
        root=root,
        depth=depth,
        direction="forward",
        max_nodes=max_nodes,
    )
