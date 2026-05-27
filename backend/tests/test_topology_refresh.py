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
from typing import Any, ClassVar
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
    """Insert one tenant + one target for it; return ``(tenant_id, target)``.

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


# ---------------------------------------------------------------------------
# Cross-target / manual-annotation collision (#673 regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_adopts_pre_existing_target_null_node_without_collision() -> None:
    """A snapshot node that already exists under ``target_id IS NULL``.

    ``graph_node`` is unique on ``(tenant_id, kind, name)`` — the index
    is target-independent. A node first created as a manual annotation
    (``target_id IS NULL``, the documented :func:`resolve_node` shape)
    or by another target can be re-asserted by *this* target's probe.
    The pre-#673 reconcile scoped its existing-node lookup by
    ``target_id`` only, missed the row, issued a second INSERT for the
    same ``(tenant, kind, name)`` and blew the unique index mid-reconcile
    (``UniqueViolationError`` surfaced via query-invoked autoflush in
    ``_reconcile_edges``). The fix keys the upsert on the tenant-wide
    natural key: the existing row is **adopted** (``last_seen`` bumped,
    ``target_id`` claimed) instead of duplicated.
    """
    _register_fake()
    tenant_id, target = await _seed_tenant_and_target()
    op = _operator(tenant_id)

    # Pre-seed ``vm-a`` as a manual annotation: same (tenant, kind, name)
    # the snapshot below re-asserts, but unowned by any target.
    sessionmaker = get_sessionmaker()
    annotated_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            GraphNode(
                id=annotated_id,
                tenant_id=tenant_id,
                kind="vm",
                name="vm-a",
                target_id=None,
                properties={},
                discovered_by="curated",
                first_seen=datetime.now(UTC),
                last_seen=None,
            )
        )
        await session.commit()

    _FakeConnector.hints = _hints_3n2e()

    with patch(_PUBLISH, new=AsyncMock()):
        result = await refresh_target_topology(target, op)

    # vm-a is adopted (updated, not added); vm-b + ds-1 are genuinely new.
    assert result.added_nodes == 2
    assert result.updated_nodes == 1
    assert result.removed_nodes == 0
    assert result.added_edges == 2

    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(GraphNode).where(GraphNode.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    by_name = {n.name: n for n in rows}
    # Exactly three rows — no duplicate vm-a.
    assert sorted(by_name) == ["ds-1", "vm-a", "vm-b"]
    # The annotated row was adopted in place: same id, now owned by the
    # discovering target, last_seen stamped, probe properties applied.
    vm_a = by_name["vm-a"]
    assert vm_a.id == annotated_id
    assert vm_a.target_id == target.id
    assert vm_a.last_seen is not None
    assert vm_a.properties == {"power": "on"}


@pytest.mark.asyncio
async def test_refresh_does_not_soft_delete_other_targets_nodes() -> None:
    """The soft-delete pass stays scoped to the refreshing target.

    Two targets in the same tenant. Target A discovers ``vm-a``; a later
    refresh of target B (whose snapshot does not contain ``vm-a``) must
    not soft-delete A's row — the widened upsert lookup must not widen
    the removal pass.
    """
    _register_fake()
    tenant_id, target_a = await _seed_tenant_and_target("tenant-x")
    op = _operator(tenant_id)

    # Second target in the same tenant.
    sessionmaker = get_sessionmaker()
    target_b = Target(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="vcenter-b",
        aliases=[],
        product="faketopo",
        host="vc-b.example.test",
    )
    async with sessionmaker() as session:
        session.add(target_b)
        await session.commit()
        await session.refresh(target_b)

    with patch(_PUBLISH, new=AsyncMock()):
        # Target A discovers the 3n/2e snapshot.
        _FakeConnector.hints = _hints_3n2e()
        await refresh_target_topology(target_a, op)
        # Target B discovers a disjoint single node.
        _FakeConnector.hints = TopologyHints(
            discovered_at=datetime.now(UTC),
            nodes=(NodeHint(kind="vm", name="vm-z"),),
            edges=(),
        )
        result_b = await refresh_target_topology(target_b, op)

    # B added its own node and removed nothing — A's nodes are off-limits.
    assert result_b.added_nodes == 1
    assert result_b.removed_nodes == 0

    async with sessionmaker() as session:
        a_vm_a = (
            await session.execute(
                select(GraphNode).where(
                    GraphNode.tenant_id == tenant_id,
                    GraphNode.kind == "vm",
                    GraphNode.name == "vm-a",
                )
            )
        ).scalar_one()
    assert a_vm_a.target_id == target_a.id
    assert a_vm_a.last_seen is not None, "target B's refresh must not soft-delete target A's node"


# ---------------------------------------------------------------------------
# G0.14-T12 (#1201) -- refresh service forwards the operator to
# operator-aware ``discover_topology`` overrides (K8s populator).
# ---------------------------------------------------------------------------


class _OperatorAwareConnector(Connector):
    """Connector whose ``discover_topology`` declares ``operator`` keyword.

    Mirrors the
    :meth:`~meho_backplane.connectors.kubernetes.connector.KubernetesConnector.discover_topology`
    signature shape (``(self, target, *, operator: Operator | None = None)``)
    so the refresh service's signature-introspection forwarder is
    exercised against the same contract the K8s populator declares,
    without booting a k3s testcontainer in the unit suite.
    """

    product = "k8s-test-populator"

    captured_operators: ClassVar[list[Operator]] = []

    async def fingerprint(self, target: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def execute(
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def discover_topology(  # type: ignore[override]
        self,
        target: Any,
        *,
        operator: Operator | None = None,
    ) -> TopologyHints:
        if operator is not None:
            type(self).captured_operators.append(operator)
        return TopologyHints(
            discovered_at=datetime.now(UTC),
            nodes=(
                NodeHint(kind="target", name=target.name, properties={"git_version": "v1.32.5"}),
                NodeHint(kind="namespace", name="default"),
                NodeHint(kind="node", name="ctrl-plane-1"),
            ),
            edges=(
                EdgeHint(
                    from_kind="namespace",
                    from_name="default",
                    to_kind="target",
                    to_name=target.name,
                    kind="belongs-to",
                ),
                EdgeHint(
                    from_kind="node",
                    from_name="ctrl-plane-1",
                    to_kind="target",
                    to_name=target.name,
                    kind="belongs-to",
                ),
            ),
        )


@pytest.mark.asyncio
async def test_refresh_forwards_operator_to_k8s_style_discover_topology() -> None:
    """The refresh service introspects the override and forwards ``operator`` when accepted.

    Pins the G0.14-T12 (#1201) decision: refresh service is the
    authority on threading the per-tenant system operator into a
    populator that needs it (e.g. K8s' kubeconfig-from-Vault chain
    reads under the operator's identity). Connectors whose override
    didn't declare ``operator`` keep their ``(self, target)`` signature
    and run unchanged.
    """
    register_connector_v2(
        product="k8s-test-populator",
        version="",
        impl_id="",
        cls=_OperatorAwareConnector,
    )
    tenant_id, target = await _seed_tenant_and_target("rdc-internal")
    target.product = "k8s-test-populator"
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(target)
        await session.commit()

    op = _operator(tenant_id)
    _OperatorAwareConnector.captured_operators = []

    with patch(_PUBLISH, new=AsyncMock()):
        result = await refresh_target_topology(target, op)

    assert _OperatorAwareConnector.captured_operators == [op], (
        "refresh service must forward the operator to a populator whose "
        "``discover_topology`` override declares the keyword parameter"
    )
    # 1 target anchor + 1 namespace + 1 cluster node = 3 nodes; 2 belongs-to edges.
    assert result.added_nodes == 3
    assert result.added_edges == 2


@pytest.mark.asyncio
async def test_refresh_with_k8s_style_populator_is_idempotent_on_recall() -> None:
    """Acceptance criterion: ``RefreshResult`` second-call counts must be all zero.

    Mirrors the issue body's "first call ``added_nodes >= 2``;
    immediate second call ``added_nodes == 0, updated_nodes == 0``"
    contract under the refresh service's diff/apply sweep — the same
    sweep that runs against the live rke2-infra cluster in the v0.7.x
    deploy.
    """
    register_connector_v2(
        product="k8s-test-populator",
        version="",
        impl_id="",
        cls=_OperatorAwareConnector,
    )
    tenant_id, target = await _seed_tenant_and_target("rdc-internal")
    target.product = "k8s-test-populator"
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(target)
        await session.commit()

    op = _operator(tenant_id)

    with patch(_PUBLISH, new=AsyncMock()):
        first = await refresh_target_topology(target, op)
        second = await refresh_target_topology(target, op)

    assert first.added_nodes >= 2  # ≥2 per the issue body's acceptance criterion.
    assert second.added_nodes == 0
    assert second.added_edges == 0
    assert second.updated_nodes == 0
    assert second.updated_edges == 0
    assert second.removed_nodes == 0
    assert second.removed_edges == 0

    # Acceptance criterion: graph_node rows are visible to subsequent
    # queries scoped to this (tenant, target). Existing ``query_topology``
    # and MCP tools (``topology/dependents/{name}`` /
    # ``topology/path/{from}/{to}``) read against this same
    # (tenant_id, kind, name) natural key, so visibility here implies
    # visibility there without further wiring.
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(GraphNode).where(
                        GraphNode.tenant_id == tenant_id,
                        GraphNode.target_id == target.id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert {(r.kind, r.name) for r in rows} == {
        ("target", target.name),
        ("namespace", "default"),
        ("node", "ctrl-plane-1"),
    }
