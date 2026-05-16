# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G9.1-T3 topology refresh service.

Coverage matrix (Task #450 acceptance criteria):

* **Insert path** — a connector returning 3 nodes + 2 edges inserts
  3 + 2 rows; :class:`RefreshResult` carries the correct counts.
* **Unchanged path** — a second refresh with the same hints reports
  zero added / removed and bumps ``last_seen`` only (no spurious
  ``updated`` from a proxy-vs-dict properties comparison).
* **Soft-delete path** — a node dropped from discovery gets
  ``last_seen = NULL`` and ``removed_nodes == 1``; the row survives.
* **Update path** — changed ``properties`` on an existing node take
  the UPDATE branch; properties + ``last_seen`` are refreshed.
* **Transactional** — a mid-reconcile failure rolls the whole
  transaction back; the graph is byte-identical to before.
* **Audit + broadcast** — one ``audit_log`` row per refresh with the
  canonical ``topology.refresh`` op_id + counts; one broadcast event
  published (mocked at :func:`publish_event`).
* **Tenant boundary** — a refresh writes only ``(tenant_id=A)`` rows;
  a same-named target in tenant B is untouched.

The tests run against ``sqlite+aiosqlite`` via the shared engine cache
the autouse ``_default_database_url`` fixture in :mod:`tests.conftest`
pre-migrates to ``alembic upgrade head`` — the same shape every other
DB-touching unit test uses. The PG-only advisory-lock path is exercised
in :mod:`tests.test_topology_scheduler`'s non-PG no-op assertion and in
the integration suite.
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
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import (
    EdgeHint,
    NodeHint,
    TopologyHints,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, GraphEdge, GraphNode, Target, Tenant
from meho_backplane.operations._handler_resolve import reset_connector_instance_cache
from meho_backplane.settings import get_settings
from meho_backplane.topology.refresh import RefreshResult, refresh_target_topology

_PUBLISH = "meho_backplane.topology.refresh.publish_event"


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
# Fakes
# ---------------------------------------------------------------------------


class _FakeConnector(Connector):
    """Connector whose ``discover_topology`` returns a class-level snapshot.

    Tests mutate :attr:`hints` between refreshes to drive the
    insert / update / soft-delete branches.
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
    register_connector_v2(
        product="faketopo",
        version="",
        impl_id="",
        cls=_FakeConnector,
    )


async def _seed_tenant_and_target(slug: str = "rdc-internal") -> tuple[uuid.UUID, Target]:
    """Insert one tenant + one target for it; return ``(tenant_id, target)``."""
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
        tenant_role=TenantRole.OPERATOR,
    )


def _hints_3n2e() -> TopologyHints:
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


# ---------------------------------------------------------------------------
# Insert path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_refresh_inserts_nodes_and_edges() -> None:
    _register_fake()
    tenant_id, target = await _seed_tenant_and_target()
    _FakeConnector.hints = _hints_3n2e()

    with patch(_PUBLISH, new=AsyncMock()):
        result = await refresh_target_topology(target, _operator(tenant_id))

    assert isinstance(result, RefreshResult)
    assert result.added_nodes == 3
    assert result.added_edges == 2
    assert result.updated_nodes == 0
    assert result.removed_nodes == 0
    assert result.removed_edges == 0

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        nodes = (
            (await session.execute(select(GraphNode).where(GraphNode.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        edges = (
            (await session.execute(select(GraphEdge).where(GraphEdge.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    assert len(nodes) == 3
    assert len(edges) == 2
    assert all(n.target_id == target.id for n in nodes)
    assert all(n.last_seen is not None for n in nodes)


# ---------------------------------------------------------------------------
# Unchanged path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_refresh_same_hints_is_unchanged() -> None:
    _register_fake()
    tenant_id, target = await _seed_tenant_and_target()
    _FakeConnector.hints = _hints_3n2e()
    op = _operator(tenant_id)

    with patch(_PUBLISH, new=AsyncMock()):
        await refresh_target_topology(target, op)
        result = await refresh_target_topology(target, op)

    assert result.added_nodes == 0
    assert result.added_edges == 0
    assert result.updated_nodes == 0
    assert result.updated_edges == 0
    assert result.removed_nodes == 0
    assert result.removed_edges == 0


# ---------------------------------------------------------------------------
# Soft-delete path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dropped_node_is_soft_deleted() -> None:
    _register_fake()
    tenant_id, target = await _seed_tenant_and_target()
    _FakeConnector.hints = _hints_3n2e()
    op = _operator(tenant_id)

    with patch(_PUBLISH, new=AsyncMock()):
        await refresh_target_topology(target, op)
        # Drop vm-b (and its edge) from discovery.
        _FakeConnector.hints = TopologyHints(
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
        result = await refresh_target_topology(target, op)

    assert result.removed_nodes == 1
    assert result.removed_edges == 1
    assert result.added_nodes == 0

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        vm_b = (
            await session.execute(
                select(GraphNode).where(
                    GraphNode.tenant_id == tenant_id,
                    GraphNode.name == "vm-b",
                )
            )
        ).scalar_one()
    # Soft delete — row survives, last_seen nulled.
    assert vm_b.last_seen is None


# ---------------------------------------------------------------------------
# Update path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_changed_properties_take_update_path() -> None:
    _register_fake()
    tenant_id, target = await _seed_tenant_and_target()
    _FakeConnector.hints = _hints_3n2e()
    op = _operator(tenant_id)

    with patch(_PUBLISH, new=AsyncMock()):
        await refresh_target_topology(target, op)
        _FakeConnector.hints = TopologyHints(
            discovered_at=datetime.now(UTC),
            nodes=(
                NodeHint(kind="vm", name="vm-a", properties={"power": "off"}),
                NodeHint(kind="vm", name="vm-b"),
                NodeHint(kind="datastore", name="ds-1"),
            ),
            edges=_hints_3n2e().edges,
        )
        result = await refresh_target_topology(target, op)

    assert result.updated_nodes == 1
    assert result.added_nodes == 0
    assert result.removed_nodes == 0

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        vm_a = (
            await session.execute(
                select(GraphNode).where(
                    GraphNode.tenant_id == tenant_id,
                    GraphNode.name == "vm-a",
                )
            )
        ).scalar_one()
    assert vm_a.properties == {"power": "off"}
    assert vm_a.last_seen is not None


# ---------------------------------------------------------------------------
# Transactional rollback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mid_reconcile_failure_rolls_back() -> None:
    _register_fake()
    tenant_id, target = await _seed_tenant_and_target()
    _FakeConnector.hints = _hints_3n2e()
    op = _operator(tenant_id)

    # Fail the audit write (last step inside the transaction) so the
    # already-staged node/edge inserts must roll back.
    with (
        patch(_PUBLISH, new=AsyncMock()),
        patch(
            "meho_backplane.topology.refresh._write_audit_and_broadcast",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await refresh_target_topology(target, op)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        nodes = (
            (await session.execute(select(GraphNode).where(GraphNode.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        audit = (
            (await session.execute(select(AuditLog).where(AuditLog.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    assert nodes == []
    assert audit == []


# ---------------------------------------------------------------------------
# Audit + broadcast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_emits_audit_row_and_broadcast() -> None:
    _register_fake()
    tenant_id, target = await _seed_tenant_and_target()
    _FakeConnector.hints = _hints_3n2e()

    publish_mock = AsyncMock()
    with patch(_PUBLISH, new=publish_mock):
        await refresh_target_topology(target, _operator(tenant_id))

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.path == "topology.refresh"
    assert row.target_id == target.id
    assert row.payload["op_id"] == "topology.refresh"
    assert row.payload["op_class"] == "read"
    assert row.payload["added_nodes"] == 3

    publish_mock.assert_awaited_once()
    event = publish_mock.await_args.args[0]
    assert event.op_id == "topology.refresh"
    assert event.op_class == "read"
    assert event.tenant_id == tenant_id
    assert event.payload["added_nodes"] == 3
    # No per-resource leak — only counts + metadata.
    assert "nodes" not in event.payload
    assert "name" not in event.payload


@pytest.mark.asyncio
async def test_broadcast_failure_does_not_fail_refresh() -> None:
    _register_fake()
    tenant_id, target = await _seed_tenant_and_target()
    _FakeConnector.hints = _hints_3n2e()

    with patch(_PUBLISH, new=AsyncMock(side_effect=RuntimeError("redis down"))):
        result = await refresh_target_topology(target, _operator(tenant_id))

    # Refresh still succeeded; audit row committed.
    assert result.added_nodes == 3
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Tenant boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_is_tenant_scoped() -> None:
    _register_fake()
    tenant_a, target_a = await _seed_tenant_and_target("tenant-a")
    tenant_b, _target_b = await _seed_tenant_and_target("tenant-b")
    _FakeConnector.hints = _hints_3n2e()

    with patch(_PUBLISH, new=AsyncMock()):
        await refresh_target_topology(target_a, _operator(tenant_a))

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        a_nodes = (
            (await session.execute(select(GraphNode).where(GraphNode.tenant_id == tenant_a)))
            .scalars()
            .all()
        )
        b_nodes = (
            (await session.execute(select(GraphNode).where(GraphNode.tenant_id == tenant_b)))
            .scalars()
            .all()
        )
    assert len(a_nodes) == 3
    assert b_nodes == []
    assert all(n.tenant_id == tenant_a for n in a_nodes)
