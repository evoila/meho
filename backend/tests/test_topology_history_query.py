# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end tests for :func:`query_history` (G9.3-T3 #859).

Coverage matrix (Task #859 acceptance criteria):

* **Anchor + tenant scoping** -- seed history rows on two tenants;
  query one; cross-tenant rows never returned. A cross-tenant *name*
  surfaces as :class:`NodeNotFoundError`, identical to an unknown
  name (the tenant boundary is opaque to the caller).
* **Name + kind resolution** -- bare-name lookup that resolves to
  multiple kinds raises :class:`AmbiguousNodeError`; the ``kind``
  parameter pins the anchor and the call succeeds.
* **Unknown node -> NodeNotFoundError (the route maps to 404).**
* **``include_edges`` joins incident edges' history** -- the merged
  result includes node-side AND edge-side rows for every edge with
  the anchor at either endpoint, ordered ``(valid_from DESC,
  history_id DESC)``.
* **``include_edges`` ignored when False** -- node-side only; edge
  rows of incident edges do NOT leak in.
* **Window scoping** -- ``since`` / ``until`` bound ``valid_from``
  inclusively at both ends.
* **Snapshot round-trip** -- the full ``{before, after}`` JSONB is
  carried on each row so the CLI's ``--json`` mode (and the MCP
  facet) can reconstruct pre/post state. The timeline summary
  truncation does not apply here.
* **Ordering** -- rows return in ``(valid_from DESC, history_id
  DESC)`` order.
* **Limit validation** -- out-of-range ``limit`` raises
  :class:`ValueError`.

The tests run against ``sqlite+aiosqlite`` via the autouse
``_default_database_url`` fixture in :mod:`tests.conftest``. Each
seed helper opens its own ``async with sessionmaker() as session``
+ ``await session.commit()`` so the rows are visible to the
substrate's own session before :func:`query_history` runs --
:func:`query_history` calls :func:`resolve_node` on its own
sessionmaker, and on SQLite the resolver's anchor probe needs the
seed commits to land outside any open transaction the fixture
might be holding. Mirrors :mod:`test_topology_annotate`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    GraphEdge,
    GraphEdgeHistory,
    GraphHistoryChangeKind,
    GraphNode,
    GraphNodeHistory,
    Tenant,
)
from meho_backplane.settings import get_settings
from meho_backplane.topology.query import query_history
from meho_backplane.topology.resolvers import AmbiguousNodeError, NodeNotFoundError


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the Keycloak + Vault env vars :class:`Settings` requires."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_operator(tenant_id: uuid.UUID) -> Operator:
    """Construct an :class:`Operator` for the history call.

    The handler reads only ``operator.tenant_id``; the other fields
    are populated to satisfy the frozen Pydantic model.
    """
    return Operator(
        sub="operator-test",
        name="Test Operator",
        email=None,
        raw_jwt="not-a-real-token",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


# ---------------------------------------------------------------------------
# Seeding helpers (per-seed sessions; mirrors test_topology_annotate.py)
# ---------------------------------------------------------------------------


async def _seed_tenant(*, slug: str = "tenant-a") -> uuid.UUID:
    """Insert a :class:`Tenant` row and return its UUID."""
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
        await session.commit()
    return tenant_id


async def _seed_node(
    tenant_id: uuid.UUID,
    *,
    name: str,
    kind: str = "vm",
) -> uuid.UUID:
    """Insert a :class:`GraphNode` row and return its UUID."""
    sessionmaker = get_sessionmaker()
    node_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            GraphNode(
                id=node_id,
                tenant_id=tenant_id,
                kind=kind,
                name=name,
                discovered_by="vmware",
            )
        )
        await session.commit()
    return node_id


async def _seed_edge(
    tenant_id: uuid.UUID,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    *,
    kind: str = "runs-on",
) -> uuid.UUID:
    """Insert a :class:`GraphEdge` row and return its UUID."""
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
                discovered_by="vmware",
            )
        )
        await session.commit()
    return edge_id


async def _seed_node_history(
    *,
    tenant_id: uuid.UUID,
    node_id: uuid.UUID | None,
    valid_from: datetime,
    change_kind: GraphHistoryChangeKind = GraphHistoryChangeKind.CREATED,
    snapshot: dict[str, object] | None = None,
    audit_id: uuid.UUID | None = None,
) -> None:
    """Insert one :class:`GraphNodeHistory` row."""
    if snapshot is None:
        snapshot = {"before": None, "after": {"kind": "vm", "name": "vm-test"}}
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            GraphNodeHistory(
                node_id=node_id,
                tenant_id=tenant_id,
                change_kind=change_kind.value,
                snapshot=snapshot,
                audit_id=audit_id or uuid.uuid4(),
                valid_from=valid_from,
            )
        )
        await session.commit()


async def _seed_edge_history(
    *,
    tenant_id: uuid.UUID,
    edge_id: uuid.UUID | None,
    valid_from: datetime,
    change_kind: GraphHistoryChangeKind = GraphHistoryChangeKind.CREATED,
    snapshot: dict[str, object] | None = None,
    audit_id: uuid.UUID | None = None,
) -> None:
    """Insert one :class:`GraphEdgeHistory` row."""
    if snapshot is None:
        snapshot = {"before": None, "after": {"kind": "runs-on", "source": "auto"}}
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            GraphEdgeHistory(
                edge_id=edge_id,
                tenant_id=tenant_id,
                change_kind=change_kind.value,
                snapshot=snapshot,
                audit_id=audit_id or uuid.uuid4(),
                valid_from=valid_from,
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Anchor + tenant scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_node_history_for_anchor() -> None:
    """Anchor on one node; only its history rows return."""
    tenant_id = await _seed_tenant()
    node_a = await _seed_node(tenant_id, name="vm-a")
    node_b = await _seed_node(tenant_id, name="vm-b")

    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    for i in range(3):
        await _seed_node_history(
            tenant_id=tenant_id,
            node_id=node_a,
            valid_from=base + timedelta(seconds=i),
        )
    # node_b history rows that must NOT surface in node_a's walk.
    for i in range(2):
        await _seed_node_history(
            tenant_id=tenant_id,
            node_id=node_b,
            valid_from=base + timedelta(seconds=10 + i),
        )

    result = await query_history(_make_operator(tenant_id), "vm-a")

    assert result.anchor_node_id == node_a
    assert result.include_edges is False
    assert len(result.rows) == 3
    assert all(row.resource_id == node_a for row in result.rows)


@pytest.mark.asyncio
async def test_unknown_node_raises_not_found() -> None:
    """An unknown name surfaces as :class:`NodeNotFoundError`."""
    tenant_id = await _seed_tenant()
    with pytest.raises(NodeNotFoundError):
        await query_history(_make_operator(tenant_id), "nope")


@pytest.mark.asyncio
async def test_cross_tenant_name_is_not_found() -> None:
    """A name that exists only in another tenant raises NotFound (no leak)."""
    tenant_a = await _seed_tenant(slug="tenant-a")
    tenant_b = await _seed_tenant(slug="tenant-b")
    node_b = await _seed_node(tenant_b, name="vm-secret")
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    await _seed_node_history(
        tenant_id=tenant_b,
        node_id=node_b,
        valid_from=base,
    )

    # Tenant A asks for the name that lives in tenant B. The tenant
    # boundary surfaces as NodeNotFoundError, identical to the
    # unknown-name case; tenant B's row count is never observable.
    with pytest.raises(NodeNotFoundError):
        await query_history(_make_operator(tenant_a), "vm-secret")


@pytest.mark.asyncio
async def test_tenant_isolated_history_for_same_name() -> None:
    """Same node name in two tenants -- each side sees only its own rows."""
    tenant_a = await _seed_tenant(slug="tenant-a")
    tenant_b = await _seed_tenant(slug="tenant-b")
    node_a = await _seed_node(tenant_a, name="vm-shared")
    node_b = await _seed_node(tenant_b, name="vm-shared")

    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    for i in range(3):
        await _seed_node_history(
            tenant_id=tenant_a,
            node_id=node_a,
            valid_from=base + timedelta(seconds=i),
        )
        await _seed_node_history(
            tenant_id=tenant_b,
            node_id=node_b,
            valid_from=base + timedelta(seconds=10 + i),
        )

    result_a = await query_history(_make_operator(tenant_a), "vm-shared")
    assert len(result_a.rows) == 3
    assert all(r.resource_id == node_a for r in result_a.rows)

    result_b = await query_history(_make_operator(tenant_b), "vm-shared")
    assert len(result_b.rows) == 3
    assert all(r.resource_id == node_b for r in result_b.rows)


@pytest.mark.asyncio
async def test_ambiguous_bare_name_raises() -> None:
    """A bare name resolving to multiple kinds raises AmbiguousNodeError."""
    tenant_id = await _seed_tenant()
    await _seed_node(tenant_id, name="app", kind="vm")
    await _seed_node(tenant_id, name="app", kind="host")

    with pytest.raises(AmbiguousNodeError):
        await query_history(_make_operator(tenant_id), "app")


@pytest.mark.asyncio
async def test_kind_pin_disambiguates() -> None:
    """Passing ``kind`` resolves the anchor unambiguously."""
    tenant_id = await _seed_tenant()
    node_vm = await _seed_node(tenant_id, name="app", kind="vm")
    await _seed_node(tenant_id, name="app", kind="host")

    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    await _seed_node_history(
        tenant_id=tenant_id,
        node_id=node_vm,
        valid_from=base,
    )

    result = await query_history(_make_operator(tenant_id), "app", kind="vm")
    assert result.anchor_node_id == node_vm
    assert len(result.rows) == 1


# ---------------------------------------------------------------------------
# include_edges
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_include_edges_joins_incident_edges() -> None:
    """``include_edges=True`` adds history rows for every incident edge."""
    tenant_id = await _seed_tenant()
    node_a = await _seed_node(tenant_id, name="vm-a")
    node_b = await _seed_node(tenant_id, name="host-b", kind="host")
    node_c = await _seed_node(tenant_id, name="host-c", kind="host")
    # Two edges incident to vm-a (one outgoing, one incoming -- the
    # join lands on either endpoint).
    edge_ab = await _seed_edge(tenant_id, node_a, node_b)
    edge_ca = await _seed_edge(tenant_id, node_c, node_a, kind="depends-on")
    # Unrelated edge that must NOT show up.
    edge_bc = await _seed_edge(tenant_id, node_b, node_c, kind="mounts")

    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    # 2 node-history rows for vm-a.
    for i in range(2):
        await _seed_node_history(
            tenant_id=tenant_id,
            node_id=node_a,
            valid_from=base + timedelta(seconds=i),
        )
    # 1 edge-history row each for the two incident edges.
    await _seed_edge_history(
        tenant_id=tenant_id,
        edge_id=edge_ab,
        valid_from=base + timedelta(seconds=2),
    )
    await _seed_edge_history(
        tenant_id=tenant_id,
        edge_id=edge_ca,
        valid_from=base + timedelta(seconds=3),
    )
    # The unrelated edge's history row -- must be filtered out.
    await _seed_edge_history(
        tenant_id=tenant_id,
        edge_id=edge_bc,
        valid_from=base + timedelta(seconds=4),
    )

    result = await query_history(_make_operator(tenant_id), "vm-a", include_edges=True)

    assert result.include_edges is True
    # 2 node rows + 2 incident-edge rows = 4. The unrelated edge_bc's
    # history row stays out.
    assert len(result.rows) == 4
    edge_resource_ids = {r.resource_id for r in result.rows if r.source == "edge"}
    assert edge_resource_ids == {edge_ab, edge_ca}


@pytest.mark.asyncio
async def test_default_excludes_incident_edges() -> None:
    """``include_edges`` defaults to False; edge rows do not leak in."""
    tenant_id = await _seed_tenant()
    node_a = await _seed_node(tenant_id, name="vm-a")
    node_b = await _seed_node(tenant_id, name="host-b", kind="host")
    edge_ab = await _seed_edge(tenant_id, node_a, node_b)

    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    await _seed_node_history(
        tenant_id=tenant_id,
        node_id=node_a,
        valid_from=base,
    )
    await _seed_edge_history(
        tenant_id=tenant_id,
        edge_id=edge_ab,
        valid_from=base + timedelta(seconds=1),
    )

    result = await query_history(_make_operator(tenant_id), "vm-a")
    assert result.include_edges is False
    assert all(r.source == "node" for r in result.rows)
    assert len(result.rows) == 1


# ---------------------------------------------------------------------------
# Window scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_since_until_bounds_inclusive() -> None:
    """``since`` / ``until`` bound ``valid_from`` inclusively at both ends."""
    tenant_id = await _seed_tenant()
    node_id = await _seed_node(tenant_id, name="vm-a")
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    for i in range(10):
        await _seed_node_history(
            tenant_id=tenant_id,
            node_id=node_id,
            valid_from=base + timedelta(minutes=i),
        )

    # Window covers minutes 3..7 inclusive on both ends -> 5 rows.
    result = await query_history(
        _make_operator(tenant_id),
        "vm-a",
        since=base + timedelta(minutes=3),
        until=base + timedelta(minutes=7),
    )
    assert len(result.rows) == 5
    # SQLite strips tzinfo on read-back; compare naive on both sides.
    timestamps = [r.valid_from.replace(tzinfo=None) for r in result.rows]
    assert max(timestamps) == (base + timedelta(minutes=7)).replace(tzinfo=None)
    assert min(timestamps) == (base + timedelta(minutes=3)).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Snapshot round-trip (the differentiator from timeline)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_snapshot_carries_on_each_row() -> None:
    """Each row carries the full ``snapshot.before`` / ``snapshot.after``."""
    tenant_id = await _seed_tenant()
    node_id = await _seed_node(tenant_id, name="vm-a")

    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    pre_state = {"kind": "vm", "name": "vm-a", "properties": {"status": "running"}}
    post_state = {"kind": "vm", "name": "vm-a", "properties": {"status": "stopped"}}
    await _seed_node_history(
        tenant_id=tenant_id,
        node_id=node_id,
        valid_from=base,
        change_kind=GraphHistoryChangeKind.UPDATED,
        snapshot={"before": pre_state, "after": post_state},
    )

    result = await query_history(_make_operator(tenant_id), "vm-a")
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.snapshot is not None
    assert row.snapshot["before"] == pre_state
    assert row.snapshot["after"] == post_state
    assert row.change_kind == "updated"


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_ordered_newest_first() -> None:
    """Rows return in ``(valid_from DESC, history_id DESC)`` order."""
    tenant_id = await _seed_tenant()
    node_a = await _seed_node(tenant_id, name="vm-a")
    node_b = await _seed_node(tenant_id, name="host-b", kind="host")
    edge_ab = await _seed_edge(tenant_id, node_a, node_b)

    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    # Interleave node + edge history at increasing timestamps.
    for i in range(3):
        await _seed_node_history(
            tenant_id=tenant_id,
            node_id=node_a,
            valid_from=base + timedelta(seconds=i * 2),
        )
        await _seed_edge_history(
            tenant_id=tenant_id,
            edge_id=edge_ab,
            valid_from=base + timedelta(seconds=i * 2 + 1),
        )

    result = await query_history(_make_operator(tenant_id), "vm-a", include_edges=True)
    keys = [(r.valid_from, r.history_id) for r in result.rows]
    assert keys == sorted(keys, reverse=True)


# ---------------------------------------------------------------------------
# Limit validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_limit_raises() -> None:
    """Out-of-range ``limit`` raises :class:`ValueError`."""
    tenant_id = await _seed_tenant()
    await _seed_node(tenant_id, name="vm-a")
    with pytest.raises(ValueError):
        await query_history(_make_operator(tenant_id), "vm-a", limit=0)
    with pytest.raises(ValueError):
        await query_history(_make_operator(tenant_id), "vm-a", limit=100_000)
