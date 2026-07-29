# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Concurrency coverage for the topology write paths (#2535).

The topology suite had zero real-concurrency tests: every write-path
test runs its calls sequentially, and the scheduler's per-target
advisory lock was exercised only against a *mocked* pre-held lock
(unit suite). This module closes both gaps against the real
``pgvector/pgvector:pg16`` container:

* **refresh vs annotate race** — ``asyncio.gather`` of
  :func:`~meho_backplane.topology.refresh.refresh_target_topology` and
  :func:`~meho_backplane.topology.annotate.annotate_edge` on two
  independent connections. Asserts the invariants that must hold under
  *either* interleaving: the curated edge is never lost or clobbered
  (no lost update), every §6 marker in the tenant references an
  existing edge row (no dangling markers), and both operations'
  synchronous audit rows are present.
* **real advisory-lock path** — a held ``pg_advisory_lock`` on the
  scheduler's ``(tenant, target)`` key makes
  :func:`~meho_backplane.topology.scheduler._refresh_one_target` skip
  the target (no audit row — the refresh never ran), and the same call
  proceeds after release. This is the real-PG replacement for the
  mocked pre-held-lock unit test.

Determinism note: the race test's curated edge hangs off an *external*
from-node (``target_id IS NULL``, not in the connector snapshot).
``_load_reconcile_candidate_nodes`` loads only snapshot-key or
target-owned nodes and ``_load_existing_edges_by_key`` loads edges by
``from_node_id`` over that set, so the concurrent refresh structurally
cannot see — much less soft-delete or rewrite — the curated row no
matter how the two transactions interleave. The assertions therefore
hold on every schedule instead of flaking on a lucky one; the
same-triple auto-vs-curated write-write race is deliberately not
staged here because its outcome is order-defined (last-refresh-wins by
design, see ``docs/architecture/topology.md`` §Soft-delete semantics).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select, text

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector
from meho_backplane.connectors.schemas import (
    CandidateHint,
    EdgeHint,
    FingerprintResult,
    NodeHint,
    OperationResult,
    ProbeResult,
    TopologyHints,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, GraphEdge, GraphNode
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.topology.annotate import NodeRef, annotate_edge
from meho_backplane.topology.refresh import refresh_target_topology
from meho_backplane.topology.scheduler import (
    _advisory_lock_key,
    _refresh_one_target,
    _SchedulerState,
)
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON

# Match the tenant rows the ``pg_engine`` conftest fixture seeds.
TENANT_A_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")

_skip_no_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

_PRODUCT = "race-test-product"

#: Audit ``path`` values the two write paths stamp (mirrors
#: ``refresh._REFRESH_OP_ID`` / ``annotate._ANNOTATE_OP_ID``).
_REFRESH_OP_ID = "topology.refresh"
_ANNOTATE_OP_ID = "topology.annotate"


class _RaceTopoConnector(Connector):
    """Deterministic 3-node / 2-edge snapshot keyed off the target name."""

    product = _PRODUCT

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:
        raise NotImplementedError

    async def discover_topology(self, target: Any) -> TopologyHints:
        tname = target.name
        return TopologyHints(
            nodes=(
                NodeHint(kind="target", name=tname),
                NodeHint(kind="vm", name=f"vm-{tname}"),
                NodeHint(kind="host", name=f"host-{tname}"),
            ),
            edges=(
                EdgeHint(
                    from_kind="target",
                    from_name=tname,
                    to_kind="vm",
                    to_name=f"vm-{tname}",
                    kind="belongs-to",
                ),
                EdgeHint(
                    from_kind="vm",
                    from_name=f"vm-{tname}",
                    to_kind="host",
                    to_name=f"host-{tname}",
                    kind="runs-on",
                ),
            ),
            discovered_at=datetime.now(UTC),
        )

    async def list_candidates(self, seed_target: Any | None = None) -> list[CandidateHint]:
        return []


def _operator(tenant_id: uuid.UUID) -> Operator:
    """Build a minimal :class:`Operator` pinned to *tenant_id*."""
    return Operator(
        sub="op-topology-race",
        name=None,
        email=None,
        raw_jwt="not-a-real-jwt",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.fixture
async def race_target(pg_engine: None) -> AsyncIterator[TargetORM]:
    """Register the connector, insert one target row, yield it loaded.

    The target name carries a per-test random suffix: ``targets`` is
    not in the conftest's per-test TRUNCATE list (it has no graph
    coupling), so a fixed name would collide with the row a previous
    test in the same container session inserted.
    """
    clear_registry()
    _CONNECTOR_INSTANCE_CACHE.clear()
    register_connector(_PRODUCT, _RaceTopoConnector)

    target_id = uuid.uuid4()
    now = datetime.now(UTC)
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        session.add(
            TargetORM(
                id=target_id,
                tenant_id=TENANT_A_ID,
                name=f"race-target-{uuid.uuid4().hex[:8]}",
                product=_PRODUCT,
                host="10.0.0.1",
                aliases=[],
                port=None,
                fqdn=None,
                secret_ref=None,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                notes=None,
                created_at=now,
                updated_at=now,
            )
        )
    async with sm() as session:
        target = (
            await session.execute(select(TargetORM).where(TargetORM.id == target_id))
        ).scalar_one()

    yield target

    clear_registry()
    _CONNECTOR_INSTANCE_CACHE.clear()


async def _audit_count(op_id: str) -> int:
    """Rows in ``audit_log`` whose ``path`` is *op_id* (tenant A)."""
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.tenant_id == TENANT_A_ID, AuditLog.path == op_id)
        )
        return int(result.scalar_one())


async def _assert_no_dangling_markers() -> None:
    """Every §6 marker in the tenant references an existing edge row."""
    sm = get_sessionmaker()
    async with sm() as session:
        edges = (
            (await session.execute(select(GraphEdge).where(GraphEdge.tenant_id == TENANT_A_ID)))
            .scalars()
            .all()
        )
        edge_ids = {str(e.id) for e in edges}
        for edge in edges:
            props = edge.properties or {}
            superseded_by = props.get("superseded_by")
            if superseded_by is not None:
                assert superseded_by in edge_ids, (
                    f"dangling superseded_by marker {superseded_by} on edge {edge.id}"
                )
            for other in props.get("conflicts_with", []) or []:
                assert other in edge_ids, f"dangling conflicts_with entry {other} on edge {edge.id}"


@_skip_no_docker
async def test_refresh_vs_annotate_concurrent_writes_hold_invariants(
    race_target: TargetORM,
) -> None:
    """``asyncio.gather(refresh, annotate)`` — invariants hold either way.

    One initial refresh populates the target's 3-node / 2-edge graph.
    Then three rounds of a genuinely concurrent refresh + annotate (two
    independent asyncpg connections; the annotate's note changes per
    round so the idempotent-upsert merge path races the reconcile too).
    After every round:

    * **No lost update** — the curated ``ext-svc --depends-on-->
      host-<target>`` row exists, is still ``source='curated'``,
      carries the round's note, and is live (``last_seen`` set). The
      concurrent reconcile pass must never soft-delete or rewrite it.
    * **No dangling markers** — every ``superseded_by`` /
      ``conflicts_with`` value in the tenant resolves to an existing
      ``graph_edge`` row.
    * **Both audit rows present** — the synchronous-audit contract
      (spec §6: no success without a committed audit row) holds under
      concurrency: one ``topology.refresh`` and one
      ``topology.annotate`` row per round.
    * The snapshot itself reconciled cleanly (nothing spuriously
      removed).
    """
    op = _operator(TENANT_A_ID)

    first = await refresh_target_topology(race_target, op)
    assert (first.added_nodes, first.added_edges) == (3, 2)

    # The curated edge's from-node: external to the target (no
    # target_id, never in the snapshot) — see the module docstring's
    # determinism note.
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        session.add(
            GraphNode(
                id=uuid.uuid4(),
                tenant_id=TENANT_A_ID,
                kind="service",
                name="ext-svc",
                source="auto",
                properties={},
                discovered_by="test",
            )
        )

    host_name = f"host-{race_target.name}"

    async def _annotate(note: str) -> GraphEdge:
        async with sm() as session:
            return await annotate_edge(
                session,
                op,
                NodeRef(name="ext-svc", kind="service"),
                "depends-on",
                NodeRef(name=host_name, kind="host"),
                note=note,
            )

    rounds = 3
    for round_no in range(rounds):
        note = f"curated race note {round_no}"
        refresh_result, annotated = await asyncio.gather(
            refresh_target_topology(race_target, op),
            _annotate(note),
        )

        # Refresh reconciled the unchanged snapshot: nothing removed.
        assert refresh_result.removed_nodes == 0
        assert refresh_result.removed_edges == 0
        assert annotated.source == "curated"

        # No lost update: reload the curated row from a fresh session.
        async with sm() as session:
            curated = (
                await session.execute(
                    select(GraphEdge).where(
                        GraphEdge.tenant_id == TENANT_A_ID,
                        GraphEdge.id == annotated.id,
                    )
                )
            ).scalar_one()
            assert curated.source == "curated"
            assert curated.properties.get("note") == note
            assert curated.kind == "depends-on"
            assert curated.last_seen is not None

        await _assert_no_dangling_markers()

        # Both synchronous audit rows landed for this round.
        assert await _audit_count(_REFRESH_OP_ID) == 1 + (round_no + 1)  # initial + rounds
        assert await _audit_count(_ANNOTATE_OP_ID) == round_no + 1

    # The snapshot graph survived all rounds live (last-refresh-wins:
    # a concurrent annotate must never knock out probe-derived rows).
    async with sm() as session:
        live_nodes = (
            await session.execute(
                select(func.count())
                .select_from(GraphNode)
                .where(GraphNode.tenant_id == TENANT_A_ID, GraphNode.last_seen.is_not(None))
            )
        ).scalar_one()
    # 3 snapshot nodes; ext-svc was seeded with last_seen NULL (model
    # default) so only the probe-owned rows count here.
    assert int(live_nodes) == 3


@_skip_no_docker
async def test_scheduler_advisory_lock_skips_and_releases_on_real_pg(
    race_target: TargetORM,
) -> None:
    """The per-target advisory lock is exercised against real PG.

    While another connection holds ``pg_advisory_lock`` on the
    scheduler's ``(tenant, target)`` key, ``_refresh_one_target`` must
    skip the target without refreshing (no ``topology.refresh`` audit
    row — the reconcile never ran; the previously unit-tested path used
    a mocked pre-held lock and never touched ``pg_try_advisory_lock``
    itself). After the holder releases, the same call refreshes
    normally — proving the skip was the lock, not an error swallowed by
    the scheduler's failure isolation.
    """
    key = _advisory_lock_key(TENANT_A_ID, race_target.id)
    state = _SchedulerState()
    sm = get_sessionmaker()

    async with sm() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
        try:
            await _refresh_one_target(race_target, state)
            assert await _audit_count(_REFRESH_OP_ID) == 0, (
                "refresh ran despite a held advisory lock — the "
                "stampede guard is broken on real PostgreSQL"
            )
        finally:
            # Session-level lock: release explicitly before the pooled
            # connection returns, or it would leak into the next test.
            await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})

    await _refresh_one_target(race_target, state)
    assert await _audit_count(_REFRESH_OP_ID) == 1
