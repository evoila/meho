# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dense-mesh ``find_path`` coverage: per-branch pruning + worst case.

Task #2535 (Initiative #2533). Every prior perf artifact models a
sparse hub-and-chains forest (out-degree exactly 1, no converging
paths, no cycles — :class:`tests.fixtures.topology_10k_nodes.GraphSpec`),
so the load profile that actually makes ``_PATH_SQL`` expensive — a
dense mesh where the bidirectional walk enumerates ~branch_factor^hops
simple paths — was never exercised in CI. This module pins it:

* **Pruning equivalence** — the per-branch target prune added to
  ``_PATH_SQL`` (a non-recursive ``target`` CTE + ``NOT EXISTS`` in the
  recursive term) returns the same shortest-path hop count / ``None``
  as the unpruned walk on the mesh, for near, far, cyclic and
  unreachable endpoint pairs.
* **Pruning row count** — the pruned walk materialises strictly fewer
  rows than the unpruned equivalent when the target is reachable, with
  the exact counts regression-pinned (they are deterministic functions
  of the fixture shape, not of runner speed).
* **Worst-case envelope** — an *unreachable* target (pruning can never
  fire) on a dense cyclic mesh with ``max_hops`` at the API ceiling
  (32, ``api/v1/topology.py::_MAX_HOPS_MAX``). Gated the same
  load-invariant way as the depth-16 traversal test (#1434): a
  row-count pin plus a hops-32 / hops-8 wall-clock *ratio* (runner load
  inflates numerator and denominator together and divides out), with a
  generous absolute ceiling as a catastrophic-regression backstop
  only. The measured envelope is documented in
  ``docs/architecture/topology.md`` §Performance expectations.

Docker-gated like the rest of ``tests/integration`` (real
``pgvector/pgvector:pg16`` — the recursive CYCLE clause is PG-only).
"""

from __future__ import annotations

import time
import uuid

import pytest
from sqlalchemy import text

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphNode
from meho_backplane.topology.query import _PATH_SQL, find_path
from tests.fixtures.topology_10k_nodes import MeshSpec, seed_mesh_graph
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON

# Match the tenant rows the ``pg_engine`` conftest fixture seeds.
TENANT_A_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")

_skip_no_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

# The exact prune predicate shipped in ``_PATH_SQL``'s recursive term.
# The harness derives the *unpruned* control statement by deleting this
# line from the shipped SQL, so the substring doubles as a drift guard:
# if the predicate is reworded or removed in ``query.py``, the
# ``in``-assertion below fails before any query runs.
_PRUNE_PREDICATE = "AND NOT EXISTS (SELECT 1 FROM target t WHERE t.id = w.node_id)"

# Anchor of the final select in ``_PATH_SQL`` — everything before it is
# the CTE list (including the CYCLE clause), which the row-count harness
# reuses verbatim under a ``count(*)`` projection.
_FINAL_SELECT_ANCHOR = "SELECT w.hops, w.node_ids, w.edge_kinds"

# ---------------------------------------------------------------------------
# Fixture shapes. Deliberately small node counts: the walk enumerates
# *simple paths*, whose number grows combinatorially with density, so a
# handful of well-connected nodes already produces walks of thousands of
# rows — the load profile under test — while staying CI-cheap.
# ---------------------------------------------------------------------------

#: Mesh for the pruning equivalence / row-count tests. 20 nodes,
#: 32 forward + 6 back edges, converging paths, cycles, mixed kinds and
#: a sprinkle of soft-deleted rows (still walked — traversal does not
#: filter ``last_seen``).
_PRUNE_MESH = MeshSpec(
    layers=5,
    width=4,
    branch_factor=2,
    cycle_stride=2,
    soft_delete_every=7,
    name_prefix="prune",
)

#: Mesh for the worst-case benchmark. 16 nodes / 28 edges with
#: converging forward fans and back-edges (undirected degree ~4-6): the
#: full simple-path enumeration at the 32-hop ceiling materialises
#: ~31k walk rows (measured; pinned below) — dense-mesh load the forest
#: fixture structurally cannot produce, yet CI-cheap (~120 ms/query on
#: a dev laptop).
_WORST_MESH = MeshSpec(
    layers=4,
    width=4,
    branch_factor=2,
    cycle_stride=2,
    soft_delete_every=5,
    name_prefix="worst",
)

#: API-boundary hop ceiling (mirrors ``api/v1/topology.py::_MAX_HOPS_MAX``)
#: — the worst value any operator-role caller can select.
_MAX_HOPS_CEILING = 32

# Regression pins for the walk-volume assertions. Row counts of a
# recursive CTE are a deterministic function of graph shape + hop bound
# (no runner-speed dependence); measured once on the seeded fixtures and
# re-derived deliberately whenever a mesh spec or the walk semantics
# change.
_EXPECTED_PRUNED_ROWS = 795
_EXPECTED_UNPRUNED_ROWS = 1544
_EXPECTED_WORST_CASE_ROWS = 31_041

# Perf-gate calibration for the worst-case benchmark. Measured healthy
# hops-32 / hops-8 ratio on :data:`_WORST_MESH` is ~7.7 (row-count
# driven: 31 041 vs 3 866 walk rows); the 20.0 ceiling sits ~2.6x above
# healthy while any superlinear regression (broken CYCLE bookkeeping,
# dropped hop bound) lands orders of magnitude higher. The absolute
# ceiling is a catastrophic-regression backstop only (~40x the measured
# ~121 ms median), mirroring the #1434 discipline.
_PERF_SAMPLES = 5
_WORST_RATIO_CEILING = 20.0
_WORST_ABS_CEILING_MS = 5000.0


def _operator(tenant_id: uuid.UUID) -> Operator:
    """Build a minimal :class:`Operator` pinned to *tenant_id*."""
    return Operator(
        sub="op-topology-prune",
        name=None,
        email=None,
        raw_jwt="not-a-real-jwt",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


def _path_sql(*, pruned: bool) -> str:
    """The shipped ``_PATH_SQL`` text, optionally with the prune removed.

    The ``in``-assertion is the drift guard: it fails loudly if the
    prune predicate is reworded or dropped from ``query.py``, so this
    harness can never silently compare the pruned statement against
    itself.
    """
    sql = _PATH_SQL.text
    assert _PRUNE_PREDICATE in sql, (
        "per-branch prune predicate not found in _PATH_SQL — the "
        "#2535 pruning was removed or reworded; update this harness "
        "in lock-step"
    )
    return sql if pruned else sql.replace(_PRUNE_PREDICATE, "")


def _walk_count_sql(*, pruned: bool) -> str:
    """Rewrite the final select into ``count(*) FROM walk``.

    Keeps the whole CTE list (target, bi_edge, walk + CYCLE clause)
    byte-identical to the shipped statement so the counted volume is
    exactly what a real ``find_path`` materialises.
    """
    sql = _path_sql(pruned=pruned)
    head, sep, _tail = sql.partition(_FINAL_SELECT_ANCHOR)
    assert sep, "final-select anchor not found in _PATH_SQL — update this harness"
    return head + "SELECT count(*) AS walk_rows FROM walk"


def _params(
    from_name: str,
    to_name: str,
    *,
    max_hops: int,
) -> dict[str, object]:
    """Bind-parameter dict matching ``_PATH_SQL``'s named binds."""
    return {
        "tenant_id": str(TENANT_A_ID),
        "from_name": from_name,
        "to_name": to_name,
        "from_kind": None,
        "to_kind": None,
        "max_hops": max_hops,
        # #2538 staleness opt-out — the harness always walks the
        # default last-refresh-wins view.
        "include_stale": True,
    }


async def _walk_rows(from_name: str, to_name: str, *, max_hops: int, pruned: bool) -> int:
    """Total rows the (pruned or unpruned) walk materialises."""
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(
            text(_walk_count_sql(pruned=pruned)),
            _params(from_name, to_name, max_hops=max_hops),
        )
        return int(result.scalar_one())


async def _unpruned_hops(from_name: str, to_name: str, *, max_hops: int) -> int | None:
    """Shortest-path hop count from the unpruned control statement."""
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(
            text(_path_sql(pruned=False)),
            _params(from_name, to_name, max_hops=max_hops),
        )
        row = result.first()
        return None if row is None else int(row._mapping["hops"])


@pytest.fixture
async def prune_mesh(pg_engine: None) -> dict[str, uuid.UUID]:
    """Seed :data:`_PRUNE_MESH` plus one edge-less island node."""
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        ids = await seed_mesh_graph(session, tenant_id=TENANT_A_ID, spec=_PRUNE_MESH)
        island = GraphNode(
            id=uuid.uuid4(),
            tenant_id=TENANT_A_ID,
            kind="vm",
            name="prune-island",
            source="auto",
            properties={},
            discovered_by="test",
        )
        session.add(island)
        ids["prune-island"] = island.id
    return ids


def test_mesh_spec_derived_counts() -> None:
    """Cheap no-Docker smoke: the spec's derived sizes match the formula.

    Mirrors ``test_module_imports_cleanly`` in the query suite — this
    fails first on every sandbox if the generator's shape drifts, not
    only on Docker-equipped runners.
    """
    assert _PRUNE_MESH.total_nodes == 5 * 4
    assert _PRUNE_MESH.total_forward_edges == 4 * 4 * 2
    assert _PRUNE_MESH.total_cycle_edges == 3 * 2  # layers 2..4, cols 0 and 2
    assert _PRUNE_MESH.total_edges == 32 + 6
    assert _WORST_MESH.total_nodes == 4 * 4
    assert _WORST_MESH.total_edges == 3 * 4 * 2 + 2 * 2  # forward + back-edges
    with pytest.raises(ValueError):
        MeshSpec(layers=1, width=4)
    with pytest.raises(ValueError):
        MeshSpec(layers=3, width=2, branch_factor=3)
    with pytest.raises(ValueError):
        MeshSpec(layers=3, width=2, edge_kinds=())


@_skip_no_docker
async def test_pruned_path_results_match_unpruned_on_mesh(
    prune_mesh: dict[str, uuid.UUID],
) -> None:
    """Pruning is behavior-invariant: same hops / same ``None``.

    Compares the public ``find_path`` (pruned statement) against the
    unpruned control statement on the same seeded mesh for an adjacent
    pair, a cross-mesh pair (where back-edges shorten the route), and
    an unreachable island. Only the hop count is compared — with ties
    at the minimum hop count ``ORDER BY hops LIMIT 1`` may pick any
    winning path in either variant, so the node sequence is not part
    of the contract.
    """
    op = _operator(TENANT_A_ID)
    cases = [
        ("prune-0-0", "prune-1-0"),  # adjacent (1 forward hop)
        ("prune-0-0", "prune-4-3"),  # far corner, through converging paths
        ("prune-2-0", "prune-0-0"),  # reachable via a back-edge (cycle leg)
        ("prune-0-1", "prune-island"),  # unreachable — pruning can't fire
    ]
    for from_name, to_name in cases:
        pruned = await find_path(op, from_name, to_name, max_hops=6)
        control = await _unpruned_hops(from_name, to_name, max_hops=6)
        if control is None:
            assert pruned is None, f"{from_name}->{to_name}: pruned found a path, control none"
        else:
            assert pruned is not None, f"{from_name}->{to_name}: control found a path, pruned none"
            assert pruned.total_hops == control, f"{from_name}->{to_name}: hop count diverged"
            assert pruned.nodes[0].name == from_name
            assert pruned.nodes[-1].name == to_name


@_skip_no_docker
async def test_pruned_walk_materialises_fewer_rows(
    prune_mesh: dict[str, uuid.UUID],
) -> None:
    """The pruned walk emits strictly fewer rows on a reachable target.

    Row counts of a recursive CTE are a deterministic function of the
    graph shape and the hop bound — no runner-speed dependence — so the
    assertion is a strict inequality plus an exact regression pin. A
    change to :data:`_PRUNE_MESH` or to the walk semantics moves the
    pinned numbers and must be re-derived deliberately, which is the
    point (issue #2535 acceptance: "EXPLAIN-level or row-count
    assertion, regression-pinned").

    The target (``prune-1-0``) is adjacent to the anchor and sits on
    converging paths, so a large share of simple paths pass through it
    — every one of them is cut at the hit under pruning, while the
    unpruned walk keeps extending them to the hop bound.
    """
    pruned_rows = await _walk_rows("prune-0-0", "prune-1-0", max_hops=6, pruned=True)
    unpruned_rows = await _walk_rows("prune-0-0", "prune-1-0", max_hops=6, pruned=False)

    assert pruned_rows < unpruned_rows, (
        f"pruning did not reduce walk volume: pruned={pruned_rows} unpruned={unpruned_rows}"
    )
    # Regression pins (deterministic — see docstring). Measured on the
    # seeded fixture; re-derive on purpose if the mesh shape changes.
    assert unpruned_rows == _EXPECTED_UNPRUNED_ROWS, (
        f"unpruned walk volume moved: {unpruned_rows} != {_EXPECTED_UNPRUNED_ROWS} — "
        f"fixture shape or walk semantics changed"
    )
    assert pruned_rows == _EXPECTED_PRUNED_ROWS, (
        f"pruned walk volume moved: {pruned_rows} != {_EXPECTED_PRUNED_ROWS} — "
        f"fixture shape, walk semantics, or prune placement changed"
    )


async def _median_path_ms(
    op: Operator,
    from_name: str,
    to_name: str,
    *,
    max_hops: int,
    samples: int = _PERF_SAMPLES,
) -> float:
    """Median wall-clock (ms) of ``samples`` ``find_path`` calls."""
    elapsed: list[float] = []
    for _ in range(samples):
        started = time.monotonic()
        await find_path(op, from_name, to_name, max_hops=max_hops)
        elapsed.append((time.monotonic() - started) * 1000.0)
    elapsed.sort()
    return elapsed[len(elapsed) // 2]


@pytest.fixture
async def worst_mesh(pg_engine: None) -> dict[str, uuid.UUID]:
    """Seed :data:`_WORST_MESH` plus the unreachable island target."""
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        ids = await seed_mesh_graph(session, tenant_id=TENANT_A_ID, spec=_WORST_MESH)
        island = GraphNode(
            id=uuid.uuid4(),
            tenant_id=TENANT_A_ID,
            kind="vm",
            name="worst-island",
            source="auto",
            properties={},
            discovered_by="test",
        )
        session.add(island)
        ids["worst-island"] = island.id
    return ids


@_skip_no_docker
async def test_worst_case_find_path_envelope_on_dense_mesh(
    worst_mesh: dict[str, uuid.UUID],
) -> None:
    """Worst case: unreachable target, dense cyclic mesh, ``max_hops=32``.

    Pruning cannot fire (the target is never hit), so the walk
    enumerates every simple path from the anchor — the cost profile the
    hub-and-chains fixture structurally cannot produce and the one an
    operator triggers with a typo'd ``to=`` plus ``max_hops=32`` (the
    API ceiling).

    Three gates, strongest first (same discipline as the depth-16
    ratio gate, #1434 — load-invariant properties primary, wall-clock
    backstop secondary):

    1. **Row-count pin** — the materialised walk volume is a
       deterministic function of the mesh, independent of runner speed.
    2. **Hops-32 / hops-8 wall-clock ratio** — both sides are timed in
       the same run so host contention divides out. On this 12-node
       mesh every simple path is shorter than 12 hops, so hops-32 and
       hops-8 differ only by how much of the combinatorial explosion
       the bound cuts off; a superlinear regression (e.g. broken CYCLE
       bookkeeping) blows the ratio while runner noise does not.
    3. **Absolute ceiling** — generous catastrophic-regression backstop
       only, not the load-bearing gate.
    """
    op = _operator(TENANT_A_ID)

    # Correctness half: the island is genuinely unreachable.
    result = await find_path(op, "worst-0-0", "worst-island", max_hops=_MAX_HOPS_CEILING)
    assert result is None

    # Gate 1 — deterministic volume pin (see docstring).
    worst_rows = await _walk_rows(
        "worst-0-0", "worst-island", max_hops=_MAX_HOPS_CEILING, pruned=True
    )
    assert worst_rows == _EXPECTED_WORST_CASE_ROWS, (
        f"worst-case walk volume moved: {worst_rows} != {_EXPECTED_WORST_CASE_ROWS} — "
        f"mesh shape or walk semantics changed; re-derive the envelope and update "
        f"docs/architecture/topology.md §Performance expectations"
    )

    # Warm the connection/plan so the timed medians measure steady state.
    await find_path(op, "worst-0-0", "worst-island", max_hops=1)

    baseline_ms = await _median_path_ms(op, "worst-0-0", "worst-island", max_hops=8)
    worst_ms = await _median_path_ms(op, "worst-0-0", "worst-island", max_hops=_MAX_HOPS_CEILING)

    # Gate 2 — load-invariant scaling ratio.
    ratio = worst_ms / baseline_ms
    assert ratio < _WORST_RATIO_CEILING, (
        f"worst-case find_path hops-32/hops-8 ratio {ratio:.1f} exceeds "
        f"{_WORST_RATIO_CEILING} (hops-8 {baseline_ms:.1f} ms, hops-32 "
        f"{worst_ms:.1f} ms) — the walk is scaling worse than the "
        f"documented envelope; check the CYCLE guard and the hop bound."
    )
    # Gate 3 — generous absolute backstop (documented envelope lives in
    # docs/architecture/topology.md; this only catches an
    # order-of-magnitude catastrophe that also slowed the baseline).
    assert worst_ms < _WORST_ABS_CEILING_MS, (
        f"worst-case find_path took {worst_ms:.1f} ms (median), over the "
        f"{_WORST_ABS_CEILING_MS:.0f} ms catastrophic-regression backstop"
    )
