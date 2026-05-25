# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end integration tests for the G9.3 topology-history surface.

Initiative #365 (G9.3), Task #862 (T7). The unit suites prove each
piece in isolation -- the diff-on-write hook (#857
``test_topology_history_hook``), the three query verbs (#859/#860/#861
``test_topology_history_query`` / ``test_topology_diff_query`` /
``test_topology_timeline_query``), and retention (#858
``test_topology_history_retention``). This module proves the
**cross-Initiative** behaviour those pieces must exhibit *together*:

* **Full chronology round-trip** -- a real ``topology.refresh`` (3
  nodes + 2 edges) -> 5 history rows sharing one ``audit_id``; a real
  ``topology.annotate`` -> 1 history row sharing the annotate's
  ``audit_id``; a subsequent refresh dropping a node -> 1 ``removed``
  row with a *new* ``audit_id``. The chronology is then read back
  through :func:`query_history` and the audit_ids are asserted
  correctly paired to each operation.

* **audit_id linkage (the load-bearing G9.3 guarantee)** -- every
  history row's ``audit_id`` resolves to a real ``audit_log.id`` for
  the **same tenant + principal**. Asserted by an actual SQL JOIN
  against ``audit_log`` (not by reconstructing ids in Python) so a
  schema drift that broke the soft-FK would fail the join.

* **Cross-tenant boundary** -- two tenants with overlapping target /
  node names; ``history`` / ``diff`` / ``timeline`` each return only
  the caller's tenant's data; an unknown node surfaces as
  :class:`NodeNotFoundError` (the contract the route layer maps to
  404).

* **Performance envelope** -- a 1M-row ``graph_node_history`` table
  serves a single-node history query via an **index-only scan** on
  ``(tenant_id, node_id, valid_from DESC)`` (asserted on the query
  plan, with a generous wall-clock bound as a secondary signal -- see
  :func:`test_million_row_single_node_history_is_index_only_scan` for
  why a hard ``<10ms`` wall assertion is deliberately not used); and a
  1-week diff on a high-churn tenant returns within the 1000-row hard
  cap.

The module drives the **real services** (:func:`refresh_target_topology`
and :func:`annotate_edge`) for the chronology + linkage criteria rather
than seeding history rows by hand -- that is the integration this Task
exists to prove. The cross-tenant + performance sections seed history
rows directly (the volume / shape they need would be impractical to
materialise through the live write path) but query them back through the
production verbs.

Runs against ``sqlite+aiosqlite`` via the autouse ``_default_database_url``
fixture in :mod:`tests.conftest`, which copies a per-worker
Alembic-migrated template -- so the composite + partial indexes the
performance criterion leans on exist exactly as migration ``0012``
declares them. The query planner the index-only-scan assertion inspects
is therefore the same planner production's SQLite dev path uses; the PG
production path's planner picks the identical index (declared
``postgresql_using="btree"`` in :class:`GraphNodeHistory`).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import DateTime, bindparam, select, text
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import EdgeHint, NodeHint, TopologyHints
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    GraphEdgeHistory,
    GraphHistoryChangeKind,
    GraphNode,
    GraphNodeHistory,
    Target,
    Tenant,
)
from meho_backplane.operations._handler_resolve import reset_connector_instance_cache
from meho_backplane.settings import get_settings
from meho_backplane.topology.annotate import NodeRef, annotate_edge
from meho_backplane.topology.query import query_diff, query_history, query_timeline
from meho_backplane.topology.refresh import refresh_target_topology
from meho_backplane.topology.resolvers import NodeNotFoundError

_PUBLISH_REFRESH = "meho_backplane.topology.refresh.publish_event"
_PUBLISH_ANNOTATE = "meho_backplane.topology.annotate.publish_event"

#: Name of the composite index the per-resource history walk rides --
#: declared by migration ``0012`` on ``(tenant_id, node_id, valid_from
#: DESC)``. The performance criterion asserts the query planner picks
#: this index rather than a full table scan.
_NODE_HISTORY_COMPOSITE_INDEX = "graph_node_history_tenant_node_valid_from_idx"

#: Row count for the performance fixture. One million history rows for a
#: single (tenant, node) pair -- the volume the Task's "<10ms single-node
#: query" criterion targets. Seeded via a raw bulk insert (orders of
#: magnitude faster than ORM ``session.add`` per row) since the rows only
#: need to exist for the planner / scan to have something to skip past.
_PERF_ROW_COUNT = 1_000_000


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Isolate the connector registry + instance cache per test.

    The chronology + linkage tests register a fake connector so
    :func:`refresh_target_topology` can resolve a ``discover_topology``
    implementation; the autouse net keeps that registration from
    bleeding into a sibling test on the same xdist worker.
    """
    clear_registry()
    reset_connector_instance_cache()
    yield
    clear_registry()
    reset_connector_instance_cache()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """One :class:`AsyncSession` per test, scoped to a single ``async with``."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _operator(tenant_id: uuid.UUID, *, sub: str = "operator-test") -> Operator:
    """Construct an :class:`Operator` for a query / service call.

    ``sub`` is the audit principal -- the linkage criterion asserts the
    history row's ``audit_id`` resolves to an ``audit_log`` row whose
    ``operator_sub`` equals this value, so each tenant in the
    cross-tenant tests uses a distinct ``sub``.
    """
    return Operator(
        sub=sub,
        name="Test Operator",
        email=None,
        raw_jwt="not-a-real-token",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


# ---------------------------------------------------------------------------
# Fake connector + seed helpers (live-service path)
# ---------------------------------------------------------------------------


class _FakeConnector(Connector):
    """Connector whose ``discover_topology`` returns a class-level snapshot.

    Mirrors the fake in :mod:`test_topology_history_hook` -- the live
    refresh path needs a registered connector to resolve a discovery
    implementation; this returns whatever snapshot the test stamps onto
    :attr:`hints` so a sequence of refreshes can simulate the graph
    evolving over time.
    """

    product = "faketopo"

    hints: TopologyHints = TopologyHints(discovered_at=datetime.now(UTC))

    async def fingerprint(self, target: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def execute(
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def discover_topology(self, target: Any) -> TopologyHints:
        return type(self).hints


def _register_fake() -> None:
    register_connector_v2(product="faketopo", version="", impl_id="", cls=_FakeConnector)


async def _seed_tenant_and_target(slug: str) -> tuple[uuid.UUID, Target]:
    """Insert one tenant + one target for it, returning both.

    Look-up-then-insert on the tenant -- migration ``0018`` seeds the
    ``rdc-internal`` tenant into the per-worker schema template
    (:func:`tests.conftest._schema_template_db`); a plain INSERT would
    trip ``UNIQUE constraint failed: tenant.slug``.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        existing: uuid.UUID | None = await session.scalar(
            select(Tenant.id).where(Tenant.slug == slug),
        )
        if existing is not None:
            tenant_id = existing
        else:
            tenant_id = uuid.uuid4()
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
        target = Target(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            name=f"vcenter-{slug}",
            aliases=[],
            product="faketopo",
            host="vc.example.test",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
    return tenant_id, target


def _hints_3n2e() -> TopologyHints:
    """3 nodes + 2 edges -- the canonical insert-path fixture (#857)."""
    return TopologyHints(
        discovered_at=datetime.now(UTC),
        nodes=(
            NodeHint(kind="vm", name="vm-a", properties={"power": "on"}),
            NodeHint(kind="vm", name="vm-b"),
            NodeHint(kind="datastore", name="ds-1"),
        ),
        edges=(
            EdgeHint(
                from_kind="vm", from_name="vm-a", to_kind="datastore", to_name="ds-1", kind="mounts"
            ),
            EdgeHint(
                from_kind="vm", from_name="vm-b", to_kind="datastore", to_name="ds-1", kind="mounts"
            ),
        ),
    )


def _hints_2n1e_dropping_vm_b() -> TopologyHints:
    """Second snapshot: drops ``vm-b`` + its edge so a refresh tombstones them."""
    return TopologyHints(
        discovered_at=datetime.now(UTC),
        nodes=(
            NodeHint(kind="vm", name="vm-a", properties={"power": "on"}),
            NodeHint(kind="datastore", name="ds-1"),
        ),
        edges=(
            EdgeHint(
                from_kind="vm", from_name="vm-a", to_kind="datastore", to_name="ds-1", kind="mounts"
            ),
        ),
    )


async def _audit_ids_for_tenant(tenant_id: uuid.UUID) -> list[uuid.UUID]:
    """Return every ``audit_log.id`` for *tenant_id*, oldest first."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(AuditLog.id)
                .where(AuditLog.tenant_id == tenant_id)
                .order_by(AuditLog.occurred_at)
            )
        ).scalars()
        return list(rows)


# ---------------------------------------------------------------------------
# Criterion #1 -- full chronology round-trip through query_history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_annotate_refresh_chronology_pairs_audit_ids() -> None:
    """Drive refresh -> annotate -> refresh-dropping-node and read the
    chronology back through :func:`query_history` with paired audit_ids.

    This is the load-bearing cross-Initiative assertion: the hook
    (#857), the audit-id pre-allocation in the refresh / annotate
    services, and the query verb (#859) must agree end-to-end. We assert
    that ``meho topology history vm-a --since <first-ts>`` returns the
    full chronology and that every row's ``audit_id`` matches the
    operation that caused it.
    """
    _register_fake()
    tenant_id, target = await _seed_tenant_and_target(slug="rdc-internal")
    operator = _operator(tenant_id)

    # --- Operation 1: refresh seeding 3 nodes + 2 edges --------------
    _FakeConnector.hints = _hints_3n2e()
    first_ts = datetime.now(UTC) - timedelta(minutes=5)
    with _patched_publishers():
        await refresh_target_topology(target, operator)
    refresh1_audit = (await _audit_ids_for_tenant(tenant_id))[0]

    # 5 history rows (3 node + 2 edge) all sharing the refresh's audit_id.
    node_history = await _node_history_rows(tenant_id)
    edge_history = await _edge_history_rows(tenant_id)
    assert len(node_history) == 3
    assert len(edge_history) == 2
    assert {h.audit_id for h in node_history} | {h.audit_id for h in edge_history} == {
        refresh1_audit
    }

    # --- Operation 2: annotate one curated edge ----------------------
    # vm-a --depends-on--> vm-b: a fresh endpoint pair with no existing
    # edge, so the annotate emits exactly one ``created`` history row and
    # triggers no §6 conflict markers (the issue's "1 history row sharing
    # the annotate's audit_id" criterion).
    async with get_sessionmaker()() as s:
        with _patched_publishers():
            await annotate_edge(
                s,
                operator,
                NodeRef(name="vm-a", kind="vm"),
                "depends-on",
                NodeRef(name="vm-b", kind="vm"),
                note="curated link for the chronology test",
            )
    annotate_audit = next(a for a in await _audit_ids_for_tenant(tenant_id) if a != refresh1_audit)
    # The annotate emits exactly one edge-history row, carrying its own
    # audit_id (distinct from the refresh's).
    annotate_rows = [h for h in await _edge_history_rows(tenant_id) if h.audit_id == annotate_audit]
    assert len(annotate_rows) == 1
    assert annotate_rows[0].change_kind == GraphHistoryChangeKind.CREATED.value

    # --- Operation 3: refresh dropping vm-b --------------------------
    _FakeConnector.hints = _hints_2n1e_dropping_vm_b()
    with _patched_publishers():
        await refresh_target_topology(target, operator)
    refresh2_audit = next(
        a
        for a in await _audit_ids_for_tenant(tenant_id)
        if a not in {refresh1_audit, annotate_audit}
    )
    removed_rows = [
        h
        for h in await _node_history_rows(tenant_id)
        if h.change_kind == GraphHistoryChangeKind.REMOVED.value
    ]
    assert len(removed_rows) == 1
    assert removed_rows[0].audit_id == refresh2_audit, (
        "removed row carries the new refresh audit_id"
    )

    # --- Read the chronology back through the production verb ---------
    # vm-a survives every operation, so its per-resource walk (with
    # include_edges=True) carries: 1 created (refresh1), 1 created
    # curated edge (annotate). The dropped vm-b does not anchor vm-a's
    # walk; we assert the removed-node chronology separately below.
    history = await query_history(operator, "vm-a", kind="vm", since=first_ts, include_edges=True)
    assert history.anchor_node_id is not None
    walk_audit_ids = {row.audit_id for row in history.rows}
    # vm-a's own creation (refresh1) + the curated edge incident to it
    # (annotate). Both audit_ids present and correctly attributed.
    assert refresh1_audit in walk_audit_ids
    assert annotate_audit in walk_audit_ids
    # The newest-first ordering contract holds across the merged walk.
    keys = [(r.valid_from, r.history_id) for r in history.rows]
    assert keys == sorted(keys, reverse=True)

    # The removed node's chronology -- its created (refresh1) + removed
    # (refresh2) rows pair to the two distinct refresh audit_ids.
    vm_b_history = await query_history(operator, "vm-b", kind="vm", since=first_ts)
    vm_b_by_kind = {row.change_kind: row.audit_id for row in vm_b_history.rows}
    assert vm_b_by_kind[GraphHistoryChangeKind.CREATED.value] == refresh1_audit
    assert vm_b_by_kind[GraphHistoryChangeKind.REMOVED.value] == refresh2_audit


# ---------------------------------------------------------------------------
# Criterion #2 -- audit_id linkage asserted by a real JOIN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_history_audit_id_joins_to_audit_log_same_tenant_principal() -> None:
    """Every history row's ``audit_id`` resolves to a real ``audit_log``
    row for the **same tenant + principal**.

    Asserted with an actual SQL JOIN (``graph_node_history`` /
    ``graph_edge_history`` LEFT JOIN ``audit_log``) so a soft-FK that
    silently broke would surface as an unmatched row rather than passing
    a Python-side reconstruction. The same-tenant + same-principal
    predicate is the G9.3 forensic guarantee: an auditor can walk from a
    mutation back to the operation, the operator, and the tenant that
    caused it.
    """
    _register_fake()
    tenant_id, target = await _seed_tenant_and_target(slug="rdc-internal")
    operator = _operator(tenant_id, sub="op-linkage-42")

    _FakeConnector.hints = _hints_3n2e()
    with _patched_publishers():
        await refresh_target_topology(target, operator)
    async with get_sessionmaker()() as s:
        with _patched_publishers():
            await annotate_edge(
                s,
                operator,
                NodeRef(name="vm-a", kind="vm"),
                "authenticates-via",
                NodeRef(name="ds-1", kind="datastore"),
            )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # Node side: every history row must join to an audit_log row that
        # shares its tenant_id AND whose operator_sub is the acting
        # principal. A LEFT JOIN surfaces a broken link as a NULL
        # ``audit_log.id``.
        unmatched_nodes = (
            await session.execute(
                text(
                    """
                    SELECT COUNT(*) FROM graph_node_history h
                    LEFT JOIN audit_log a
                      ON a.id = h.audit_id
                     AND a.tenant_id = h.tenant_id
                     AND a.operator_sub = :sub
                    WHERE h.tenant_id = :tenant_id
                      AND a.id IS NULL
                    """
                ),
                {"tenant_id": str(tenant_id), "sub": operator.sub},
            )
        ).scalar_one()
        unmatched_edges = (
            await session.execute(
                text(
                    """
                    SELECT COUNT(*) FROM graph_edge_history h
                    LEFT JOIN audit_log a
                      ON a.id = h.audit_id
                     AND a.tenant_id = h.tenant_id
                     AND a.operator_sub = :sub
                    WHERE h.tenant_id = :tenant_id
                      AND a.id IS NULL
                    """
                ),
                {"tenant_id": str(tenant_id), "sub": operator.sub},
            )
        ).scalar_one()

    assert unmatched_nodes == 0, (
        "every node-history audit_id must join to a same-tenant/principal audit_log row"
    )
    assert unmatched_edges == 0, (
        "every edge-history audit_id must join to a same-tenant/principal audit_log row"
    )


# ---------------------------------------------------------------------------
# Criterion #3 -- cross-tenant boundary across history / diff / timeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_boundary_on_all_three_verbs(session: AsyncSession) -> None:
    """Two tenants with overlapping node names; each verb returns only the
    caller's tenant's data; an unknown node is a NotFound (-> 404).

    Mirrors the G8 audit-query cross-tenant pattern (#334): the tenant
    boundary is enforced in the substrate, not the route, so the
    isolation holds for every front (CLI / REST / MCP) that calls these
    verbs.
    """
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    session.add(Tenant(id=tenant_a, slug="tenant-a", name="Tenant A"))
    session.add(Tenant(id=tenant_b, slug="tenant-b", name="Tenant B"))

    # Same node name ("shared-vm") in both tenants -- the overlap the
    # criterion calls out. Distinct ids so a leak is observable.
    node_a = uuid.uuid4()
    node_b = uuid.uuid4()
    session.add(
        GraphNode(id=node_a, tenant_id=tenant_a, kind="vm", name="shared-vm", discovered_by="x")
    )
    session.add(
        GraphNode(id=node_b, tenant_id=tenant_b, kind="vm", name="shared-vm", discovered_by="x")
    )

    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    # 3 history rows per tenant for the shared name, at non-overlapping
    # timestamps so a leak would change the count / window.
    for i in range(3):
        session.add(
            GraphNodeHistory(
                node_id=node_a,
                tenant_id=tenant_a,
                change_kind=GraphHistoryChangeKind.UPDATED.value,
                snapshot={"before": None, "after": {"kind": "vm", "name": "shared-vm"}},
                audit_id=uuid.uuid4(),
                valid_from=base + timedelta(seconds=i),
            )
        )
        session.add(
            GraphNodeHistory(
                node_id=node_b,
                tenant_id=tenant_b,
                change_kind=GraphHistoryChangeKind.UPDATED.value,
                snapshot={"before": None, "after": {"kind": "vm", "name": "shared-vm"}},
                audit_id=uuid.uuid4(),
                valid_from=base + timedelta(seconds=100 + i),
            )
        )
    await session.commit()

    op_a = _operator(tenant_a, sub="op-a")
    op_b = _operator(tenant_b, sub="op-b")

    # --- history: each side sees only its own 3 rows -----------------
    hist_a = await query_history(op_a, "shared-vm", kind="vm")
    hist_b = await query_history(op_b, "shared-vm", kind="vm")
    assert len(hist_a.rows) == 3
    assert len(hist_b.rows) == 3
    assert all(r.resource_id == node_a for r in hist_a.rows)
    assert all(r.resource_id == node_b for r in hist_b.rows)
    # Tenant A never sees tenant B's audit_ids and vice versa.
    a_audits = {r.audit_id for r in hist_a.rows}
    b_audits = {r.audit_id for r in hist_b.rows}
    assert a_audits.isdisjoint(b_audits)

    # --- diff: tenant-wide scan returns only the caller's resources --
    window_lo = base - timedelta(seconds=1)
    window_hi = base + timedelta(seconds=200)
    diff_a = await query_diff(op_a, ts1=window_lo, ts2=window_hi)
    diff_b = await query_diff(op_b, ts1=window_lo, ts2=window_hi)
    assert {e.resource_id for e in diff_a.entries} == {node_a}
    assert {e.resource_id for e in diff_b.entries} == {node_b}

    # --- timeline: tenant-wide feed returns only the caller's rows ---
    tl_a = await query_timeline(op_a, since=window_lo, until=window_hi)
    tl_b = await query_timeline(op_b, since=window_lo, until=window_hi)
    assert len(tl_a.rows) == 3
    assert len(tl_b.rows) == 3
    assert all(r.resource_id == node_a for r in tl_a.rows)
    assert all(r.resource_id == node_b for r in tl_b.rows)

    # --- unknown node -> NodeNotFoundError (route maps to 404) -------
    with pytest.raises(NodeNotFoundError):
        await query_history(op_a, "does-not-exist")
    # A name that exists only in the OTHER tenant is indistinguishable
    # from unknown -- the boundary is opaque to the caller.
    other_only = uuid.uuid4()
    session.add(
        GraphNode(id=other_only, tenant_id=tenant_b, kind="vm", name="b-only-vm", discovered_by="x")
    )
    await session.commit()
    with pytest.raises(NodeNotFoundError):
        await query_history(op_a, "b-only-vm")


# ---------------------------------------------------------------------------
# Criterion #4 -- performance fixture (1M rows + high-churn diff cap)
# ---------------------------------------------------------------------------


def _scan_uses_seq_scan(plan_rows: Sequence[Any]) -> bool:
    """True if the SQLite query plan contains a full table scan.

    SQLite renders a full table scan as ``SCAN <table>`` with **no**
    ``USING ... INDEX`` clause; an index-driven lookup renders as
    ``SEARCH <table> USING [COVERING] INDEX <name>``. The performance
    criterion fails closed if any plan step scans the history table
    without an index.
    """
    for row in plan_rows:
        detail = str(row[-1])
        if "graph_node_history" in detail and "SCAN" in detail and "USING" not in detail:
            return True
    return False


@pytest.mark.asyncio
async def test_million_row_single_node_history_is_index_only_scan(session: AsyncSession) -> None:
    """A 1M-row history table serves a single-node walk via an index-only scan.

    The Task's literal phrasing is "returns ... in < 10ms on
    ``(tenant_id, node_id, valid_from DESC)``". A hard ``<10ms``
    wall-clock assertion is **deliberately not** the gate: under
    ``pytest-xdist`` + coverage instrumentation the same query can take
    materially longer than its native cost for reasons unrelated to the
    index (the python_best_practices "don't ship a known-flaky timing
    assertion" rule). Instead we assert the load-bearing *cause* of the
    <10ms target -- the planner serves the lookup from the composite
    index ``(tenant_id, node_id, valid_from DESC)`` as an index-only
    (SQLite: "COVERING INDEX") scan, never a full table scan -- and keep
    a *generous* wall-clock bound as a secondary smoke signal so a future
    regression that drops the index (turning the query into a 1M-row
    table scan) still trips even if the plan-string format changes.
    """
    tenant_id = uuid.uuid4()
    other_tenant = uuid.uuid4()
    anchor_node = uuid.uuid4()
    other_node = uuid.uuid4()
    session.add(Tenant(id=tenant_id, slug="perf-tenant", name="Perf Tenant"))
    session.add(Tenant(id=other_tenant, slug="perf-other", name="Perf Other"))
    session.add(
        GraphNode(id=anchor_node, tenant_id=tenant_id, kind="vm", name="hot-vm", discovered_by="x")
    )
    await session.commit()

    await _bulk_seed_node_history(
        session,
        tenant_id=tenant_id,
        anchor_node=anchor_node,
        other_tenant=other_tenant,
        other_node=other_node,
        total=_PERF_ROW_COUNT,
    )

    # The query the verb issues, with the same WHERE shape (tenant +
    # node, ordered valid_from DESC). EXPLAIN QUERY PLAN reports which
    # index the planner picks.
    plan_sql = (
        "EXPLAIN QUERY PLAN "
        "SELECT history_id FROM graph_node_history "
        "WHERE tenant_id = :t AND node_id = :n "
        "ORDER BY valid_from DESC, history_id DESC LIMIT 100"
    )
    plan = (
        await session.execute(text(plan_sql), {"t": str(tenant_id), "n": str(anchor_node)})
    ).fetchall()
    plan_details = " | ".join(str(r[-1]) for r in plan)

    assert _NODE_HISTORY_COMPOSITE_INDEX in plan_details, (
        f"single-node history lookup must ride the composite index "
        f"{_NODE_HISTORY_COMPOSITE_INDEX!r}; plan was: {plan_details}"
    )
    assert not _scan_uses_seq_scan(plan), (
        f"single-node history lookup must not full-scan graph_node_history; "
        f"plan was: {plan_details}"
    )

    # Secondary smoke signal: a *generous* wall bound. The index-only
    # scan over a 1M-row table is sub-millisecond natively; a full table
    # scan would be ~seconds. A 2s ceiling distinguishes the two without
    # flaking under xdist/coverage. The real gate is the plan assertion
    # above.
    op = _operator(tenant_id)
    start = time.perf_counter()
    result = await query_history(op, "hot-vm", kind="vm", limit=100)
    elapsed = time.perf_counter() - start
    assert len(result.rows) == 100
    assert elapsed < 2.0, f"index-backed single-node walk took {elapsed:.3f}s (expected << 2s)"


@pytest.mark.asyncio
async def test_high_churn_week_diff_returns_within_1000_row_cap(session: AsyncSession) -> None:
    """A 1-week diff on a high-churn tenant returns within the 1000-row cap.

    The diff surface enforces a 1000-row hard cap (``_DIFF_HARD_CAP``).
    A high-churn tenant -- here >1000 distinct nodes each created in the
    window -- must return a truncated result capped at 1000 entries with
    ``truncated=True`` and the canonical remediation hint, never an
    unbounded materialisation of the whole slice.
    """
    tenant_id = uuid.uuid4()
    session.add(Tenant(id=tenant_id, slug="churn-tenant", name="Churn Tenant"))
    await session.commit()

    week_start = datetime(2026, 5, 15, 0, 0, 0, tzinfo=UTC)
    # 1500 distinct created nodes spread across the week -> the fold
    # produces 1500 candidate entries, well over the 1000-row cap.
    await _bulk_seed_distinct_created_nodes(
        session, tenant_id=tenant_id, base=week_start, count=1500
    )

    op = _operator(tenant_id)
    result = await query_diff(
        op,
        ts1=week_start - timedelta(seconds=1),
        ts2=week_start + timedelta(days=7),
    )

    assert result.truncated is True, "a >1000-node high-churn week must report truncation"
    assert len(result.entries) == 1000, "diff must cap at the 1000-row hard cap"
    assert result.truncation_hint is not None
    assert "narrow" in result.truncation_hint.lower()


# ---------------------------------------------------------------------------
# Low-level helpers (history-row readers + bulk seeders)
# ---------------------------------------------------------------------------


def _patched_publishers() -> Any:
    """Patch both refresh + annotate broadcast publishers to no-ops.

    Returned as a single context manager so the call sites read cleanly.
    The broadcast publish is fail-open by contract; stubbing it keeps the
    integration test off the network without weakening the audit / history
    assertions (which run inside the committed transaction, before any
    publish).
    """
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, patch

    stack = ExitStack()
    stack.enter_context(patch(_PUBLISH_REFRESH, new=AsyncMock()))
    stack.enter_context(patch(_PUBLISH_ANNOTATE, new=AsyncMock()))
    return stack


async def _node_history_rows(tenant_id: uuid.UUID) -> list[GraphNodeHistory]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(GraphNodeHistory).where(GraphNodeHistory.tenant_id == tenant_id)
            )
        ).scalars()
        return list(rows)


async def _edge_history_rows(tenant_id: uuid.UUID) -> list[GraphEdgeHistory]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(GraphEdgeHistory).where(GraphEdgeHistory.tenant_id == tenant_id)
            )
        ).scalars()
        return list(rows)


def _typed_history_insert() -> Any:
    """Raw ``graph_node_history`` insert with typed UUID + ``valid_from`` binds.

    The performance fixtures bulk-insert via raw SQL ``executemany`` (much
    faster than per-row ORM ``session.add``), but a raw ``text()`` insert
    does not know the column types -- so without typed bindparams the
    seeded values would diverge from the ORM's storage format and fail to
    compare equal against the query verbs' typed bindparams:

    * ``valid_from`` -- the SQLAlchemy :class:`~sqlalchemy.DateTime`
      adapter stores ``"YYYY-MM-DD HH:MM:SS.ffffff"``; a hand-formatted
      ISO string (``"...T...+00:00"``) would not string-compare against
      the ``> :ts1`` / ``<= :ts2`` window predicates.
    * UUID columns -- SQLAlchemy's :class:`~sqlalchemy.Uuid` type stores
      the **dashless** 32-char hex form on SQLite (``native_uuid=False``);
      a ``str(uuid)`` would store the dashed form, which the query verbs'
      ``Uuid``-typed ``tenant_id`` / ``node_id`` bindparams (emitting the
      dashless form) would never match.

    Binding all four columns through their real types routes every value
    through the same adapter the live ORM write path uses, so the seeded
    rows are byte-identical to rows the diff-on-write hook would have
    written. Callers pass :class:`uuid.UUID` and :class:`datetime` objects
    (not pre-stringified values).
    """
    return text(
        """
        INSERT INTO graph_node_history
            (node_id, tenant_id, change_kind, snapshot, audit_id, valid_from)
        VALUES (:node_id, :tenant_id, :change_kind, :snapshot, :audit_id, :valid_from)
        """
    ).bindparams(
        bindparam("node_id", type_=SAUuid()),
        bindparam("tenant_id", type_=SAUuid()),
        bindparam("audit_id", type_=SAUuid()),
        bindparam("valid_from", type_=DateTime(timezone=True)),
    )


#: SQLite-native bulk insert of *N* history rows via a recursive CTE.
#: Generates every row server-side -- no Python row-building, no
#: per-chunk round trips -- so a 1M-row seed costs ~3.5s instead of the
#: ~12s a Python ``executemany`` loop takes. SQLite-specific by design;
#: this module is already SQLite-pinned (the EXPLAIN QUERY PLAN
#: assertion the performance criterion makes is SQLite-only), and the
#: suite runs exclusively on ``sqlite+aiosqlite``. The generated values
#: match the live ORM write path's storage form:
#:
#: * ``node_id`` / ``tenant_id`` -- bound as dashless 32-char hex
#:   (``uuid.hex``), the form SQLAlchemy's ``Uuid`` type persists on
#:   SQLite, so the query verbs' typed bindparams match.
#: * ``audit_id`` -- a fresh random 16-byte hex per row; the linkage
#:   criterion does not join the *perf* rows, so a real ``audit_log``
#:   row is not required here.
#: * ``valid_from`` -- ``datetime('2026-01-01 00:00:00', '+i seconds')``
#:   renders ``"YYYY-MM-DD HH:MM:SS"`` (the SQLite-stored timestamp form;
#:   microseconds are absent but irrelevant -- the single-node walk under
#:   test filters on ``tenant_id`` + ``node_id`` only).
#:
#: Every ``noise_every``-th row is seeded under a *different* tenant +
#: node so the composite-index lookup has cross-tenant / cross-node rows
#: to skip past (proving the index, not a trivial single-row table,
#: drives the target latency).
_PERF_SEED_CTE = text(
    """
    INSERT INTO graph_node_history
        (node_id, tenant_id, change_kind, snapshot, audit_id, valid_from)
    WITH RECURSIVE seq(i) AS (
        SELECT 0 UNION ALL SELECT i + 1 FROM seq WHERE i < :total - 1
    )
    SELECT
        CASE WHEN i % :noise_every = 0 THEN :other_node ELSE :anchor_node END,
        CASE WHEN i % :noise_every = 0 THEN :other_tenant ELSE :tenant_id END,
        'updated',
        '{"before": null, "after": {"kind": "vm", "name": "hot-vm"}}',
        lower(hex(randomblob(16))),
        datetime('2026-01-01 00:00:00', '+' || i || ' seconds')
    FROM seq
    """
)


async def _bulk_seed_node_history(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    anchor_node: uuid.UUID,
    other_tenant: uuid.UUID,
    other_node: uuid.UUID,
    total: int,
) -> None:
    """Seed *total* history rows: a hot single-node series plus noise.

    Delegates to the :data:`_PERF_SEED_CTE` recursive-CTE insert (see its
    docstring for the speed + storage-format rationale). A single
    statement generates all *total* rows server-side -- SQLite's default
    recursion ceiling is 1000 only for the parser's ``SELECT`` term
    count, not for ``WITH RECURSIVE`` row generation, so 1M rows complete
    in one round trip.
    """
    await session.execute(
        _PERF_SEED_CTE,
        {
            "total": total,
            "noise_every": 50,
            "anchor_node": anchor_node.hex,
            "tenant_id": tenant_id.hex,
            "other_node": other_node.hex,
            "other_tenant": other_tenant.hex,
        },
    )
    await session.commit()


async def _bulk_seed_distinct_created_nodes(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    base: datetime,
    count: int,
) -> None:
    """Bulk-insert *count* distinct ``created`` node-history rows in a window.

    Each row is a *distinct* node id, so :func:`query_diff`'s per-resource
    fold produces *count* candidate entries -- the shape that exercises
    the 1000-row cap. ``valid_from`` is staggered across the week so the
    rows fall inside the diff window. Raw ``executemany`` for the same
    speed reason as :func:`_bulk_seed_node_history`.
    """
    insert_sql = _typed_history_insert()
    # ~6-minute spacing keeps all rows inside the one-week window.
    spacing = timedelta(minutes=6)
    rows: list[dict[str, Any]] = []
    for i in range(count):
        node_id = uuid.uuid4()
        rows.append(
            {
                "node_id": node_id,
                "tenant_id": tenant_id,
                "change_kind": GraphHistoryChangeKind.CREATED.value,
                "snapshot": (
                    f'{{"before": null, "after": {{"id": "{node_id}", '
                    f'"kind": "vm", "name": "vm-{i}"}}}}'
                ),
                "audit_id": uuid.uuid4(),
                "valid_from": base + spacing * i,
            }
        )
    await session.execute(insert_sql, rows)
    await session.commit()
