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
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from meho_backplane.db.models import GraphEdge, GraphNode

__all__ = ["TEN_K", "GraphSpec", "seed_perf_graph"]


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
