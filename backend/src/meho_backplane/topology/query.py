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
in another tenant is invisible to this tenant's traversal.

Anchor disambiguation
---------------------

``graph_node`` uniqueness is ``(tenant_id, kind, name)`` — a ``target``
named ``app`` and a ``vm`` named ``app`` legitimately coexist in one
tenant. Resolving a root by ``name`` alone would anchor on *both* and
silently traverse a merged closure of two unrelated objects. Every
entrypoint therefore accepts an optional ``kind``: when supplied the
anchor is pinned to ``(tenant_id, kind, name)`` (unambiguous by the
unique index); when omitted and the name resolves to more than one
kind in the tenant, the call raises :class:`AmbiguousNodeError` rather
than traversing the merged closure. ``find_path`` applies the same
contract independently to each endpoint via ``from_kind`` /
``to_kind``.

SQL parameter binding mirrors the established raw-SQL pattern in
:mod:`meho_backplane.retrieval.retriever`: every statement is a
fully-literal ``text("...")`` (nothing interpolated, so the SQLAlchemy
``avoid-sqlalchemy-text`` SAST rule does not fire) with ``:named``
binds, a ``CAST(:x AS text) IS NULL OR ...`` guard for the optional
``kind`` pin and ``kind_filter`` so one statement string serves the
pinned/unpinned and filtered/unfiltered cases, and UUIDs passed as
``str`` for the asyncpg text codec. The reverse and forward traversal
directions are two separate literal statements rather than one
f-string with the join columns swapped.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.topology.resolvers import (
    AmbiguousNodeError,
    NodeNotFoundError,
    _collect_distinct_kinds,
    resolve_node,
)
from meho_backplane.topology.schemas import (
    TopologyEdge,
    TopologyEdgeEndpoint,
    TopologyNode,
    TopologyPath,
)

# ``AmbiguousNodeError`` was historically defined in this module and is
# part of the G9.1 read-half public surface; Task #594 (G9.2-T2) moved
# the canonical definition into :mod:`meho_backplane.topology.resolvers`
# so the ambiguity rule is owned by the resolver (the surface that also
# carries :func:`resolve_node` / :class:`NodeNotFoundError`). The
# re-import preserves every pre-existing
# ``from meho_backplane.topology.query import AmbiguousNodeError`` call
# site without churn.
__all__ = [
    "AmbiguousNodeError",
    "find_dependencies",
    "find_dependents",
    "find_path",
    "list_edges",
]


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


# Reverse traversal (dependents, "what depends on me"): join edges
# *into* the frontier node and step to their source. The outer
# DISTINCT ON (id) collapses a node reached by several converging
# paths to a single row at its minimum depth — CYCLE only dedupes
# within one branch, not across converging branches of a DAG, so the
# closure-wide collapse is what makes the result one row per node.
# Two fully-literal statements (rather than one f-string) keep the
# Semgrep avoid-sqlalchemy-text rule from firing: nothing is
# interpolated, every value is a :named bind.
_TRAVERSAL_SQL_REVERSE = text(
    """
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
          AND (CAST(:kind AS text) IS NULL OR n.kind = :kind)
        UNION ALL
        SELECT
            n.id,
            n.kind,
            n.name,
            n.properties,
            w.depth + 1,
            e.kind
        FROM graph_edge e
        JOIN walk w ON e.to_node_id = w.id
        JOIN graph_node n ON n.id = e.from_node_id
        WHERE e.tenant_id = :tenant_id
          AND n.tenant_id = :tenant_id
          AND w.depth < :depth
          AND (CAST(:kind_filter AS text) IS NULL OR e.kind = :kind_filter)
    ) CYCLE id SET is_cycle USING path
    SELECT id, kind, name, properties, depth, via_edge_kind
    FROM (
        SELECT DISTINCT ON (id)
            id, kind, name, properties, depth, via_edge_kind
        FROM walk
        WHERE depth <= :depth
          AND NOT is_cycle
        ORDER BY id, depth, name
    ) deduped
    ORDER BY depth, name
    """
)

# Forward traversal (dependencies, "what I depend on"): the mirror —
# join edges *out of* the frontier node and step to their target. Only
# the two join columns differ from the reverse statement; everything
# else (tenant scoping, kind pin, kind filter, depth bound, CYCLE
# guard, closure-wide DISTINCT ON dedupe, ordering) is identical.
_TRAVERSAL_SQL_FORWARD = text(
    """
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
          AND (CAST(:kind AS text) IS NULL OR n.kind = :kind)
        UNION ALL
        SELECT
            n.id,
            n.kind,
            n.name,
            n.properties,
            w.depth + 1,
            e.kind
        FROM graph_edge e
        JOIN walk w ON e.from_node_id = w.id
        JOIN graph_node n ON n.id = e.to_node_id
        WHERE e.tenant_id = :tenant_id
          AND n.tenant_id = :tenant_id
          AND w.depth < :depth
          AND (CAST(:kind_filter AS text) IS NULL OR e.kind = :kind_filter)
    ) CYCLE id SET is_cycle USING path
    SELECT id, kind, name, properties, depth, via_edge_kind
    FROM (
        SELECT DISTINCT ON (id)
            id, kind, name, properties, depth, via_edge_kind
        FROM walk
        WHERE depth <= :depth
          AND NOT is_cycle
        ORDER BY id, depth, name
    ) deduped
    ORDER BY depth, name
    """
)


async def _assert_anchor_unambiguous(
    session: Any,
    *,
    tenant_id: str,
    name: str,
    kind: str | None,
) -> None:
    """Raise :class:`AmbiguousNodeError` if *name* needs a ``kind``.

    No-op when ``kind`` is supplied (the ``(tenant_id, kind, name)``
    unique index already makes the anchor unambiguous) or when the name
    resolves to at most one kind in the tenant. Only when ``kind`` is
    omitted *and* the name spans multiple kinds does the traversal
    refuse — anchoring on all of them would merge unrelated closures.

    Delegates the kind-collection query to
    :func:`meho_backplane.topology.resolvers._collect_distinct_kinds`
    so the ambiguity-probe SQL is single-sourced between the
    traversal verbs and :func:`resolve_node`. The not-found
    behavior intentionally stays unchanged here: a name that maps to
    zero kinds is a silent no-op for traversal (G9.1 contract — an
    empty result rather than an exception), only ``resolve_node``
    surfaces :class:`NodeNotFoundError`.
    """
    if kind is not None:
        return
    kinds = await _collect_distinct_kinds(session, tenant_id=tenant_id, name=name)
    if len(kinds) > 1:
        raise AmbiguousNodeError(name, kinds)


async def _traverse(
    operator: Operator,
    name_or_alias: str,
    *,
    depth: int,
    kind: str | None,
    kind_filter: str | None,
    reverse: bool,
) -> list[TopologyNode]:
    """Shared dependents/dependencies recursive-CTE traversal.

    Picks the reverse or forward literal statement and runs it
    tenant-scoped on its own session, mirroring the session-per-call
    shape of the memory / kb services. Resolves the anchor up front:
    an ambiguous bare-name root (multiple kinds, no ``kind`` pin)
    raises :class:`AmbiguousNodeError` rather than traversing a merged
    closure.
    """
    sql = _TRAVERSAL_SQL_REVERSE if reverse else _TRAVERSAL_SQL_FORWARD
    tenant_id = str(operator.tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await _assert_anchor_unambiguous(
            session, tenant_id=tenant_id, name=name_or_alias, kind=kind
        )
        result = await session.execute(
            sql,
            {
                "name": name_or_alias,
                "tenant_id": tenant_id,
                "kind": kind,
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
    kind: str | None = None,
    depth: int = _DEFAULT_DEPTH,
    kind_filter: str | None = None,
) -> list[TopologyNode]:
    """Reverse traversal: every node that depends on *name_or_alias*.

    Returns a flattened list ordered ``(depth, name)`` with **one row
    per reachable node** (a node reachable by several converging paths
    is collapsed to its minimum-depth occurrence): the root at depth 0,
    its immediate dependents at depth 1, transitive dependents at depth
    2, and so on, up to and including ``depth``.

    ``kind`` pins the anchor to ``(tenant_id, kind, name_or_alias)``,
    the unique index. Omit it only when the name is unique across kinds
    in the tenant — if it is not, the call raises
    :class:`AmbiguousNodeError` instead of merging unrelated closures.
    ``kind_filter`` restricts the walk to edges of that
    ``graph_edge.kind``. The tenant boundary is ``operator.tenant_id``
    — a same-named node in another tenant is never returned. Cycles
    terminate at the CYCLE clause.

    The root node itself is included (depth 0) so a caller can
    distinguish "node exists but has no dependents" (one-element list)
    from "node does not exist in this tenant" (empty list).
    """
    return await _traverse(
        operator,
        name_or_alias,
        depth=depth,
        kind=kind,
        kind_filter=kind_filter,
        reverse=True,
    )


async def find_dependencies(
    operator: Operator,
    name_or_alias: str,
    *,
    kind: str | None = None,
    depth: int = _DEFAULT_DEPTH,
    kind_filter: str | None = None,
) -> list[TopologyNode]:
    """Forward traversal: everything *name_or_alias* depends on.

    The mirror of :func:`find_dependents` — same shape, same one-row-
    per-node closure dedupe, same ``kind`` disambiguation contract,
    same tenant scoping, same cycle safety and depth bound — with edges
    walked in the opposite direction (out of the current node rather
    than into it). Root included at depth 0; empty list means the node
    does not exist in this tenant.
    """
    return await _traverse(
        operator,
        name_or_alias,
        depth=depth,
        kind=kind,
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
          AND (CAST(:from_kind AS text) IS NULL OR n.kind = :from_kind)
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
     AND (CAST(:to_kind AS text) IS NULL OR tn.kind = :to_kind)
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
    from_kind: str | None = None,
    to_kind: str | None = None,
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

    ``from_kind`` / ``to_kind`` pin each endpoint to its
    ``(tenant_id, kind, name)`` unique row. The same disambiguation
    contract as the traversal verbs applies independently to each
    endpoint: an unpinned name that resolves to multiple kinds in the
    tenant raises :class:`AmbiguousNodeError` rather than letting an
    unintended endpoint enter the search.

    A second resolving query materialises the node rows in path order
    so the :class:`TopologyPath` carries full :class:`TopologyNode`
    records, not bare ids.
    """
    tenant_id = str(operator.tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await _assert_anchor_unambiguous(
            session, tenant_id=tenant_id, name=from_name, kind=from_kind
        )
        await _assert_anchor_unambiguous(session, tenant_id=tenant_id, name=to_name, kind=to_kind)
        path_result = await session.execute(
            _PATH_SQL,
            {
                "tenant_id": tenant_id,
                "from_name": from_name,
                "to_name": to_name,
                "from_kind": from_kind,
                "to_kind": to_kind,
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


# Default page size for :func:`list_edges`. Picked to fit the typical
# inventory survey ("show me the curated edges in this tenant") into one
# screenful without the operator paging, but small enough that an
# unbounded list cannot exhaust the asyncpg connection's buffer on a
# large tenant. The route layer (T5) caps it tighter at the HTTP
# boundary; the service primitive enforces only the practical upper
# limit below.
_DEFAULT_EDGE_LIMIT = 200

#: Hard ceiling on the per-call edge page size. The route layer caps
#: tighter (Initiative #364 §4 specifies the HTTP cap); this is the
#: substrate-level guard that prevents a non-route caller (e.g. the MCP
#: front in T7, or a developer at the REPL) from accidentally asking for
#: an unbounded result. Bumping it requires a coordinated review with the
#: HTTP cap because the recursive-CTE indexes assume tenant-bounded
#: result sets.
_MAX_EDGE_LIMIT = 1000

# Flat edge listing with tenant scoping and composable filters. The
# statement joins both endpoint nodes so the result carries the
# human-readable ``from`` / ``to`` ``kind`` + ``name`` an operator
# survey needs without a second round trip. Pagination orders by
# ``last_seen DESC NULLS LAST, id`` — a strict total order (``id`` is
# the UUID primary key) so ``LIMIT`` / ``OFFSET`` partition the result
# set deterministically and a two-page sweep reassembles to the unpaged
# set with no gaps or duplicates.
#
# Soft-deleted edges (``e.last_seen IS NULL``) are excluded by default —
# same default as traversal, which filters them out implicitly via the
# ``last_seen`` column not appearing in its projection. Including them
# would surface stale relationships in an inventory view.
#
# The ``conflicts_only`` predicate guards against the
# ``jsonb_array_length`` non-array raise: the
# ``jsonb_typeof(...) = 'array'`` check short-circuits if the key is
# absent (``->`` yields SQL NULL → ``jsonb_typeof`` yields NULL → not
# equal to ``'array'``) or carries a non-array value (a stringly-typed
# write would land as ``'string'`` / ``'object'``). The
# ``jsonb_array_length`` call only runs on a confirmed array.
#
# Every optional filter uses the ``CAST(:x AS <type>) IS NULL OR ...``
# idiom the traversal verbs established so one literal statement serves
# every combination of filters without string interpolation — keeps the
# ``avoid-sqlalchemy-text`` SAST rule clean.
_LIST_EDGES_SQL = text(
    """
    SELECT
        e.id                AS id,
        e.kind              AS kind,
        e.source            AS source,
        e.properties        AS properties,
        e.last_seen         AS last_seen,
        f.id                AS from_id,
        f.kind              AS from_kind,
        f.name              AS from_name,
        t.id                AS to_id,
        t.kind              AS to_kind,
        t.name              AS to_name
    FROM graph_edge e
    JOIN graph_node f ON f.id = e.from_node_id
    JOIN graph_node t ON t.id = e.to_node_id
    WHERE e.tenant_id = :tenant_id
      AND e.last_seen IS NOT NULL
      AND (CAST(:kind AS text) IS NULL OR e.kind = :kind)
      AND (CAST(:source AS text) IS NULL OR e.source = :source)
      AND (CAST(:from_node_id AS uuid) IS NULL OR e.from_node_id = :from_node_id)
      AND (CAST(:to_node_id AS uuid) IS NULL OR e.to_node_id = :to_node_id)
      AND (
          NOT :conflicts_only
          OR (
              jsonb_typeof(e.properties -> 'conflicts_with') = 'array'
              AND jsonb_array_length(e.properties -> 'conflicts_with') > 0
          )
      )
    ORDER BY e.last_seen DESC NULLS LAST, e.id
    LIMIT :limit
    OFFSET :offset
    """
)


def _row_to_edge(row: Row[Any]) -> TopologyEdge:
    """Map one :data:`_LIST_EDGES_SQL` row to a :class:`TopologyEdge`.

    The row carries the edge's own columns plus the two endpoints'
    ``(id, kind, name)`` projected from the joins. ``properties``
    arrives as a ``dict`` from JSONB on asyncpg and is deep-frozen by
    the :class:`TopologyEdge` model validator.
    """
    m = row._mapping
    return TopologyEdge(
        id=m["id"],
        from_endpoint=TopologyEdgeEndpoint(
            id=m["from_id"], kind=m["from_kind"], name=m["from_name"]
        ),
        to_endpoint=TopologyEdgeEndpoint(id=m["to_id"], kind=m["to_kind"], name=m["to_name"]),
        kind=m["kind"],
        source=m["source"],
        properties=m["properties"] or {},
        last_seen=m["last_seen"],
    )


async def _resolve_ref_or_empty(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    name: str,
) -> UUID | None:
    """Resolve a ``from_ref`` / ``to_ref`` name to a ``graph_node.id``.

    Returns the node's ``id`` when the name resolves to exactly one
    row in the tenant. Returns the sentinel ``None`` for the
    "no node by this name" case so the caller can short-circuit to
    an empty result without running the main query — the acceptance
    criterion is that a ref pointing at nothing yields an empty list,
    not an error.

    Ambiguity (a bare name that resolves to multiple kinds in the
    tenant) propagates as :class:`AmbiguousNodeError` — the same
    contract :func:`find_dependents` / :func:`find_dependencies` /
    :func:`find_path` use. Callers that want to pin a kind issue the
    ref against the kind-qualified resolver directly (or, at the
    route / CLI layer, surface the kind disambiguation up to the
    operator).
    """
    try:
        node = await resolve_node(session, tenant_id, name)
    except NodeNotFoundError:
        return None
    return node.id


async def list_edges(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    kind: str | None = None,
    source: str | None = None,
    from_ref: str | None = None,
    to_ref: str | None = None,
    conflicts_only: bool = False,
    limit: int = _DEFAULT_EDGE_LIMIT,
    offset: int = 0,
) -> list[TopologyEdge]:
    """Tenant-scoped, filter-composable flat listing of ``graph_edge`` rows.

    Task #596 (G9.2-T4). The service primitive every front consuming a
    list of curated and auto edges goes through — the REST route
    ``GET /api/v1/topology/edges`` (T5), the CLI verb
    ``meho topology list-edges`` (T6), and the MCP
    ``query_topology(kind='edges')`` facet (T7) all call this helper
    rather than re-deriving the tenant boundary and the filter
    composition.

    Unlike the traversal verbs in this module — which take an
    :class:`Operator` and open their own session per call — this helper
    accepts the ``session`` and the ``tenant_id`` directly. That mirrors
    the shape of :func:`meho_backplane.topology.resolvers.resolve_node`
    and lets callers compose the listing inside a larger transactional
    boundary (e.g. the annotation flow asserting an edge exists, the
    MCP layer batching reads). The tenant boundary is enforced
    unconditionally against ``tenant_id`` in the SQL — no filter
    combination, including a crafted ``from_ref``/``to_ref``, can leak
    a row from another tenant.

    Args:
        session: An open :class:`AsyncSession`. Caller owns the
            transaction. The helper performs only read statements; no
            commits, no flushes.
        tenant_id: The tenant scope. Mandatory and non-optional — there
            is no "list every edge across tenants" mode by construction.
        kind: Optional :class:`~meho_backplane.db.models.GraphEdgeKind`
            filter (one of the ten v0.2 vocabulary values). The CHECK
            constraint already restricts the column; this filter
            narrows the listing.
        source: Optional ``graph_edge.source`` filter (``'auto'`` for
            probe-derived edges, ``'curated'`` for operator-asserted
            ones — see :class:`~meho_backplane.db.models.GraphEdge`).
        from_ref: Optional ``graph_node.name`` to restrict the listing
            to edges originating at this node. Resolved via
            :func:`resolve_node`; an ambiguous bare name (multiple
            kinds in the tenant) raises :class:`AmbiguousNodeError`
            (caller passes a kind-qualified ref to disambiguate at the
            front layer). A name that does not resolve to any node
            yields an empty result rather than an error — consistent
            with the traversal verbs' "missing anchor → empty list"
            shape.
        to_ref: Same contract as ``from_ref`` but restricting on the
            edge's destination node.
        conflicts_only: When ``True``, restrict the listing to edges
            whose ``properties.conflicts_with`` JSONB key is a
            non-empty array. This is the recoverability surface for a
            wrong annotation — G9.2-T3 (#595) writes these markers on
            edges that conflict with an annotation. When the marker
            surface lands, this filter immediately becomes useful
            without further changes here; until then the predicate is
            still safe (an absent key, a NULL value, or any non-array
            value yields ``false`` via the ``jsonb_typeof`` guard).
        limit: Maximum rows to return per call (1..``1000``). Defaults
            to ``200``. The route layer (T5) caps this further at the
            HTTP boundary.
        offset: Rows to skip before the first returned row, for
            pagination. Combined with the strict total order
            (``last_seen DESC NULLS LAST, id``), paging a result set is
            deterministic — a two-page sweep over an unchanging
            dataset reassembles to the unpaged result with no gaps or
            duplicates.

    Returns:
        A list of :class:`TopologyEdge` rows in
        ``(last_seen DESC NULLS LAST, id)`` order, capped at
        ``limit``. Soft-deleted edges (``last_seen IS NULL``) are
        excluded — an inventory view should not surface stale
        relationships. Empty list when no edge matches in the
        tenant.

    Raises:
        AmbiguousNodeError: ``from_ref`` or ``to_ref`` is a bare name
            that resolves to multiple kinds in the tenant. The caller
            re-issues with a kind-qualified ref through the front
            layer.
        ValueError: ``limit`` is out of range (``< 1`` or
            ``> _MAX_EDGE_LIMIT``) or ``offset`` is negative — caught
            client-side at the route layer too, but the substrate
            refuses defensively because a non-route caller (CLI/MCP/
            REPL) may not have the same validation.
    """
    if limit < 1 or limit > _MAX_EDGE_LIMIT:
        raise ValueError(f"limit must be in 1..{_MAX_EDGE_LIMIT}; got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be >= 0; got {offset}")

    from_node_id: UUID | None = None
    to_node_id: UUID | None = None
    if from_ref is not None:
        from_node_id = await _resolve_ref_or_empty(session, tenant_id=tenant_id, name=from_ref)
        if from_node_id is None:
            # The ref points at no row in this tenant — the listing is
            # empty by construction. Short-circuit to skip the main
            # query (and, importantly, avoid passing a NULL through the
            # ``e.from_node_id = :from_node_id`` filter, which the
            # ``CAST IS NULL OR ...`` guard would otherwise read as
            # "no filter" — a correctness bug that would broaden the
            # listing across the *entire* tenant when the operator
            # asked to scope it to one node).
            return []
    if to_ref is not None:
        to_node_id = await _resolve_ref_or_empty(session, tenant_id=tenant_id, name=to_ref)
        if to_node_id is None:
            return []

    result = await session.execute(
        _LIST_EDGES_SQL,
        {
            "tenant_id": str(tenant_id),
            "kind": kind,
            "source": source,
            "from_node_id": str(from_node_id) if from_node_id is not None else None,
            "to_node_id": str(to_node_id) if to_node_id is not None else None,
            "conflicts_only": conflicts_only,
            "limit": limit,
            "offset": offset,
        },
    )
    return [_row_to_edge(row) for row in result.fetchall()]
