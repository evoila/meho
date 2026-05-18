# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the G9.2-T3 annotate / unannotate service.

Coverage matrix (Task #595 acceptance criteria):

* **Idempotent annotate** — annotating the same ``(from, kind, to)``
  triple twice yields one row; the second call refreshes
  ``last_seen`` + ``properties`` instead of erroring.
* **Same-kind / different-endpoint conflict** — the auto edge is
  stamped ``superseded_by``; the curated row carries the canonical
  assertion; ``unannotate`` of the curated row clears the supersede
  mark on the auto edge.
* **Incompatible-kind conflict** — both edges survive; bidirectional
  ``conflicts_with`` markers on each.
* **Auto-edge deletion refusal** — ``unannotate_edge`` on an
  ``source='auto'`` row raises :class:`AutoEdgeDeletionError`.
* **Selector validation** — both / neither / partial triple raises
  :class:`UnannotateSelectorError`.
* **Kind validation** — non-enum ``kind`` raises
  :class:`InvalidEdgeKindError` *before* any DB write.
* **Tenant boundary** — a tenant-A annotate cannot reference a
  tenant-B node (``NodeNotFoundError``).
* **Audit + broadcast** — every annotate / unannotate writes exactly
  one ``audit_log`` row with the canonical op_id +
  ``op_class='write'`` and publishes exactly one broadcast event.
  ``target_id`` is populated when the *from* node has one; null
  otherwise.
* **Refresh-merge preservation** — ``_reconcile_edges`` running
  against a hint that re-observes a superseded edge preserves the
  ``superseded_by`` marker while still applying the hint's other
  property changes.

The tests run against ``sqlite+aiosqlite`` via the autouse
``_default_database_url`` fixture in :mod:`tests.conftest` (the same
shape :mod:`tests.test_topology_refresh` uses). The traversal-exclusion
guard added to :mod:`meho_backplane.topology.query` uses PG-only JSON
syntax and is covered by :mod:`tests.integration.test_topology_query`
in the integration suite.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import EdgeHint, NodeHint, TopologyHints
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, GraphEdge, GraphNode, Target, Tenant
from meho_backplane.settings import get_settings
from meho_backplane.topology import (
    AutoEdgeDeletionError,
    InvalidEdgeKindError,
    NodeNotFoundError,
    NodeRef,
    UnannotateSelectorError,
    annotate_edge,
    unannotate_edge,
)

_PUBLISH = "meho_backplane.topology.annotate.publish_event"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


async def _seed_tenant(slug: str = "rdc-internal") -> uuid.UUID:
    """Insert one ``tenant`` row and return its id."""
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
        await session.commit()
    return tenant_id


async def _seed_node(
    tenant_id: uuid.UUID,
    *,
    kind: str,
    name: str,
    target_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert one ``graph_node`` row and return its id."""
    sessionmaker = get_sessionmaker()
    node_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            GraphNode(
                id=node_id,
                tenant_id=tenant_id,
                kind=kind,
                name=name,
                target_id=target_id,
                properties={},
                discovered_by="test",
                first_seen=datetime.now(UTC),
            )
        )
        await session.commit()
    return node_id


async def _seed_target_node(
    tenant_id: uuid.UUID,
    *,
    kind: str,
    name: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a ``target`` row + a ``graph_node`` linked to it.

    Returns ``(target_id, node_id)``. Annotation routinely references
    target-backed nodes (the canonical case is a vSphere VM-target);
    the audit row's ``target_id`` column is populated iff the *from*
    node has a ``target_id`` foreign key.
    """
    sessionmaker = get_sessionmaker()
    target_id = uuid.uuid4()
    node_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            Target(
                id=target_id,
                tenant_id=tenant_id,
                name=f"target-{name}",
                aliases=[],
                product="vsphere",
                host="vc.example.test",
            )
        )
        await session.commit()
    async with sessionmaker() as session:
        session.add(
            GraphNode(
                id=node_id,
                tenant_id=tenant_id,
                kind=kind,
                name=name,
                target_id=target_id,
                properties={},
                discovered_by="test",
                first_seen=datetime.now(UTC),
            )
        )
        await session.commit()
    return target_id, node_id


async def _seed_auto_edge(
    tenant_id: uuid.UUID,
    *,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    kind: str,
    properties: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Insert one ``graph_edge`` row with ``source='auto'``."""
    sessionmaker = get_sessionmaker()
    edge_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            GraphEdge(
                id=edge_id,
                tenant_id=tenant_id,
                from_node_id=from_id,
                to_node_id=to_id,
                kind=kind,
                source="auto",
                properties=properties or {},
                discovered_by="test",
                first_seen=datetime.now(UTC),
                last_seen=datetime.now(UTC),
            )
        )
        await session.commit()
    return edge_id


def _operator(tenant_id: uuid.UUID, sub: str = "op-1") -> Operator:
    return Operator(
        sub=sub,
        name="Op One",
        email=None,
        raw_jwt="",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


# ---------------------------------------------------------------------------
# Happy path: insert + idempotent re-annotate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_annotate_creates_curated_edge() -> None:
    """First annotate inserts a fresh ``source='curated'`` row."""
    tenant_id = await _seed_tenant()
    sa = await _seed_node(tenant_id, kind="principal", name="foo")
    vr = await _seed_node(tenant_id, kind="vault-role", name="bar")

    sessionmaker = get_sessionmaker()
    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            edge = await annotate_edge(
                session,
                _operator(tenant_id),
                NodeRef("foo", "principal"),
                "authenticates-via",
                NodeRef("bar", "vault-role"),
                note="canonical k8s SA → Vault role",
                evidence_url="https://example.test/inventory#L42",
            )

    assert edge.source == "curated"
    assert edge.kind == "authenticates-via"
    assert edge.from_node_id == sa
    assert edge.to_node_id == vr
    assert edge.properties["note"] == "canonical k8s SA → Vault role"
    assert edge.properties["evidence_url"] == "https://example.test/inventory#L42"
    assert edge.properties["annotated_by"] == "op-1"
    assert "annotated_at" in edge.properties


@pytest.mark.asyncio
async def test_annotate_is_idempotent() -> None:
    """A second annotate of the same triple updates last_seen, not duplicates."""
    tenant_id = await _seed_tenant()
    await _seed_node(tenant_id, kind="principal", name="foo")
    await _seed_node(tenant_id, kind="vault-role", name="bar")

    sessionmaker = get_sessionmaker()
    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            first = await annotate_edge(
                session,
                _operator(tenant_id),
                NodeRef("foo", "principal"),
                "authenticates-via",
                NodeRef("bar", "vault-role"),
                note="first",
            )
        async with sessionmaker() as session:
            second = await annotate_edge(
                session,
                _operator(tenant_id),
                NodeRef("foo", "principal"),
                "authenticates-via",
                NodeRef("bar", "vault-role"),
                note="second",
            )

    # Same row, not a duplicate.
    assert first.id == second.id

    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(GraphEdge).where(GraphEdge.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    # Properties refreshed to the second annotation's payload.
    assert rows[0].properties["note"] == "second"


# ---------------------------------------------------------------------------
# Kind validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_annotate_rejects_unknown_kind() -> None:
    """A kind outside :class:`GraphEdgeKind` raises before any DB write."""
    tenant_id = await _seed_tenant()
    await _seed_node(tenant_id, kind="principal", name="foo")
    await _seed_node(tenant_id, kind="vault-role", name="bar")

    sessionmaker = get_sessionmaker()
    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            with pytest.raises(InvalidEdgeKindError):
                await annotate_edge(
                    session,
                    _operator(tenant_id),
                    NodeRef("foo", "principal"),
                    "made-up-kind",
                    NodeRef("bar", "vault-role"),
                )

    # No edge was written.
    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(GraphEdge).where(GraphEdge.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    assert rows == []


# ---------------------------------------------------------------------------
# Tenant boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_annotate_cannot_reference_other_tenant_node() -> None:
    """Cross-tenant annotation hits :class:`NodeNotFoundError`, not the other row."""
    tenant_a = await _seed_tenant("tenant-a")
    tenant_b = await _seed_tenant("tenant-b")
    # Seed the *target* node only in tenant B.
    await _seed_node(tenant_a, kind="principal", name="foo")
    await _seed_node(tenant_b, kind="vault-role", name="bar")

    sessionmaker = get_sessionmaker()
    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            with pytest.raises(NodeNotFoundError):
                await annotate_edge(
                    session,
                    _operator(tenant_a),
                    NodeRef("foo", "principal"),
                    "authenticates-via",
                    NodeRef("bar", "vault-role"),
                )


# Note on bare-name ambiguity: :func:`annotate_edge` propagates
# :class:`AmbiguousNodeError` directly from :func:`resolve_node` (#594).
# The resolver's bare-name ambiguity-probe SQL binds ``tenant_id`` as
# a stringified UUID through a fully-literal ``text("...")`` statement
# (asyncpg's text codec convention); SQLAlchemy's ``Uuid()`` type
# stores UUIDs as 32-char no-dash CHAR on SQLite, so the raw-SQL
# string comparison misses on SQLite alone. The ambiguity contract is
# exercised against real PG by :mod:`tests.integration.test_topology_resolvers`
# — replicating it here would duplicate that coverage and would also
# require a PG container, which the unit suite deliberately avoids.


# ---------------------------------------------------------------------------
# §6 conflict detection: same-kind / different-endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_kind_different_endpoint_marks_auto_superseded() -> None:
    """Curated ``runs-on(vm→hostY)`` supersedes auto ``runs-on(vm→hostX)``."""
    tenant_id = await _seed_tenant()
    vm = await _seed_node(tenant_id, kind="vm", name="vm-a")
    host_x = await _seed_node(tenant_id, kind="host", name="host-x")
    # host_y must exist in the tenant so the curated annotate can
    # resolve it; the id itself is not used after seeding.
    await _seed_node(tenant_id, kind="host", name="host-y")
    auto_edge_id = await _seed_auto_edge(tenant_id, from_id=vm, to_id=host_x, kind="runs-on")

    sessionmaker = get_sessionmaker()
    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            curated = await annotate_edge(
                session,
                _operator(tenant_id),
                NodeRef("vm-a", "vm"),
                "runs-on",
                NodeRef("host-y", "host"),
            )

    async with sessionmaker() as session:
        auto = (
            await session.execute(select(GraphEdge).where(GraphEdge.id == auto_edge_id))
        ).scalar_one()
    assert auto.properties["superseded_by"] == str(curated.id)
    # The auto row itself survives.
    assert auto.source == "auto"


@pytest.mark.asyncio
async def test_unannotate_clears_supersede_marker() -> None:
    """Removing the curated row clears ``superseded_by`` on the auto edge."""
    tenant_id = await _seed_tenant()
    vm = await _seed_node(tenant_id, kind="vm", name="vm-a")
    host_x = await _seed_node(tenant_id, kind="host", name="host-x")
    await _seed_node(tenant_id, kind="host", name="host-y")
    auto_edge_id = await _seed_auto_edge(tenant_id, from_id=vm, to_id=host_x, kind="runs-on")

    sessionmaker = get_sessionmaker()
    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            curated = await annotate_edge(
                session,
                _operator(tenant_id),
                NodeRef("vm-a", "vm"),
                "runs-on",
                NodeRef("host-y", "host"),
            )
        async with sessionmaker() as session:
            await unannotate_edge(session, _operator(tenant_id), edge_id=curated.id)

    async with sessionmaker() as session:
        auto = (
            await session.execute(select(GraphEdge).where(GraphEdge.id == auto_edge_id))
        ).scalar_one()
        # Curated edge is gone (hard delete).
        gone = (
            await session.execute(select(GraphEdge).where(GraphEdge.id == curated.id))
        ).scalar_one_or_none()
    assert "superseded_by" not in auto.properties
    assert gone is None


# ---------------------------------------------------------------------------
# §6 conflict detection: incompatible kinds, same endpoint pair
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incompatible_kinds_bidirectional_markers() -> None:
    """Curated ``depends-on(svc→db)`` and auto ``routes-through(svc→db)`` coexist."""
    tenant_id = await _seed_tenant()
    svc = await _seed_node(tenant_id, kind="service", name="svc-x")
    db = await _seed_node(tenant_id, kind="vm", name="db-y")
    auto_edge_id = await _seed_auto_edge(tenant_id, from_id=svc, to_id=db, kind="routes-through")

    sessionmaker = get_sessionmaker()
    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            curated = await annotate_edge(
                session,
                _operator(tenant_id),
                NodeRef("svc-x", "service"),
                "depends-on",
                NodeRef("db-y", "vm"),
            )

    async with sessionmaker() as session:
        auto = (
            await session.execute(select(GraphEdge).where(GraphEdge.id == auto_edge_id))
        ).scalar_one()
        cur = (
            await session.execute(select(GraphEdge).where(GraphEdge.id == curated.id))
        ).scalar_one()

    # Bidirectional: auto sees curated; curated sees auto.
    assert str(curated.id) in auto.properties["conflicts_with"]
    assert str(auto.id) in cur.properties["conflicts_with"]
    # Neither is superseded.
    assert "superseded_by" not in auto.properties
    assert "superseded_by" not in cur.properties


@pytest.mark.asyncio
async def test_unannotate_clears_reciprocal_conflict_marker() -> None:
    """Removing the curated row also clears the auto edge's back-reference."""
    tenant_id = await _seed_tenant()
    svc = await _seed_node(tenant_id, kind="service", name="svc-x")
    db = await _seed_node(tenant_id, kind="vm", name="db-y")
    auto_edge_id = await _seed_auto_edge(tenant_id, from_id=svc, to_id=db, kind="routes-through")

    sessionmaker = get_sessionmaker()
    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            curated = await annotate_edge(
                session,
                _operator(tenant_id),
                NodeRef("svc-x", "service"),
                "depends-on",
                NodeRef("db-y", "vm"),
            )
        async with sessionmaker() as session:
            await unannotate_edge(session, _operator(tenant_id), edge_id=curated.id)

    async with sessionmaker() as session:
        auto = (
            await session.execute(select(GraphEdge).where(GraphEdge.id == auto_edge_id))
        ).scalar_one()
    assert "conflicts_with" not in auto.properties


# ---------------------------------------------------------------------------
# Unannotate: triple selector + auto-edge refusal + selector validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unannotate_triple_selector() -> None:
    """The full (from, kind, to) triple resolves to the row and deletes it."""
    tenant_id = await _seed_tenant()
    await _seed_node(tenant_id, kind="principal", name="foo")
    await _seed_node(tenant_id, kind="vault-role", name="bar")

    sessionmaker = get_sessionmaker()
    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            curated = await annotate_edge(
                session,
                _operator(tenant_id),
                NodeRef("foo", "principal"),
                "authenticates-via",
                NodeRef("bar", "vault-role"),
            )
        async with sessionmaker() as session:
            removed_id = await unannotate_edge(
                session,
                _operator(tenant_id),
                from_ref=NodeRef("foo", "principal"),
                kind="authenticates-via",
                to_ref=NodeRef("bar", "vault-role"),
            )

    assert removed_id == curated.id
    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(GraphEdge).where(GraphEdge.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    assert rows == []


@pytest.mark.asyncio
async def test_unannotate_refuses_auto_edge() -> None:
    """Targeting a ``source='auto'`` row raises :class:`AutoEdgeDeletionError`."""
    tenant_id = await _seed_tenant()
    vm = await _seed_node(tenant_id, kind="vm", name="vm-a")
    host = await _seed_node(tenant_id, kind="host", name="host-x")
    auto_edge_id = await _seed_auto_edge(tenant_id, from_id=vm, to_id=host, kind="runs-on")

    sessionmaker = get_sessionmaker()
    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            with pytest.raises(AutoEdgeDeletionError):
                await unannotate_edge(session, _operator(tenant_id), edge_id=auto_edge_id)

    # Auto row survived the refused delete.
    async with sessionmaker() as session:
        row = (
            await session.execute(select(GraphEdge).where(GraphEdge.id == auto_edge_id))
        ).scalar_one()
    assert row.source == "auto"


@pytest.mark.asyncio
async def test_unannotate_rejects_both_or_neither_selector() -> None:
    """Both / neither selector form raises :class:`UnannotateSelectorError`."""
    tenant_id = await _seed_tenant()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # Neither.
        with pytest.raises(UnannotateSelectorError):
            await unannotate_edge(session, _operator(tenant_id))
        # Both.
        with pytest.raises(UnannotateSelectorError):
            await unannotate_edge(
                session,
                _operator(tenant_id),
                edge_id=uuid.uuid4(),
                from_ref=NodeRef("x"),
                kind="depends-on",
                to_ref=NodeRef("y"),
            )
        # Partial triple (kind missing).
        with pytest.raises(UnannotateSelectorError):
            await unannotate_edge(
                session,
                _operator(tenant_id),
                from_ref=NodeRef("x"),
                to_ref=NodeRef("y"),
            )


@pytest.mark.asyncio
async def test_unannotate_other_tenant_edge_not_found() -> None:
    """Tenant boundary: an edge in tenant B is not findable from tenant A."""
    tenant_a = await _seed_tenant("tenant-a")
    tenant_b = await _seed_tenant("tenant-b")
    vm = await _seed_node(tenant_b, kind="vm", name="vm-a")
    host = await _seed_node(tenant_b, kind="host", name="host-x")
    other_edge = await _seed_auto_edge(tenant_b, from_id=vm, to_id=host, kind="runs-on")

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        with pytest.raises(ValueError):
            await unannotate_edge(session, _operator(tenant_a), edge_id=other_edge)


# ---------------------------------------------------------------------------
# Audit + broadcast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_annotate_writes_audit_row_and_broadcast() -> None:
    """One ``audit_log`` row + one broadcast event per annotate."""
    tenant_id = await _seed_tenant()
    target_id, _from_node = await _seed_target_node(tenant_id, kind="vm", name="vm-a")
    await _seed_node(tenant_id, kind="host", name="host-x")

    publish_mock = AsyncMock()
    sessionmaker = get_sessionmaker()
    with patch(_PUBLISH, new=publish_mock):
        async with sessionmaker() as session:
            await annotate_edge(
                session,
                _operator(tenant_id),
                NodeRef("vm-a", "vm"),
                "runs-on",
                NodeRef("host-x", "host"),
            )

    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.path == "topology.annotate"
    assert row.payload["op_id"] == "topology.annotate"
    # Critical: op_class is *write*, not the refresh service's *read*.
    assert row.payload["op_class"] == "write"
    # ``target_id`` populated because the *from* node has one.
    assert row.target_id == target_id

    publish_mock.assert_awaited_once()
    event = publish_mock.await_args.args[0]
    assert event.op_id == "topology.annotate"
    assert event.op_class == "write"
    assert event.payload["from"]["name"] == "vm-a"
    assert event.payload["to"]["name"] == "host-x"
    assert event.payload["kind"] == "runs-on"


@pytest.mark.asyncio
async def test_unannotate_writes_audit_row_and_broadcast() -> None:
    """One ``audit_log`` row + one broadcast event per unannotate."""
    tenant_id = await _seed_tenant()
    await _seed_node(tenant_id, kind="principal", name="foo")
    await _seed_node(tenant_id, kind="vault-role", name="bar")

    publish_mock = AsyncMock()
    sessionmaker = get_sessionmaker()
    with patch(_PUBLISH, new=publish_mock):
        async with sessionmaker() as session:
            curated = await annotate_edge(
                session,
                _operator(tenant_id),
                NodeRef("foo", "principal"),
                "authenticates-via",
                NodeRef("bar", "vault-role"),
            )
        async with sessionmaker() as session:
            await unannotate_edge(session, _operator(tenant_id), edge_id=curated.id)

    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.tenant_id == tenant_id)
                    .order_by(AuditLog.occurred_at)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 2
    assert rows[0].path == "topology.annotate"
    assert rows[1].path == "topology.unannotate"
    assert rows[1].payload["op_class"] == "write"
    # Non-target from-node → null ``target_id`` per §10 spec.
    assert rows[1].target_id is None

    # Exactly two broadcast events (one per verb call).
    assert publish_mock.await_count == 2


@pytest.mark.asyncio
async def test_annotate_broadcast_failure_does_not_fail_call() -> None:
    """Broadcast publish is fail-open per the refresh-service pattern."""
    tenant_id = await _seed_tenant()
    await _seed_node(tenant_id, kind="principal", name="foo")
    await _seed_node(tenant_id, kind="vault-role", name="bar")

    sessionmaker = get_sessionmaker()
    with patch(_PUBLISH, new=AsyncMock(side_effect=RuntimeError("redis down"))):
        async with sessionmaker() as session:
            edge = await annotate_edge(
                session,
                _operator(tenant_id),
                NodeRef("foo", "principal"),
                "authenticates-via",
                NodeRef("bar", "vault-role"),
            )

    # Edge + audit row committed despite the publish failure.
    assert edge.id is not None
    async with sessionmaker() as session:
        audit_rows = (
            (await session.execute(select(AuditLog).where(AuditLog.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        edge_rows = (
            (await session.execute(select(GraphEdge).where(GraphEdge.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    assert len(audit_rows) == 1
    assert len(edge_rows) == 1


# ---------------------------------------------------------------------------
# Refresh-merge preservation: §6 markers survive a reconcile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_edges_preserves_superseded_marker() -> None:
    """``_reconcile_edges`` re-applies a hint without losing §6 markers.

    Setup: an auto edge carries ``superseded_by`` (set by an earlier
    annotate). A fresh refresh re-observes the same edge with
    otherwise-changed properties. The merge must keep the marker
    *and* apply the hint's other property changes.
    """
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import (
        clear_registry,
        register_connector_v2,
    )
    from meho_backplane.operations._handler_resolve import (
        reset_connector_instance_cache,
    )
    from meho_backplane.topology.refresh import refresh_target_topology

    clear_registry()
    reset_connector_instance_cache()

    tenant_id = await _seed_tenant("refresh-merge-tenant")
    target_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = Target(
            id=target_id,
            tenant_id=tenant_id,
            name="refresh-target",
            aliases=[],
            product="faketopo-merge",
            host="vc.example.test",
        )
        session.add(target)
        await session.commit()

    # First refresh: seed vm-a + host-x + the runs-on edge with port=80.
    hints_v1 = TopologyHints(
        discovered_at=datetime.now(UTC),
        nodes=(
            NodeHint(kind="vm", name="vm-a", properties={}),
            NodeHint(kind="host", name="host-x", properties={}),
        ),
        edges=(
            EdgeHint(
                from_kind="vm",
                from_name="vm-a",
                to_kind="host",
                to_name="host-x",
                kind="runs-on",
                properties={"port": 80},
            ),
        ),
    )

    class _FakeMergeConnector(Connector):
        product = "faketopo-merge"
        hints: TopologyHints = hints_v1

        async def fingerprint(self, target: Any) -> Any:
            raise NotImplementedError

        async def probe(self, target: Any) -> Any:
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:
            raise NotImplementedError

        async def discover_topology(self, target: Any) -> TopologyHints:
            return type(self).hints

    register_connector_v2(
        product="faketopo-merge",
        version="",
        impl_id="",
        cls=_FakeMergeConnector,
    )

    async with sessionmaker() as session:
        target_row = (
            await session.execute(select(Target).where(Target.id == target_id))
        ).scalar_one()

    with patch("meho_backplane.topology.refresh.publish_event", new=AsyncMock()):
        await refresh_target_topology(target_row, _operator(tenant_id))

    # Operator stamps superseded_by on the auto edge (the canonical
    # case is via an annotate of a different host; here we stamp it
    # directly so the test stays focused on the merge invariant).
    async with sessionmaker() as session:
        edge = (
            await session.execute(select(GraphEdge).where(GraphEdge.tenant_id == tenant_id))
        ).scalar_one()
        marked_props = dict(edge.properties)
        marked_props["superseded_by"] = str(uuid.uuid4())
        edge.properties = marked_props
        await session.commit()
        marker_value = marked_props["superseded_by"]

    # Second refresh: same edge, different ``port`` property.
    _FakeMergeConnector.hints = TopologyHints(
        discovered_at=datetime.now(UTC),
        nodes=hints_v1.nodes,
        edges=(
            EdgeHint(
                from_kind="vm",
                from_name="vm-a",
                to_kind="host",
                to_name="host-x",
                kind="runs-on",
                properties={"port": 443},
            ),
        ),
    )
    with patch("meho_backplane.topology.refresh.publish_event", new=AsyncMock()):
        await refresh_target_topology(target_row, _operator(tenant_id))

    async with sessionmaker() as session:
        refreshed = (
            await session.execute(select(GraphEdge).where(GraphEdge.tenant_id == tenant_id))
        ).scalar_one()

    # Marker preserved through the refresh.
    assert refreshed.properties["superseded_by"] == marker_value
    # Hint's other property change still applied.
    assert refreshed.properties["port"] == 443

    clear_registry()
    reset_connector_instance_cache()
