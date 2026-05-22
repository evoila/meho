# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G9.3-T2 diff-on-write history hook.

Coverage matrix (Task #857 acceptance criteria):

* **Insert path** -- a refresh adding 3 nodes + 2 edges produces 5
  history rows in the same transaction, all sharing the refresh's
  ``audit_id`` (acceptance criterion #1).
* **Annotate path** -- :func:`annotate_edge` adding one curated edge
  produces one ``GraphEdgeHistory`` row sharing the annotate's
  ``audit_id`` (acceptance criterion #2).
* **Refresh-removes-node path** -- a second refresh dropping a
  previously-discovered node produces one ``removed`` history row
  whose ``snapshot.before`` carries the full row JSON and ``after``
  is ``None``, under a new ``audit_id`` (criterion #3).
* **Transactional** -- a forced failure inside the reconcile rolls
  the live mutation **and** the history rows back together; both
  ``graph_node`` / ``graph_edge`` and ``graph_node_history`` /
  ``graph_edge_history`` are unchanged (criterion #4).
* **No own audit rows** -- a refresh writes one ``audit_log`` row;
  the diff-on-write hook does not emit additional audit rows per
  history insert (criterion #5).
* **Unannotate path** -- removing a curated edge emits a
  ``removed`` history row for that edge plus an ``updated`` row for
  every edge whose §6 marker the unannotate cleared, all sharing
  one ``audit_id``.
* **§6 conflict marker history** -- annotating a curated edge that
  supersedes an existing auto edge emits two history rows in the
  same transaction (curated ``created`` + auto ``updated`` with the
  ``superseded_by`` marker visible in ``snapshot.after``).
* **Update-without-mutation no-op** -- a refresh that re-asserts an
  unchanged node + edge produces zero history rows (the ``last_seen``
  bump alone is not a recorded mutation).

Runs against ``sqlite+aiosqlite`` via the shared engine cache the
autouse ``_default_database_url`` fixture in :mod:`tests.conftest`
pre-migrates -- same shape every other topology unit test uses.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import EdgeHint, NodeHint, TopologyHints
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    GraphEdge,
    GraphEdgeHistory,
    GraphHistoryChangeKind,
    GraphNode,
    GraphNodeHistory,
    Target,
    Tenant,
)
from meho_backplane.operations._handler_resolve import reset_connector_instance_cache
from meho_backplane.settings import get_settings
from meho_backplane.topology.annotate import NodeRef, annotate_edge, unannotate_edge
from meho_backplane.topology.refresh import refresh_target_topology

_PUBLISH_REFRESH = "meho_backplane.topology.refresh.publish_event"
_PUBLISH_ANNOTATE = "meho_backplane.topology.annotate.publish_event"


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
    """Isolate the connector registry + instance cache per test."""
    clear_registry()
    reset_connector_instance_cache()
    yield
    clear_registry()
    reset_connector_instance_cache()


# ---------------------------------------------------------------------------
# Fakes & helpers
# ---------------------------------------------------------------------------


class _FakeConnector(Connector):
    """Connector whose ``discover_topology`` returns a class-level snapshot."""

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
    register_connector_v2(
        product="faketopo",
        version="",
        impl_id="",
        cls=_FakeConnector,
    )


async def _seed_tenant_and_target(slug: str = "rdc-internal") -> tuple[uuid.UUID, Target]:
    """Insert one tenant + one target for it."""
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()
    target = Target(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=f"vcenter-{slug}",
        aliases=[],
        product="faketopo",
        host="vc.example.test",
    )
    async with sessionmaker() as session:
        session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
        session.add(target)
        await session.commit()
        await session.refresh(target)
    return tenant_id, target


def _operator(tenant_id: uuid.UUID) -> Operator:
    return Operator(
        sub="op-1",
        name="Op One",
        email=None,
        raw_jwt="",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


def _hints_3n2e() -> TopologyHints:
    """3 nodes + 2 edges -- the canonical insert-path fixture from #857."""
    return TopologyHints(
        discovered_at=datetime.now(UTC),
        nodes=(
            NodeHint(kind="vm", name="vm-a", properties={"power": "on"}),
            NodeHint(kind="vm", name="vm-b"),
            NodeHint(kind="datastore", name="ds-1"),
        ),
        edges=(
            EdgeHint(
                from_kind="vm",
                from_name="vm-a",
                to_kind="datastore",
                to_name="ds-1",
                kind="mounts",
            ),
            EdgeHint(
                from_kind="vm",
                from_name="vm-b",
                to_kind="datastore",
                to_name="ds-1",
                kind="mounts",
            ),
        ),
    )


def _hints_2n1e_dropping_vm_b() -> TopologyHints:
    """Second snapshot: drops ``vm-b`` + its edge."""
    return TopologyHints(
        discovered_at=datetime.now(UTC),
        nodes=(
            NodeHint(kind="vm", name="vm-a", properties={"power": "on"}),
            NodeHint(kind="datastore", name="ds-1"),
        ),
        edges=(
            EdgeHint(
                from_kind="vm",
                from_name="vm-a",
                to_kind="datastore",
                to_name="ds-1",
                kind="mounts",
            ),
        ),
    )


async def _all_node_history(tenant_id: uuid.UUID) -> list[GraphNodeHistory]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(GraphNodeHistory).where(GraphNodeHistory.tenant_id == tenant_id)
            )
        ).scalars()
        return list(rows)


async def _all_edge_history(tenant_id: uuid.UUID) -> list[GraphEdgeHistory]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(GraphEdgeHistory).where(GraphEdgeHistory.tenant_id == tenant_id)
            )
        ).scalars()
        return list(rows)


async def _all_audit_log(tenant_id: uuid.UUID) -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            await session.execute(select(AuditLog).where(AuditLog.tenant_id == tenant_id))
        ).scalars()
        return list(rows)


# ---------------------------------------------------------------------------
# Acceptance criterion #1 -- refresh inserts 3 nodes + 2 edges -> 5 history rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_3n2e_emits_5_history_rows_sharing_audit_id() -> None:
    """A refresh adding 3 nodes + 2 edges produces 5 history rows in one txn.

    Acceptance criterion #1 of #857 -- and the load-bearing audit_id
    linkage: every history row carries the **same** ``audit_id`` as the
    refresh's single ``audit_log`` row, so an auditor can join
    ``graph_node_history`` / ``graph_edge_history`` back against the
    causing operation.
    """
    _register_fake()
    tenant_id, target = await _seed_tenant_and_target()
    _FakeConnector.hints = _hints_3n2e()

    with patch(_PUBLISH_REFRESH, new=AsyncMock()):
        await refresh_target_topology(target, _operator(tenant_id))

    node_history = await _all_node_history(tenant_id)
    edge_history = await _all_edge_history(tenant_id)
    audits = await _all_audit_log(tenant_id)

    assert len(node_history) == 3, "expected 3 node history rows"
    assert len(edge_history) == 2, "expected 2 edge history rows"

    # Acceptance criterion #5: the diff-on-write hook emits no audit
    # rows of its own. The refresh writes exactly one audit row; the
    # history rows reference it.
    assert len(audits) == 1, "expected exactly one audit_log row (refresh's own)"
    refresh_audit_id = audits[0].id

    history_audit_ids = {h.audit_id for h in node_history} | {h.audit_id for h in edge_history}
    assert history_audit_ids == {refresh_audit_id}, (
        f"all history rows must share refresh's audit_id; got {history_audit_ids}"
    )

    # Every history row is a CREATED change with snapshot.before=None.
    for nh in node_history:
        assert nh.change_kind == GraphHistoryChangeKind.CREATED.value
        assert nh.snapshot["before"] is None
        assert nh.snapshot["after"] is not None
    for eh in edge_history:
        assert eh.change_kind == GraphHistoryChangeKind.CREATED.value
        assert eh.snapshot["before"] is None
        assert eh.snapshot["after"] is not None


# ---------------------------------------------------------------------------
# Acceptance criterion #2 -- annotate one curated edge -> one history row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_annotate_one_edge_emits_one_edge_history_row_with_shared_audit_id() -> None:
    """One curated annotation produces one edge history row carrying the
    annotate's ``audit_id``.

    Acceptance criterion #2 of #857.
    """
    _register_fake()
    tenant_id, target = await _seed_tenant_and_target()
    # Seed two nodes so the annotate has endpoints to resolve.
    _FakeConnector.hints = TopologyHints(
        discovered_at=datetime.now(UTC),
        nodes=(
            NodeHint(kind="principal", name="sa-foo"),
            NodeHint(kind="vault-role", name="role-bar"),
        ),
    )

    with patch(_PUBLISH_REFRESH, new=AsyncMock()):
        await refresh_target_topology(target, _operator(tenant_id))

    # Reset history -- focus this assertion on the annotate's emission.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        await session.execute(delete(GraphEdgeHistory))
        await session.execute(delete(GraphNodeHistory))
        await session.execute(delete(AuditLog))

    operator = _operator(tenant_id)
    async with sessionmaker() as session:
        with patch(_PUBLISH_ANNOTATE, new=AsyncMock()):
            await annotate_edge(
                session,
                operator,
                NodeRef(name="sa-foo", kind="principal"),
                "authenticates-via",
                NodeRef(name="role-bar", kind="vault-role"),
                note="rdc-vault hashicorp policy v3",
            )

    edge_history = await _all_edge_history(tenant_id)
    audits = await _all_audit_log(tenant_id)

    assert len(edge_history) == 1, "annotate should emit exactly one edge history row"
    assert len(audits) == 1, "annotate should emit exactly one audit_log row"
    history_row = edge_history[0]
    assert history_row.audit_id == audits[0].id, (
        "history row's audit_id must match the annotate's own audit_log.id"
    )
    assert history_row.change_kind == GraphHistoryChangeKind.CREATED.value
    assert history_row.snapshot["before"] is None
    after_state = history_row.snapshot["after"]
    assert isinstance(after_state, dict)
    assert after_state["source"] == "curated"
    assert after_state["kind"] == "authenticates-via"


# ---------------------------------------------------------------------------
# Acceptance criterion #3 -- refresh drops a node -> 1 removed row with new audit_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_dropping_node_emits_removed_history_with_new_audit_id() -> None:
    """A second refresh that drops a node emits 1 ``removed`` history row
    with ``snapshot.before`` = full row, ``after`` = None, under a NEW
    ``audit_id``.

    Acceptance criterion #3 of #857.
    """
    _register_fake()
    tenant_id, target = await _seed_tenant_and_target()

    _FakeConnector.hints = _hints_3n2e()
    with patch(_PUBLISH_REFRESH, new=AsyncMock()):
        await refresh_target_topology(target, _operator(tenant_id))
    first_audits = await _all_audit_log(tenant_id)
    assert len(first_audits) == 1
    first_audit_id = first_audits[0].id

    _FakeConnector.hints = _hints_2n1e_dropping_vm_b()
    with patch(_PUBLISH_REFRESH, new=AsyncMock()):
        await refresh_target_topology(target, _operator(tenant_id))

    audits = await _all_audit_log(tenant_id)
    assert len(audits) == 2, "second refresh should add a second audit row"
    second_audit_id = next(a.id for a in audits if a.id != first_audit_id)

    node_history = await _all_node_history(tenant_id)
    edge_history = await _all_edge_history(tenant_id)

    removed_nodes = [
        h for h in node_history if h.change_kind == GraphHistoryChangeKind.REMOVED.value
    ]
    removed_edges = [
        h for h in edge_history if h.change_kind == GraphHistoryChangeKind.REMOVED.value
    ]

    assert len(removed_nodes) == 1, "exactly one node should be soft-removed"
    assert len(removed_edges) == 1, "exactly one edge should be soft-removed"

    removed_node_row = removed_nodes[0]
    assert removed_node_row.audit_id == second_audit_id, (
        "removed node's history row must carry the second refresh's audit_id"
    )
    assert removed_node_row.snapshot["after"] is None
    before_state = removed_node_row.snapshot["before"]
    assert isinstance(before_state, dict)
    assert before_state["name"] == "vm-b"
    assert before_state["last_seen"] is not None, (
        "snapshot.before must capture the row as the operator last saw it (live last_seen)"
    )

    removed_edge_row = removed_edges[0]
    assert removed_edge_row.audit_id == second_audit_id


# ---------------------------------------------------------------------------
# Acceptance criterion #4 -- transactional atomicity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_failure_rolls_back_both_live_and_history_rows() -> None:
    """A forced failure after live writes but before commit rolls both
    the live mutation and the history row back together.

    Acceptance criterion #4 of #857 -- the load-bearing atomicity
    contract: the live graph and the history table can never disagree
    about which mutations committed.
    """
    _register_fake()
    tenant_id, target = await _seed_tenant_and_target()
    _FakeConnector.hints = _hints_3n2e()

    sessionmaker = get_sessionmaker()

    # Mock the audit-row writer (called inside the reconcile txn just
    # before commit) to raise; the whole transaction must roll back.
    async def _broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated mid-reconcile failure")

    with (
        patch(_PUBLISH_REFRESH, new=AsyncMock()),
        patch(
            "meho_backplane.topology.refresh._write_audit_and_broadcast",
            new=_broken,
        ),
        pytest.raises(RuntimeError, match="simulated mid-reconcile failure"),
    ):
        await refresh_target_topology(target, _operator(tenant_id))

    # Neither the live tables nor the history tables should carry any
    # rows for this tenant -- the rollback was atomic.
    async with sessionmaker() as session:
        live_nodes = (
            await session.execute(select(GraphNode).where(GraphNode.tenant_id == tenant_id))
        ).scalars()
        live_edges = (
            await session.execute(select(GraphEdge).where(GraphEdge.tenant_id == tenant_id))
        ).scalars()
        node_hist = (
            await session.execute(
                select(GraphNodeHistory).where(GraphNodeHistory.tenant_id == tenant_id)
            )
        ).scalars()
        edge_hist = (
            await session.execute(
                select(GraphEdgeHistory).where(GraphEdgeHistory.tenant_id == tenant_id)
            )
        ).scalars()
    assert list(live_nodes) == [], "live graph_node should be empty after rollback"
    assert list(live_edges) == [], "live graph_edge should be empty after rollback"
    assert list(node_hist) == [], "graph_node_history should be empty after rollback"
    assert list(edge_hist) == [], "graph_edge_history should be empty after rollback"


# ---------------------------------------------------------------------------
# Acceptance criterion #5 -- no own audit rows -- covered by criterion-#1 test
# (asserts len(audits) == 1)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Unannotate path -- removed curated edge + reciprocal markers cleared
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unannotate_emits_removed_history_and_clears_marker_history() -> None:
    """Removing a curated edge that supersedes an auto edge emits:

    * one ``removed`` history row for the curated edge;
    * one ``updated`` history row for the auto edge whose
      ``superseded_by`` marker the unannotate just cleared.

    Both rows share the unannotate's single ``audit_id``.
    """
    _register_fake()
    tenant_id, target = await _seed_tenant_and_target()
    # Refresh creates an auto edge vm-a runs-on host-old.
    _FakeConnector.hints = TopologyHints(
        discovered_at=datetime.now(UTC),
        nodes=(
            NodeHint(kind="vm", name="vm-a"),
            NodeHint(kind="host", name="host-old"),
            NodeHint(kind="host", name="host-new"),
        ),
        edges=(
            EdgeHint(
                from_kind="vm",
                from_name="vm-a",
                to_kind="host",
                to_name="host-old",
                kind="runs-on",
            ),
        ),
    )
    operator = _operator(tenant_id)
    with patch(_PUBLISH_REFRESH, new=AsyncMock()):
        await refresh_target_topology(target, operator)

    sessionmaker = get_sessionmaker()
    # Annotate vm-a runs-on host-new -- supersedes the auto edge.
    async with sessionmaker() as session:
        with patch(_PUBLISH_ANNOTATE, new=AsyncMock()):
            curated = await annotate_edge(
                session,
                operator,
                NodeRef(name="vm-a", kind="vm"),
                "runs-on",
                NodeRef(name="host-new", kind="host"),
            )
        curated_id = curated.id

    # Clear history baseline so we can scope assertions to the unannotate.
    async with sessionmaker() as session, session.begin():
        await session.execute(delete(GraphEdgeHistory))
        await session.execute(delete(AuditLog))

    async with sessionmaker() as session:
        with patch(_PUBLISH_ANNOTATE, new=AsyncMock()):
            await unannotate_edge(session, operator, edge_id=curated_id)

    edge_history = await _all_edge_history(tenant_id)
    audits = await _all_audit_log(tenant_id)

    assert len(audits) == 1, "unannotate should emit exactly one audit_log row"
    unannotate_audit_id = audits[0].id

    removed = [h for h in edge_history if h.change_kind == GraphHistoryChangeKind.REMOVED.value]
    updated = [h for h in edge_history if h.change_kind == GraphHistoryChangeKind.UPDATED.value]

    assert len(removed) == 1, "exactly one ``removed`` history row for the curated edge"
    assert removed[0].audit_id == unannotate_audit_id
    assert removed[0].edge_id == curated_id

    assert len(updated) == 1, (
        "exactly one ``updated`` history row for the auto edge whose "
        "superseded_by marker was cleared"
    )
    assert updated[0].audit_id == unannotate_audit_id
    after_state = updated[0].snapshot["after"]
    assert isinstance(after_state, dict)
    auto_after_props = after_state["properties"]
    assert isinstance(auto_after_props, dict)
    assert "superseded_by" not in auto_after_props, (
        "after the unannotate, the auto row's superseded_by marker must be cleared "
        "in snapshot.after"
    )


# ---------------------------------------------------------------------------
# §6 conflict marker history -- annotate supersedes an existing auto edge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_annotate_supersedes_auto_emits_two_history_rows() -> None:
    """Annotating a curated edge that supersedes an existing auto edge
    emits two history rows in the same transaction: the curated row
    (``created``) and the auto row (``updated`` with ``superseded_by``
    in ``snapshot.after``).
    """
    _register_fake()
    tenant_id, target = await _seed_tenant_and_target()
    _FakeConnector.hints = TopologyHints(
        discovered_at=datetime.now(UTC),
        nodes=(
            NodeHint(kind="vm", name="vm-a"),
            NodeHint(kind="host", name="host-old"),
            NodeHint(kind="host", name="host-new"),
        ),
        edges=(
            EdgeHint(
                from_kind="vm",
                from_name="vm-a",
                to_kind="host",
                to_name="host-old",
                kind="runs-on",
            ),
        ),
    )
    operator = _operator(tenant_id)
    with patch(_PUBLISH_REFRESH, new=AsyncMock()):
        await refresh_target_topology(target, operator)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        await session.execute(delete(GraphEdgeHistory))
        await session.execute(delete(AuditLog))

    async with sessionmaker() as session:
        with patch(_PUBLISH_ANNOTATE, new=AsyncMock()):
            await annotate_edge(
                session,
                operator,
                NodeRef(name="vm-a", kind="vm"),
                "runs-on",
                NodeRef(name="host-new", kind="host"),
            )

    audits = await _all_audit_log(tenant_id)
    edge_history = await _all_edge_history(tenant_id)

    assert len(audits) == 1
    annotate_audit_id = audits[0].id

    assert len(edge_history) == 2, (
        f"expected 2 edge history rows (curated CREATED + auto UPDATED); got {len(edge_history)}"
    )
    assert {h.audit_id for h in edge_history} == {annotate_audit_id}

    created = [h for h in edge_history if h.change_kind == GraphHistoryChangeKind.CREATED.value]
    updated = [h for h in edge_history if h.change_kind == GraphHistoryChangeKind.UPDATED.value]
    assert len(created) == 1
    assert len(updated) == 1

    auto_after = updated[0].snapshot["after"]
    assert isinstance(auto_after, dict)
    auto_after_props = auto_after["properties"]
    assert isinstance(auto_after_props, dict)
    assert "superseded_by" in auto_after_props, (
        "auto edge's snapshot.after must show the freshly-stamped superseded_by marker"
    )


# ---------------------------------------------------------------------------
# Negative -- a refresh that re-asserts unchanged state emits no history rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unchanged_refresh_emits_no_history_rows() -> None:
    """A second refresh with byte-identical hints produces zero history
    rows -- a pure ``last_seen`` heartbeat is not a recorded mutation.
    """
    _register_fake()
    tenant_id, target = await _seed_tenant_and_target()
    _FakeConnector.hints = _hints_3n2e()
    with patch(_PUBLISH_REFRESH, new=AsyncMock()):
        await refresh_target_topology(target, _operator(tenant_id))

    before_node = await _all_node_history(tenant_id)
    before_edge = await _all_edge_history(tenant_id)

    # Re-run with identical hints.
    with patch(_PUBLISH_REFRESH, new=AsyncMock()):
        await refresh_target_topology(target, _operator(tenant_id))

    after_node = await _all_node_history(tenant_id)
    after_edge = await _all_edge_history(tenant_id)

    assert len(after_node) == len(before_node), (
        "second identical refresh should not emit additional node history rows"
    )
    assert len(after_edge) == len(before_edge), (
        "second identical refresh should not emit additional edge history rows"
    )


# ---------------------------------------------------------------------------
# Tenant boundary -- history rows are tenant-scoped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_rows_scoped_to_tenant() -> None:
    """A refresh writes history rows only under the operator's tenant_id."""
    _register_fake()
    tenant_a_id, target_a = await _seed_tenant_and_target(slug="tenant-a")
    tenant_b_id, _target_b = await _seed_tenant_and_target(slug="tenant-b")

    _FakeConnector.hints = _hints_3n2e()
    with patch(_PUBLISH_REFRESH, new=AsyncMock()):
        await refresh_target_topology(target_a, _operator(tenant_a_id))

    history_a = await _all_node_history(tenant_a_id)
    history_b = await _all_node_history(tenant_b_id)

    assert len(history_a) == 3
    assert history_b == [], "tenant B must see zero history rows"
