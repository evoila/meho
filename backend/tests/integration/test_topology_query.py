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
* A 10k-node fixture completes a depth-16 traversal in under 100 ms.
"""

from __future__ import annotations

import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphEdge, GraphNode
from meho_backplane.topology.query import (
    AmbiguousNodeError,
    find_dependencies,
    find_dependents,
    find_path,
)
from meho_backplane.topology.resolvers import NodeNotFoundError
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON

# Match the tenant rows the ``pg_engine`` conftest fixture seeds.
TENANT_A_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
TENANT_B_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")

_skip_no_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)


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
) -> uuid.UUID:
    """Insert one ``graph_node`` and return its id.

    Flushes immediately so the row exists before any ``graph_edge``
    that references it is added — SQLAlchemy's unit-of-work otherwise
    batches inserts in an order that can emit the edge before its
    endpoint node and trip the ``REFERENCES graph_node(id)`` FK.
    """
    node = GraphNode(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        kind=kind,
        name=name,
        properties={"seeded": name},
        discovered_by="test",
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
) -> None:
    """Insert one ``graph_edge`` (``source='auto'``)."""
    session.add(
        GraphEdge(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            from_node_id=from_id,
            to_node_id=to_id,
            kind=kind,
            source="auto",
            discovered_by="test",
        )
    )


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
        await _seed_edge(session, tenant_id=TENANT_A_ID, from_id=app, to_id=vm1, kind="belongs-to")
        await _seed_edge(session, tenant_id=TENANT_A_ID, from_id=app, to_id=vm2, kind="belongs-to")
        await _seed_edge(session, tenant_id=TENANT_A_ID, from_id=vm1, to_id=host1, kind="runs-on")
        await _seed_edge(session, tenant_id=TENANT_A_ID, from_id=vm2, to_id=host1, kind="runs-on")
        await _seed_edge(session, tenant_id=TENANT_A_ID, from_id=host1, to_id=ds1, kind="mounts")
        await _seed_edge(session, tenant_id=TENANT_A_ID, from_id=vm1, to_id=ds1, kind="mounts")

    yield {
        "app": app,
        "vm1": vm1,
        "vm2": vm2,
        "host1": host1,
        "ds1": ds1,
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


@_skip_no_docker
async def test_find_dependents_untracked_vs_tracked_no_deps(
    known_graph: dict[str, uuid.UUID],
) -> None:
    """Mirror the RDC #789 N2 repro: ``[]`` must not conflate untracked
    with tracked-but-no-dependents (G0.18-T4 #1357).

    The ``known_graph`` fixture seeds ``ds1`` as a leaf — nothing
    depends on it, so the reverse closure is the one-element ``[ds1]``
    (the substrate's depth-0 anchor row). A non-existent
    ``vault-prod`` target — the exact shape a registered non-k8s
    target takes today because auto-discovery is k8s-only — raises
    :class:`NodeNotFoundError` rather than returning the bare ``[]``
    the pre-fix behaviour produced.

    The pre-G0.18-T4 implementation returned the same empty list for
    both, and the consumer's pre-destructive blast-radius check read
    the empty list as "safe to delete," a false-negative SEV-3.
    """
    tracked = await find_dependents(_operator(TENANT_A_ID), "ds1")
    assert [n.name for n in tracked] == ["ds1"]
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


@_skip_no_docker
async def test_depth_16_traversal_on_10k_nodes_under_100ms(
    pg_engine: None,
) -> None:
    """A 10k-node fixture: a depth-16 traversal completes in < 100 ms.

    Build a wide-but-shallow forest: 16 chains of ~625 nodes each
    rooted at a single hub so a depth-16 dependents traversal from the
    hub touches the whole structure. The assertion is on the query
    wall-clock only (seeding is excluded) — the
    ``graph_edge_tenant_to_idx`` / ``graph_edge_tenant_from_idx``
    indexes migration 0007 ships are what keep the recursive join
    sub-linear per level.
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
    # Warm the connection/plan once so the timed run measures steady
    # state, not first-call connection setup.
    await find_dependents(operator, "perf-hub", depth=1)

    started = time.monotonic()
    nodes = await find_dependents(operator, "perf-hub", depth=16)
    elapsed_ms = (time.monotonic() - started) * 1000.0

    # depth 0 hub + 16 levels across 16 chains = 1 + 16*16 = 257 nodes
    # reachable within the depth-16 budget.
    assert len(nodes) == 1 + chains * 16
    assert elapsed_ms < 100.0, f"depth-16 traversal took {elapsed_ms:.1f} ms"


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
