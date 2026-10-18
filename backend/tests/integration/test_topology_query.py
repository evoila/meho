# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration tests for the recursive-CTE topology query verbs.

Task #451 (G9.1-T4) acceptance suite. Every test runs against a real
``pgvector/pgvector:pg16`` container because the query module uses
PostgreSQL's ``WITH RECURSIVE ... CYCLE`` clause, which SQLite (the
unit suite's per-test ``alembic upgrade head`` DB) does not implement.
This is the same real-PG rationale, fixture wiring, and Docker-gated
skip that :mod:`tests.integration.test_tenant_isolation` documents:
the ``pg_engine`` fixture in :mod:`tests.integration.conftest` boots
the container, migrates it to head, truncates the graph tables, and
seeds the two pinned tenants ``TENANT_A_ID`` / ``TENANT_B_ID``. CI
runners have Docker and run the whole class; agent sandboxes without
Docker skip cleanly.

Why every test body is ``async def`` with no ``@pytest.mark.asyncio``:
``backend/pyproject.toml`` pins ``asyncio_mode = "auto"`` so the
plugin treats every ``async def`` test as a coroutine test on the
session loop the ``pg_engine`` asyncpg pool is bound to — same shape
as the rest of ``tests/integration/``.

Coverage matrix (mirrors the Task #451 acceptance criteria):

* ``find_dependents`` against a seeded 5-node / 6-edge graph returns
  the correct reverse closure ordered by depth.
* ``find_dependencies`` mirrors that in the forward direction.
* ``find_path`` returns the shortest path between reachable nodes and
  ``None`` when unreachable within ``max_hops``.
* The CYCLE clause makes an ``A → B → A`` graph terminate instead of
  recursing forever.
* ``kind_filter`` restricts the traversal to one edge kind.
* The tenant boundary holds: a same-named node in tenant B is invisible
  to a tenant-A query.
* A bare-name anchor that resolves to two kinds in one tenant raises
  ``AmbiguousNodeError``; an explicit ``kind`` pins the right closure.
* The converging-DAG dedupe holds: a node reachable by several paths
  appears exactly once at its minimum depth.
* A 10k-node fixture keeps a depth-16 traversal within a bounded
  multiple of a depth-1 baseline (a runner-load-invariant ratio gate
  that trips on a dropped-index regression but not on CI-runner noise).
* #2538 chain provenance: closure rows carry ``parent_node_id`` /
  ``via_edge_id`` (NULL on the root) that reconstruct the exact edge
  chain from the flat list, deterministically for converging
  equal-depth parents; path nodes carry ``via_edge_id`` per hop.
* #2538 staleness: ``include_stale=True`` (default) keeps soft-deleted
  nodes/edges walkable on all three verbs; ``False`` excludes them —
  including the reversed ``bi_edge`` leg of ``find_path``.
* #3760: ``find_path`` keeps undirected as the default and applies
  ``forward`` / ``reverse``; ``relation_class`` keeps only reviewed
  kinds; rows carry ``last_seen`` and the walked edge's source and
  ``last_seen``; a missing endpoint can be told apart from no route.
"""

from __future__ import annotations

import json
import secrets
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, event, select, text

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db import engine as engine_module
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphEdge, GraphNode, Target
from meho_backplane.topology import query as query_module
from meho_backplane.topology.evidence import classify_freshness
from meho_backplane.topology.query import (
    AmbiguousNodeError,
    ClosureBudget,
    bounded_closure,
    find_dependencies,
    find_dependents,
    find_path,
    list_edges,
)
from meho_backplane.topology.resolvers import NodeNotFoundError
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON

# Match the tenant rows the ``pg_engine`` conftest fixture seeds.
TENANT_A_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
TENANT_B_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")

_skip_no_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

# Perf-gate calibration for ``test_depth_16_traversal_scales_sub_linearly_on_10k_nodes``
# (#1434). See that test's docstring for the measured healthy (~1.8) vs
# regressed (~11.4) ratios these thresholds bracket.
_PERF_SAMPLES = 7  # median over an odd count drops a single spike
_PERF_RATIO_CEILING = 6.0  # depth-16 / depth-1; ~3.3x over healthy, ~1.9x under regressed
_PERF_ABS_CEILING_MS = 2000.0  # secondary catastrophic-regression backstop, not the gate


def _operator(tenant_id: uuid.UUID) -> Operator:
    """Build a minimal :class:`Operator` pinned to *tenant_id*."""
    return Operator(
        sub="op-topology",
        name=None,
        email=None,
        raw_jwt="not-a-real-jwt",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


async def _seed_node(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    name: str,
    last_seen: datetime | None = None,
) -> uuid.UUID:
    """Insert one ``graph_node`` and return its id.

    Flushes immediately so the row exists before any ``graph_edge``
    that references it is added — SQLAlchemy's unit-of-work otherwise
    batches inserts in an order that can emit the edge before its
    endpoint node and trip the ``REFERENCES graph_node(id)`` FK.

    ``last_seen`` defaults to ``None`` (the pre-#2538 fixture shape —
    fine for every test that keeps the default ``include_stale=True``
    walk); the staleness tests pass an explicit timestamp so the row
    counts as *live* under ``include_stale=False``.
    """
    node = GraphNode(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        kind=kind,
        name=name,
        source="auto",
        properties={"seeded": name},
        discovered_by="test",
        last_seen=last_seen,
    )
    session.add(node)
    await session.flush()
    return node.id


async def _seed_edge(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    kind: str,
    last_seen: datetime | None = None,
) -> uuid.UUID:
    """Insert one ``graph_edge`` (``source='auto'``) and return its id.

    The returned id is what the #2538 chain-provenance assertions pin
    ``via_edge_id`` against. ``last_seen`` follows the same convention
    as :func:`_seed_node`.
    """
    edge = GraphEdge(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        from_node_id=from_id,
        to_node_id=to_id,
        kind=kind,
        source="auto",
        discovered_by="test",
        last_seen=last_seen,
    )
    session.add(edge)
    return edge.id


@pytest.fixture
async def known_graph(pg_engine: None) -> AsyncIterator[dict[str, uuid.UUID]]:
    """Seed the canonical 5-node / 6-edge graph in tenant A.

    Shape (edge ``from`` depends on ``to``)::

        app  --belongs-to-->  vm1   --runs-on-->  host1  --mounts-->  ds1
        app  --belongs-to-->  vm2   --runs-on-->  host1
        vm1  --mounts------->  ds1

    Nodes: app (target), vm1 (vm), vm2 (vm), host1 (host), ds1
    (datastore). Edges: 6 total — two belongs-to, two runs-on, two
    mounts. The shape gives a multi-depth reverse closure on host1
    (vm1/vm2 at depth 1, app at depth 2) and a multi-depth forward
    closure on app, plus a kind-filterable subgraph (the two mounts
    edges).
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        app = await _seed_node(session, tenant_id=TENANT_A_ID, kind="target", name="app")
        vm1 = await _seed_node(session, tenant_id=TENANT_A_ID, kind="vm", name="vm1")
        vm2 = await _seed_node(session, tenant_id=TENANT_A_ID, kind="vm", name="vm2")
        host1 = await _seed_node(session, tenant_id=TENANT_A_ID, kind="host", name="host1")
        ds1 = await _seed_node(session, tenant_id=TENANT_A_ID, kind="datastore", name="ds1")
        e_app_vm1 = await _seed_edge(
            session, tenant_id=TENANT_A_ID, from_id=app, to_id=vm1, kind="belongs-to"
        )
        e_app_vm2 = await _seed_edge(
            session, tenant_id=TENANT_A_ID, from_id=app, to_id=vm2, kind="belongs-to"
        )
        e_vm1_host1 = await _seed_edge(
            session, tenant_id=TENANT_A_ID, from_id=vm1, to_id=host1, kind="runs-on"
        )
        e_vm2_host1 = await _seed_edge(
            session, tenant_id=TENANT_A_ID, from_id=vm2, to_id=host1, kind="runs-on"
        )
        e_host1_ds1 = await _seed_edge(
            session, tenant_id=TENANT_A_ID, from_id=host1, to_id=ds1, kind="mounts"
        )
        e_vm1_ds1 = await _seed_edge(
            session, tenant_id=TENANT_A_ID, from_id=vm1, to_id=ds1, kind="mounts"
        )

    yield {
        "app": app,
        "vm1": vm1,
        "vm2": vm2,
        "host1": host1,
        "ds1": ds1,
        # Edge ids for the #2538 chain-provenance assertions.
        "e_app_vm1": e_app_vm1,
        "e_app_vm2": e_app_vm2,
        "e_vm1_host1": e_vm1_host1,
        "e_vm2_host1": e_vm2_host1,
        "e_host1_ds1": e_host1_ds1,
        "e_vm1_ds1": e_vm1_ds1,
    }


@_skip_no_docker
async def test_find_dependents_returns_reverse_closure(
    known_graph: dict[str, uuid.UUID],
) -> None:
    """Everything that depends on ``host1`` at depth <= 16.

    vm1 and vm2 run on host1 (depth 1); app belongs to both vms so it
    transitively depends on host1 (depth 2). The root host1 is depth 0.
    """
    nodes = await find_dependents(_operator(TENANT_A_ID), "host1")

    # One row per reachable node — NOT one per path. `app` is reachable
    # from host1 through both vm1 and vm2; a per-path traversal returns
    # it twice. The Counter assertion (rather than a set) is what makes
    # the converging-DAG duplicate fail loudly instead of being masked.
    assert Counter(n.name for n in nodes) == Counter({"host1": 1, "vm1": 1, "vm2": 1, "app": 1})

    by_depth: dict[int, set[str]] = {}
    for n in nodes:
        by_depth.setdefault(n.depth, set()).add(n.name)

    assert by_depth[0] == {"host1"}
    assert by_depth[1] == {"vm1", "vm2"}
    # app is collapsed to its minimum-depth occurrence (depth 2).
    assert by_depth[2] == {"app"}
    # Result is ordered by (depth, name).
    assert [n.depth for n in nodes] == sorted(n.depth for n in nodes)
    # via_edge_kind: root has none; the depth-1 hops came over runs-on.
    root = next(n for n in nodes if n.name == "host1")
    assert root.via_edge_kind is None
    assert all(n.via_edge_kind == "runs-on" for n in nodes if n.depth == 1)
    # #2538 chain provenance: the root carries NULLs; every depth-1 row
    # hangs off the root via its exact runs-on edge.
    assert root.parent_node_id is None
    assert root.via_edge_id is None
    vm1_row = next(n for n in nodes if n.name == "vm1")
    vm2_row = next(n for n in nodes if n.name == "vm2")
    assert vm1_row.parent_node_id == known_graph["host1"]
    assert vm1_row.via_edge_id == known_graph["e_vm1_host1"]
    assert vm2_row.parent_node_id == known_graph["host1"]
    assert vm2_row.via_edge_id == known_graph["e_vm2_host1"]
    # app converges via vm1 and vm2 at equal depth; whichever parent
    # won the deterministic tie-break, the pair must be a real seeded
    # (parent, edge) combination pointing at an in-result parent.
    app_row = next(n for n in nodes if n.name == "app")
    assert (app_row.parent_node_id, app_row.via_edge_id) in {
        (known_graph["vm1"], known_graph["e_app_vm1"]),
        (known_graph["vm2"], known_graph["e_app_vm2"]),
    }


@_skip_no_docker
async def test_find_dependencies_returns_forward_closure(
    known_graph: dict[str, uuid.UUID],
) -> None:
    """Everything ``app`` depends on, forward direction.

    app -> vm1, vm2 (depth 1); vm1/vm2 -> host1 and vm1 -> ds1 (depth
    2); host1 -> ds1 (depth 3 via the vm->host->ds path).
    """
    nodes = await find_dependencies(_operator(TENANT_A_ID), "app")
    names = {n.name for n in nodes}

    assert names == {"app", "vm1", "vm2", "host1", "ds1"}
    depth_by_name: dict[str, int] = {}
    for n in nodes:
        depth_by_name[n.name] = min(n.depth, depth_by_name.get(n.name, n.depth))
    assert depth_by_name["app"] == 0
    assert depth_by_name["vm1"] == 1
    assert depth_by_name["vm2"] == 1
    assert depth_by_name["host1"] == 2
    assert depth_by_name["ds1"] == 2  # vm1 --mounts--> ds1 is depth 2


@_skip_no_docker
async def test_find_path_returns_shortest_path(
    known_graph: dict[str, uuid.UUID],
) -> None:
    """A path from ``app`` to ``ds1`` exists and is the shortest one.

    Shortest is app -> vm1 -> ds1 (2 hops) via the vm1--mounts-->ds1
    edge; the app -> vm1 -> host1 -> ds1 route is 3 hops.
    """
    path = await find_path(_operator(TENANT_A_ID), "app", "ds1")

    assert path is not None
    assert path.total_hops == 2
    assert path.nodes[0].name == "app"
    assert path.nodes[0].depth == 0
    assert path.nodes[0].via_edge_kind is None
    assert path.nodes[-1].name == "ds1"
    assert path.nodes[-1].depth == 2
    assert len(path.nodes) == 3


@_skip_no_docker
async def test_find_path_returns_none_when_unreachable(
    known_graph: dict[str, uuid.UUID],
) -> None:
    """A 1-hop ceiling cannot reach ds1 from app (shortest is 2 hops)."""
    path = await find_path(_operator(TENANT_A_ID), "app", "ds1", max_hops=1)
    assert path is None

    # Genuinely absent target also yields None.
    missing = await find_path(_operator(TENANT_A_ID), "app", "no-such-node")
    assert missing is None


# ---------------------------------------------------------------------------
# #2538 — chain provenance (parent_node_id / via_edge_id) + include_stale
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_find_path_nodes_carry_via_edge_ids(
    known_graph: dict[str, uuid.UUID],
) -> None:
    """Path nodes identify the exact edge of every hop (#2538).

    Shortest app → ds1 is app -> vm1 -> ds1; node ``i``'s
    ``via_edge_id`` is the seeded edge of hop ``i`` and
    ``parent_node_id`` the previous node — the positional
    ``via_edge_kind`` derivation is unchanged alongside.
    """
    path = await find_path(_operator(TENANT_A_ID), "app", "ds1")

    assert path is not None
    root, mid, last = path.nodes
    assert (root.parent_node_id, root.via_edge_id) == (None, None)
    assert root.via_edge_kind is None
    assert mid.parent_node_id == known_graph["app"]
    assert mid.via_edge_id == known_graph["e_app_vm1"]
    assert mid.via_edge_kind == "belongs-to"
    assert last.parent_node_id == known_graph["vm1"]
    assert last.via_edge_id == known_graph["e_vm1_ds1"]
    assert last.via_edge_kind == "mounts"


@pytest.fixture
async def diamond_graph(pg_engine: None) -> AsyncIterator[dict[str, uuid.UUID]]:
    """Seed a 3-hop diamond in tenant A (every row live, ``last_seen`` set).

    Shape (edge ``from`` depends on ``to``)::

        leaf --uses--> mid-b --uses--> neck --uses--> root
        leaf --uses--> mid-c --uses--> neck

    Reverse closure from ``root``: neck at depth 1, mid-b / mid-c at
    depth 2, leaf at depth 3 with two converging equal-depth parents —
    the exact tie-break case the deterministic DISTINCT ON ordering
    must pin.
    """
    now = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        ids: dict[str, uuid.UUID] = {}
        for name in ("root", "neck", "mid-b", "mid-c", "leaf"):
            ids[name] = await _seed_node(
                session, tenant_id=TENANT_A_ID, kind="service", name=name, last_seen=now
            )
        for edge_key, from_name, to_name in (
            ("e_neck_root", "neck", "root"),
            ("e_b_neck", "mid-b", "neck"),
            ("e_c_neck", "mid-c", "neck"),
            ("e_leaf_b", "leaf", "mid-b"),
            ("e_leaf_c", "leaf", "mid-c"),
        ):
            ids[edge_key] = await _seed_edge(
                session,
                tenant_id=TENANT_A_ID,
                from_id=ids[from_name],
                to_id=ids[to_name],
                kind="uses",
                last_seen=now,
            )
    yield ids


@_skip_no_docker
async def test_closure_chain_reconstructs_from_provenance_alone(
    diamond_graph: dict[str, uuid.UUID],
) -> None:
    """Acceptance (#2538): the flat closure list rebuilds the exact edge
    chain from ``parent_node_id`` / ``via_edge_id`` alone, and the
    converging equal-depth parents of ``leaf`` resolve identically
    across repeated runs.
    """
    seeded_edges = {
        diamond_graph[k]: (diamond_graph[f], diamond_graph[t])
        for k, f, t in (
            ("e_neck_root", "neck", "root"),
            ("e_b_neck", "mid-b", "neck"),
            ("e_c_neck", "mid-c", "neck"),
            ("e_leaf_b", "leaf", "mid-b"),
            ("e_leaf_c", "leaf", "mid-c"),
        )
    }

    nodes = await find_dependents(_operator(TENANT_A_ID), "root")
    by_id = {n.id: n for n in nodes}
    assert {n.name for n in nodes} == {"root", "neck", "mid-b", "mid-c", "leaf"}

    for n in nodes:
        if n.depth == 0:
            assert (n.parent_node_id, n.via_edge_id) == (None, None)
            continue
        # The parent is itself a closure row, one hop shallower.
        assert n.parent_node_id in by_id
        assert by_id[n.parent_node_id].depth == n.depth - 1
        # The provenance edge is the seeded edge child --uses--> parent
        # (reverse walk: the edge points INTO the parent).
        assert n.via_edge_id is not None
        assert seeded_edges[n.via_edge_id] == (n.id, n.parent_node_id)

    # Determinism: leaf's two parents (mid-b / mid-c) tie at depth 2;
    # repeated runs must keep the same provenance pair, not flip
    # arbitrarily between converging branches.
    first = [(n.id, n.parent_node_id, n.via_edge_id) for n in nodes]
    for _ in range(4):
        again = await find_dependents(_operator(TENANT_A_ID), "root")
        assert [(n.id, n.parent_node_id, n.via_edge_id) for n in again] == first


@pytest.fixture
async def stale_chain(pg_engine: None) -> AsyncIterator[dict[str, uuid.UUID]]:
    """Seed the live 2-hop chain the staleness tests mutate.

    ``leaf --uses--> mid --uses--> root``, every node and edge live
    (``last_seen`` set). Individual tests soft-delete one row and
    assert the ``include_stale`` contract on all three verbs.
    """
    now = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        ids: dict[str, uuid.UUID] = {}
        for name in ("root", "mid", "leaf"):
            ids[name] = await _seed_node(
                session, tenant_id=TENANT_A_ID, kind="service", name=name, last_seen=now
            )
        ids["e_mid_root"] = await _seed_edge(
            session,
            tenant_id=TENANT_A_ID,
            from_id=ids["mid"],
            to_id=ids["root"],
            kind="uses",
            last_seen=now,
        )
        ids["e_leaf_mid"] = await _seed_edge(
            session,
            tenant_id=TENANT_A_ID,
            from_id=ids["leaf"],
            to_id=ids["mid"],
            kind="uses",
            last_seen=now,
        )
    yield ids


async def _soft_delete_node(node_id: uuid.UUID) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        node = await session.get(GraphNode, node_id)
        assert node is not None
        node.last_seen = None


async def _soft_delete_edge(edge_id: uuid.UUID) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        edge = await session.get(GraphEdge, edge_id)
        assert edge is not None
        edge.last_seen = None


@_skip_no_docker
async def test_include_stale_toggle_with_soft_deleted_node(
    stale_chain: dict[str, uuid.UUID],
) -> None:
    """Acceptance (#2538): a soft-deleted node stays reachable under the
    default ``include_stale=True`` (last-refresh-wins) and drops out of
    all three verbs under ``include_stale=False``.
    """
    op = _operator(TENANT_A_ID)
    await _soft_delete_node(stale_chain["mid"])

    # Default: the stale node (and everything behind it) stays visible.
    dependents = await find_dependents(op, "root")
    assert {n.name for n in dependents} == {"root", "mid", "leaf"}
    dependencies = await find_dependencies(op, "leaf")
    assert {n.name for n in dependencies} == {"leaf", "mid", "root"}
    path = await find_path(op, "leaf", "root")
    assert path is not None and path.total_hops == 2

    # Opt-out: the stale node is not walked — and nothing beyond it.
    dependents = await find_dependents(op, "root", include_stale=False)
    assert {n.name for n in dependents} == {"root"}
    dependencies = await find_dependencies(op, "leaf", include_stale=False)
    assert {n.name for n in dependencies} == {"leaf"}
    assert await find_path(op, "leaf", "root", include_stale=False) is None


@_skip_no_docker
async def test_include_stale_toggle_with_soft_deleted_edge(
    stale_chain: dict[str, uuid.UUID],
) -> None:
    """Acceptance (#2538): a soft-deleted edge behaves like the node case
    — walked by default, excluded under ``include_stale=False`` on all
    three verbs.
    """
    op = _operator(TENANT_A_ID)
    await _soft_delete_edge(stale_chain["e_mid_root"])

    dependents = await find_dependents(op, "root")
    assert {n.name for n in dependents} == {"root", "mid", "leaf"}
    path = await find_path(op, "leaf", "root")
    assert path is not None

    dependents = await find_dependents(op, "root", include_stale=False)
    assert {n.name for n in dependents} == {"root"}
    # The other edge (leaf -> mid) is still live, so the forward walk
    # from leaf legitimately reaches mid — only the stale hop is cut.
    dependencies = await find_dependencies(op, "leaf", include_stale=False)
    assert {n.name for n in dependencies} == {"leaf", "mid"}
    assert await find_path(op, "leaf", "root", include_stale=False) is None


@_skip_no_docker
async def test_include_stale_false_filters_reversed_bi_edge_leg(
    stale_chain: dict[str, uuid.UUID],
) -> None:
    """Acceptance (#2538): a stale edge must not be walked *backwards*
    into a path. ``find_path(root, leaf)`` traverses both seeded edges
    against their direction (the reversed ``bi_edge`` leg); with
    ``e_leaf_mid`` soft-deleted the reversed walk must dead-end even
    though the forward leg predicate never sees that hop direction.
    """
    op = _operator(TENANT_A_ID)
    await _soft_delete_edge(stale_chain["e_leaf_mid"])

    # Default keeps the reversed-leg hop walkable.
    path = await find_path(op, "root", "leaf")
    assert path is not None and path.total_hops == 2

    assert await find_path(op, "root", "leaf", include_stale=False) is None


@_skip_no_docker
async def test_include_stale_false_exempts_both_path_endpoints(
    stale_chain: dict[str, uuid.UUID],
) -> None:
    """Acceptance (#2538, PR #2561 B1): the two endpoints named by the
    caller are exempt from the staleness filter, so the undirected
    search agrees on reachability in both argument orders even when an
    endpoint is soft-deleted. Soft-deleting ``root`` exercises the
    exemption on both sides: as the ``to`` endpoint (recursive-term
    ``target``-CTE exemption) and as the ``from`` endpoint (base term).
    """
    op = _operator(TENANT_A_ID)
    await _soft_delete_node(stale_chain["root"])

    forward = await find_path(op, "leaf", "root", include_stale=False)
    reverse = await find_path(op, "root", "leaf", include_stale=False)
    assert forward is not None and reverse is not None
    assert forward.total_hops == 2 and reverse.total_hops == 2
    assert [n.name for n in forward.nodes] == ["leaf", "mid", "root"]
    assert [n.name for n in reverse.nodes] == ["root", "mid", "leaf"]


@_skip_no_docker
async def test_include_stale_false_still_returns_a_stale_closure_root(
    stale_chain: dict[str, uuid.UUID],
) -> None:
    """The closure root is never filtered by ``include_stale`` (#2538): a
    soft-deleted root still comes back at depth 0 with ``last_seen``
    NULL, so the MCP ``evidence`` block has to name that exception
    (#3760). Reached as a non-root row, the same stale node is dropped.
    """
    op = _operator(TENANT_A_ID)
    await _soft_delete_node(stale_chain["root"])

    dependents = await find_dependents(op, "root", include_stale=False)
    assert [n.name for n in dependents] == ["root", "mid", "leaf"]
    assert dependents[0].depth == 0 and dependents[0].last_seen is None

    dependencies = await find_dependencies(op, "leaf", include_stale=False)
    assert {n.name for n in dependencies} == {"leaf", "mid"}


@_skip_no_docker
async def test_find_dependents_untracked_vs_tracked_no_deps(
    known_graph: dict[str, uuid.UUID],
) -> None:
    """Mirror the RDC #789 N2 repro: ``[]`` must not conflate untracked
    with tracked-but-no-dependents (G0.18-T4 #1357).

    The ``known_graph`` fixture seeds ``app`` with only outgoing edges
    (``app --belongs-to--> {vm1, vm2}``) — nothing depends on ``app``
    in the reverse-closure direction, so ``find_dependents("app")``
    returns the one-element ``[app]`` (the substrate's depth-0 anchor
    row). A non-existent ``vault-prod`` target — the exact shape a
    registered non-k8s target takes today because auto-discovery is
    k8s-only — raises :class:`NodeNotFoundError` rather than returning
    the bare ``[]`` the pre-fix behaviour produced.

    The pre-G0.18-T4 implementation returned the same empty list for
    both, and the consumer's pre-destructive blast-radius check read
    the empty list as "safe to delete," a false-negative SEV-3.
    """
    tracked = await find_dependents(_operator(TENANT_A_ID), "app")
    assert [n.name for n in tracked] == ["app"]
    assert tracked[0].depth == 0

    with pytest.raises(NodeNotFoundError) as excinfo:
        await find_dependents(_operator(TENANT_A_ID), "vault-prod")
    assert excinfo.value.name == "vault-prod"

    # Same contract on the forward verb.
    with pytest.raises(NodeNotFoundError):
        await find_dependencies(_operator(TENANT_A_ID), "vault-prod")


@_skip_no_docker
async def test_find_dependents_cross_tenant_node_raises_node_not_found(
    known_graph: dict[str, uuid.UUID],
) -> None:
    """A node that exists only in tenant B is untracked from tenant A.

    Tenant boundary already isolated cross-tenant nodes from the
    closure return (the empty-list contract). G0.18-T4 (#1357)
    upgrades that to the typed :class:`NodeNotFoundError` so the
    operator-facing surface reads "untracked here" rather than the
    misleading "exists with no dependents." Cross-tenant nodes are
    never visible regardless.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        await _seed_node(session, tenant_id=TENANT_B_ID, kind="host", name="cross-tenant-host")

    with pytest.raises(NodeNotFoundError):
        await find_dependents(_operator(TENANT_A_ID), "cross-tenant-host")


@_skip_no_docker
async def test_cycle_detection_terminates(pg_engine: None) -> None:
    """An ``A -> B -> A`` cycle does not infinite-loop.

    Without the CYCLE clause this traversal would recurse until the
    server stack/working-memory blew. With it, PostgreSQL stops
    recursing into an already-visited node on the branch and the
    query returns. The result is the finite reachable set, not an
    error and not a hang.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        a = await _seed_node(session, tenant_id=TENANT_A_ID, kind="vm", name="cyc-a")
        b = await _seed_node(session, tenant_id=TENANT_A_ID, kind="vm", name="cyc-b")
        await _seed_edge(session, tenant_id=TENANT_A_ID, from_id=a, to_id=b, kind="runs-on")
        await _seed_edge(session, tenant_id=TENANT_A_ID, from_id=b, to_id=a, kind="runs-on")

    # Bounded wall-clock guard: a non-terminating traversal would blow
    # well past this; a correct one is sub-second.
    started = time.monotonic()
    deps = await find_dependencies(_operator(TENANT_A_ID), "cyc-a")
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    names = {n.name for n in deps}
    assert names == {"cyc-a", "cyc-b"}

    path = await find_path(_operator(TENANT_A_ID), "cyc-a", "cyc-b")
    assert path is not None
    assert path.total_hops == 1


@_skip_no_docker
async def test_kind_filter_restricts_traversal(
    known_graph: dict[str, uuid.UUID],
) -> None:
    """``kind_filter='runs-on'`` walks only runs-on edges.

    From host1, reverse traversal restricted to runs-on reaches vm1
    and vm2 (the two runs-on edges) but NOT app — app's edges to the
    vms are belongs-to, so the filtered walk cannot step past the vms.
    """
    nodes = await find_dependents(_operator(TENANT_A_ID), "host1", kind_filter="runs-on")
    names = {n.name for n in nodes}
    assert names == {"host1", "vm1", "vm2"}
    assert "app" not in names


@_skip_no_docker
async def test_tenant_boundary_isolates_same_named_node(
    known_graph: dict[str, uuid.UUID],
) -> None:
    """A ``host1`` in tenant B is invisible to tenant A's query.

    Seed an unrelated tenant-B graph that also contains a node named
    ``host1`` with a tenant-B dependent. Tenant A's find_dependents on
    ``host1`` must return only tenant A's closure; tenant B's must
    return only tenant B's.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        b_host = await _seed_node(session, tenant_id=TENANT_B_ID, kind="host", name="host1")
        b_vm = await _seed_node(session, tenant_id=TENANT_B_ID, kind="vm", name="tenant-b-only-vm")
        await _seed_edge(
            session,
            tenant_id=TENANT_B_ID,
            from_id=b_vm,
            to_id=b_host,
            kind="runs-on",
        )

    a_nodes = await find_dependents(_operator(TENANT_A_ID), "host1")
    a_names = {n.name for n in a_nodes}
    assert "tenant-b-only-vm" not in a_names
    assert a_names == {"host1", "vm1", "vm2", "app"}

    b_nodes = await find_dependents(_operator(TENANT_B_ID), "host1")
    b_names = {n.name for n in b_nodes}
    assert b_names == {"host1", "tenant-b-only-vm"}
    assert "app" not in b_names


async def _median_traversal_ms(
    operator: Operator,
    *,
    depth: int,
    samples: int = _PERF_SAMPLES,
) -> tuple[float, list[Any]]:
    """Median wall-clock (ms) of ``samples`` depth-bounded traversals.

    Returns the median elapsed time *and* the last result list so the
    caller can assert on reachable node count. The median (rather than
    a single shot or the mean) discards the occasional GC / scheduler
    spike that a CI runner injects mid-run without needing an outlier
    filter.
    """
    elapsed: list[float] = []
    nodes: list[Any] = []
    for _ in range(samples):
        started = time.monotonic()
        nodes = await find_dependents(operator, "perf-hub", depth=depth)
        elapsed.append((time.monotonic() - started) * 1000.0)
    elapsed.sort()
    return elapsed[len(elapsed) // 2], nodes


@_skip_no_docker
async def test_depth_16_traversal_scales_sub_linearly_on_10k_nodes(
    pg_engine: None,
) -> None:
    """A 10k-node fixture: depth-16 stays within a bounded multiple of depth-1.

    Build a wide-but-shallow forest: 16 chains of ~625 nodes each
    rooted at a single hub so a depth-16 dependents traversal from the
    hub touches the whole structure. The ``graph_edge_tenant_to_idx`` /
    ``graph_edge_tenant_from_idx`` indexes migration 0007 ships are
    what keep the recursive join sub-linear per level — that is the
    property under test.

    Why a **ratio** gate, not an absolute wall-clock ceiling
    (#1434): the original ``assert elapsed_ms < 100.0`` was calibrated
    to a fast, quiet machine and flaked ~2.5x over budget on shared CI
    runners (observed 252-266 ms), red-flagging the required
    ``Python (integration testcontainers)`` lane on unrelated PRs.
    Absolute timing on a shared runner is dominated by host contention,
    not algorithmic cost. The fix measures *scaling* instead: depth-1
    and depth-16 are timed in the same run, so runner load inflates
    both denominators and numerator together and divides out. The ratio
    is therefore load-invariant while still catching the regression
    this test exists to catch.

    Calibration (measured on the seeded fixture):

    * healthy (indexes present): depth-16 / depth-1 ~= 1.8
    * regressed (both 0007 indexes dropped → per-level sequential scan):
      depth-16 / depth-1 ~= 11.4

    The ``_PERF_RATIO_CEILING`` of 6.0 sits ~3.3x above the healthy
    ratio (ample headroom for runner jitter) and ~1.9x below the
    regressed ratio, so dropping either index trips it deterministically
    regardless of host speed. ``_PERF_ABS_CEILING_MS`` is a generous
    secondary smoke signal, not the load-bearing gate — it backstops a
    catastrophic regression that somehow also slowed the depth-1
    baseline (which would mask the ratio). This mirrors the precedent
    in ``test_topology_history_integration.py`` (see
    ``docs/codebase/topology.md`` §Indexes): the load-invariant
    property is primary, a wide wall-clock bound rides along.
    """
    total = 10_000
    chains = 16
    per_chain = (total - 1) // chains  # ~624 nodes per chain + 1 hub

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        hub = await _seed_node(session, tenant_id=TENANT_A_ID, kind="host", name="perf-hub")
        for c in range(chains):
            prev = hub
            for d in range(per_chain):
                nxt = await _seed_node(
                    session,
                    tenant_id=TENANT_A_ID,
                    kind="vm",
                    name=f"perf-{c}-{d}",
                )
                # Edge points from the deeper node toward the hub so a
                # *dependents* (reverse) walk from the hub fans out
                # through the whole forest.
                await _seed_edge(
                    session,
                    tenant_id=TENANT_A_ID,
                    from_id=nxt,
                    to_id=prev,
                    kind="runs-on",
                )
                prev = nxt

    operator = _operator(TENANT_A_ID)
    # Warm the connection/plan once so the timed runs measure steady
    # state, not first-call connection setup.
    await find_dependents(operator, "perf-hub", depth=1)

    baseline_ms, _ = await _median_traversal_ms(operator, depth=1)
    deep_ms, nodes = await _median_traversal_ms(operator, depth=16)

    # depth 0 hub + 16 levels across 16 chains = 1 + 16*16 = 257 nodes
    # reachable within the depth-16 budget. Deterministic, runner-load
    # independent — the correctness half of this test.
    assert len(nodes) == 1 + chains * 16

    ratio = deep_ms / baseline_ms
    assert ratio < _PERF_RATIO_CEILING, (
        f"depth-16 / depth-1 traversal ratio {ratio:.1f} exceeds "
        f"{_PERF_RATIO_CEILING} (baseline {baseline_ms:.1f} ms, "
        f"depth-16 {deep_ms:.1f} ms) — recursive join is no longer "
        f"sub-linear per level; check the migration-0007 "
        f"graph_edge_tenant_{{from,to}}_idx indexes."
    )
    # Secondary smoke signal only (see docstring): a wide absolute
    # backstop for a regression that also slowed the depth-1 baseline.
    assert deep_ms < _PERF_ABS_CEILING_MS, (
        f"depth-16 traversal took {deep_ms:.1f} ms (median), over the "
        f"{_PERF_ABS_CEILING_MS:.0f} ms catastrophic-regression backstop"
    )


@_skip_no_docker
async def test_same_tenant_kind_collision_disambiguated_by_kind(
    pg_engine: None,
) -> None:
    """Same ``name``, two ``kind``s, one tenant — bare lookup must refuse.

    ``graph_node`` uniqueness is ``(tenant_id, kind, name)``. Seed a
    ``target`` named ``svc`` with one dependent and an unrelated ``vm``
    named ``svc`` with a different dependent. A bare-name traversal
    cannot pick one anchor, so it raises ``AmbiguousNodeError`` rather
    than silently merging the two closures. Pinning ``kind`` resolves
    to exactly the requested object's closure.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        svc_target = await _seed_node(session, tenant_id=TENANT_A_ID, kind="target", name="svc")
        svc_vm = await _seed_node(session, tenant_id=TENANT_A_ID, kind="vm", name="svc")
        dep_of_target = await _seed_node(
            session, tenant_id=TENANT_A_ID, kind="vm", name="dep-of-target"
        )
        dep_of_vm = await _seed_node(session, tenant_id=TENANT_A_ID, kind="host", name="dep-of-vm")
        await _seed_edge(
            session,
            tenant_id=TENANT_A_ID,
            from_id=dep_of_target,
            to_id=svc_target,
            kind="belongs-to",
        )
        await _seed_edge(
            session,
            tenant_id=TENANT_A_ID,
            from_id=dep_of_vm,
            to_id=svc_vm,
            kind="runs-on",
        )

    operator = _operator(TENANT_A_ID)

    with pytest.raises(AmbiguousNodeError) as excinfo:
        await find_dependents(operator, "svc")
    assert sorted(excinfo.value.kinds) == ["target", "vm"]

    # Pinning kind picks exactly one closure — no merge.
    target_dependents = await find_dependents(operator, "svc", kind="target")
    assert {n.name for n in target_dependents} == {"svc", "dep-of-target"}

    vm_dependents = await find_dependents(operator, "svc", kind="vm")
    assert {n.name for n in vm_dependents} == {"svc", "dep-of-vm"}

    # find_path applies the same contract independently per endpoint.
    with pytest.raises(AmbiguousNodeError):
        await find_path(operator, "svc", "dep-of-target")
    pinned = await find_path(operator, "svc", "dep-of-target", from_kind="target")
    assert pinned is not None
    assert pinned.total_hops == 1
    assert {n.name for n in pinned.nodes} == {"svc", "dep-of-target"}


# ---------------------------------------------------------------------------
# §6 traversal-exclusion guard (G9.2-T3 #595): superseded auto edges drop
# out of every traversal verb; non-superseded edges are unaffected.
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_superseded_edge_excluded_from_dependents(
    pg_engine: None,
) -> None:
    """An auto edge marked ``superseded_by`` is invisible to find_dependents.

    Seed ``vm-A --runs-on--> host-X`` as an auto edge, then stamp
    ``properties.superseded_by`` (the mark a curated annotation would
    leave per Initiative #364 §6). ``find_dependents`` on ``host-X``
    must NOT report vm-A — the reverse traversal's recursive term now
    filters ``properties->>'superseded_by' IS NULL``.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        vm_a = await _seed_node(session, tenant_id=TENANT_A_ID, kind="vm", name="ts-vm-a")
        host_x = await _seed_node(session, tenant_id=TENANT_A_ID, kind="host", name="ts-host-x")
        edge = GraphEdge(
            id=uuid.uuid4(),
            tenant_id=TENANT_A_ID,
            from_node_id=vm_a,
            to_node_id=host_x,
            kind="runs-on",
            source="auto",
            properties={"superseded_by": str(uuid.uuid4())},
            discovered_by="test",
        )
        session.add(edge)

    nodes = await find_dependents(_operator(TENANT_A_ID), "ts-host-x")
    names = {n.name for n in nodes}
    # The host exists; vm-A is filtered out by the superseded guard.
    assert "ts-host-x" in names
    assert "ts-vm-a" not in names


@_skip_no_docker
async def test_superseded_edge_excluded_from_dependencies(
    pg_engine: None,
) -> None:
    """Forward traversal mirrors the reverse guard: superseded edges drop."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        vm_a = await _seed_node(session, tenant_id=TENANT_A_ID, kind="vm", name="tf-vm-a")
        host_x = await _seed_node(session, tenant_id=TENANT_A_ID, kind="host", name="tf-host-x")
        edge = GraphEdge(
            id=uuid.uuid4(),
            tenant_id=TENANT_A_ID,
            from_node_id=vm_a,
            to_node_id=host_x,
            kind="runs-on",
            source="auto",
            properties={"superseded_by": str(uuid.uuid4())},
            discovered_by="test",
        )
        session.add(edge)

    nodes = await find_dependencies(_operator(TENANT_A_ID), "tf-vm-a")
    names = {n.name for n in nodes}
    assert "tf-vm-a" in names
    assert "tf-host-x" not in names


@_skip_no_docker
async def test_superseded_edge_excluded_from_find_path_both_legs(
    pg_engine: None,
) -> None:
    """``bi_edge`` filters both legs — a superseded edge is unwalkable in
    either direction.

    Seed ``A --runs-on--> B`` as auto + superseded. ``find_path(A, B)``
    must return ``None`` — both the forward leg
    (``A.from_node_id → A.to_node_id``) and the reversed leg
    (``B.to_node_id ← A.from_node_id``) are filtered out by the guard.
    Without the reversed-leg filter the path ``A ← B`` would still
    appear, hiding the supersede contract.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        a = await _seed_node(session, tenant_id=TENANT_A_ID, kind="vm", name="tp-a")
        b = await _seed_node(session, tenant_id=TENANT_A_ID, kind="host", name="tp-b")
        edge = GraphEdge(
            id=uuid.uuid4(),
            tenant_id=TENANT_A_ID,
            from_node_id=a,
            to_node_id=b,
            kind="runs-on",
            source="auto",
            properties={"superseded_by": str(uuid.uuid4())},
            discovered_by="test",
        )
        session.add(edge)

    forward = await find_path(_operator(TENANT_A_ID), "tp-a", "tp-b")
    reverse = await find_path(_operator(TENANT_A_ID), "tp-b", "tp-a")
    assert forward is None
    assert reverse is None


@_skip_no_docker
async def test_guard_does_not_drop_non_superseded_edges(
    known_graph: dict[str, uuid.UUID],
) -> None:
    """Regression: a graph with zero superseded edges returns the identical
    closure.

    The known 5-node / 6-edge graph from :func:`known_graph` carries
    no ``superseded_by`` markers; the closure must match the
    pre-guard expected set exactly. A malformed guard (e.g.
    ``properties->>'superseded_by' = 'x'`` instead of ``IS NULL``)
    would silently drop every edge here.
    """
    nodes = await find_dependents(_operator(TENANT_A_ID), "host1")
    assert {n.name for n in nodes} == {"host1", "vm1", "vm2", "app"}

    deps = await find_dependencies(_operator(TENANT_A_ID), "app")
    assert {n.name for n in deps} == {"app", "vm1", "vm2", "host1", "ds1"}

    path = await find_path(_operator(TENANT_A_ID), "app", "ds1")
    assert path is not None
    assert path.total_hops == 2


def _closure_shape(nodes: Any) -> list[tuple[Any, ...]]:
    """The pre-#3739 fields of a closure, in result order."""
    return [
        (n.id, n.kind, n.name, n.source, n.depth, n.via_edge_kind, n.parent_node_id, n.via_edge_id)
        for n in nodes
    ]


@_skip_no_docker
async def test_legacy_rows_read_back_unresolved_and_results_unchanged(
    known_graph: dict[str, uuid.UUID],
) -> None:
    """#3739 expand step: readers label legacy rows and never pick a scope.

    Every seeded row has ``source_scope`` NULL, the legacy shape. Readers
    must return it as ``unresolved``. Setting scopes afterwards must not
    change which rows come back, their order or their chain provenance --
    the scope is reported, not used to choose a source.
    """
    op = _operator(TENANT_A_ID)
    legacy_closure = await find_dependencies(op, "app")
    legacy_path = await find_path(op, "app", "ds1")
    assert legacy_path is not None
    assert all(n.source_scope is None for n in legacy_closure)
    assert {n.scope_status for n in legacy_closure} == {"unresolved"}
    assert {n.scope_status for n in legacy_path.nodes} == {"unresolved"}

    target_scope = f"target:{uuid.uuid4()}"
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        app = await session.get(GraphNode, known_graph["app"])
        vm1 = await session.get(GraphNode, known_graph["vm1"])
        assert app is not None
        assert vm1 is not None
        app.source_scope = "global"
        vm1.source_scope = target_scope

    scoped_closure = await find_dependencies(op, "app")
    scoped_path = await find_path(op, "app", "ds1")
    assert scoped_path is not None
    assert _closure_shape(scoped_closure) == _closure_shape(legacy_closure)
    assert _closure_shape(scoped_path.nodes) == _closure_shape(legacy_path.nodes)
    status = {n.name: (n.source_scope, n.scope_status) for n in scoped_closure}
    assert status["app"] == ("global", "global")
    assert status["vm1"] == (target_scope, "target")
    assert status["host1"] == (None, "unresolved")
    path_status = {n.name: n.scope_status for n in scoped_path.nodes}
    assert path_status["app"] == "global"


@_skip_no_docker
async def test_last_observed_target_is_not_read_as_scope(pg_engine: None) -> None:
    """A legacy row with ``target_id`` set still reads as ``unresolved``."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        target = Target(
            id=uuid.uuid4(),
            tenant_id=TENANT_A_ID,
            name=f"cluster-{uuid.uuid4().hex[:8]}",
            aliases=[],
            product="kubernetes",
            host="k8s.example.test",
        )
        session.add(target)
        await session.flush()
        session.add(
            GraphNode(
                id=uuid.uuid4(),
                tenant_id=TENANT_A_ID,
                kind="namespace",
                name="default",
                target_id=target.id,
                source="auto",
                properties={},
                discovered_by="kubernetes",
            )
        )

    nodes = await find_dependents(_operator(TENANT_A_ID), "default", kind="namespace")

    assert [(n.name, n.source_scope, n.scope_status) for n in nodes] == [
        ("default", None, "unresolved")
    ]


# ---------------------------------------------------------------------------
# #3760 — path direction, relation class, source and freshness
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_undirected_path_is_the_default_and_keeps_legacy_reachability(
    known_graph: dict[str, uuid.UUID],
) -> None:
    """With no direction, edges are walked both ways, as before."""
    op = _operator(TENANT_A_ID)
    path = await find_path(op, "ds1", "app")
    assert path is not None
    assert path.direction == "undirected"
    assert [n.name for n in path.nodes] == ["ds1", "vm1", "app"]
    # vm1 and vm2 only meet against edge direction (both run on host1).
    sibling = await find_path(op, "vm1", "vm2")
    assert sibling is not None and sibling.total_hops == 2
    explicit = await find_path(op, "vm1", "vm2", direction="undirected")
    assert explicit is not None and explicit.total_hops == 2


@_skip_no_docker
async def test_forward_and_reverse_paths_follow_edge_direction(
    known_graph: dict[str, uuid.UUID],
) -> None:
    """``forward`` follows ``from -> to`` only; ``reverse`` the other way."""
    op = _operator(TENANT_A_ID)

    forward = await find_path(op, "app", "ds1", direction="forward")
    assert forward is not None
    assert forward.direction == "forward"
    assert [n.name for n in forward.nodes] == ["app", "vm1", "ds1"]
    # Every edge points away from ds1, so nothing leads forward from it.
    assert await find_path(op, "ds1", "app", direction="forward") is None

    reverse = await find_path(op, "ds1", "app", direction="reverse")
    assert reverse is not None
    assert reverse.direction == "reverse"
    assert [n.name for n in reverse.nodes] == ["ds1", "vm1", "app"]
    assert await find_path(op, "app", "ds1", direction="reverse") is None

    # The siblings only meet when both directions are allowed.
    assert await find_path(op, "vm1", "vm2", direction="forward") is None
    assert await find_path(op, "vm1", "vm2", direction="reverse") is None


@_skip_no_docker
async def test_relation_class_filter_keeps_only_mapped_kinds(
    known_graph: dict[str, uuid.UUID],
) -> None:
    """Containment is ``belongs-to`` only; ``mounts`` is ``unknown``."""
    op = _operator(TENANT_A_ID)

    contained = await find_dependencies(op, "app", relation_class="containment")
    assert {n.name for n in contained} == {"app", "vm1", "vm2"}
    assert {n.via_relation_class for n in contained if n.depth} == {"containment"}

    placed = await find_dependents(op, "host1", relation_class="placement")
    assert {n.name for n in placed} == {"host1", "vm1", "vm2"}
    assert {n.via_relation_class for n in placed if n.depth} == {"placement"}

    unknown = await find_dependencies(op, "vm1", relation_class="unknown")
    assert {n.name for n in unknown} == {"vm1", "ds1"}
    assert {n.via_relation_class for n in unknown if n.depth} == {"unknown"}

    # No ``authenticates-via`` edge exists, so no dependency is claimed.
    assert [n.name for n in await find_dependencies(op, "app", relation_class="dependency")] == [
        "app"
    ]

    # The filter applies to path search too.
    assert await find_path(op, "app", "ds1", relation_class="containment") is None
    via_host = await find_path(op, "vm1", "vm2", relation_class="placement")
    assert via_host is not None
    assert [n.name for n in via_host.nodes] == ["vm1", "host1", "vm2"]


@_skip_no_docker
async def test_edge_listing_carries_relation_class_and_endpoint_scope(
    pg_engine: None,
) -> None:
    """``list_edges`` filters by relation class and reports endpoint scopes."""
    now = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        ids = {
            name: await _seed_node(
                session, tenant_id=TENANT_A_ID, kind=kind, name=name, last_seen=now
            )
            for name, kind in (
                ("sa-1", "serviceaccount"),
                ("role-1", "vault-role"),
                ("pod-1", "pod"),
                ("ns-1", "namespace"),
            )
        }
        role = await session.get(GraphNode, ids["role-1"])
        assert role is not None
        role.source_scope = "global"
        for frm, to, kind in (
            ("sa-1", "role-1", "authenticates-via"),
            ("pod-1", "ns-1", "belongs-to"),
            ("pod-1", "sa-1", "depends-on"),
        ):
            await _seed_edge(
                session,
                tenant_id=TENANT_A_ID,
                from_id=ids[frm],
                to_id=ids[to],
                kind=kind,
                last_seen=now,
            )

    async with sessionmaker() as session:
        dependency = await list_edges(session, TENANT_A_ID, relation_class="dependency")
        unknown = await list_edges(session, TENANT_A_ID, relation_class="unknown")
        everything = await list_edges(session, TENANT_A_ID)

    assert [(e.kind, e.relation_class) for e in dependency] == [("authenticates-via", "dependency")]
    assert dependency[0].from_endpoint.source_scope is None
    assert dependency[0].to_endpoint.source_scope == "global"
    # ``depends-on`` is not a reviewed kind, so it stays unknown.
    assert [(e.kind, e.relation_class) for e in unknown] == [("depends-on", "unknown")]
    assert {e.relation_class for e in everything} == {"dependency", "containment", "unknown"}


@_skip_no_docker
async def test_missing_endpoint_and_no_route_are_told_apart(
    known_graph: dict[str, uuid.UUID],
) -> None:
    """``missing_endpoint_raises`` separates "not in the graph" from "no route"."""
    op = _operator(TENANT_A_ID)
    with pytest.raises(NodeNotFoundError) as excinfo:
        await find_path(op, "app", "no-such-node", missing_endpoint_raises=True)
    assert excinfo.value.name == "no-such-node"
    # Both ends exist; the route is just too long for one hop.
    assert await find_path(op, "app", "ds1", max_hops=1, missing_endpoint_raises=True) is None


@_skip_no_docker
async def test_stale_edge_and_old_observation_read_differently(
    stale_chain: dict[str, uuid.UUID],
) -> None:
    """Rows carry ``last_seen`` and edge facts, so freshness can be told apart."""
    op = _operator(TENANT_A_ID)
    await _soft_delete_edge(stale_chain["e_mid_root"])
    long_ago = datetime.now(UTC) - timedelta(days=3)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        leaf = await session.get(GraphNode, stale_chain["leaf"])
        assert leaf is not None
        leaf.last_seen = long_ago

    rows = {n.name: n for n in await find_dependents(op, "root")}
    path = await find_path(op, "root", "leaf")
    assert path is not None
    now = datetime.now(UTC)

    def freshness(source: str | None, last_seen: datetime | None) -> str:
        return classify_freshness(source, last_seen, now=now, refresh_interval_seconds=3600)

    mid, leaf_row = rows["mid"], rows["leaf"]
    # The edge into mid was dropped by the latest refresh: stale.
    assert mid.via_edge_source == "auto"
    assert mid.via_edge_last_seen is None
    assert freshness(mid.via_edge_source, mid.via_edge_last_seen) == "stale"
    assert freshness(mid.source, mid.last_seen) == "observed"
    # leaf is still readable but was last seen days ago: old.
    assert leaf_row.last_seen == long_ago
    assert freshness(leaf_row.source, leaf_row.last_seen) == "old_observation"
    assert freshness(leaf_row.via_edge_source, leaf_row.via_edge_last_seen) == "observed"
    # Path rows carry the same facts per hop.
    by_name = {n.name: n for n in path.nodes}
    assert by_name["root"].via_edge_last_seen is None and by_name["root"].via_edge_source is None
    assert by_name["mid"].via_edge_last_seen is None
    assert by_name["mid"].via_edge_source == "auto"
    assert by_name["leaf"].last_seen == long_ago


def test_module_imports_cleanly() -> None:
    """Cheap collection-time smoke that runs on no-Docker sandboxes.

    Mirrors the same guard :mod:`tests.integration.test_tenant_isolation`
    keeps: if a public symbol in the query module were renamed or
    removed, this fails first on every sandbox, not only the
    Docker-gated runners.
    """
    from meho_backplane.topology import query, schemas

    assert callable(query.find_dependents)
    assert callable(query.find_dependencies)
    assert callable(query.find_path)
    assert issubclass(query.AmbiguousNodeError, ValueError)
    assert schemas.TopologyNode.model_config["frozen"] is True
    assert schemas.TopologyPath.model_config["frozen"] is True


# ---------------------------------------------------------------------------
# #3764 — bounded closure answers. New fixtures: the star (one hub, many
# dependents) and the dense mesh (layers fully linked to the next layer).
# ---------------------------------------------------------------------------


def _incompressible_pad(size: int) -> str:
    """A random string PostgreSQL cannot compress, so it is stored out of line."""
    return secrets.token_hex(size // 2)


async def _seed_new_star_fixture(
    *, leaves: int, pad_bytes: int = 0, children: int = 0, random_pad: bool = False
) -> None:
    """New #3764 fixture: ``star-hub`` with *leaves* dependents ``star-leaf-NNNN``.

    Each leaf ``runs-on`` the hub, so a dependents walk from the hub has
    one level with *leaves* nodes. *pad_bytes* adds a string of that size
    to every leaf's properties, for the byte-limit test; *random_pad*
    makes it a random string that stays large on disk. *children* adds
    that many ``star-child-NNNN-MMMM`` nodes under every leaf, a second
    level that a walk cut at the first level must never ask for.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        hub = await _seed_node(session, tenant_id=TENANT_A_ID, kind="host", name="star-hub")
        leaf_nodes = [
            GraphNode(
                id=uuid.uuid4(),
                tenant_id=TENANT_A_ID,
                kind="vm",
                name=f"star-leaf-{i:04d}",
                source="auto",
                properties={
                    "pad": _incompressible_pad(pad_bytes) if random_pad else "x" * pad_bytes
                },
                discovered_by="test",
            )
            for i in range(leaves)
        ]
        child_nodes = [
            (
                leaf,
                GraphNode(
                    id=uuid.uuid4(),
                    tenant_id=TENANT_A_ID,
                    kind="app",
                    name=f"star-child-{i:04d}-{j:04d}",
                    source="auto",
                    discovered_by="test",
                ),
            )
            for i, leaf in enumerate(leaf_nodes)
            for j in range(children)
        ]
        session.add_all(leaf_nodes)
        session.add_all(child for _leaf, child in child_nodes)
        await session.flush()
        session.add_all(
            GraphEdge(
                id=uuid.uuid4(),
                tenant_id=TENANT_A_ID,
                from_node_id=leaf.id,
                to_node_id=hub,
                kind="runs-on",
                source="auto",
                discovered_by="test",
            )
            for leaf in leaf_nodes
        )
        session.add_all(
            GraphEdge(
                id=uuid.uuid4(),
                tenant_id=TENANT_A_ID,
                from_node_id=child.id,
                to_node_id=leaf.id,
                kind="runs-on",
                source="auto",
                discovered_by="test",
            )
            for leaf, child in child_nodes
        )


class _HopStatements:
    """Record the statements the bounded walk sends to PostgreSQL; count the hops."""

    def __init__(self) -> None:
        self.hops = 0
        self.statements: list[str] = []

    def __enter__(self) -> _HopStatements:
        event.listen(engine_module._engine.sync_engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(engine_module._engine.sync_engine, "before_cursor_execute", self._record)

    def _record(self, conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        self.statements.append(statement)
        if "WITH reached AS NOT MATERIALIZED" in statement:
            self.hops += 1


async def _graph_node_toast_blocks(conn: Any) -> int:
    """Blocks read so far from ``graph_node``'s out-of-line storage (large properties).

    PostgreSQL keeps a value larger than about 2 kB out of line, so these
    blocks are read only when a statement loads such a value. The backend
    publishes its counts when it goes idle; ``pg_stat_force_next_flush``
    makes that happen right after the next commit.
    """
    await conn.execute(text("SELECT pg_stat_force_next_flush()"))
    await conn.commit()
    await conn.execute(text("SELECT pg_stat_clear_snapshot()"))
    blocks = (
        await conn.execute(
            text(
                "SELECT COALESCE(toast_blks_hit, 0) + COALESCE(toast_blks_read, 0)"
                " FROM pg_statio_user_tables WHERE relname = 'graph_node'"
            )
        )
    ).scalar_one()
    await conn.commit()
    return int(blocks)


def _plan_sort_space_kb(plan: dict[str, Any]) -> int:
    """Memory or disk every sort in the plan used, in kB."""
    own = int(plan.get("Sort Space Used", 0))
    return own + sum(_plan_sort_space_kb(child) for child in plan.get("Plans", []))


async def _explain(conn: Any, sql: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run *sql* under ``EXPLAIN ANALYZE`` and return the top plan node."""
    raw = (await conn.execute(text("EXPLAIN (ANALYZE, FORMAT JSON) " + sql), params)).scalar_one()
    plan = raw if isinstance(raw, list) else json.loads(raw)
    return dict(plan[0]["Plan"])


async def _seed_new_dense_mesh_fixture(*, layers: int, width: int) -> None:
    """New #3764 fixture: ``mesh-root`` plus *layers* layers of *width* nodes.

    Every node of a layer depends on every node of the layer above it, so
    the number of distinct walks grows as ``width ** layers``. The
    recursive CTE behind the unbounded verbs follows each walk, which is
    the dense fan-out a depth limit alone does not bound. The bounded walk
    expands each node once, so it visits ``layers * width`` nodes.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        root = await _seed_node(session, tenant_id=TENANT_A_ID, kind="host", name="mesh-root")
        above = [root]
        for layer in range(layers):
            current = [
                await _seed_node(
                    session, tenant_id=TENANT_A_ID, kind="vm", name=f"mesh-{layer}-{i}"
                )
                for i in range(width)
            ]
            for lower in current:
                for upper in above:
                    await _seed_edge(
                        session,
                        tenant_id=TENANT_A_ID,
                        from_id=lower,
                        to_id=upper,
                        kind="runs-on",
                    )
            above = current


@_skip_no_docker
async def test_bounded_closure_new_star_fixture_stops_at_node_limit(pg_engine: None) -> None:
    """A dense star stops at the node limit and returns the first part in order."""
    await _seed_new_star_fixture(leaves=300)
    operator = _operator(TENANT_A_ID)

    answer = await bounded_closure(
        operator, "star-hub", reverse=True, budget=ClosureBudget(max_nodes=40)
    )

    assert answer.limit_hit == "node_limit"
    assert len(answer.nodes) == 40
    full = await find_dependents(operator, "star-hub")
    assert len(full) == 301
    # The cut answer is the first part of the full closure, in its order.
    assert [n.id for n in answer.nodes] == [n.id for n in full[:40]]
    assert answer.root is not None
    assert answer.root.name == "star-hub"


@_skip_no_docker
async def test_bounded_closure_new_star_fixture_stops_at_byte_limit(pg_engine: None) -> None:
    """Large node properties stop the walk at the byte limit, not the node limit."""
    await _seed_new_star_fixture(leaves=50, pad_bytes=1000)

    answer = await bounded_closure(
        _operator(TENANT_A_ID),
        "star-hub",
        reverse=True,
        budget=ClosureBudget(max_bytes=10_000),
    )

    assert answer.limit_hit == "byte_limit"
    assert 1 < len(answer.nodes) < 12
    # Every kept node arrives whole; the row past the budget is not kept.
    assert all(len(n.properties["pad"]) == 1000 for n in answer.nodes[1:])


@_skip_no_docker
async def test_bounded_closure_new_star_fixture_stops_asking_past_the_node_limit(
    pg_engine: None,
) -> None:
    """A walk cut at the first hop never asks for the second hop at all."""
    await _seed_new_star_fixture(leaves=50, children=20)  # 1000 nodes at depth 2
    operator = _operator(TENANT_A_ID)

    with _HopStatements() as statements:
        answer = await bounded_closure(
            operator, "star-hub", reverse=True, budget=ClosureBudget(max_nodes=10)
        )

    assert answer.limit_hit == "node_limit"
    assert len(answer.nodes) == 10
    assert all(n.depth <= 1 for n in answer.nodes)
    assert statements.hops == 1


@_skip_no_docker
async def test_bounded_closure_new_star_fixture_hop_reads_properties_for_kept_nodes_only(
    pg_engine: None,
) -> None:
    """A wide hop sorts ids and names only and reads properties for the kept rows alone."""
    leaves, pad_bytes, row_limit = 300, 32_768, 10
    await _seed_new_star_fixture(leaves=leaves, pad_bytes=pad_bytes, random_pad=True)
    hop_sql = query_module._CLOSURE_STEP_SQL_REVERSE
    all_properties_sql = text(
        "SELECT sum(octet_length(CAST(properties AS text))) FROM graph_node"
        " WHERE tenant_id = :tenant_id AND kind = 'vm'"
    )
    async with engine_module._engine.connect() as conn:
        hub = (
            await conn.execute(select(GraphNode.id).where(GraphNode.name == "star-hub"))
        ).scalar_one()
        params = {
            "tenant_id": str(TENANT_A_ID),
            "frontier": [str(hub)],
            "visited": [str(hub)],
            "kind_filter": None,
            "class_kinds": None,
            "class_excluded_kinds": None,
            "include_stale": True,
            "byte_budget": 1 << 30,
            "bytes_before": 0,
            "depth": 1,
            "row_limit": row_limit,
        }
        hop_plan = await _explain(conn, hop_sql.text, params)
        before = await _graph_node_toast_blocks(conn)
        rows = (await conn.execute(hop_sql, params)).fetchall()
        after_hop = await _graph_node_toast_blocks(conn)
        # Reading every leaf's properties is the work the hop must not do.
        total = (
            await conn.execute(all_properties_sql, {"tenant_id": str(TENANT_A_ID)})
        ).scalar_one()
        after_all = await _graph_node_toast_blocks(conn)

    assert len(rows) == row_limit
    assert all(len(row._mapping["properties"]["pad"]) == pad_bytes for row in rows)
    assert total >= leaves * pad_bytes
    hop_blocks, all_blocks = after_hop - before, after_all - after_hop
    # The hop read the kept rows' properties, and a small part of all of them.
    assert hop_blocks > 0
    assert hop_blocks * 10 < all_blocks, (hop_blocks, all_blocks)
    # The hop's sorts hold ids and names, not the leaves' 9.6 MB of properties.
    assert _plan_sort_space_kb(hop_plan) < 1024, hop_plan


@_skip_no_docker
async def test_bounded_closure_reads_one_snapshot_while_edges_change_between_hops(
    pg_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edges written during the walk stay out of the answer; the next walk sees them."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        root = await _seed_node(session, tenant_id=TENANT_A_ID, kind="host", name="snap-root")
        middle = await _seed_node(session, tenant_id=TENANT_A_ID, kind="vm", name="snap-a")
        old_leaf = await _seed_node(session, tenant_id=TENANT_A_ID, kind="app", name="snap-b")
        await _seed_edge(session, tenant_id=TENANT_A_ID, from_id=middle, to_id=root, kind="runs-on")
        old_edge = await _seed_edge(
            session, tenant_id=TENANT_A_ID, from_id=old_leaf, to_id=middle, kind="belongs-to"
        )

    real_execute = query_module._execute_within_deadline
    changed = False

    async def _execute_then_change_the_graph(
        session: Any, sql: Any, params: dict[str, Any], deadline: float
    ) -> Any:
        rows = await real_execute(session, sql, params, deadline)
        nonlocal changed
        if not changed and "frontier" in params and params["depth"] == 1:
            changed = True
            async with sessionmaker() as other, other.begin():
                new_leaf = await _seed_node(other, tenant_id=TENANT_A_ID, kind="app", name="snap-c")
                await _seed_edge(
                    other, tenant_id=TENANT_A_ID, from_id=new_leaf, to_id=middle, kind="belongs-to"
                )
                await other.execute(delete(GraphEdge).where(GraphEdge.id == old_edge))
        return rows

    monkeypatch.setattr(query_module, "_execute_within_deadline", _execute_then_change_the_graph)
    operator = _operator(TENANT_A_ID)

    during = await bounded_closure(operator, "snap-root", reverse=True)

    assert changed
    assert during.limit_hit is None
    assert [n.name for n in during.nodes] == ["snap-root", "snap-a", "snap-b"]
    after = await bounded_closure(operator, "snap-root", reverse=True)
    assert [n.name for n in after.nodes] == ["snap-root", "snap-a", "snap-c"]


@_skip_no_docker
async def test_bounded_closure_names_an_oversized_root_without_loading_its_properties(
    pg_engine: None,
) -> None:
    """A root over the byte budget is named; its properties never reach Python."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        fat_root = GraphNode(
            id=uuid.uuid4(),
            tenant_id=TENANT_A_ID,
            kind="host",
            name="fat-root",
            source="auto",
            properties={"pad": _incompressible_pad(65_536)},
            discovered_by="test",
        )
        session.add(fat_root)
        await session.flush()
        leaf = await _seed_node(session, tenant_id=TENANT_A_ID, kind="vm", name="fat-leaf")
        await _seed_edge(
            session, tenant_id=TENANT_A_ID, from_id=leaf, to_id=fat_root.id, kind="runs-on"
        )

    with _HopStatements() as statements:
        answer = await bounded_closure(
            _operator(TENANT_A_ID), "fat-root", reverse=True, budget=ClosureBudget(max_bytes=1000)
        )

    assert answer.limit_hit == "byte_limit"
    assert answer.root is not None
    assert answer.root.name == "fat-root"
    assert answer.root.properties == {}
    assert [n.name for n in answer.nodes] == ["fat-root"]
    assert answer.nodes[0].properties == {}
    assert statements.hops == 0
    lookup = next(stmt for stmt in statements.statements if "graph_node.name" in stmt)
    assert "properties" not in lookup


@_skip_no_docker
async def test_bounded_closure_reports_time_budget_when_the_root_lookup_is_slow(
    pg_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow lookup ends at the time budget with no root; the pool stays usable."""
    await _seed_new_star_fixture(leaves=3)
    operator = _operator(TENANT_A_ID)
    monkeypatch.setattr(
        query_module, "node_identity_statement", lambda *args, **kwargs: text("SELECT pg_sleep(5)")
    )

    started = time.monotonic()
    answer = await bounded_closure(
        operator, "star-hub", reverse=True, budget=ClosureBudget(time_budget_ms=100)
    )

    assert time.monotonic() - started < 3
    assert answer.limit_hit == "time_budget"
    assert answer.root is None
    assert answer.nodes == ()
    monkeypatch.undo()
    recovered = await bounded_closure(operator, "star-hub", reverse=True)
    assert recovered.limit_hit is None
    assert len(recovered.nodes) == 4


@_skip_no_docker
async def test_bounded_closure_new_dense_mesh_fixture_expands_each_node_once(
    pg_engine: None,
) -> None:
    """The dense mesh costs one hop statement per layer, not one walk per path."""
    await _seed_new_dense_mesh_fixture(layers=8, width=6)
    operator = _operator(TENANT_A_ID)

    with _HopStatements() as statements:
        answer = await bounded_closure(operator, "mesh-root", reverse=True)

    assert answer.limit_hit is None
    assert len(answer.nodes) == 1 + 8 * 6
    assert [n.depth for n in answer.nodes] == sorted(n.depth for n in answer.nodes)
    # Eight layers plus the empty probe one hop past the last layer.
    assert statements.hops == 9
    assert [n.id for n in answer.nodes] == [
        n.id for n in await find_dependents(operator, "mesh-root")
    ]


@_skip_no_docker
async def test_bounded_closure_new_dense_mesh_fixture_stops_at_time_budget(
    pg_engine: None,
) -> None:
    """A walk that runs out of time has no nodes and a known root."""
    await _seed_new_dense_mesh_fixture(layers=8, width=6)
    operator = _operator(TENANT_A_ID)

    answer = await bounded_closure(
        operator, "mesh-root", reverse=True, budget=ClosureBudget(time_budget_ms=1)
    )

    assert answer.limit_hit == "time_budget"
    assert answer.nodes == ()
    assert answer.root is not None
    assert answer.root.name == "mesh-root"
    # A cancelled statement leaves the pool usable for the next query.
    shallow = await bounded_closure(operator, "mesh-root", reverse=True, depth=1)
    assert shallow.limit_hit == "hop_limit"
    assert len(shallow.nodes) == 7


@_skip_no_docker
async def test_bounded_closure_reports_hop_limit_only_when_more_nodes_exist(
    known_graph: dict[str, uuid.UUID],
) -> None:
    """``hop_limit`` means nodes exist past the depth; a full answer has no limit."""
    operator = _operator(TENANT_A_ID)

    cut = await bounded_closure(operator, "ds1", reverse=True, depth=1)
    assert cut.limit_hit == "hop_limit"
    assert [n.name for n in cut.nodes] == ["ds1", "host1", "vm1"]

    full = await bounded_closure(operator, "ds1", reverse=True, depth=2)
    assert full.limit_hit is None
    assert [n.name for n in full.nodes] == ["ds1", "host1", "vm1", "app", "vm2"]


@_skip_no_docker
async def test_bounded_closure_complete_answer_continues_through_shared_handle(
    pg_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A complete large closure is kept behind a handle only its owner can read."""
    import meho_backplane.operations.jsonflux_reducer as reducer_module
    import meho_backplane.operations.result_query as result_query_core
    from meho_backplane.mcp.tools.topology import _query_topology_handler
    from tests._result_store_helpers import MemoryResultStore

    store = MemoryResultStore()
    monkeypatch.setattr(reducer_module, "get_result_handle_store", lambda: store)
    monkeypatch.setattr(result_query_core, "get_result_handle_store", lambda: store)
    await _seed_new_star_fixture(leaves=120)
    operator = _operator(TENANT_A_ID)

    answer = await _query_topology_handler(operator, {"kind": "dependents", "target": "star-hub"})

    assert answer["completeness"]["state"] == "complete"
    assert answer["completeness"]["limits_hit"] == []
    assert answer["completeness"]["found"] == answer["completeness"]["retained"] == 121
    assert answer["delivery"] == "complete"
    assert "nodes" not in answer
    assert answer["node_count"] == 121
    assert answer["edge_count"] == 120
    handle_id = uuid.UUID(answer["handle"]["handle_id"])

    window = await result_query_core.read_result_window(operator, handle_id, 0, 500)
    full = await find_dependents(operator, "star-hub")
    assert [row["id"] for row in window["rows"]] == [str(n.id) for n in full]
    assert window["rows"][1]["parent_node_id"] == answer["root"]["id"]
    assert all(row["via_relation_class"] == "placement" for row in window["rows"][1:])

    other_caller = operator.model_copy(update={"sub": "someone-else"})
    other_tenant = _operator(TENANT_B_ID)
    for intruder in (other_caller, other_tenant):
        with pytest.raises(result_query_core.ResultHandleNotFoundError):
            await result_query_core.read_result_window(intruder, handle_id, 0, 50)
