# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parametric large-graph generator for the G9.1 performance acceptance.

Initiative #363 (G9.1), Task #456 (T8). The Initiative's definition of
done requires a depth-16 traversal against a seeded **10k-node** graph
to return in well under 100 ms on the test fixture (documented, not
enforced as an SLO). This module owns the generator that builds that
graph so the shape is one reusable, parametric helper rather than an
inline loop copy-pasted across test modules.

Shape
=====

The generator builds a **hub-rooted forest**: one root ``host`` node
(``perf-hub`` by default) with ``fanout`` chains hanging off it, each
chain a straight line of ``vm`` nodes ``depth`` levels deep. Every edge
points from the *deeper* node toward the hub with ``kind="runs-on"`` so
a ``find_dependents`` (reverse) traversal rooted at the hub fans out
through the whole forest — the exact traversal the G9 blast-radius use
case runs ("what depends on this host").

Sizing
======

With ``fanout`` chains of ``depth`` nodes plus the single hub the graph
has ``1 + fanout * depth`` nodes and ``fanout * depth`` edges. The
acceptance preset :data:`TEN_K` picks ``fanout`` / ``per_chain`` so the
node count lands at ~10k with chains long enough that a depth-16 walk
only reaches a bounded slice (``1 + fanout * min(depth, per_chain)``) —
the recursive join, not the row count, is what the < 100 ms budget
measures. The ``graph_edge_tenant_to_idx`` /
``graph_edge_tenant_from_idx`` indexes migration ``0007`` ships are what
keep that join sub-linear per level.

Seeding cost is excluded from every performance assertion: the caller
seeds once (outside the timed region), warms the connection/plan with a
shallow query, then times the depth-16 traversal in isolation.

Dense meshes (#2535)
====================

The forest shape above has out-degree exactly 1 — no converging paths,
no cycles — which structurally cannot produce the load profile that
makes ``find_path``'s bidirectional walk expensive (simple-path
enumeration, ~branch_factor^hops on a mesh). :class:`MeshSpec` /
:func:`seed_mesh_graph` generate that adversarial shape: layered
meshes with configurable branch factor (converging paths), optional
back-edges (cycles), mixed edge kinds, and optional soft-deleted rows.
Used by ``tests/integration/test_topology_path_pruning.py`` to pin the
per-branch pruning win and the worst-case envelope documented in
``docs/architecture/topology.md`` §Performance expectations.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from meho_backplane.db.models import GraphEdge, GraphNode

__all__ = ["TEN_K", "GraphSpec", "MeshSpec", "seed_mesh_graph", "seed_perf_graph"]


@dataclass(frozen=True)
class GraphSpec:
    """A parametric description of a hub-rooted performance forest.

    Attributes:
        fanout: Number of chains hanging off the single hub node.
        per_chain: Nodes per chain (the chain's length / max depth).
        hub_name: Name of the root ``host`` node every chain points at.
        edge_kind: ``graph_edge.kind`` every chain edge carries. Kept a
            single auto-discoverable kind so a ``kind_filter`` traversal
            over the fixture stays meaningful.

    ``total_nodes`` / ``total_edges`` are derived so a test can assert
    the fixture is the size it claims without re-deriving the formula.
    """

    fanout: int
    per_chain: int
    hub_name: str = "perf-hub"
    edge_kind: str = "runs-on"

    @property
    def total_nodes(self) -> int:
        """``1`` hub ``+ fanout * per_chain`` chain nodes."""
        return 1 + self.fanout * self.per_chain

    @property
    def total_edges(self) -> int:
        """One edge per chain node (each points one step toward the hub)."""
        return self.fanout * self.per_chain

    def reachable_within(self, depth: int) -> int:
        """Nodes a depth-``depth`` reverse walk from the hub touches.

        The hub is depth 0; each chain contributes ``min(depth,
        per_chain)`` nodes within the budget. A test asserts the
        traversal returns exactly this many rows so a regression that
        silently truncates (or over-returns via a converging-path
        duplicate) fails loudly.
        """
        return 1 + self.fanout * min(depth, self.per_chain)


#: ~10k-node acceptance preset. 16 chains x 625 nodes + 1 hub = 10 001
#: nodes / 10 000 edges. Chains are longer than the depth-16 ceiling so
#: the timed traversal exercises the recursive join at every level
#: without the row count itself being the dominant cost.
TEN_K = GraphSpec(fanout=16, per_chain=625)


async def seed_perf_graph(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    spec: GraphSpec = TEN_K,
) -> uuid.UUID:
    """Seed *spec*'s forest under *tenant_id*; return the hub node id.

    Must run inside an open ``session.begin()`` block owned by the
    caller — the generator only ``add``s rows and ``flush``es the hub so
    the first chain edge has a valid endpoint; the caller controls the
    surrounding transaction (and excludes this call from any timed
    region). Nodes are flushed per chain step so each ``graph_edge``'s
    endpoint exists before the edge row is emitted, sidestepping the
    unit-of-work insert-ordering hazard the T4 query suite documents.

    Args:
        session: An open ``AsyncSession`` inside a ``session.begin()``.
        tenant_id: The tenant every seeded row is written under — the
            generator never crosses a tenant boundary.
        spec: The graph shape. Defaults to the ~10k :data:`TEN_K`
            preset; pass a smaller :class:`GraphSpec` for fast unit
            coverage of the generator itself.

    Returns:
        The ``graph_node.id`` of the hub — the anchor a
        ``find_dependents`` performance probe roots at.
    """
    hub_id = uuid.uuid4()
    session.add(
        GraphNode(
            id=hub_id,
            tenant_id=tenant_id,
            kind="host",
            name=spec.hub_name,
            source="auto",
            properties={"role": "perf-hub"},
            discovered_by="test",
        )
    )
    await session.flush()

    for chain in range(spec.fanout):
        prev = hub_id
        for level in range(spec.per_chain):
            node_id = uuid.uuid4()
            session.add(
                GraphNode(
                    id=node_id,
                    tenant_id=tenant_id,
                    kind="vm",
                    name=f"perf-{chain}-{level}",
                    source="auto",
                    properties={},
                    discovered_by="test",
                )
            )
            await session.flush()
            session.add(
                GraphEdge(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    from_node_id=node_id,
                    to_node_id=prev,
                    kind=spec.edge_kind,
                    source="auto",
                    discovered_by="test",
                )
            )
            prev = node_id

    return hub_id


@dataclass(frozen=True)
class MeshSpec:
    """A parametric description of a dense layered mesh (#2535).

    The hub-and-chains :class:`GraphSpec` forest has out-degree exactly
    1, no converging paths, and no cycles — the exact opposite of the
    load profile that makes ``find_path``'s bidirectional walk
    expensive (the walk enumerates *simple paths*, which grow
    ~branch_factor^hops on a mesh). This spec generates that adversarial
    shape: ``layers`` rows of ``width`` nodes each, every node fanning
    out to ``branch_factor`` nodes of the next layer (cross-column, so
    paths both diverge and converge), plus optional back-edges (cycles)
    and optional soft-deleted rows.

    Attributes:
        layers: Number of node layers (the mesh's depth). Must be >= 2.
        width: Nodes per layer. Must be >= ``branch_factor`` so the
            per-node fanout targets ``branch_factor`` *distinct*
            next-layer columns.
        branch_factor: Forward edges per node: node ``(L, i)`` connects
            to ``(L+1, (i + j) % width)`` for ``j`` in
            ``0..branch_factor-1``. Each next-layer node therefore also
            *receives* ``branch_factor`` edges — converging paths, the
            case the forest fixture never exercises.
        cycle_stride: When > 0, every ``cycle_stride``-th column of
            every layer ``L >= 2`` gains a back-edge ``(L, i) ->
            (L-2, i)``, making the graph cyclic (exercises the CYCLE
            clause under load). ``0`` keeps the mesh acyclic.
        edge_kinds: Round-robin ``graph_edge.kind`` cycle applied over
            all generated edges, so a ``kind_filter`` walk over the
            fixture stays meaningful. Values must be valid
            ``GraphEdgeKind`` members.
        soft_delete_every: When > 0, every Nth forward edge is seeded
            soft-deleted (``last_seen=NULL``). Traversal verbs do not
            filter ``last_seen`` (last-refresh-wins, see
            ``docs/architecture/topology.md`` §Soft-delete semantics),
            so these rows still add walk load — which is exactly why
            the perf fixture must include them. Live edges carry a
            real ``last_seen`` timestamp. ``0`` seeds everything live.
        name_prefix: Node-name prefix; node ``(L, i)`` is named
            ``{name_prefix}-{L}-{i}`` (kind ``vm``).
    """

    layers: int
    width: int
    branch_factor: int = 2
    cycle_stride: int = 0
    edge_kinds: tuple[str, ...] = ("runs-on", "belongs-to")
    soft_delete_every: int = 0
    name_prefix: str = "mesh"

    def __post_init__(self) -> None:
        if self.layers < 2:
            raise ValueError("MeshSpec.layers must be >= 2")
        if self.branch_factor < 1 or self.branch_factor > self.width:
            raise ValueError("MeshSpec.branch_factor must be in 1..width")
        if not self.edge_kinds:
            raise ValueError("MeshSpec.edge_kinds must be non-empty")

    @property
    def total_nodes(self) -> int:
        """``layers * width`` — the mesh has no extra hub node."""
        return self.layers * self.width

    @property
    def total_forward_edges(self) -> int:
        """``(layers - 1) * width * branch_factor`` forward edges."""
        return (self.layers - 1) * self.width * self.branch_factor

    @property
    def total_cycle_edges(self) -> int:
        """Back-edges added by ``cycle_stride`` (0 when acyclic)."""
        if self.cycle_stride <= 0:
            return 0
        per_layer = -(-self.width // self.cycle_stride)  # ceil division
        return (self.layers - 2) * per_layer

    @property
    def total_edges(self) -> int:
        """Forward plus cycle edges."""
        return self.total_forward_edges + self.total_cycle_edges

    def node_name(self, layer: int, col: int) -> str:
        """Canonical name of the node at ``(layer, col)``."""
        return f"{self.name_prefix}-{layer}-{col}"


async def seed_mesh_graph(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    spec: MeshSpec,
) -> dict[str, uuid.UUID]:
    """Seed *spec*'s mesh under *tenant_id*; return ``name -> node id``.

    Same transaction contract as :func:`seed_perf_graph`: the caller
    owns the surrounding ``session.begin()`` block and excludes this
    call from any timed region. All nodes are flushed before any edge
    is added so the unit-of-work never emits an edge ahead of its
    endpoint row.

    Args:
        session: An open ``AsyncSession`` inside a ``session.begin()``.
        tenant_id: The tenant every seeded row is written under.
        spec: The mesh shape (see :class:`MeshSpec`).

    Returns:
        Mapping of every node's name to its ``graph_node.id`` so a test
        can anchor a walk at any mesh position without re-deriving the
        naming scheme.
    """
    now = datetime.now(UTC)
    ids: dict[str, uuid.UUID] = {}
    for layer in range(spec.layers):
        for col in range(spec.width):
            node_id = uuid.uuid4()
            ids[spec.node_name(layer, col)] = node_id
            session.add(
                GraphNode(
                    id=node_id,
                    tenant_id=tenant_id,
                    kind="vm",
                    name=spec.node_name(layer, col),
                    source="auto",
                    properties={},
                    discovered_by="test",
                )
            )
    await session.flush()

    def _add_edge(edge_index: int, from_id: uuid.UUID, to_id: uuid.UUID) -> None:
        soft_deleted = spec.soft_delete_every > 0 and (
            (edge_index + 1) % spec.soft_delete_every == 0
        )
        session.add(
            GraphEdge(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                from_node_id=from_id,
                to_node_id=to_id,
                kind=spec.edge_kinds[edge_index % len(spec.edge_kinds)],
                source="auto",
                discovered_by="test",
                last_seen=None if soft_deleted else now,
            )
        )

    edge_index = 0
    for layer in range(spec.layers - 1):
        for col in range(spec.width):
            for j in range(spec.branch_factor):
                _add_edge(
                    edge_index,
                    ids[spec.node_name(layer, col)],
                    ids[spec.node_name(layer + 1, (col + j) % spec.width)],
                )
                edge_index += 1

    if spec.cycle_stride > 0:
        for layer in range(2, spec.layers):
            for col in range(0, spec.width, spec.cycle_stride):
                _add_edge(
                    edge_index,
                    ids[spec.node_name(layer, col)],
                    ids[spec.node_name(layer - 2, col)],
                )
                edge_index += 1

    return ids
