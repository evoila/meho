# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Recursive-CTE topology traversal: dependents / dependencies / path.

Task #451 (G9.1-T4). The three verbs here are the read surface every
blast-radius check and topology question in v0.2 goes through. All
three use PostgreSQL's ``WITH RECURSIVE ... CYCLE`` clause (PG manual
§7.8.2.2; the chassis floor is PG 16, where the clause behaves
identically to the PG 17 reference) for cycle-safe graph traversal
over the adjacency-list ``graph_node`` / ``graph_edge`` tables that
migration ``0007`` (Task #448) ships.

Edge-direction model
--------------------

An edge ``from_node --kind--> to_node`` reads "``from_node`` depends on
``to_node``" (e.g. a ``vm`` ``runs-on`` a ``host``: the vm depends on
the host). The two directed verbs follow that convention:

* :func:`find_dependents` — *reverse* traversal. "What depends on me?"
  Starting at the root, repeatedly find every node that has an edge
  pointing *into* the current node (``edge.to_node_id == current``),
  stepping to ``edge.from_node_id``. Depth-1 results are immediate
  dependents, depth-2 transitive, and so on.
* :func:`find_dependencies` — *forward* traversal. "What do I depend
  on?" The mirror: follow edges *out of* the current node
  (``edge.from_node_id == current``) to ``edge.to_node_id``.

:func:`find_path` is an unweighted shortest-path search: every edge
costs one hop, direction-agnostic (it walks both ``from``→``to`` and
``to``→``from``), and returns the first path found at the minimum hop
count or ``None`` if the target is unreachable within ``max_hops``.

Cycle safety
------------

The ``CYCLE id SET is_cycle USING path`` clause makes PostgreSQL track
the set of node ids already on each traversal branch and stop
recursing into a node it has already visited on that branch, flagging
the repeat row with ``is_cycle = true``. The traversal therefore
terminates on a graph with a cycle (``A → B → A``) instead of
recursing forever. The post-filter keeps only ``NOT is_cycle`` rows so
the duplicate cycle-closing row is excluded from the result. A
``depth`` bound in the recursive term is an independent second guard:
even an acyclic but very deep / fan-out-heavy graph is capped at the
caller's ``depth`` so a pathological topology cannot exhaust the
server.

Tenant scoping
--------------

Every statement filters ``graph_node.tenant_id`` *and*
``graph_edge.tenant_id`` against ``operator.tenant_id`` in both the
anchor and the recursive term. A node with the same ``(kind, name)``
in another tenant is invisible to this tenant's traversal — the
``(tenant_id, kind, name)`` unique index makes the root lookup
unambiguous within the tenant.

SQL parameter binding mirrors the established raw-SQL pattern in
:mod:`meho_backplane.retrieval.retriever`: ``text()`` with ``:named``
binds, a ``CAST(:x AS text) IS NULL OR ...`` guard for the optional
``kind_filter`` so one statement string serves the filtered and
unfiltered cases, and UUIDs passed as ``str`` for the asyncpg text
codec.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Row

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.topology.schemas import TopologyNode, TopologyPath

__all__ = ["find_dependencies", "find_dependents", "find_path"]

#: Default traversal depth. Matches the Task #451 contract; deep enough
#: for real datacentre topologies (target → vm → host → datastore →
#: network rarely exceeds a handful of hops) while bounding a runaway.
_DEFAULT_DEPTH = 16

#: Default shortest-path hop ceiling. Smaller than the traversal depth
#: default because a *path* between two named nodes is a much shorter
#: structure than a full dependent/dependency closure.
_DEFAULT_MAX_HOPS = 8


def _row_to_node(row: Row[Any]) -> TopologyNode:
    """Map a traversal result row to a :class:`TopologyNode`.

    The recursive CTE projects exactly ``id, kind, name, properties,
    depth, via_edge_kind``; ``is_cycle`` / ``path`` are CYCLE-clause
    bookkeeping filtered out in SQL and never selected into the row.
    ``properties`` arrives as a ``dict`` from JSONB on asyncpg (or a
    JSON string on the rare passthrough); the model's validator
    freezes it.
    """
    mapping = row._mapping
    return TopologyNode(
        id=mapping["id"],
        kind=mapping["kind"],
        name=mapping["name"],
        properties=mapping["properties"] or {},
        depth=mapping["depth"],
        via_edge_kind=mapping["via_edge_kind"],
    )


def _traversal_sql(*, reverse: bool) -> Any:
    """Build the dependents/dependencies recursive-CTE statement.

    ``reverse=True`` walks edges *into* the frontier node
    (``e.to_node_id == w.id``, step to ``e.from_node_id``) — the
    dependents direction. ``reverse=False`` walks *out of* it
    (``e.from_node_id == w.id``, step to ``e.to_node_id``) — the
    dependencies direction. Only those two join columns differ; tenant
    scoping, the optional kind filter, the depth bound, the CYCLE
    guard, and the ``(depth, name)`` ordering are identical, so the
    statement is built once with the join columns swapped.

    The anchor row is the query root at depth 0, reached via no edge
    (``via_edge_kind`` NULL). The depth guard in the recursive term
    bounds an acyclic-but-deep graph; CYCLE stops true cycles; the
    final ``NOT is_cycle`` filter drops the cycle-closing rows.
    """
    if reverse:
        join_to_frontier = "e.to_node_id = w.id"
        step_to = "e.from_node_id"
    else:
        join_to_frontier = "e.from_node_id = w.id"
        step_to = "e.to_node_id"

    return text(
        f"""
        WITH RECURSIVE walk AS (
            SELECT
                n.id            AS id,
                n.kind          AS kind,
                n.name          AS name,
                n.properties    AS properties,
                0               AS depth,
                CAST(NULL AS text) AS via_edge_kind
            FROM graph_node n
            WHERE n.name = :name
              AND n.tenant_id = :tenant_id
            UNION ALL
            SELECT
                n.id,
                n.kind,
                n.name,
                n.properties,
                w.depth + 1,
                e.kind
            FROM graph_edge e
            JOIN walk w ON {join_to_frontier}
            JOIN graph_node n ON n.id = {step_to}
            WHERE e.tenant_id = :tenant_id
              AND n.tenant_id = :tenant_id
              AND w.depth < :depth
              AND (CAST(:kind_filter AS text) IS NULL OR e.kind = :kind_filter)
        ) CYCLE id SET is_cycle USING path
        SELECT id, kind, name, properties, depth, via_edge_kind
        FROM walk
        WHERE depth <= :depth
          AND NOT is_cycle
        ORDER BY depth, name
        """
    )


async def _traverse(
    operator: Operator,
    name_or_alias: str,
    *,
    depth: int,
    kind_filter: str | None,
    reverse: bool,
) -> list[TopologyNode]:
    """Shared dependents/dependencies recursive-CTE traversal.

    Delegates statement construction to :func:`_traversal_sql` and
    runs it tenant-scoped on its own session, mirroring the
    session-per-call shape of the memory / kb services.
    """
    sql = _traversal_sql(reverse=reverse)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            sql,
            {
                "name": name_or_alias,
                "tenant_id": str(operator.tenant_id),
                "depth": depth,
                "kind_filter": kind_filter,
            },
        )
        rows = result.fetchall()

    return [_row_to_node(row) for row in rows]


async def find_dependents(
    operator: Operator,
    name_or_alias: str,
    *,
    depth: int = _DEFAULT_DEPTH,
    kind_filter: str | None = None,
) -> list[TopologyNode]:
    """Reverse traversal: every node that depends on *name_or_alias*.

    Returns a flattened list ordered ``(depth, name)``: the root at
    depth 0, its immediate dependents at depth 1, transitive dependents
    at depth 2, and so on, up to and including ``depth``. ``kind_filter``
    restricts the walk to edges of that ``graph_edge.kind``. The tenant
    boundary is ``operator.tenant_id`` — a same-named node in another
    tenant is never returned. Cycles terminate at the CYCLE clause.

    The root node itself is included (depth 0) so a caller can
    distinguish "node exists but has no dependents" (one-element list)
    from "node does not exist in this tenant" (empty list).
    """
    return await _traverse(
        operator,
        name_or_alias,
        depth=depth,
        kind_filter=kind_filter,
        reverse=True,
    )


async def find_dependencies(
    operator: Operator,
    name_or_alias: str,
    *,
    depth: int = _DEFAULT_DEPTH,
    kind_filter: str | None = None,
) -> list[TopologyNode]:
    """Forward traversal: everything *name_or_alias* depends on.

    The mirror of :func:`find_dependents` — same shape, same tenant
    scoping, same cycle safety and depth bound — with edges walked in
    the opposite direction (out of the current node rather than into
    it). Root included at depth 0; empty list means the node does not
    exist in this tenant.
    """
    return await _traverse(
        operator,
        name_or_alias,
        depth=depth,
        kind_filter=kind_filter,
        reverse=False,
    )


# Bidirectional shortest-path recursive CTE. The ``bi_edge`` CTE is the
# union of forward and reversed edges (tenant-scoped) so the walk treats
# the graph as undirected for reachability while storage stays directed.
# The ``node_ids`` / ``edge_kinds`` arrays accumulate the ordered route
# so the winning row already describes the whole path; ``visited`` is the
# CYCLE bookkeeping set (independent of ``node_ids``). The hop bound plus
# the CYCLE guard terminate the search on cyclic graphs; ``ORDER BY hops
# LIMIT 1`` yields a shortest path.
_PATH_SQL = text(
    """
    WITH RECURSIVE
    bi_edge AS (
        SELECT from_node_id AS src, to_node_id AS dst, kind
        FROM graph_edge
        WHERE tenant_id = :tenant_id
        UNION ALL
        SELECT to_node_id AS src, from_node_id AS dst, kind
        FROM graph_edge
        WHERE tenant_id = :tenant_id
    ),
    walk AS (
        SELECT
            n.id                                   AS node_id,
            0                                      AS hops,
            ARRAY[n.id]                            AS node_ids,
            CAST(ARRAY[] AS text[])                AS edge_kinds
        FROM graph_node n
        WHERE n.name = :from_name
          AND n.tenant_id = :tenant_id
        UNION ALL
        SELECT
            be.dst,
            w.hops + 1,
            w.node_ids || be.dst,
            w.edge_kinds || be.kind
        FROM bi_edge be
        JOIN walk w ON be.src = w.node_id
        WHERE w.hops < :max_hops
    ) CYCLE node_id SET is_cycle USING visited
    SELECT w.hops, w.node_ids, w.edge_kinds
    FROM walk w
    JOIN graph_node tn
      ON tn.id = w.node_id
     AND tn.name = :to_name
     AND tn.tenant_id = :tenant_id
    WHERE NOT w.is_cycle
    ORDER BY w.hops
    LIMIT 1
    """
)

# Materialise the full node rows for a winning path id list. The CYCLE
# guard already excluded repeats so the id list has no duplicates within
# a single path; the caller re-orders the fetched rows back into path
# sequence.
_PATH_NODES_SQL = text(
    """
    SELECT id, kind, name, properties
    FROM graph_node
    WHERE tenant_id = :tenant_id
      AND id = ANY(:node_ids)
    """
)


def _build_path_nodes(
    node_ids: list[UUID],
    edge_kinds: list[str],
    by_id: dict[UUID, Any],
) -> tuple[TopologyNode, ...]:
    """Assemble the ordered :class:`TopologyNode` tuple for a path.

    ``node_ids`` is the path sequence; ``by_id`` maps each id to its
    fetched node mapping. ``via_edge_kind`` is ``None`` for the root
    (position 0) and the kind of the ``(position-1)``-th hop otherwise.
    """
    nodes: list[TopologyNode] = []
    for position, node_id in enumerate(node_ids):
        m = by_id[node_id]
        nodes.append(
            TopologyNode(
                id=m["id"],
                kind=m["kind"],
                name=m["name"],
                properties=m["properties"] or {},
                depth=position,
                via_edge_kind=None if position == 0 else edge_kinds[position - 1],
            )
        )
    return tuple(nodes)


async def find_path(
    operator: Operator,
    from_name: str,
    to_name: str,
    *,
    max_hops: int = _DEFAULT_MAX_HOPS,
) -> TopologyPath | None:
    """Shortest unweighted path from *from_name* to *to_name*.

    Walks edges in both directions (``from``→``to`` and ``to``→``from``)
    so the path follows the graph's connectivity rather than only its
    edge orientation — "is there *any* route between these two
    objects" is the operator question this answers. Every edge costs
    one hop (v0.2 is unweighted). Returns the first path found at the
    minimum hop count, or ``None`` if *to_name* is unreachable from
    *from_name* within ``max_hops`` (or either endpoint does not exist
    in this tenant).

    A second resolving query materialises the node rows in path order
    so the :class:`TopologyPath` carries full :class:`TopologyNode`
    records, not bare ids.
    """
    tenant_id = str(operator.tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        path_result = await session.execute(
            _PATH_SQL,
            {
                "tenant_id": tenant_id,
                "from_name": from_name,
                "to_name": to_name,
                "max_hops": max_hops,
            },
        )
        winner = path_result.first()
        if winner is None:
            return None

        node_ids: list[UUID] = list(winner._mapping["node_ids"])
        edge_kinds: list[str] = list(winner._mapping["edge_kinds"])
        total_hops: int = winner._mapping["hops"]

        node_result = await session.execute(
            _PATH_NODES_SQL,
            {"tenant_id": tenant_id, "node_ids": node_ids},
        )
        by_id = {r._mapping["id"]: r._mapping for r in node_result.fetchall()}

    nodes = _build_path_nodes(node_ids, edge_kinds, by_id)
    return TopologyPath(nodes=nodes, total_hops=total_hops)
