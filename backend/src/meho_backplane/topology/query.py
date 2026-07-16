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

Superseded-edge exclusion (Initiative #364 §6, Task #595)
---------------------------------------------------------

The recursive term of every traversal verb filters
``graph_edge.properties->>'superseded_by' IS NULL`` — auto edges that
an operator's curated annotation has marked superseded drop out of
every closure. The mark is sticky across refresh (preserved by
:func:`meho_backplane.topology.refresh._reconcile_edges`) and only
cleared by :func:`meho_backplane.topology.annotate.unannotate_edge` of
the curated row. The guard fires on all four edge-pulling sites: the
forward and reverse recursive terms here, and **both legs** of the
``bi_edge`` CTE in :data:`_PATH_SQL` (missing the reversed leg would
let a superseded edge be walked backwards into a shortest path).

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

Untracked-anchor handling (G0.18-T4 #1357)
------------------------------------------

Pre-G0.18-T4, an anchor with no matching :class:`GraphNode` in the
tenant returned ``[]`` from :func:`find_dependents` /
:func:`find_dependencies`. Auto-discovery is k8s-only — every
non-k8s connector inherits the no-op
:meth:`~meho_backplane.connectors.base.Connector.discover_topology`
from the ABC — so every registered ``vault`` / ``vcenter`` / ``nsx``
/ ``sddc-manager`` / ``gh`` target falls into that hole and reads as
"nothing depends on me," which the pre-destructive blast-radius use
case mis-reads as "safe to delete" (RDC #789 N2 false-negative).

The closure verbs now resolve the anchor via
:func:`~meho_backplane.topology.resolvers.resolve_node` before
executing the CTE; an untracked anchor surfaces as
:class:`NodeNotFoundError` and an empty return list is structurally
impossible. A tracked node with no dependents still returns the
one-element ``[root]`` (the CTE's depth-0 anchor row).
:func:`find_path` keeps its G9.1 silent-on-miss contract — an
unreachable / missing endpoint reads as ``None`` and the verb's
*null* is the recoverable "no route" answer, distinct from "anchor
doesn't exist."

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

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy import DateTime, bindparam, text
from sqlalchemy import Uuid as SAUuid
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
    TopologyDiffEntry,
    TopologyDiffResult,
    TopologyEdge,
    TopologyEdgeEndpoint,
    TopologyHistoryEntry,
    TopologyHistoryResult,
    TopologyNode,
    TopologyPath,
    TopologyTimelineEntry,
    TopologyTimelineResult,
)
from meho_backplane.topology.timeline_cursor import (
    TimelineCursorPosition,
    decode_timeline_cursor,
    encode_timeline_cursor,
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
    "TopologyNodeListEntry",
    "find_dependencies",
    "find_dependents",
    "find_path",
    "list_edges",
    "list_nodes",
    "query_diff",
    "query_history",
    "query_timeline",
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
#
# Traversal exclusion (§6 of Initiative #364, Task #595): the recursive
# term filters ``e.properties->>'superseded_by' IS NULL`` so an auto
# edge an operator's curated annotation has marked superseded is
# invisible to the closure. ``->>`` is PG's text-extract JSON operator
# (PG 16 manual §9.16); the column is JSONB on PG (and JSON on the
# unit-test SQLite engine, which never runs this CTE — recursive CYCLE
# is PG-only, per the docstring above). A row with no
# ``superseded_by`` key reads ``NULL`` from ``->>`` and passes the
# filter, so the guard does not affect non-superseded edges.
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
          AND e.properties->>'superseded_by' IS NULL
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
# guard, closure-wide DISTINCT ON dedupe, ordering, §6 superseded-edge
# exclusion) is identical.
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
          AND e.properties->>'superseded_by' IS NULL
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

    * an ambiguous bare-name root (multiple kinds, no ``kind`` pin)
      raises :class:`AmbiguousNodeError` rather than traversing a merged
      closure;
    * an anchor that does not exist in this tenant raises
      :class:`NodeNotFoundError` so the caller can distinguish
      **untracked** (no anchor in the graph — auto-discovery is k8s-only
      today, so every registered non-k8s target falls here) from
      **tracked with no dependents / dependencies** (the anchor exists,
      the closure is just the one-element root). Bare ``[]`` was the
      pre-G0.18-T4 behaviour and conflated the two; the conflation
      reads as "safe to delete" for the pre-destructive blast-radius
      use case and is the RDC #789 N2 false-negative this resolution
      closes.
    """
    sql = _TRAVERSAL_SQL_REVERSE if reverse else _TRAVERSAL_SQL_FORWARD
    tenant_id = str(operator.tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # G0.18-T4 (#1357): resolve the anchor up front. The two-step
        # path — :func:`resolve_node` then the recursive CTE — is the
        # planned migration the G9.2 resolver was designed for (see
        # :mod:`meho_backplane.topology.resolvers` module docstring's
        # "Not-found semantics intentionally differ" carve-out, now
        # reconciled for the closure verbs). ``resolve_node`` raises
        # :class:`NodeNotFoundError` on miss and
        # :class:`AmbiguousNodeError` on multi-kind, so the previous
        # ``_assert_anchor_unambiguous`` probe is subsumed — one
        # session round-trip covers both checks. The kind pin
        # (``kind=`` argument) flows through verbatim. ``resolve_node``
        # also returns the canonical ``GraphNode.kind``; we pass it
        # to the CTE so the traversal anchors on the exact row the
        # resolver picked (avoiding a redundant ``CAST(:kind AS text)
        # IS NULL OR n.kind = :kind`` re-evaluation that would in
        # principle traverse a different row when, say, an
        # alias-resolution layer lands on top of this code path).
        anchor = await resolve_node(session, operator.tenant_id, name_or_alias, kind)
        result = await session.execute(
            sql,
            {
                "name": name_or_alias,
                "tenant_id": tenant_id,
                "kind": anchor.kind,
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

    The root node itself is included (depth 0). G0.18-T4 (#1357,
    RDC #789 N2) changed the not-found contract: an anchor name with
    no matching :class:`~meho_backplane.db.models.GraphNode` in the
    tenant raises :class:`NodeNotFoundError` rather than returning
    ``[]``. **An empty return is now structurally impossible** — the
    minimum response is the one-element list ``[root]`` for a tracked
    node with no dependents. Callers distinguish the two cases by the
    exception:

    * one-element list → **tracked, no dependents** (safe to delete
      from a blast-radius perspective).
    * :class:`NodeNotFoundError` → **untracked** (the node is not in
      the topology graph; for registered non-k8s targets this is the
      expected state today because only the KubernetesConnector
      overrides
      :meth:`~meho_backplane.connectors.base.Connector.discover_topology`).
      *Not* equivalent to "safe to delete" — the call has produced no
      blast-radius signal.
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
    than into it). Root included at depth 0; an untracked anchor
    raises :class:`NodeNotFoundError` (G0.18-T4 #1357 — see
    :func:`find_dependents` for the full contract). An empty return
    list is structurally impossible.
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
#
# Per-branch target pruning (#2535). The non-recursive ``target`` CTE
# resolves the destination id once; the recursive term refuses to extend
# a branch whose frontier row already *is* the target (``NOT EXISTS``
# against ``target``). A branch that reaches the target stops growing —
# extending past a hit can never shorten a path to that same hit, and
# the CYCLE guard already forbids re-entering the target on the same
# branch, so pruning removes only rows the final select could never
# pick. On a dense mesh (branch factor b) the unpruned walk enumerates
# ~b^hops simple paths regardless of where the target sits; the pruned
# walk bounds every branch at its first hit. Results are byte-identical:
# same shortest path, same ``None`` on unreachable. Only *per-branch*
# pruning is expressible — PostgreSQL restricts the recursive
# self-reference (one reference, not in a subquery), so a global
# "some branch already found the target at h hops" cross-branch stop
# cannot be written, and ``ORDER BY hops LIMIT 1`` cannot terminate
# evaluation early because the sort must consume the full walk (PG 16
# §7.8 — lazy CTE evaluation does not apply once the parent sorts).
# Referencing the *non-recursive* ``target`` CTE inside the recursive
# term's subquery is legal; the restriction covers only ``walk`` itself.
# The unreachable-target worst case still enumerates the whole
# ≤max_hops ball — that envelope is pinned by the dense-mesh benchmark
# in ``tests/integration/test_topology_path_pruning.py``.
_PATH_SQL = text(
    """
    WITH RECURSIVE
    target AS (
        SELECT id
        FROM graph_node
        WHERE name = :to_name
          AND tenant_id = :tenant_id
          AND (CAST(:to_kind AS text) IS NULL OR kind = :to_kind)
    ),
    bi_edge AS (
        SELECT from_node_id AS src, to_node_id AS dst, kind
        FROM graph_edge
        WHERE tenant_id = :tenant_id
          AND properties->>'superseded_by' IS NULL
        UNION ALL
        SELECT to_node_id AS src, from_node_id AS dst, kind
        FROM graph_edge
        WHERE tenant_id = :tenant_id
          AND properties->>'superseded_by' IS NULL
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
          AND NOT EXISTS (SELECT 1 FROM target t WHERE t.id = w.node_id)
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
# Soft-deleted edges (``e.last_seen IS NULL``) are excluded from this
# listing by the ``e.last_seen IS NOT NULL`` predicate below — surfacing
# them would clutter an inventory view with stale relationships. This is
# the *opposite* of the traversal verbs (``find_dependents`` /
# ``find_dependencies`` / ``find_path``), which do not filter
# ``last_seen`` at all, so a soft-deleted node stays reachable
# (last-refresh-wins); see ``docs/architecture/topology.md``
# §Soft-delete semantics.
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
        kind: Optional exact-match ``graph_edge.kind`` filter (any
            kind slug; the vocabulary is open per T1 #2534, with
            :class:`~meho_backplane.db.models.GraphEdgeKind` as the
            well-known set). This filter narrows the listing.
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


# ---------------------------------------------------------------------------
# G10.5-T1 (#880) — tenant-scoped node listing for the UI tabular surface
# ---------------------------------------------------------------------------
#
# Tabular ``/ui/topology?view=table`` (Initiative #342 work-item #1)
# needs a paginated, sortable, filterable list of nodes plus an inbound
# edge count per row so the operator can scan the tenant inventory at
# a glance. The traversal verbs above answer "what depends on X" — they
# do not produce a per-tenant survey.
#
# Shape decisions:
#
#   * One frozen dataclass (:class:`TopologyNodeListEntry`) per row,
#     carrying the columns the UI table renders: ``id``, ``kind``,
#     ``name``, ``last_seen``, ``children_count``, ``parents``. The
#     ``parents`` list projects the immediate ``runs-on`` /
#     ``part-of`` / curated edges out of the node (the v0.2 "parent"
#     semantic surface; deeper ancestry stays in the graph view).
#   * ORM ``select`` against :class:`GraphNode` rather than a raw
#     ``text("...")`` so the helper is dialect-portable: the unit-test
#     suite runs on SQLite (no ``jsonb_typeof`` / ``jsonb_array_length``),
#     while PG production gets the same query. The ``children_count``
#     correlated subquery counts inbound edges
#     (``graph_edge.to_node_id = graph_node.id``) — the same shape
#     :func:`find_dependents` walks.
#   * Sort + filter are tightly enumerated: ``sort`` is one of a
#     fixed set so a hostile caller cannot inject an arbitrary
#     ``ORDER BY``; ``kind`` / ``name_contains`` filters compose
#     additively. ``name_contains`` uses SQLAlchemy's ``ilike`` so
#     the substring match is case-insensitive on both dialects.
#   * Tenant scoping is unconditional: every statement starts with
#     ``WHERE graph_node.tenant_id = :tenant_id``. No filter
#     combination can leak a row from another tenant.

#: Default page size for :func:`list_nodes`. The UI table renders the
#: full page in one HTMX swap; 200 is large enough for the typical
#: ~30-150-node tenant the v0.2 onboarding targets while bounding a
#: pathological discovery. The route layer caps tighter at the HTTP
#: boundary.
_DEFAULT_NODE_LIMIT = 200

#: Hard ceiling on the per-call node-listing page size. Mirrors
#: :data:`_MAX_EDGE_LIMIT` so the two inventory verbs share the same
#: operator ergonomics.
_MAX_NODE_LIMIT = 1000

#: Closed enum of sort columns the UI table exposes. The route layer
#: surfaces this as an enum query param so an out-of-range value 422s
#: at the boundary rather than reaching the SQL layer. Pinned as a
#: tuple so a typo at a call site is a static type error rather than a
#: silent SQL injection vector.
_NODE_SORT_COLUMNS: tuple[str, ...] = ("name", "kind", "last_seen", "first_seen")


class TopologyNodeListEntry:
    """One row of the per-tenant node inventory the UI table renders.

    Lightweight frozen dataclass-like shape; not a Pydantic model
    because this helper is a UI-internal projection (consumed only by
    the ``/ui/topology`` routes), not part of the public REST surface
    every external client validates against. The HTTP-visible Pydantic
    shapes stay in :mod:`meho_backplane.topology.schemas`.

    Fields:

    * ``id`` — ``graph_node.id`` UUID.
    * ``kind`` — ``graph_node.kind``.
    * ``name`` — ``graph_node.name``.
    * ``target_id`` — ``graph_node.target_id`` (NULL for inner
      graph nodes; populated for nodes that are themselves a
      registered target). Drives the "recent ops" filter on the
      detail drawer — only target-backed nodes carry audit rows.
    * ``first_seen`` / ``last_seen`` — observation timestamps.
      ``last_seen`` is NULL after a refresh soft-delete; the helper
      excludes such rows by default.
    * ``discovered_by`` — the connector slug or ``curated`` marker.
    * ``children_count`` — number of inbound non-soft-deleted edges
      (``graph_edge.to_node_id = node.id AND last_seen IS NOT NULL``).
      The same count :func:`find_dependents` walks. ``0`` for a leaf.
    """

    __slots__ = (
        "children_count",
        "discovered_by",
        "first_seen",
        "id",
        "kind",
        "last_seen",
        "name",
        "target_id",
    )

    def __init__(
        self,
        *,
        id: UUID,  # noqa: A002 — column name; the attribute mirrors ``graph_node.id``
        kind: str,
        name: str,
        target_id: UUID | None,
        first_seen: datetime,
        last_seen: datetime | None,
        discovered_by: str,
        children_count: int,
    ) -> None:
        self.id = id
        self.kind = kind
        self.name = name
        self.target_id = target_id
        self.first_seen = first_seen
        self.last_seen = last_seen
        self.discovered_by = discovered_by
        self.children_count = children_count


async def list_nodes(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    kind: str | None = None,
    name_contains: str | None = None,
    sort: str = "name",
    direction: str = "asc",
    include_soft_deleted: bool = False,
    limit: int = _DEFAULT_NODE_LIMIT,
    offset: int = 0,
) -> list[TopologyNodeListEntry]:
    """Tenant-scoped paginated listing of ``graph_node`` rows.

    Task #880 (G10.5-T1). The substrate primitive the UI tabular surface
    (``GET /ui/topology?view=table``) calls; the helper is intentionally
    UI-leaning (richer per-row projection than the traversal verbs'
    :class:`TopologyNode`) but stays in the substrate module so the CLI
    and MCP layers can reuse it later without duplicating the SQL.

    Args:
        session: An open :class:`AsyncSession`. Caller owns the
            transaction. The helper performs only read statements.
        tenant_id: The tenant scope. Mandatory; no "list every node
            across tenants" mode by construction. The first SQL WHERE
            clause is always ``graph_node.tenant_id = :tenant_id``;
            no filter combination overrides it.
        kind: Optional exact-match ``graph_node.kind`` filter (any
            kind slug; the vocabulary is open per T1 #2534, with
            :data:`meho_backplane.db.models.WELL_KNOWN_NODE_KINDS` as
            the well-known set). This narrows the listing.
        name_contains: Optional case-insensitive substring filter on
            ``graph_node.name``. Uses SQLAlchemy ``ilike`` with the
            user-controllable input wrapped in ``%`` on both sides;
            the binding parameter pattern means the substring is
            treated literally (no glob), and ``%`` / ``_`` chars in
            the operator's input match themselves.
        sort: Column to sort by. Must be one of
            :data:`_NODE_SORT_COLUMNS`; any other value raises
            :class:`ValueError`. The closed enum is the
            SQL-injection guard — the route layer validates the
            same set at the HTTP boundary, but the substrate refuses
            defensively because a non-route caller (CLI/MCP/REPL)
            may not have the same validation.
        direction: ``"asc"`` or ``"desc"``. Any other value raises
            :class:`ValueError`.
        include_soft_deleted: When ``False`` (the default), exclude
            rows with ``last_seen IS NULL`` — the soft-delete signal
            refresh writes on a node that disappeared from probe
            output. Set ``True`` for the audit / history surface
            (G9.3) that needs to see deleted rows; the UI table
            never sets it.
        limit: Maximum rows per call (1..``_MAX_NODE_LIMIT``).
        offset: Rows to skip; combined with the stable
            ``(<sort>, id)`` total order, paging is deterministic.

    Returns:
        A list of :class:`TopologyNodeListEntry` rows in the requested
        ``(sort, direction)`` order plus a stable ``id`` tie-breaker.
        Empty when no node matches.

    Raises:
        ValueError: ``sort`` is not in :data:`_NODE_SORT_COLUMNS`,
            ``direction`` is not in ``{"asc", "desc"}``, ``limit``
            is out of range, or ``offset`` is negative.
    """
    if sort not in _NODE_SORT_COLUMNS:
        raise ValueError(
            f"sort must be one of {_NODE_SORT_COLUMNS}; got {sort!r}",
        )
    if direction not in ("asc", "desc"):
        raise ValueError(f"direction must be 'asc' or 'desc'; got {direction!r}")
    if limit < 1 or limit > _MAX_NODE_LIMIT:
        raise ValueError(f"limit must be in 1..{_MAX_NODE_LIMIT}; got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be >= 0; got {offset}")

    # Local import: keeping the heavy ORM imports under the helper
    # rather than at module top avoids a circular-import risk with the
    # broader topology package and matches the existing per-helper
    # localisation pattern in this module.
    from sqlalchemy import asc, desc, func, select

    from meho_backplane.db.models import GraphEdge, GraphNode

    sort_column = {
        "name": GraphNode.name,
        "kind": GraphNode.kind,
        "last_seen": GraphNode.last_seen,
        "first_seen": GraphNode.first_seen,
    }[sort]
    order_clause = asc(sort_column) if direction == "asc" else desc(sort_column)

    children_count_subq = (
        select(func.count(GraphEdge.id))
        .where(
            GraphEdge.tenant_id == tenant_id,
            GraphEdge.to_node_id == GraphNode.id,
            GraphEdge.last_seen.is_not(None),
        )
        .correlate(GraphNode)
        .scalar_subquery()
    )

    stmt = select(
        GraphNode.id,
        GraphNode.kind,
        GraphNode.name,
        GraphNode.target_id,
        GraphNode.first_seen,
        GraphNode.last_seen,
        GraphNode.discovered_by,
        children_count_subq.label("children_count"),
    ).where(GraphNode.tenant_id == tenant_id)

    if not include_soft_deleted:
        stmt = stmt.where(GraphNode.last_seen.is_not(None))
    if kind is not None:
        stmt = stmt.where(GraphNode.kind == kind)
    if name_contains is not None and name_contains:
        # ``ilike`` is case-insensitive on PG; on SQLite the codec
        # collapses to ``LIKE`` with a case-sensitive match unless
        # the connection is configured with ``case_sensitive_like=0``.
        # The unit-test fixture uses the default (insensitive
        # ASCII), so the substring match behaves identically on both
        # dialects for ASCII inputs.
        stmt = stmt.where(GraphNode.name.ilike(f"%{name_contains}%"))

    # Compose a stable secondary sort by ``id`` so paging is
    # deterministic even when the primary sort column has duplicate
    # values (e.g. two nodes share a ``last_seen`` timestamp).
    stmt = stmt.order_by(order_clause, asc(GraphNode.id)).limit(limit).offset(offset)

    result = await session.execute(stmt)
    return [
        TopologyNodeListEntry(
            id=row.id,
            kind=row.kind,
            name=row.name,
            target_id=row.target_id,
            first_seen=row.first_seen,
            last_seen=row.last_seen,
            discovered_by=row.discovered_by,
            children_count=row.children_count,
        )
        for row in result.all()
    ]


# ---------------------------------------------------------------------------
# G9.3-T5 (#861) — tenant-wide timeline of graph changes
# ---------------------------------------------------------------------------
#
# The timeline UNIONs ``graph_node_history`` + ``graph_edge_history``
# and walks them in ``(valid_from DESC, history_id DESC)`` order. The
# ``source`` discriminator is the third sort key because the two
# tables have independent ``BIGSERIAL`` counters -- a node and an
# edge can share ``(valid_from, history_id)``.
#
# Indexes the timeline walks (declared by migration 0012):
#
#   * ``graph_node_history_tenant_valid_from_idx`` (composite b-tree
#     on ``(tenant_id, valid_from DESC)``)
#   * ``graph_edge_history_tenant_valid_from_idx`` (mirror for edges)
#
# Both are tenant-scoped composite indexes so the per-tenant slice is
# sub-millisecond on the test fixture and indexed under realistic
# load.
#
# Cursor stability under concurrent inserts: every history row from
# a single transaction shares the same ``valid_from`` (the diff-on-
# write hook in :mod:`.history` enforces this). The keyset compare
# ``(valid_from, history_id, source) < cursor`` is therefore
# correctness-preserving against any T2 hook write that lands
# between page N and page N+1 -- a new row either appears on a later
# page (if it lands below the cursor) or never (if above), and no
# row is duplicated or skipped.

#: Default timeline page size per the Task #861 acceptance criterion
#: ("default ``--limit 50``"). The substrate clamps to 1..1000 to
#: cap a hostile / misconfigured caller; the route / CLI layers cap
#: tighter where appropriate.
_DEFAULT_TIMELINE_LIMIT = 50

#: Hard ceiling on the per-call timeline page size. Mirrors the
#: G8.1 audit-query limit ceiling so an operator paging through
#: history at maximum width gets the same one-page-per-call ergonomics
#: across both surfaces.
_MAX_TIMELINE_LIMIT = 1000


def _format_node_summary(change_kind: str, snapshot: dict[str, Any] | None) -> str:
    """Render a one-line node-mutation summary from a snapshot.

    Picks the post-state for ``created`` / ``updated`` (the row as it
    exists after the mutation; more useful for "what's new" surveys)
    and the pre-state for ``removed`` (the row that just went away).
    Falls back to ``"<change_kind> node"`` when the snapshot is
    missing or malformed -- a tombstone whose ``ON DELETE SET NULL``
    cleared its ``node_id`` may still have a snapshot, but a legacy
    row from a pre-hook era might not.
    """
    if snapshot is None:
        return f"{change_kind} node"
    side = snapshot.get("before") if change_kind == "removed" else snapshot.get("after")
    if not isinstance(side, dict):
        return f"{change_kind} node"
    kind = side.get("kind") or "node"
    name = side.get("name") or "<unknown>"
    return f"{change_kind} {kind} {name}"


def _format_edge_summary(change_kind: str, snapshot: dict[str, Any] | None) -> str:
    """Render a one-line edge-mutation summary from a snapshot.

    Mirror of :func:`_format_node_summary` for the edge side.
    Endpoint names are not in the edge snapshot directly (the edge
    rows carry FK ids, not names) -- the renderer falls back to the
    ``kind`` field plus the change kind. Operators wanting the full
    endpoint detail use ``--json`` and look up the FKs themselves;
    the table view is a "what's new in the graph" survey, not a full
    reconstruction.
    """
    if snapshot is None:
        return f"{change_kind} edge"
    side = snapshot.get("before") if change_kind == "removed" else snapshot.get("after")
    if not isinstance(side, dict):
        return f"{change_kind} edge"
    edge_kind = side.get("kind") or "edge"
    return f"{change_kind} {edge_kind}"


# Two literal SQL statements -- one per history table -- joined by a
# Python-side merge rather than a SQL ``UNION ALL``. Two reasons:
#
#   1. Each statement is a single-index scan over its respective
#      ``graph_*_history_tenant_valid_from_idx``. A ``UNION ALL``
#      with an outer ``ORDER BY`` works but most PG planners
#      materialise the union before sorting, defeating the
#      tenant-scoped index. Two parallel single-index scans then a
#      Python merge is O(limit) memory and one round trip per table.
#
#   2. The ``--target`` filter joins ``graph_node`` (for the node
#      side) and ``graph_edge`` + endpoint ``graph_node`` (for the
#      edge side). Inlining those joins into a UNION'd statement
#      would make the planner choose between two different access
#      paths, which is precisely the kind of decision that flips
#      under data growth. Two statements keep each plan obvious.
# Cursor compare nuances across the two tables
# --------------------------------------------
#
# The DESC global ordering is ``(valid_from, history_id, source)``
# with ``"node"`` placed before ``"edge"`` (we pick ``"node" > "edge"``
# in DESC; i.e. ``"edge"`` < ``"node"`` ASC). When the cursor names a
# node row, the next-page boundary is "strictly after this node row
# in the DESC order" -- which means:
#
#   * Node-table candidates with the same ``(valid_from,
#     history_id)`` as the cursor are excluded (history_id is unique
#     within the node table, so the only same-key candidate IS the
#     cursor row).
#   * Edge-table candidates with the same ``(valid_from,
#     history_id)`` as the cursor are included (edge sorts after
#     node in DESC, so they fall *after* the cursor).
#
# When the cursor names an edge row, the next-page boundary is
# "strictly after this edge row":
#
#   * Both tables exclude rows at exactly ``(valid_from,
#     history_id)`` because edge already comes after node at the
#     same key; nothing after an edge row at the same key.
#
# The per-side SQL therefore receives ``cursor_src`` and applies the
# inclusive-vs-exclusive choice at the same-key boundary. Below the
# boundary (``(valid_from, history_id) <`` cursor), every row is
# included unconditionally.
_TIMELINE_NODE_SQL = text(
    """
    SELECT
        h.history_id    AS history_id,
        h.node_id       AS resource_id,
        h.change_kind   AS change_kind,
        h.snapshot      AS snapshot,
        h.audit_id      AS audit_id,
        h.valid_from    AS valid_from
    FROM graph_node_history h
    WHERE h.tenant_id = :tenant_id
      AND (CAST(:since_marker AS text) IS NULL OR h.valid_from >= :since)
      AND (CAST(:until_marker AS text) IS NULL OR h.valid_from <= :until)
      AND (
          :target_id IS NULL
          OR h.node_id IN (
              SELECT n.id FROM graph_node n
              WHERE n.tenant_id = :tenant_id
                AND n.target_id = :target_id
          )
      )
      AND (
          CAST(:cursor_marker AS text) IS NULL
          OR h.valid_from < :cursor_ts
          OR (h.valid_from = :cursor_ts AND h.history_id < :cursor_id)
      )
    ORDER BY h.valid_from DESC, h.history_id DESC
    LIMIT :limit
    """
).bindparams(
    bindparam("tenant_id", type_=SAUuid()),
    bindparam("target_id", type_=SAUuid()),
    bindparam("since", type_=DateTime(timezone=True)),
    bindparam("until", type_=DateTime(timezone=True)),
    bindparam("cursor_ts", type_=DateTime(timezone=True)),
)

_TIMELINE_EDGE_SQL = text(
    """
    SELECT
        h.history_id    AS history_id,
        h.edge_id       AS resource_id,
        h.change_kind   AS change_kind,
        h.snapshot      AS snapshot,
        h.audit_id      AS audit_id,
        h.valid_from    AS valid_from
    FROM graph_edge_history h
    WHERE h.tenant_id = :tenant_id
      AND (CAST(:since_marker AS text) IS NULL OR h.valid_from >= :since)
      AND (CAST(:until_marker AS text) IS NULL OR h.valid_from <= :until)
      AND (
          :target_id IS NULL
          OR h.edge_id IN (
              SELECT e.id FROM graph_edge e
              JOIN graph_node n_from ON n_from.id = e.from_node_id
              JOIN graph_node n_to   ON n_to.id   = e.to_node_id
              WHERE e.tenant_id = :tenant_id
                AND (
                    n_from.target_id = :target_id
                    OR n_to.target_id = :target_id
                )
          )
      )
      AND (
          CAST(:cursor_marker AS text) IS NULL
          OR h.valid_from < :cursor_ts
          OR (
              h.valid_from = :cursor_ts
              AND (
                  h.history_id < :cursor_id
                  OR (h.history_id = :cursor_id AND :cursor_src = 'node')
              )
          )
      )
    ORDER BY h.valid_from DESC, h.history_id DESC
    LIMIT :limit
    """
).bindparams(
    bindparam("tenant_id", type_=SAUuid()),
    bindparam("target_id", type_=SAUuid()),
    bindparam("since", type_=DateTime(timezone=True)),
    bindparam("until", type_=DateTime(timezone=True)),
    bindparam("cursor_ts", type_=DateTime(timezone=True)),
)


def _row_to_timeline_entry(row: Row[Any], source: str) -> TopologyTimelineEntry:
    """Map one node-or-edge history row to a :class:`TopologyTimelineEntry`.

    ``source`` is supplied by the caller because the row itself does
    not know which history table it came from -- the two SQL
    statements share a column shape so the same materialiser handles
    both, with the discriminator threaded through as a literal.

    ``snapshot`` arrives as a ``dict`` on PG (asyncpg's JSONB codec)
    and may arrive as a JSON-encoded string on SQLite (where the
    ``text()`` statement bypasses the ORM column-type round-trip).
    Both shapes are deserialised here so the summary renderer always
    sees a dict-or-None.
    """
    m = row._mapping
    snapshot_raw = m["snapshot"]
    snapshot: dict[str, Any] | None
    if isinstance(snapshot_raw, dict):
        snapshot = snapshot_raw
    elif isinstance(snapshot_raw, str):
        try:
            parsed = json.loads(snapshot_raw)
            snapshot = parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            snapshot = None
    else:
        snapshot = None
    change_kind = m["change_kind"]
    if source == "node":
        summary = _format_node_summary(change_kind, snapshot)
    else:
        summary = _format_edge_summary(change_kind, snapshot)
    return TopologyTimelineEntry(
        valid_from=m["valid_from"],
        history_id=m["history_id"],
        source=source,
        change_kind=change_kind,
        resource_id=m["resource_id"],
        summary=summary,
        audit_id=m["audit_id"],
    )


def _merge_timeline_pages(
    node_rows: list[TopologyTimelineEntry],
    edge_rows: list[TopologyTimelineEntry],
    limit: int,
) -> list[TopologyTimelineEntry]:
    """Merge two pre-sorted ``DESC`` lists into one, capped at ``limit``.

    Each input is already ordered by ``(valid_from DESC, history_id
    DESC)`` from its own SQL statement. The merge is a single
    two-pointer pass: pop the larger head from either list at each
    step, with the source discriminator (``"edge"`` < ``"node"``
    alphabetically) as the tie-breaker when both heads share
    ``(valid_from, history_id)`` -- which can happen only across the
    two tables, never within one.
    """
    merged: list[TopologyTimelineEntry] = []
    i = j = 0
    while len(merged) < limit and (i < len(node_rows) or j < len(edge_rows)):
        if i >= len(node_rows):
            merged.append(edge_rows[j])
            j += 1
            continue
        if j >= len(edge_rows):
            merged.append(node_rows[i])
            i += 1
            continue
        n = node_rows[i]
        e = edge_rows[j]
        # Lex compare: newer valid_from wins; same ts → higher
        # history_id wins; same (ts, id) → 'edge' before 'node'
        # alphabetically (deterministic + matches the cursor sort).
        if (n.valid_from, n.history_id) > (e.valid_from, e.history_id):
            merged.append(n)
            i += 1
        elif (n.valid_from, n.history_id) < (e.valid_from, e.history_id):
            merged.append(e)
            j += 1
        else:
            # Same (valid_from, history_id) across the two tables.
            # 'edge' sorts before 'node' alphabetically; in DESC
            # order across the keyset, 'node' (lex-greater) lands
            # first. Pick node first to mirror the cursor compare
            # in the SQL.
            merged.append(n)
            i += 1
    return merged


def _build_timeline_bind_params(
    operator: Operator,
    *,
    target_id: UUID | None,
    since: datetime | None,
    until: datetime | None,
    cursor_pos: TimelineCursorPosition | None,
    per_side_fetch: int,
) -> dict[str, Any]:
    """Assemble the bind-parameter dict for the two timeline SQL statements.

    ``tenant_id`` / ``target_id`` ride through SQLAlchemy's
    :class:`Uuid` bind type (declared on the ``text()`` via
    ``.bindparams`` on the two statements above) so the asyncpg /
    aiosqlite drivers serialise the UUID natively -- ``str(uuid)``
    would deliver the dashed-canonical form, which mismatches the
    hex storage SQLite uses for ``Uuid()`` columns and produces a
    silent zero-row result on the dev-test DB.

    ``*_marker`` parameters are text-typed sentinels parallel to the
    native-typed ones; the SQL uses ``CAST(:*_marker AS text) IS
    NULL`` to detect the optional-clause "off" state portably across
    PostgreSQL (where ``CAST(NULL AS text)`` is NULL with a known
    type) and SQLite. Mirrors the ``CAST(:x AS text) IS NULL OR
    ...`` idiom the traversal SQL in this module uses.
    """
    return {
        "tenant_id": operator.tenant_id,
        "since": since,
        "since_marker": "x" if since is not None else None,
        "until": until,
        "until_marker": "x" if until is not None else None,
        "target_id": target_id,
        "cursor_ts": cursor_pos.ts if cursor_pos is not None else None,
        "cursor_id": cursor_pos.history_id if cursor_pos is not None else 0,
        "cursor_src": cursor_pos.source if cursor_pos is not None else "node",
        "cursor_marker": "x" if cursor_pos is not None else None,
        "limit": per_side_fetch,
    }


def _compute_next_cursor(
    node_rows: list[TopologyTimelineEntry],
    edge_rows: list[TopologyTimelineEntry],
    merged: list[TopologyTimelineEntry],
    limit: int,
) -> str | None:
    """Encode the next-page cursor when there are unvisited rows past the page.

    Returns ``None`` when the merge consumed every fetched row -- the
    page is the end of the matching set. Otherwise encodes
    ``(valid_from, history_id, source)`` of the page's last row so
    the next call paginates strictly past it.

    ``has_more`` is true when *either* per-side fetch overflowed its
    per-side budget *or* the merge left rows un-emitted on either
    side after capping at ``limit``. Both signals matter: a per-side
    overflow alone doesn't necessarily mean the merged page is full
    (the overflowing side may have all-older rows), but a merge that
    capped at ``limit`` with leftovers on either side definitively
    indicates more rows exist.
    """
    if not merged or len(merged) < limit:
        return None
    has_more = (
        len(node_rows) > limit
        or len(edge_rows) > limit
        or _merge_has_leftovers(node_rows, edge_rows, merged)
    )
    if not has_more:
        return None
    last = merged[-1]
    return encode_timeline_cursor(
        TimelineCursorPosition(
            ts=last.valid_from,
            history_id=last.history_id,
            source=last.source,
        )
    )


async def query_timeline(
    operator: Operator,
    *,
    target_id: UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = _DEFAULT_TIMELINE_LIMIT,
    cursor: str | None = None,
) -> TopologyTimelineResult:
    """Tenant-wide chronological feed of graph changes.

    Initiative #365 (G9.3), Task #861 (T5). Walks
    :class:`GraphNodeHistory` + :class:`GraphEdgeHistory` in
    ``(valid_from DESC, history_id DESC, source DESC)`` order
    tenant-scoped on ``operator.tenant_id`` so a cross-tenant probe is
    structurally impossible -- the first WHERE clause of both literal
    SQL statements is ``tenant_id = :tenant_id``.

    Args:
        operator: The calling operator. ``tenant_id`` lifted from the
            JWT; never sourced from caller-controllable args.
        target_id: Optional ``targets.id`` filter -- restrict the
            timeline to history rows for resources belonging to one
            target. Nodes filter on ``graph_node.target_id``; edges
            filter on the endpoint nodes' ``target_id`` (either
            endpoint touching the target qualifies the edge, since
            an edge crossing two targets is part of both timelines).
        since: Optional lower bound on ``valid_from``. Inclusive.
        until: Optional upper bound on ``valid_from``. Inclusive.
        limit: Page size (default 50; ceiling 1000). Per the Task
            #861 acceptance criterion default.
        cursor: Opaque forward-pagination cursor from a prior page's
            ``next_cursor``. Decoded into ``(valid_from, history_id,
            source)`` and applied as a strict keyset compare against
            the row immediately after the cursor's row.

    Returns:
        :class:`TopologyTimelineResult` -- a page of rows ordered by
        ``(valid_from DESC, history_id DESC)``. ``next_cursor`` is
        ``None`` when the page is the end of the matching set; a
        non-None cursor encodes the last-row keyset position for the
        next call.

    Raises:
        ValueError: ``limit`` is out of range (``< 1`` or ``>
        _MAX_TIMELINE_LIMIT``).
        :class:`InvalidTimelineCursorError`: ``cursor`` is not a
        valid opaque token (the caller's previous next_cursor was
        not echoed verbatim, the token was tampered with, or the
        client typed a string by hand).
    """
    if limit < 1 or limit > _MAX_TIMELINE_LIMIT:
        raise ValueError(f"limit must be in 1..{_MAX_TIMELINE_LIMIT}; got {limit}")

    cursor_pos: TimelineCursorPosition | None = (
        decode_timeline_cursor(cursor) if cursor is not None else None
    )
    # Fetch ``limit + 1`` from each table; the +1 over the per-side
    # limit is the "has_more" detection trick that
    # :func:`_compute_next_cursor` reads.
    per_side_fetch = limit + 1
    bind_params = _build_timeline_bind_params(
        operator,
        target_id=target_id,
        since=since,
        until=until,
        cursor_pos=cursor_pos,
        per_side_fetch=per_side_fetch,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        node_result = await session.execute(_TIMELINE_NODE_SQL, bind_params)
        node_rows = [_row_to_timeline_entry(row, "node") for row in node_result.fetchall()]
        edge_result = await session.execute(_TIMELINE_EDGE_SQL, bind_params)
        edge_rows = [_row_to_timeline_entry(row, "edge") for row in edge_result.fetchall()]

    merged = _merge_timeline_pages(node_rows, edge_rows, limit)
    next_cursor = _compute_next_cursor(node_rows, edge_rows, merged, limit)
    return TopologyTimelineResult(rows=tuple(merged), next_cursor=next_cursor)


def _merge_has_leftovers(
    node_rows: list[TopologyTimelineEntry],
    edge_rows: list[TopologyTimelineEntry],
    merged: list[TopologyTimelineEntry],
) -> bool:
    """Detect "more rows exist past this page" after the merge cap.

    A merge that consumed exactly ``len(node_rows)`` from one side
    and only some of the other side -- without overflowing the
    per-side fetch -- still has leftovers iff the un-consumed side
    contributed fewer than its fetch size. We need to know whether
    the merge stopped on the cap *and* there are un-consumed rows
    on either side.
    """
    consumed_nodes = sum(1 for r in merged if r.source == "node")
    consumed_edges = len(merged) - consumed_nodes
    return consumed_nodes < len(node_rows) or consumed_edges < len(edge_rows)


# ---------------------------------------------------------------------------
# G9.3-T3 (#859) — per-resource history walk
# ---------------------------------------------------------------------------
#
# Unlike :func:`query_timeline` (tenant-wide chronological feed),
# :func:`query_history` anchors on **one** :class:`GraphNode` and
# returns every history row that mentions it -- the node-side rows
# directly, and (when ``include_edges=True``) every edge-side row whose
# ``edge_id`` resolves to an edge with the anchor at either endpoint.
# The shape is the operator surface "show me what changed for THIS
# resource" parallel to the timeline's "what changed in the graph at
# all".
#
# Indexes the per-resource walk leans on (declared by migration 0012
# for G9.3-T1 #856):
#
#   * ``graph_node_history`` ``(tenant_id, node_id, valid_from DESC)``
#     -- per-(tenant, node, time) lookup is a single composite-index
#     scan.
#   * ``graph_edge_history`` ``(tenant_id, edge_id, valid_from DESC)``
#     -- mirror for the edge side.
#
# Both indexes are tenant-scoped composites so the per-resource slice
# is sub-millisecond on the test fixture and indexed under realistic
# load.

#: Hard ceiling on rows returned in one :func:`query_history` call.
#: Picked to fit the typical retention window (90 days x a few
#: writes/day per resource) into a single response without paginating;
#: the route layer caps tighter at the HTTP boundary. Bumping requires
#: a coordinated review with the retention-cadence ``Settings``
#: defaults.
_MAX_HISTORY_ROWS = 5000


# Node-side history walk for one anchor node. ``anchor_node_id`` is
# the resolved :class:`GraphNode.id`; the FK is direct so this is a
# single indexed scan over the composite
# ``(tenant_id, node_id, valid_from DESC)`` index. ``since`` /
# ``until`` ride the established ``CAST(:marker AS text) IS NULL OR
# ...`` optional-filter idiom so one literal statement serves the
# bounded and unbounded cases.
_HISTORY_NODE_SQL = text(
    """
    SELECT
        h.history_id    AS history_id,
        h.node_id       AS resource_id,
        h.change_kind   AS change_kind,
        h.snapshot      AS snapshot,
        h.audit_id      AS audit_id,
        h.valid_from    AS valid_from
    FROM graph_node_history h
    WHERE h.tenant_id = :tenant_id
      AND h.node_id = :anchor_node_id
      AND (CAST(:since_marker AS text) IS NULL OR h.valid_from >= :since)
      AND (CAST(:until_marker AS text) IS NULL OR h.valid_from <= :until)
    ORDER BY h.valid_from DESC, h.history_id DESC
    LIMIT :limit
    """
).bindparams(
    bindparam("tenant_id", type_=SAUuid()),
    bindparam("anchor_node_id", type_=SAUuid()),
    bindparam("since", type_=DateTime(timezone=True)),
    bindparam("until", type_=DateTime(timezone=True)),
)

# Edge-side history walk for every edge incident to the anchor. The
# inner subquery resolves the edge ids whose ``from_node_id`` or
# ``to_node_id`` matches the anchor; the outer query pulls every
# history row for those ids. Tenant scope is enforced on both the
# inner (``graph_edge.tenant_id``) and outer
# (``graph_edge_history.tenant_id``) so a cross-tenant edge id cannot
# leak in. Tombstones (rows whose ``edge_id`` was NULLed by
# ``ON DELETE SET NULL``) drop out of the inner subquery's id list
# and therefore stay out of the per-resource walk -- a tombstoned
# edge has no surviving live row to associate with the anchor.
# Operators wanting the full tombstone replay use
# ``meho topology timeline`` (G9.3-T5 #861) which surfaces every
# history row including tombstones.
_HISTORY_EDGE_SQL = text(
    """
    SELECT
        h.history_id    AS history_id,
        h.edge_id       AS resource_id,
        h.change_kind   AS change_kind,
        h.snapshot      AS snapshot,
        h.audit_id      AS audit_id,
        h.valid_from    AS valid_from
    FROM graph_edge_history h
    WHERE h.tenant_id = :tenant_id
      AND h.edge_id IN (
          SELECT e.id FROM graph_edge e
          WHERE e.tenant_id = :tenant_id
            AND (e.from_node_id = :anchor_node_id
                 OR e.to_node_id = :anchor_node_id)
      )
      AND (CAST(:since_marker AS text) IS NULL OR h.valid_from >= :since)
      AND (CAST(:until_marker AS text) IS NULL OR h.valid_from <= :until)
    ORDER BY h.valid_from DESC, h.history_id DESC
    LIMIT :limit
    """
).bindparams(
    bindparam("tenant_id", type_=SAUuid()),
    bindparam("anchor_node_id", type_=SAUuid()),
    bindparam("since", type_=DateTime(timezone=True)),
    bindparam("until", type_=DateTime(timezone=True)),
)


def _row_to_history_entry(row: Row[Any], source: str) -> TopologyHistoryEntry:
    """Map one history row to :class:`TopologyHistoryEntry`.

    ``source`` is threaded through by the caller because the SELECT
    column shape is identical for the two history tables; the same
    materialiser handles both with the discriminator passed as a
    literal. ``snapshot`` arrives as a ``dict`` on PG (asyncpg JSONB
    codec) and as a JSON-encoded string on SQLite -- both are
    normalised to ``dict-or-None`` here so the front layers always
    see a structured payload.

    Unlike :class:`TopologyTimelineEntry`, the history entry preserves
    the full snapshot rather than rendering it down to a one-line
    summary. The summary collapse is timeline's space-saving trick
    appropriate for a tenant-wide feed; per-resource history is the
    forensic surface where the snapshot is the load-bearing payload.
    """
    m = row._mapping
    snapshot_raw = m["snapshot"]
    snapshot: dict[str, Any] | None
    if isinstance(snapshot_raw, dict):
        snapshot = snapshot_raw
    elif isinstance(snapshot_raw, str):
        try:
            parsed = json.loads(snapshot_raw)
            snapshot = parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            snapshot = None
    else:
        snapshot = None
    return TopologyHistoryEntry(
        valid_from=m["valid_from"],
        history_id=m["history_id"],
        source=source,
        change_kind=m["change_kind"],
        resource_id=m["resource_id"],
        snapshot=snapshot,
        audit_id=m["audit_id"],
    )


def _merge_history_pages(
    node_rows: list[TopologyHistoryEntry],
    edge_rows: list[TopologyHistoryEntry],
    limit: int,
) -> list[TopologyHistoryEntry]:
    """Merge two pre-sorted ``DESC`` lists into one, capped at ``limit``.

    Mirror of :func:`_merge_timeline_pages` for the history shape.
    Each input is already ordered by ``(valid_from DESC, history_id
    DESC)`` from its own SQL statement. Two-pointer pass; when both
    heads share the same ``(valid_from, history_id)`` (only possible
    across the two tables), the node side lands first to mirror the
    timeline merge convention.
    """
    merged: list[TopologyHistoryEntry] = []
    i = j = 0
    while len(merged) < limit and (i < len(node_rows) or j < len(edge_rows)):
        if i >= len(node_rows):
            merged.append(edge_rows[j])
            j += 1
            continue
        if j >= len(edge_rows):
            merged.append(node_rows[i])
            i += 1
            continue
        n = node_rows[i]
        e = edge_rows[j]
        if (n.valid_from, n.history_id) > (e.valid_from, e.history_id):
            merged.append(n)
            i += 1
        elif (n.valid_from, n.history_id) < (e.valid_from, e.history_id):
            merged.append(e)
            j += 1
        else:
            merged.append(n)
            i += 1
    return merged


async def query_history(
    operator: Operator,
    name_or_alias: str,
    *,
    kind: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    include_edges: bool = False,
    limit: int = _MAX_HISTORY_ROWS,
) -> TopologyHistoryResult:
    """Per-resource history walk anchored at one ``graph_node``.

    Initiative #365 (G9.3), Task #859 (T3). The companion to
    :func:`query_timeline`: timeline is "what changed in the graph at
    all"; history is "what changed for THIS specific resource".
    Resolves the anchor tenant-scoped via :func:`resolve_node` so an
    unknown name (or a name that exists only in another tenant)
    surfaces as :class:`NodeNotFoundError`, the contract the route
    layer maps to 404. A bare name that resolves to multiple kinds
    raises :class:`AmbiguousNodeError`, mapped to 409 by the route
    layer.

    Args:
        operator: The calling operator. ``tenant_id`` lifted from the
            JWT; never sourced from caller-controllable args.
        name_or_alias: The anchor node's :attr:`GraphNode.name`. The
            G9.1 resolver only matches on ``name`` -- formal alias
            resolution is deferred (see
            ``docs/codebase/topology.md`` "Known issues") -- so an
            aliased name will currently surface as
            :class:`NodeNotFoundError` here just as it does for the
            traversal verbs. The argument is named
            ``name_or_alias`` for forward-compat with the planned
            G10 alias substrate.
        kind: Optional :attr:`GraphNode.kind` pin to disambiguate
            when the bare name resolves to multiple kinds in the
            tenant.
        since: Optional lower bound on ``valid_from``. Inclusive.
        until: Optional upper bound on ``valid_from``. Inclusive.
        include_edges: When ``True``, also walk every history row for
            edges incident to the anchor (joined via the inner
            subquery on ``graph_edge.from_node_id`` /
            ``graph_edge.to_node_id``). The merged result still
            orders newest-first.
        limit: Hard cap on returned rows (1..``_MAX_HISTORY_ROWS``).
            Defaults to the ceiling because per-resource history is
            bounded by retention; tighter caps would silently
            truncate the walk.

    Returns:
        :class:`TopologyHistoryResult` carrying the resolved
        ``anchor_node_id``, the echoed ``include_edges`` flag, and a
        tuple of :class:`TopologyHistoryEntry` rows in
        ``(valid_from DESC, history_id DESC)`` order.

    Raises:
        NodeNotFoundError: ``name_or_alias`` (and ``kind``, when
            supplied) does not resolve in this tenant.
        AmbiguousNodeError: Bare-name lookup hit multiple kinds; pass
            ``kind=`` to disambiguate.
        ValueError: ``limit`` is out of range (``< 1`` or
            ``> _MAX_HISTORY_ROWS``).
    """
    if limit < 1 or limit > _MAX_HISTORY_ROWS:
        raise ValueError(f"limit must be in 1..{_MAX_HISTORY_ROWS}; got {limit}")

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # Resolve the anchor up front so a missing / cross-tenant /
        # ambiguous name surfaces with the canonical exception types
        # the route + MCP layers already map; never let the SQL
        # statements run with a NULL anchor (a quirk of the
        # ``CAST(:x AS text) IS NULL OR ...`` idiom is that a NULL
        # anchor would broaden the filter, not narrow it).
        anchor = await resolve_node(session, operator.tenant_id, name_or_alias, kind=kind)
        bind_params: dict[str, Any] = {
            "tenant_id": operator.tenant_id,
            "anchor_node_id": anchor.id,
            "since": since,
            "since_marker": "x" if since is not None else None,
            "until": until,
            "until_marker": "x" if until is not None else None,
            "limit": limit,
        }
        node_result = await session.execute(_HISTORY_NODE_SQL, bind_params)
        node_rows = [_row_to_history_entry(row, "node") for row in node_result.fetchall()]
        edge_rows: list[TopologyHistoryEntry] = []
        if include_edges:
            edge_result = await session.execute(_HISTORY_EDGE_SQL, bind_params)
            edge_rows = [_row_to_history_entry(row, "edge") for row in edge_result.fetchall()]

    if not include_edges:
        merged = node_rows[:limit]
    else:
        merged = _merge_history_pages(node_rows, edge_rows, limit)

    return TopologyHistoryResult(
        anchor_node_id=anchor.id,
        include_edges=include_edges,
        rows=tuple(merged),
    )


# ---------------------------------------------------------------------------
# G9.3-T4 (#860) — graph-level diff between two timestamps
# ---------------------------------------------------------------------------
#
# ``query_diff`` is the heavy temporal query: per resource, fold every
# in-window history row into a single net delta (created / updated /
# removed) for ``(ts1, ts2]``. The fold rule:
#
#   * created -- first in-window row is ``created`` AND last is not
#     ``removed``.
#   * removed -- last in-window row is ``removed``. (Includes the
#     "created and removed in the same window" case: the post-window
#     state is "gone".)
#   * updated -- otherwise (the resource existed before ``ts1`` and has
#     at least one in-window mutation that isn't a net delete).
#
# ``--changed-only`` suppresses ``updated`` entries whose only mutation
# in the window was a ``last_seen``-bump (the refresh service's "I
# observed this row again at T+15m" emission). The substrate folds these
# by comparing the snapshot's ``before`` and ``after`` projections: if
# the only differing field is ``last_seen``, the row is a refresh
# heartbeat and gets dropped under the flag.
#
# The 1000-row cap is enforced at the substrate -- the CLI / REST / MCP
# fronts mirror it but the substrate is authoritative. Truncation
# returns a partial result with ``truncated=True`` plus the canonical
# "narrow the time window" hint; consumers render the hint verbatim.

#: Hard ceiling on diff entries returned in one call. v0.2 contract per
#: the Task #860 acceptance criterion ("output exceeding 1000 rows
#: returns a truncation marker + remediation hint"). The cap is at the
#: substrate so a non-route caller (REPL, MCP front, CLI) cannot
#: accidentally request an unbounded fold.
_DIFF_HARD_CAP: Final[int] = 1000

#: SQL-layer ceiling on the number of *distinct resources* fetched per
#: side (#987). Bounds the rows materialised by ``fetchall`` so a wide
#: window over a churning tenant cannot pull the whole history slice
#: into memory before the Python fold caps it -- mirroring the
#: list-query precedent (:data:`_MAX_EDGE_LIMIT` / :data:`_MAX_NODE_LIMIT`)
#: where the bound lives in the statement, not in a post-fetch slice.
#:
#: The bound is on **distinct resources**, not raw rows, because the
#: cap counts post-fold entries (one per resource) and a single
#: resource can carry many in-window history rows (one ``updated`` per
#: refresh heartbeat). A raw ``LIMIT`` would either split a resource's
#: row group mid-fold (corrupting that entry) or undercount entries
#: (1000 rows might fold to 500 resources, never tripping the cap). The
#: ``DENSE_RANK`` window keeps **complete** row groups for the first
#: ``_DIFF_HARD_CAP + 1`` distinct resources -- enough to fill the cap
#: and observe one resource beyond it, so ``truncated`` is detected
#: with the same outcome as the unbounded fetch for the unfiltered
#: cohort (the load-bearing high-churn AC).
#:
#: Note on post-fold filters: ``changed_only`` / ``kind_filter`` drop
#: entries *after* the fetch, so under those flags an adversarially
#: wide window could fetch cap+1 resources, have some dropped, and
#: report ``truncated=False`` where an unbounded fetch would have
#: surfaced a later surviving resource. This is an accepted, bounded
#: tradeoff: the substrate guard never over-fetches, the route layer
#: caps tighter separately, and the unfiltered cohort -- the case the
#: cap exists to protect -- stays exact.
_DIFF_FETCH_RESOURCE_CAP: Final[int] = _DIFF_HARD_CAP + 1

#: Canonical operator-facing remediation surfaced verbatim on every
#: front when the cap fires. Pinned as a module constant so the CLI /
#: REST / MCP wording stays in lock-step without duplicating the string
#: across three layers.
_DIFF_TRUNCATION_HINT: Final[str] = (
    "diff output truncated at the 1000-row hard cap; narrow the "
    "time window (ts1..ts2) or filter by --kind to reduce churn."
)


# Two literal SQL statements -- one per history table -- pulling the
# in-window rows tenant-scoped, bounded at the SQL layer to the first
# ``:resource_cap`` distinct resources (#987). The fold is Python-side
# because the per-resource "first vs last in-window row" rule is awkward
# in pure SQL and the projection-shape diff for ``--changed-only`` needs
# access to the deserialised snapshot. Indexes the queries lean on
# (migration 0012): ``graph_*_history_tenant_valid_from_idx`` --
# composite ``(tenant_id, valid_from DESC)`` so the per-tenant slice is
# sub-ms under realistic load.
#
# ``valid_from`` is ``>`` ``ts1`` (exclusive lower) and ``<=`` ``ts2``
# (inclusive upper) per the Task #860 contract.
#
# SQL-side bound (#987): a ``DENSE_RANK`` window ranks resources by
# their first in-window appearance (``valid_from ASC, history_id ASC``)
# and the outer ``WHERE grp_rank <= :resource_cap`` keeps only the
# first ``:resource_cap`` distinct resources' **complete** row groups.
# The resource key is ``resource_id`` when present, else the row's own
# ``history_id`` -- so a hard-deleted tombstone (``resource_id IS NULL``
# after ``ON DELETE SET NULL``) counts as its own group rather than
# collapsing every tombstone under one key (the create-then-delete
# rejoin via the recovered id still happens Python-side after fetch).
# Keeping whole groups means the fold never sees a partial resource;
# bounding by distinct resources -- not raw rows -- means the cap is
# reached on the same cohort as the unbounded fetch. ``:resource_cap``
# is ``_DIFF_HARD_CAP + 1`` so the fold can observe one resource past
# the cap and set ``truncated`` correctly.
#
# Ordering: the outer ``ORDER BY valid_from ASC, history_id ASC`` is
# what the fold sees, so the "first vs last in-window row" rule falls
# out of a single left-to-right pass per resource.
#
# Each side is a separate **fully-literal** ``text("...")`` -- the node
# and edge statements differ only in the history table and the
# resource-id column, but they are written out verbatim rather than
# built from a shared ``.format()`` template so nothing is interpolated
# into the SQL string. That keeps the convention this module documents
# (see the module docstring) and keeps the SAST gate happy: a templated
# ``text(template.format(...))`` reads as a SQL-injection sink to
# ``avoid-sqlalchemy-text`` even when every substitution is a trusted
# module constant, whereas a literal ``text("...")`` does not.
_DIFF_NODE_SQL = text(
    """
    WITH windowed AS (
        SELECT
            h.history_id    AS history_id,
            h.node_id       AS resource_id,
            h.change_kind   AS change_kind,
            h.snapshot      AS snapshot,
            h.valid_from    AS valid_from,
            COALESCE(
                CAST(h.node_id AS TEXT),
                'h:' || CAST(h.history_id AS TEXT)
            )               AS grp_key
        FROM graph_node_history h
        WHERE h.tenant_id = :tenant_id
          AND h.valid_from > :ts1
          AND h.valid_from <= :ts2
    ),
    grouped AS (
        SELECT
            w.*,
            MIN(w.valid_from) OVER (PARTITION BY w.grp_key)
                AS grp_first_valid_from,
            FIRST_VALUE(w.history_id) OVER (
                PARTITION BY w.grp_key
                ORDER BY w.valid_from ASC, w.history_id ASC
                ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
            )               AS grp_first_history_id
        FROM windowed w
    ),
    ranked AS (
        SELECT
            g.*,
            DENSE_RANK() OVER (
                ORDER BY g.grp_first_valid_from ASC, g.grp_first_history_id ASC
            )               AS grp_rank
        FROM grouped g
    )
    SELECT
        r.history_id    AS history_id,
        r.resource_id   AS resource_id,
        r.change_kind   AS change_kind,
        r.snapshot      AS snapshot,
        r.valid_from    AS valid_from
    FROM ranked r
    WHERE r.grp_rank <= :resource_cap
    ORDER BY r.valid_from ASC, r.history_id ASC
    """
).bindparams(
    bindparam("tenant_id", type_=SAUuid()),
    bindparam("ts1", type_=DateTime(timezone=True)),
    bindparam("ts2", type_=DateTime(timezone=True)),
    bindparam("resource_cap"),
)

_DIFF_EDGE_SQL = text(
    """
    WITH windowed AS (
        SELECT
            h.history_id    AS history_id,
            h.edge_id       AS resource_id,
            h.change_kind   AS change_kind,
            h.snapshot      AS snapshot,
            h.valid_from    AS valid_from,
            COALESCE(
                CAST(h.edge_id AS TEXT),
                'h:' || CAST(h.history_id AS TEXT)
            )               AS grp_key
        FROM graph_edge_history h
        WHERE h.tenant_id = :tenant_id
          AND h.valid_from > :ts1
          AND h.valid_from <= :ts2
    ),
    grouped AS (
        SELECT
            w.*,
            MIN(w.valid_from) OVER (PARTITION BY w.grp_key)
                AS grp_first_valid_from,
            FIRST_VALUE(w.history_id) OVER (
                PARTITION BY w.grp_key
                ORDER BY w.valid_from ASC, w.history_id ASC
                ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
            )               AS grp_first_history_id
        FROM windowed w
    ),
    ranked AS (
        SELECT
            g.*,
            DENSE_RANK() OVER (
                ORDER BY g.grp_first_valid_from ASC, g.grp_first_history_id ASC
            )               AS grp_rank
        FROM grouped g
    )
    SELECT
        r.history_id    AS history_id,
        r.resource_id   AS resource_id,
        r.change_kind   AS change_kind,
        r.snapshot      AS snapshot,
        r.valid_from    AS valid_from
    FROM ranked r
    WHERE r.grp_rank <= :resource_cap
    ORDER BY r.valid_from ASC, r.history_id ASC
    """
).bindparams(
    bindparam("tenant_id", type_=SAUuid()),
    bindparam("ts1", type_=DateTime(timezone=True)),
    bindparam("ts2", type_=DateTime(timezone=True)),
    bindparam("resource_cap"),
)


def _deserialise_snapshot(snapshot_raw: Any) -> dict[str, Any] | None:
    """Coerce a snapshot column value into a dict-or-None.

    Asyncpg's JSONB codec returns ``dict``; SQLite (the test DB) returns
    a JSON-encoded string because the ``text()`` literal bypasses the
    ORM column-type round-trip. Mirrors :func:`_row_to_timeline_entry`'s
    snapshot coercion so the same deserialisation rule applies to every
    history-table read path.
    """
    if isinstance(snapshot_raw, dict):
        return snapshot_raw
    if isinstance(snapshot_raw, str):
        try:
            parsed = json.loads(snapshot_raw)
        except (TypeError, ValueError):
            return None
        if isinstance(parsed, dict):
            return parsed
        return None
    return None


def _snapshot_side(snapshot: dict[str, Any] | None, key: str) -> dict[str, Any] | None:
    """Pick the ``before`` or ``after`` side of a snapshot, or None.

    Returns the side as a plain dict; ``None`` when the side is absent
    or non-dict (e.g. ``created`` rows have ``before=None``).
    """
    if snapshot is None:
        return None
    side = snapshot.get(key)
    if isinstance(side, dict):
        return side
    return None


def _is_last_seen_only_change(snapshot: dict[str, Any] | None) -> bool:
    """Detect an ``updated`` row whose only diff is ``last_seen``.

    The refresh service emits an ``updated`` history row whenever a
    probed live row's column changes -- including ``last_seen``-only
    bumps from the periodic refresh ("I observed this row again at
    T+15m"). The ``--changed-only`` flag exists precisely to suppress
    these refresh heartbeats from a diff so the operator sees only
    substantive change.

    Returns ``True`` only when both snapshot sides are dicts that share
    every key with identical values *except* ``last_seen``. A snapshot
    missing either side (a ``created`` or ``removed`` row, where the
    flag is irrelevant in the first place) returns ``False`` so the
    caller never accidentally drops a non-``updated`` row.
    """
    before = _snapshot_side(snapshot, "before")
    after = _snapshot_side(snapshot, "after")
    if before is None or after is None:
        return False
    # Same key set -- if a key was added or removed that itself is a
    # substantive change worth surfacing.
    if set(before.keys()) != set(after.keys()):
        return False
    for key, before_value in before.items():
        if key == "last_seen":
            continue
        if before_value != after.get(key):
            return False
    return True


def _pick_kind_for_entry(rows: list[Row[Any]], net_kind: str) -> str:
    """Pick a renderable ``kind`` from a resource's in-window history rows.

    For a ``removed`` resource the operator wants to see what was
    removed -- pull the ``kind`` from the pre-state of the last row.
    For ``created`` / ``updated`` the post-state of the last row
    carries the current kind. Falls back to ``"unknown"`` only when
    both snapshot sides are missing (a legacy row from a pre-hook
    era), preserving the entry rather than dropping it.
    """
    last = rows[-1]
    snapshot = _deserialise_snapshot(last._mapping["snapshot"])
    if net_kind == "removed":
        side = _snapshot_side(snapshot, "before")
        if side is not None and "kind" in side:
            return str(side["kind"])
    side = _snapshot_side(snapshot, "after")
    if side is not None and "kind" in side:
        return str(side["kind"])
    side = _snapshot_side(snapshot, "before")
    if side is not None and "kind" in side:
        return str(side["kind"])
    return "unknown"


def _pick_name_for_node_entry(rows: list[Row[Any]], net_kind: str) -> str | None:
    """Pick a renderable ``name`` for a node entry from its history rows.

    Mirror of :func:`_pick_kind_for_entry` for the ``name`` column.
    Edges return ``None`` (the edge snapshot does not carry a name --
    only endpoint FKs).
    """
    last = rows[-1]
    snapshot = _deserialise_snapshot(last._mapping["snapshot"])
    if net_kind == "removed":
        side = _snapshot_side(snapshot, "before")
        if side is not None and "name" in side:
            return str(side["name"])
    side = _snapshot_side(snapshot, "after")
    if side is not None and "name" in side:
        return str(side["name"])
    side = _snapshot_side(snapshot, "before")
    if side is not None and "name" in side:
        return str(side["name"])
    return None


def _fold_to_net_kind(rows: list[Row[Any]]) -> Literal["created", "updated", "removed"]:
    """Fold a resource's in-window history rows into one net ``change_kind``.

    The fold rule per resource over the window ``(ts1, ts2]``:

    * First in-window row is ``created`` AND last is not ``removed``
      -> ``created``.
    * Last in-window row is ``removed`` -> ``removed`` (includes the
      created-and-removed-in-same-window case; the post-window state
      is "gone").
    * Otherwise -> ``updated`` (the resource existed before ``ts1`` and
      mutated in window).
    """
    first_kind = rows[0]._mapping["change_kind"]
    last_kind = rows[-1]._mapping["change_kind"]
    if last_kind == "removed":
        return "removed"
    if first_kind == "created":
        return "created"
    return "updated"


def _all_window_rows_are_last_seen_only(rows: list[Row[Any]]) -> bool:
    """True when every in-window ``updated`` row is a ``last_seen``-only bump.

    For ``--changed-only`` we suppress an entry whose net kind is
    ``updated`` AND every constituent in-window row is a ``last_seen``-only
    refresh heartbeat. If any in-window row is a substantive change
    (kind changed, properties changed, target_id moved, etc.) the entry
    survives because the operator does want to see the substantive
    column flip even if the cohort also includes refresh bumps. A
    ``created`` or ``removed`` entry is never a refresh heartbeat by
    construction, so this check is only relevant for the
    ``updated`` net-kind branch.
    """
    for row in rows:
        if row._mapping["change_kind"] != "updated":
            return False
        snapshot = _deserialise_snapshot(row._mapping["snapshot"])
        if not _is_last_seen_only_change(snapshot):
            return False
    return True


def _build_diff_entry(
    rows: list[Row[Any]],
    *,
    source: Literal["node", "edge"],
    resource_id: UUID | None,
    changed_only: bool,
) -> TopologyDiffEntry | None:
    """Fold one resource's history rows into a single :class:`TopologyDiffEntry`.

    Returns ``None`` when the entry should be suppressed under
    ``changed_only`` (an ``updated`` net-kind whose every in-window row
    is a ``last_seen``-only refresh heartbeat).

    Summary format mirrors :func:`_format_node_summary` /
    :func:`_format_edge_summary` so the diff and timeline surfaces read
    consistently: ``"<change_kind> <source> <kind>[ <name>]"``.
    """
    net_kind = _fold_to_net_kind(rows)
    if changed_only and net_kind == "updated" and _all_window_rows_are_last_seen_only(rows):
        return None
    domain_kind = _pick_kind_for_entry(rows, net_kind)
    name = _pick_name_for_node_entry(rows, net_kind) if source == "node" else None
    if name is None:
        summary = f"{net_kind} {source} {domain_kind}"
    else:
        summary = f"{net_kind} {source} {domain_kind} {name}"
    return TopologyDiffEntry(
        change_kind=net_kind,
        source=source,
        resource_id=resource_id,
        kind=domain_kind,
        name=name,
        summary=summary,
    )


def _recover_resource_id(row: Row[Any]) -> UUID | None:
    """Recover a tombstone row's resource id from its snapshot payload.

    A hard-deleted resource's history row carries ``resource_id``
    (``node_id`` / ``edge_id``) NULL because the FK is ``ON DELETE SET
    NULL`` (migration 0012) -- the cascade fires when the live row is
    deleted in the same flush. The deleted resource's own id survives in
    the snapshot (``before.id`` for the ``removed`` row; the
    ``_*_SNAPSHOT_COLUMNS`` include ``id`` for exactly this reason -- see
    :mod:`meho_backplane.topology.annotate`). Returns the UUID, or
    ``None`` when the snapshot is missing/malformed (e.g. a legacy
    pre-hook tombstone).
    """
    snapshot = _deserialise_snapshot(row._mapping["snapshot"])
    for key in ("before", "after"):
        side = _snapshot_side(snapshot, key)
        if side is not None:
            raw = side.get("id")
            if raw is not None:
                try:
                    return UUID(str(raw))
                except (ValueError, AttributeError):
                    return None
    return None


def _group_rows_by_resource(
    rows: Sequence[Row[Any]],
) -> list[tuple[UUID | None, list[Row[Any]]]]:
    """Group history rows by resource preserving first-occurrence order.

    The SQL returns rows in ``(valid_from ASC, history_id ASC)`` order;
    grouping per resource while keeping the first-seen order gives the
    substrate a deterministic per-resource ordering for the cap.

    Tombstone rows (``resource_id IS NULL`` after an ``ON DELETE SET
    NULL`` hard-delete) must NOT all collapse under a single ``None``
    key -- that would merge two distinct deletions into one diff entry,
    undercount the truncation cap, and drop the deleted resources'
    identities. Instead the resource id is recovered from the snapshot
    (:func:`_recover_resource_id`); a create-then-delete within the same
    window re-joins its pre-delete rows because the recovered id equals
    the original ``resource_id``. When the id is unrecoverable, the row
    is keyed on its unique ``history_id`` so each such tombstone still
    becomes its own entry rather than merging.

    Returns a list of ``(resource_id, rows)`` tuples in first-occurrence
    order, so a caller iterating it sees the same order in every call
    (important for the cap-stable truncation behaviour). ``resource_id``
    is the recovered UUID where known, else ``None``.
    """
    by_key: dict[tuple[str, object], list[Row[Any]]] = {}
    resource_id_for_key: dict[tuple[str, object], UUID | None] = {}
    order: list[tuple[str, object]] = []
    for row in rows:
        rid = row._mapping["resource_id"]
        if rid is not None:
            key: tuple[str, object] = ("id", rid)
            resource_id: UUID | None = rid
        else:
            recovered = _recover_resource_id(row)
            if recovered is not None:
                key = ("id", recovered)
                resource_id = recovered
            else:
                key = ("history", row._mapping["history_id"])
                resource_id = None
        if key not in by_key:
            by_key[key] = []
            resource_id_for_key[key] = resource_id
            order.append(key)
        by_key[key].append(row)
    return [(resource_id_for_key[key], by_key[key]) for key in order]


async def query_diff(
    operator: Operator,
    *,
    ts1: datetime,
    ts2: datetime,
    changed_only: bool = False,
    kind_filter: str | None = None,
) -> TopologyDiffResult:
    """Graph-level diff between ``ts1`` (exclusive) and ``ts2`` (inclusive).

    Initiative #365 (G9.3), Task #860 (T4). Walks
    :class:`GraphNodeHistory` + :class:`GraphEdgeHistory` tenant-scoped
    on ``operator.tenant_id`` and folds every in-window row per
    resource into one net ``created`` / ``updated`` / ``removed``
    entry. Returns at most :data:`_DIFF_HARD_CAP` entries; on overflow,
    ``truncated=True`` and ``truncation_hint`` carries the canonical
    "narrow the time window" remediation.

    Args:
        operator: The calling operator. ``tenant_id`` lifted from the
            JWT; never sourced from caller-controllable args.
        ts1: Exclusive lower bound on ``valid_from``.
        ts2: Inclusive upper bound on ``valid_from``.
        changed_only: When ``True``, suppress ``updated`` entries
            whose every in-window history row is a ``last_seen``-only
            refresh heartbeat. ``created`` / ``removed`` entries are
            never affected (by construction they are not heartbeats).
        kind_filter: Optional resource-kind filter -- restrict the
            result to entries whose domain ``kind`` matches (node
            ``kind`` like ``vm``, or edge ``kind`` like ``runs-on``).
            Filter is applied **after** the fold so the cap fires on
            the post-filter cohort.

    Returns:
        :class:`TopologyDiffResult`. ``entries`` is ordered by
        first-seen ``resource_id`` from the underlying SQL scan;
        consumers wanting a presentation order sort client-side.

    Raises:
        ValueError: ``ts1 >= ts2`` -- the window must be non-empty;
            an empty / inverted window is an operator-actionable input
            problem and surfaced before any DB call.
    """
    if not ts1 < ts2:
        raise ValueError(f"query_diff requires ts1 < ts2; got ts1={ts1!r}, ts2={ts2!r}")

    bind_params: dict[str, Any] = {
        "tenant_id": operator.tenant_id,
        "ts1": ts1,
        "ts2": ts2,
        # Bound the fetch at the SQL layer to the first
        # ``_DIFF_HARD_CAP + 1`` distinct resources per side (#987) so a
        # wide window cannot materialise the whole history slice in
        # memory. The +1 lets the Python fold observe one resource past
        # the cap and set ``truncated`` with the same outcome as an
        # unbounded fetch for the unfiltered cohort.
        "resource_cap": _DIFF_FETCH_RESOURCE_CAP,
    }

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        node_result = await session.execute(_DIFF_NODE_SQL, bind_params)
        node_rows = node_result.fetchall()
        edge_result = await session.execute(_DIFF_EDGE_SQL, bind_params)
        edge_rows = edge_result.fetchall()

    entries, truncated = _fold_diff_entries(
        node_rows=node_rows,
        edge_rows=edge_rows,
        changed_only=changed_only,
        kind_filter=kind_filter,
    )

    return TopologyDiffResult(
        entries=tuple(entries),
        truncated=truncated,
        truncation_hint=_DIFF_TRUNCATION_HINT if truncated else None,
    )


def _fold_diff_entries(
    *,
    node_rows: Sequence[Row[Any]],
    edge_rows: Sequence[Row[Any]],
    changed_only: bool,
    kind_filter: str | None,
) -> tuple[list[TopologyDiffEntry], bool]:
    """Fold the per-side history rows into net diff entries + a cap flag.

    Folds the node side first, then the edge side. Within a side the
    SQL's ``ORDER BY valid_from ASC, history_id ASC`` ordering is
    preserved by :func:`_group_rows_by_resource`, so the per-side
    iteration is deterministic. Across sides, nodes come before edges --
    a stable presentation order that mirrors the timeline tie-breaker
    (node before edge at the same key).

    The :data:`_DIFF_HARD_CAP` truncation fires on the post-fold,
    post-``kind_filter`` cohort and is shared across both sides:
    ``entries`` accumulates node entries then edge entries, and the
    cap trips when the combined count reaches the ceiling. The SQL
    already bounded the fetch to :data:`_DIFF_FETCH_RESOURCE_CAP`
    distinct resources per side (#987), so this fold can always observe
    one resource past the cap for the unfiltered cohort.

    Returns the ``(entries, truncated)`` pair the caller wraps in a
    :class:`TopologyDiffResult`.
    """
    entries: list[TopologyDiffEntry] = []
    truncated = False
    sides: tuple[tuple[Literal["node", "edge"], Sequence[Row[Any]]], ...] = (
        ("node", node_rows),
        ("edge", edge_rows),
    )
    for source_label, raw_rows in sides:
        groups = _group_rows_by_resource(raw_rows)
        for resource_id, group_rows in groups:
            entry = _build_diff_entry(
                group_rows,
                source=source_label,
                resource_id=resource_id,
                changed_only=changed_only,
            )
            if entry is None:
                continue
            if kind_filter is not None and entry.kind != kind_filter:
                continue
            if len(entries) >= _DIFF_HARD_CAP:
                truncated = True
                break
            entries.append(entry)
        if truncated:
            break
    return entries, truncated
