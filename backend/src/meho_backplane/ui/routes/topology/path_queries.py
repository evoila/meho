# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""UI-facing topology shortest-path query for the graph path overlay.

Initiative #342 (G10.5 Topology UI), Task #882 (G10.5-T3). Sibling
module to :mod:`meho_backplane.ui.routes.topology.queries` -- the
subgraph (dependents/dependencies) helpers live there, the
shortest-path search lives here. The split keeps each module inside
the code-quality file-size budget and the test coverage scoped by
overlay flavour.

Algorithm
=========

Bidirectional BFS treating storage-directed edges as undirected for
reachability. The path search answers "is there *any* route between
these two objects?" -- the operator question -- so an edge ``A->B``
contributes to the walk from both A and B. Returns the **shortest**
path by hop count (BFS guarantees minimum-hop) or ``None`` when the
target is unreachable within ``max_hops`` / either endpoint does not
resolve.

Mirrors the substrate
:data:`meho_backplane.topology.query._PATH_SQL` (the PG recursive
``WITH RECURSIVE ... bi_edge ... CYCLE`` shape) but in dialect-
portable SQLAlchemy ORM so the UI test suite (SQLite) can cover the
route end-to-end. The substrate verb stays the source of truth for
the REST + CLI + MCP surfaces; this module is UI-only.

Tenant scoping
==============

Every edge query enforces the boundary at three points -- edge row +
both endpoint joins -- matching :mod:`...queries`'
:func:`_bfs_neighbours` posture. Defense in depth against a future
invariant violation that lets a stray cross-tenant edge slip into
storage.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass

from sqlalchemy import JSON, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from meho_backplane.db.models import GraphEdge, GraphNode
from meho_backplane.ui.routes.topology.queries import (
    DEFAULT_PATH_MAX_HOPS,
    PathSubgraphResult,
    SubgraphEdgeRow,
    SubgraphNodeRow,
    resolve_anchor,
)

__all__ = ["fetch_path_subgraph"]


async def _bidirectional_neighbours(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    frontier_ids: list[uuid.UUID],
) -> list[tuple[uuid.UUID, GraphNode, GraphEdge]]:
    """One bidirectional BFS step: every edge touching the frontier.

    Returns ``(anchor_id, other_endpoint, edge)`` tuples where
    ``anchor_id`` is the frontier id the edge touched and
    ``other_endpoint`` is the node on the other side -- regardless of
    edge orientation. Self-loops (``from == to``) are surfaced once.

    Superseded edges (``properties->>'superseded_by' IS NOT NULL``)
    are excluded to mirror the G9.1 substrate path verb
    (:func:`meho_backplane.topology.query.find_path`, Initiative #364
    §6). Operators must see the same reachability in the UI overlay
    that the substrate REST/CLI API exposes.
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
            GraphEdge.last_seen.is_not(None),
            # Substrate parity: drop edges curated as superseded so the
            # UI path overlay's reachability matches what the CLI/REST
            # find_path verb returns. The ``or_(.is_(None), ==
            # JSON.NULL)`` combo handles both PG (SQL NULL on missing
            # key) and SQLite (JSON NULL token); see the sibling
            # ``queries._bfs_neighbours`` for the same predicate.
            or_(
                GraphEdge.properties["superseded_by"].is_(None),
                GraphEdge.properties["superseded_by"] == JSON.NULL,
            ),
            (GraphEdge.from_node_id.in_(frontier_ids)) | (GraphEdge.to_node_id.in_(frontier_ids)),
        )
        .order_by(GraphEdge.id)
    )
    result = await db_session.execute(stmt)
    rows: list[tuple[uuid.UUID, GraphNode, GraphEdge]] = []
    for from_node, to_node, edge in result.all():
        if from_node.id in frontier_ids:
            rows.append((from_node.id, to_node, edge))
        if to_node.id in frontier_ids and to_node.id != from_node.id:
            # Distinct branch when the edge is reachable from the
            # other side too. Self-loops (from == to) are only
            # surfaced once.
            rows.append((to_node.id, from_node, edge))
    return rows


def _reconstruct_path(
    parents: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]],
    source_id: uuid.UUID,
    target_id: uuid.UUID,
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """Walk the BFS parent map back from target to source.

    Returns ``(node_ids_in_order, edge_ids_in_order)`` from source to target.
    """
    node_path: list[uuid.UUID] = [target_id]
    edge_path: list[uuid.UUID] = []
    cursor = target_id
    while cursor != source_id:
        parent, edge_id = parents[cursor]
        node_path.append(parent)
        edge_path.append(edge_id)
        cursor = parent
    node_path.reverse()
    edge_path.reverse()
    return node_path, edge_path


@dataclass(frozen=True)
class _PathSearchState:
    """The running state of the bidirectional path BFS.

    The state is mutated in-place by the helpers (Python dicts /
    sets); using a frozen dataclass for the container keeps the
    *handle* itself immutable so a caller cannot swap fields mid-
    search.
    """

    visited: set[uuid.UUID]
    parents: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]]
    node_by_id: dict[uuid.UUID, GraphNode]
    edges_by_id: dict[uuid.UUID, GraphEdge]


def _process_hop_rows(
    state: _PathSearchState,
    rows: list[tuple[uuid.UUID, GraphNode, GraphEdge]],
    target_id: uuid.UUID,
) -> tuple[list[uuid.UUID], bool]:
    """Fold one hop's neighbour rows into the search state.

    Returns ``(next_frontier_ids, found_target)``. When the target is
    seen, the caller breaks the outer loop -- the parent map is
    sufficient for path reconstruction.
    """
    next_frontier: list[uuid.UUID] = []
    found = False
    for anchor_id, neighbour, edge in rows:
        state.edges_by_id.setdefault(edge.id, edge)
        state.node_by_id.setdefault(neighbour.id, neighbour)
        if neighbour.id in state.visited:
            continue
        state.visited.add(neighbour.id)
        state.parents[neighbour.id] = (anchor_id, edge.id)
        next_frontier.append(neighbour.id)
        if neighbour.id == target_id:
            found = True
            break
    return next_frontier, found


async def _search_shortest_path(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    source: GraphNode,
    target: GraphNode,
    max_hops: int,
) -> tuple[_PathSearchState, int | None]:
    """Run the bidirectional BFS; return state + hops (``None`` if unreached)."""
    state = _PathSearchState(
        visited={source.id},
        parents={},
        node_by_id={source.id: source},
        edges_by_id={},
    )
    frontier: deque[uuid.UUID] = deque([source.id])
    for hop in range(1, max_hops + 1):
        if not frontier:
            break
        current = list(frontier)
        frontier.clear()
        rows = await _bidirectional_neighbours(
            db_session,
            tenant_id=tenant_id,
            frontier_ids=current,
        )
        next_frontier, found = _process_hop_rows(state, rows, target.id)
        if found:
            return state, hop
        frontier.extend(next_frontier)
    return state, None


def _build_path_result(
    state: _PathSearchState,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    hops: int,
) -> PathSubgraphResult:
    """Reconstruct the path from the BFS state into a :class:`PathSubgraphResult`."""
    node_path, edge_path = _reconstruct_path(state.parents, source_id, target_id)
    path_nodes = [
        SubgraphNodeRow(
            id=state.node_by_id[node_id].id,
            kind=state.node_by_id[node_id].kind,
            name=state.node_by_id[node_id].name,
        )
        for node_id in node_path
    ]
    path_edges = [
        SubgraphEdgeRow(
            id=state.edges_by_id[edge_id].id,
            kind=state.edges_by_id[edge_id].kind,
            source=state.edges_by_id[edge_id].source,
            from_id=state.edges_by_id[edge_id].from_node_id,
            to_id=state.edges_by_id[edge_id].to_node_id,
        )
        for edge_id in edge_path
    ]
    return PathSubgraphResult(
        path_node_ids=tuple(node_path),
        nodes=path_nodes,
        edges=path_edges,
        highlighted_edge_ids=frozenset(edge_path),
        total_hops=hops,
        truncated=False,
    )


async def fetch_path_subgraph(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    from_name: str,
    to_name: str,
    from_kind: str | None = None,
    to_kind: str | None = None,
    max_hops: int = DEFAULT_PATH_MAX_HOPS,
) -> PathSubgraphResult:
    """Return the shortest path subgraph from ``from_name`` to ``to_name``.

    Walks edges bidirectionally (treats the graph as undirected for
    reachability) and returns the **shortest** path by hop count, or
    a result with ``total_hops=None`` and empty highlighted set when
    the target is unreachable within ``max_hops``.

    The returned ``nodes`` / ``edges`` are the path itself only --
    the UI renders the highlighted-only view rather than embedding
    the path in a wider context (which would re-introduce the
    500-node hairball problem the overlay is meant to escape).

    Raises:
        meho_backplane.ui.routes.topology.queries.NodeNotFoundError:
            either endpoint does not resolve in this tenant.
        meho_backplane.ui.routes.topology.queries.AmbiguousNodeError:
            either endpoint is a bare name resolving to multiple kinds
            in the tenant.
    """
    source = await resolve_anchor(db_session, tenant_id=tenant_id, name=from_name, kind=from_kind)
    target = await resolve_anchor(db_session, tenant_id=tenant_id, name=to_name, kind=to_kind)

    # Same-node short-circuit: a zero-hop "path" to itself.
    if source.id == target.id:
        return PathSubgraphResult(
            path_node_ids=(source.id,),
            nodes=[SubgraphNodeRow(id=source.id, kind=source.kind, name=source.name)],
            edges=[],
            highlighted_edge_ids=frozenset(),
            total_hops=0,
            truncated=False,
        )

    state, hops = await _search_shortest_path(
        db_session,
        tenant_id=tenant_id,
        source=source,
        target=target,
        max_hops=max_hops,
    )
    if hops is None:
        return PathSubgraphResult(
            path_node_ids=(),
            nodes=[],
            edges=[],
            highlighted_edge_ids=frozenset(),
            total_hops=None,
            truncated=False,
        )
    return _build_path_result(state, source.id, target.id, hops)
