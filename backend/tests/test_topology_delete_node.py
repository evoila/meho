# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the #2485 ``delete_node`` service (Initiative #2494).

Coverage matrix (the service-level half; the MCP surface is covered in
:mod:`tests.test_mcp_tools_topology_delete_node`, the REST route in
:mod:`tests.test_api_v1_topology`):

* **Happy path** — a manually-seeded (``source='curated'``,
  ``target_id IS NULL``) node is hard-deleted: the live row is gone, a
  ``removed`` history tombstone carries the pre-delete ``{kind, name}``
  snapshot, and exactly one ``audit_log`` row lands
  (``op_id='topology.delete_node'``, ``method='DELETE_NODE'``,
  ``op_class='write'``).
* **History survives** — pre-existing ``graph_node_history`` rows for the
  node (plus the fresh tombstone) survive the delete with ``node_id``
  NULL via the ``ON DELETE SET NULL`` FK.
* **404** — a missing / cross-tenant id raises
  :class:`NodeNotFoundForDeleteError`.
* **409 probe_owned** — a probe-derived (``source='auto'``) node and a
  target-bound node both raise :class:`NodeNotDeletableError`.
* **409 has-edges** — a node with a live edge raises
  :class:`NodeHasLiveEdgesError` listing the blocking edge id; a
  soft-deleted (``last_seen IS NULL``) edge does not block.
* **Second delete → 404** — the id no longer resolves after the delete.

Runs against ``sqlite+aiosqlite`` via the autouse ``_default_database_url``
fixture in :mod:`tests.conftest` — same shape
:mod:`tests.test_topology_create_node` uses. SQLite defaults FK-off, so
the one test asserting the ``ON DELETE SET NULL`` tombstone-survival
cascade opts in via the ``_enforce_sqlite_foreign_keys`` fixture (same
mechanism :mod:`tests.test_topology_history_hook` uses).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    GraphEdge,
    GraphNode,
    GraphNodeHistory,
    Target,
    Tenant,
)
from meho_backplane.settings import get_settings
from meho_backplane.topology import create_or_get_node, delete_node
from meho_backplane.topology.node_delete import (
    NodeHasLiveEdgesError,
    NodeNotDeletableError,
    NodeNotFoundForDeleteError,
)
from meho_backplane.topology.query import query_timeline

_PUBLISH = "meho_backplane.topology.node_delete.publish_event"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def _enforce_sqlite_foreign_keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Opt one test in to SQLite foreign-key enforcement.

    SQLite defaults FK-off (even on aiosqlite), which silently masks the
    ``graph_node_history.node_id`` ``ON DELETE SET NULL`` cascade the
    tombstone-survival contract relies on. Setting
    ``MEHO_SQLITE_FOREIGN_KEYS=1`` flips
    :func:`db.engine.create_engine_for_url` into the gated branch that
    attaches a ``PRAGMA foreign_keys=ON`` listener on every new SQLite
    connection; :func:`reset_engine_for_testing` drops the cached engine
    so the next ``get_engine()`` rebuilds with the listener attached.
    Same fixture shape :mod:`tests.test_topology_history_hook` uses for
    the sibling edge-history cascade. Requested per-test (not autouse)
    because this module's other tests insert FK-referencing rows and do
    not need the cascade.
    """
    from meho_backplane.db.engine import reset_engine_for_testing

    monkeypatch.setenv("MEHO_SQLITE_FOREIGN_KEYS", "1")
    reset_engine_for_testing()
    yield
    reset_engine_for_testing()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _seed_tenant(slug: str = "rdc-internal") -> uuid.UUID:
    """Return the tenant row's id, inserting it on the first call.

    Look-up-then-insert because migration ``0018`` seeds ``rdc-internal``
    into the per-worker schema template (see
    :mod:`tests.test_topology_create_node`).
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        existing: uuid.UUID | None = await session.scalar(
            select(Tenant.id).where(Tenant.slug == slug),
        )
        if existing is not None:
            return existing
        tenant_id = uuid.uuid4()
        session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
        await session.commit()
    return tenant_id


def _operator(tenant_id: uuid.UUID, sub: str = "op-1") -> Operator:
    return Operator(
        sub=sub,
        name="Op One",
        email=None,
        raw_jwt="",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


async def _seed_curated_node(tenant_id: uuid.UUID, *, kind: str, name: str) -> uuid.UUID:
    """Seed a manual (``curated``, target-unbound) node; return its id."""
    sessionmaker = get_sessionmaker()
    with (
        patch(_PUBLISH, new=AsyncMock()),
        patch("meho_backplane.topology.nodes.publish_event", new=AsyncMock()),
    ):
        async with sessionmaker() as session:
            result = await create_or_get_node(session, _operator(tenant_id), kind=kind, name=name)
    return result.node.id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_node_removes_row_and_writes_tombstone() -> None:
    """A manual seed is hard-deleted with a ``removed`` history tombstone."""
    tenant_id = await _seed_tenant()
    node_id = await _seed_curated_node(tenant_id, kind="vault-role", name="rdc-vault")
    sessionmaker = get_sessionmaker()

    with patch(_PUBLISH, new=AsyncMock()) as publish_mock:
        async with sessionmaker() as session:
            result = await delete_node(session, _operator(tenant_id), node_id=node_id)

    assert result.node_id == node_id
    assert result.kind == "vault-role"
    assert result.name == "rdc-vault"
    publish_mock.assert_awaited_once()

    async with sessionmaker() as session:
        # The live row is gone.
        live = await session.get(GraphNode, node_id)
        assert live is None

        # A ``removed`` tombstone carries the pre-delete {kind, name}.
        history = (
            (
                await session.execute(
                    select(GraphNodeHistory).where(
                        GraphNodeHistory.tenant_id == tenant_id,
                        GraphNodeHistory.change_kind == "removed",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(history) == 1
        snapshot = history[0].snapshot
        assert snapshot["after"] is None
        assert snapshot["before"]["kind"] == "vault-role"
        assert snapshot["before"]["name"] == "rdc-vault"
        assert snapshot["before"]["id"] == str(node_id)

        # Exactly one audit row for the delete.
        audit = (
            (await session.execute(select(AuditLog).where(AuditLog.path == "topology.delete_node")))
            .scalars()
            .all()
        )
        assert len(audit) == 1
        assert audit[0].method == "DELETE_NODE"
        assert audit[0].payload["op_class"] == "write"
        assert audit[0].payload["node_id"] == str(node_id)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_enforce_sqlite_foreign_keys")
async def test_delete_node_leaves_prior_history_with_null_node_id() -> None:
    """Pre-existing history rows survive the delete with ``node_id`` NULL.

    The ``created`` row :func:`create_or_get_node` wrote plus the fresh
    ``removed`` tombstone both outlive the hard-delete: the
    ``ON DELETE SET NULL`` FK nulls their ``node_id`` while the snapshot
    keeps the row identity. Proves the timeline facet stays renderable.
    """
    tenant_id = await _seed_tenant()
    node_id = await _seed_curated_node(tenant_id, kind="service", name="keep-history")
    sessionmaker = get_sessionmaker()

    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            await delete_node(session, _operator(tenant_id), node_id=node_id)

    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(GraphNodeHistory).where(GraphNodeHistory.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )
    # created + removed, both surviving with a nulled FK.
    kinds = sorted(r.change_kind for r in rows)
    assert kinds == ["created", "removed"]
    assert all(r.node_id is None for r in rows)

    # The timeline facet reads history directly, so the tombstone shows.
    timeline = await query_timeline(_operator(tenant_id), limit=50)
    assert any(entry.change_kind == "removed" for entry in timeline.rows)


# ---------------------------------------------------------------------------
# 404 — missing / cross-tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_node_missing_id_raises_not_found() -> None:
    """An id that resolves to no row raises ``NodeNotFoundForDeleteError``."""
    tenant_id = await _seed_tenant()
    sessionmaker = get_sessionmaker()

    with patch(_PUBLISH, new=AsyncMock()) as publish_mock:
        async with sessionmaker() as session:
            with pytest.raises(NodeNotFoundForDeleteError):
                await delete_node(session, _operator(tenant_id), node_id=uuid.uuid4())
    publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_node_cross_tenant_id_raises_not_found() -> None:
    """A node in tenant-B is invisible (404) to a tenant-A operator."""
    tenant_a = await _seed_tenant()
    tenant_b = await _seed_tenant(slug="other-tenant")
    node_in_b = await _seed_curated_node(tenant_b, kind="vault-role", name="b-only")
    sessionmaker = get_sessionmaker()

    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            with pytest.raises(NodeNotFoundForDeleteError):
                await delete_node(session, _operator(tenant_a), node_id=node_in_b)

    # The tenant-B row is untouched.
    async with sessionmaker() as session:
        assert await session.get(GraphNode, node_in_b) is not None


@pytest.mark.asyncio
async def test_second_delete_raises_not_found() -> None:
    """Deleting the same id twice: the second call is a 404."""
    tenant_id = await _seed_tenant()
    node_id = await _seed_curated_node(tenant_id, kind="vault-role", name="once")
    sessionmaker = get_sessionmaker()

    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            await delete_node(session, _operator(tenant_id), node_id=node_id)
        async with sessionmaker() as session:
            with pytest.raises(NodeNotFoundForDeleteError):
                await delete_node(session, _operator(tenant_id), node_id=node_id)


# ---------------------------------------------------------------------------
# 409 — probe-owned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_node_refuses_auto_source() -> None:
    """A probe-derived (``source='auto'``) node is refused, even target-unbound.

    This is the auto-discovered inner-graph node case: ``target_id`` is
    NULL but the row is owned by refresh reconciliation, so the delete is
    refused with ``probe_owned`` — matching the issue's out-of-scope
    boundary (deleting auto-discovered nodes is refresh's job).
    """
    tenant_id = await _seed_tenant()
    sessionmaker = get_sessionmaker()
    node_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            GraphNode(
                id=node_id,
                tenant_id=tenant_id,
                kind="vm",
                name="probe-vm",
                target_id=None,
                source="auto",
                properties={"status": "running"},
                discovered_by="vmware",
                first_seen=datetime.now(UTC),
                last_seen=datetime.now(UTC),
            )
        )
        await session.commit()

    with patch(_PUBLISH, new=AsyncMock()) as publish_mock:
        async with sessionmaker() as session:
            with pytest.raises(NodeNotDeletableError) as excinfo:
                await delete_node(session, _operator(tenant_id), node_id=node_id)
    assert excinfo.value.source == "auto"
    publish_mock.assert_not_awaited()

    async with sessionmaker() as session:
        assert await session.get(GraphNode, node_id) is not None


@pytest.mark.asyncio
async def test_delete_node_refuses_target_bound_node() -> None:
    """A node adopted onto a registered target (``target_id`` set) is refused.

    The acceptance criterion's ``target_id IS NOT NULL`` case: refresh
    adopts such rows and they resurrect on the next probe, so a manual
    delete is refused with ``probe_owned``.
    """
    tenant_id = await _seed_tenant()
    sessionmaker = get_sessionmaker()
    target_id = uuid.uuid4()
    node_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            Target(
                id=target_id,
                tenant_id=tenant_id,
                name="vcenter-a",
                aliases=[],
                product="vmware",
                host="vc.example.test",
            )
        )
        session.add(
            GraphNode(
                id=node_id,
                tenant_id=tenant_id,
                kind="vm",
                name="target-node",
                target_id=target_id,
                source="auto",
                properties={},
                discovered_by="vmware",
                first_seen=datetime.now(UTC),
                last_seen=datetime.now(UTC),
            )
        )
        await session.commit()

    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            with pytest.raises(NodeNotDeletableError) as excinfo:
                await delete_node(session, _operator(tenant_id), node_id=node_id)
    assert excinfo.value.target_id == target_id


# ---------------------------------------------------------------------------
# 409 — live edges
# ---------------------------------------------------------------------------


async def _seed_edge(
    tenant_id: uuid.UUID,
    *,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    last_seen: datetime | None,
) -> uuid.UUID:
    """Insert one ``graph_edge`` row; return its id."""
    sessionmaker = get_sessionmaker()
    edge_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            GraphEdge(
                id=edge_id,
                tenant_id=tenant_id,
                from_node_id=from_id,
                to_node_id=to_id,
                kind="depends-on",
                source="curated",
                properties={},
                discovered_by="op-1",
                first_seen=datetime.now(UTC),
                last_seen=last_seen,
            )
        )
        await session.commit()
    return edge_id


@pytest.mark.asyncio
async def test_delete_node_refuses_when_live_edge_references_it() -> None:
    """A node with a live edge is refused, listing the blocking edge id."""
    tenant_id = await _seed_tenant()
    node_a = await _seed_curated_node(tenant_id, kind="service", name="svc-a")
    node_b = await _seed_curated_node(tenant_id, kind="database", name="db-b")
    edge_id = await _seed_edge(tenant_id, from_id=node_a, to_id=node_b, last_seen=datetime.now(UTC))
    sessionmaker = get_sessionmaker()

    with patch(_PUBLISH, new=AsyncMock()) as publish_mock:
        async with sessionmaker() as session:
            with pytest.raises(NodeHasLiveEdgesError) as excinfo:
                await delete_node(session, _operator(tenant_id), node_id=node_a)
    assert excinfo.value.edge_ids == [edge_id]
    publish_mock.assert_not_awaited()

    async with sessionmaker() as session:
        assert await session.get(GraphNode, node_a) is not None


@pytest.mark.asyncio
async def test_delete_node_ignores_soft_deleted_edges() -> None:
    """A soft-deleted (``last_seen IS NULL``) edge does not block the delete."""
    tenant_id = await _seed_tenant()
    node_a = await _seed_curated_node(tenant_id, kind="service", name="svc-stale")
    node_b = await _seed_curated_node(tenant_id, kind="database", name="db-stale")
    await _seed_edge(tenant_id, from_id=node_a, to_id=node_b, last_seen=None)
    sessionmaker = get_sessionmaker()

    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            result = await delete_node(session, _operator(tenant_id), node_id=node_a)
    assert result.node_id == node_a

    async with sessionmaker() as session:
        assert await session.get(GraphNode, node_a) is None
