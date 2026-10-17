# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""PostgreSQL tests for the staged topology scope cut-over (#3749).

The cut-over changes the schema inside one transaction, so only
PostgreSQL can show it. Each test starts from a *legacy* graph built by
the real refresh path: two Kubernetes-shaped targets that both report a
``default`` namespace, merged into one row by the old
``(tenant_id, kind, name)`` key.

Coverage:

* **Staged manifest** -- staging writes only a manifest; the two
  ``default`` namespaces stage as distinct ``target:<uuid>`` identities.
* **Atomic cut-over** -- one transaction publishes the scoped rows, sets
  the merged row aside as ``legacy:<id>``, swaps the unique key, makes
  ``source_scope`` required and switches the writers. No path joins the
  two clusters through a shared namespace name.
* **Order** -- discovering the targets in the opposite order gives the
  same scoped graph.
* **Refusals** -- changed snapshot, unavailable target, deleted target,
  malformed scope, unresolved edge: nothing changes and the old graph
  stays readable.
* **Interrupted cut-over** -- a crash after the old key was dropped rolls
  everything back; the same manifest then rolls forward.
* **Soft-delete** -- after the cut-over a refresh only soft-deletes rows
  in its own scope.
* **Rollback guard** -- a second cut-over, a new staging run and the
  ``0109`` downgrade all refuse once the graph is scoped.
* **Curated quarantine** -- a curated edge to the merged row stays, with
  its history, but leaves every graph answer.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any, ClassVar
from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa
from alembic import command

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import EdgeHint, NodeHint, TopologyHints
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.migrations import alembic_config
from meho_backplane.db.models import GraphEdge, GraphNode, GraphNodeHistory, Target
from meho_backplane.operations._handler_resolve import reset_connector_instance_cache
from meho_backplane.topology.nodes import create_or_get_node
from meho_backplane.topology.query import (
    AmbiguousNodeError,
    find_dependencies,
    find_dependents,
    find_path,
)
from meho_backplane.topology.refresh import refresh_target_topology
from meho_backplane.topology.scope_repair import (
    AlreadyScopedError,
    CutoverRefusedError,
    cut_over,
    load_manifest,
    stage_scope_repair,
)
from meho_backplane.topology.scope_repair import cutover as cutover_module
from meho_backplane.topology.scope_repair.store import MANIFEST
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_PUBLISH = "meho_backplane.topology.refresh.publish_event"

# Return the schema to its pre-cut-over shape between tests. Test-only:
# production never restores the old key once scoped rows exist.
_RESET_SQL = (
    "DROP INDEX IF EXISTS graph_node_tenant_scope_kind_name_idx",
    "ALTER TABLE graph_node ALTER COLUMN source_scope DROP NOT NULL",
    "CREATE INDEX graph_node_tenant_scope_kind_name_idx "
    "ON graph_node (tenant_id, source_scope, kind, name)",
    "CREATE UNIQUE INDEX IF NOT EXISTS graph_node_tenant_kind_name_idx "
    "ON graph_node (tenant_id, kind, name)",
    "UPDATE topology_identity_state SET mode = 'legacy', manifest_id = NULL",
    "TRUNCATE topology_scope_manifest",
    "DELETE FROM targets",
)


class _FakeCluster(Connector):
    """Kubernetes-shaped populator; answers per target id from :attr:`answers`."""

    product = "fakek8s"
    answers: ClassVar[dict[uuid.UUID, TopologyHints | Exception]] = {}

    async def fingerprint(self, target: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def execute(
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def discover_topology(self, target: Any) -> TopologyHints:
        answer = type(self).answers[target.id]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _cluster(name: str, *namespaces: str, dangling: bool = False) -> TopologyHints:
    nodes = [NodeHint(kind="target", name=name)]
    edges = []
    for ns in namespaces:
        nodes.append(NodeHint(kind="namespace", name=ns, properties={"status": "Active"}))
        edges.append(
            EdgeHint(
                from_kind="namespace",
                from_name=ns,
                to_kind="target",
                to_name=name,
                kind="belongs-to",
            )
        )
    if dangling:
        edges.append(
            EdgeHint(
                from_kind="namespace",
                from_name="ghost",
                to_kind="target",
                to_name=name,
                kind="belongs-to",
            )
        )
    return TopologyHints(discovered_at=datetime.now(UTC), nodes=tuple(nodes), edges=tuple(edges))


def _operator() -> Operator:
    return Operator(
        sub="op-scope",
        name=None,
        email=None,
        raw_jwt="",
        tenant_id=TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


async def _run_sql(*statements: str) -> None:
    async with get_sessionmaker()() as session, session.begin():
        for statement in statements:
            await session.execute(sa.text(statement))


async def _scalar(statement: str) -> Any:
    async with get_sessionmaker()() as session:
        return (await session.execute(sa.text(statement))).scalar()


@pytest.fixture(autouse=True)
def _fake_registry() -> Iterator[None]:
    clear_registry()
    reset_connector_instance_cache()
    register_connector_v2(product="fakek8s", version="", impl_id="", cls=_FakeCluster)
    _FakeCluster.answers = {}
    yield
    clear_registry()
    reset_connector_instance_cache()


@pytest.fixture
async def scope_db(pg_engine: None) -> AsyncIterator[None]:
    """A legacy-mode schema with an empty graph, before and after each test."""
    await _run_sql(*_RESET_SQL)
    try:
        yield
    finally:
        await _run_sql("TRUNCATE graph_edge, graph_edge_history, graph_node, graph_node_history")
        await _run_sql(*_RESET_SQL)


async def _add_target(name: str, target_id: uuid.UUID | None = None) -> Target:
    target = Target(
        id=target_id or uuid.uuid4(),
        tenant_id=TENANT,
        name=name,
        aliases=[],
        product="fakek8s",
        host=f"{name}.example.test",
    )
    async with get_sessionmaker()() as session:
        session.add(target)
        await session.commit()
        await session.refresh(target)
    return target


async def _refresh(target: Target, hints: TopologyHints) -> None:
    _FakeCluster.answers[target.id] = hints
    with patch(_PUBLISH, new=AsyncMock()):
        await refresh_target_topology(target, _operator())


async def _legacy_graph(
    *, a_id: uuid.UUID | None = None, b_id: uuid.UUID | None = None
) -> tuple[Target, Target]:
    """Two clusters that both report ``default``; the old key merges it into one row."""
    a = await _add_target("cluster-a", a_id)
    b = await _add_target("cluster-b", b_id)
    await _refresh(a, _cluster("cluster-a", "default", "only-a"))
    await _refresh(b, _cluster("cluster-b", "default", "only-b"))
    return a, b


async def _nodes() -> list[GraphNode]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(sa.select(GraphNode))).scalars().all())


async def _index_exists(name: str) -> bool:
    return bool(await _scalar(f"SELECT to_regclass('{name}') IS NOT NULL"))


async def _mode() -> str:
    return str(await _scalar("SELECT mode FROM topology_identity_state WHERE id = 1"))


async def _set_manifest(manifest_id: uuid.UUID, **values: Any) -> None:
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(
            sa.update(MANIFEST).where(MANIFEST.c.id == manifest_id).values(**values)
        )


async def test_stage_writes_only_a_manifest_with_distinct_scoped_identities(scope_db: None) -> None:
    a, b = await _legacy_graph()
    before = {(n.id, n.kind, n.name, n.source_scope, n.last_seen) for n in await _nodes()}
    assert sum(1 for n in before if n[2] == "default") == 1, "legacy key merges default"

    manifest = await stage_scope_repair()

    assert {(n.id, n.kind, n.name, n.source_scope, n.last_seen) for n in await _nodes()} == before
    assert manifest.status == "staged"
    scoped_defaults = {
        n.scope for n in manifest.plan.nodes if n.name == "default" and n.action == "publish"
    }
    assert scoped_defaults == {f"target:{a.id}", f"target:{b.id}"}
    merged = next(n for n in manifest.plan.nodes if n.name == "default" and n.old_node_id)
    assert (merged.action, merged.reason) == ("legacy", "claimed_by_several_targets")
    assert {t.target_id for t in manifest.plan.targets if t.available} == {a.id, b.id}
    async with get_sessionmaker()() as session:
        stored = await load_manifest(session, manifest.id)
    assert stored is not None and stored.plan == manifest.plan


async def test_cutover_publishes_the_scoped_graph_in_one_step(scope_db: None) -> None:
    a, b = await _legacy_graph()
    merged = next(n for n in await _nodes() if n.name == "default")
    manifest = await stage_scope_repair()

    result = await cut_over(manifest.id)

    assert result.nodes["legacy"] == 1
    assert await _mode() == "scoped"
    assert not await _index_exists("graph_node_tenant_kind_name_idx")
    assert await _scalar(
        "SELECT indisunique FROM pg_index WHERE indexrelid = "
        "'graph_node_tenant_scope_kind_name_idx'::regclass"
    )
    assert (
        await _scalar(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'graph_node' AND column_name = 'source_scope'"
        )
        == "NO"
    )
    nodes = {(n.source_scope, n.kind, n.name): n for n in await _nodes()}
    assert (f"target:{a.id}", "namespace", "default") in nodes
    assert (f"target:{b.id}", "namespace", "default") in nodes
    assert (f"target:{a.id}", "namespace", "only-a") in nodes
    set_aside = nodes[(f"legacy:{merged.id}", "namespace", "default")]
    assert set_aside.id == merged.id and set_aside.last_seen is None
    assert nodes[("global", "target", "cluster-a")].target_id == a.id
    async with get_sessionmaker()() as session:
        stored = await load_manifest(session, manifest.id)
    assert stored is not None and stored.status == "cut_over"

    # No path joins the two clusters through the shared namespace name.
    assert (
        await find_path(_operator(), "cluster-a", "cluster-b", from_kind="target", to_kind="target")
        is None
    )
    closure = await find_dependents(_operator(), "cluster-a", kind="target")
    assert {(n.name, n.source_scope) for n in closure if n.kind == "namespace"} == {
        ("default", f"target:{a.id}"),
        ("only-a", f"target:{a.id}"),
    }
    with pytest.raises(AmbiguousNodeError) as excinfo:
        await find_dependencies(_operator(), "default", kind="namespace")
    assert excinfo.value.scopes == sorted([f"target:{a.id}", f"target:{b.id}"])


async def test_reversed_discovery_order_gives_the_same_scoped_graph(scope_db: None) -> None:
    low, high = uuid.UUID(int=1), uuid.UUID(int=2)

    async def _scoped_shape(a_id: uuid.UUID, b_id: uuid.UUID) -> set[tuple[Any, ...]]:
        a, b = await _legacy_graph(a_id=a_id, b_id=b_id)
        await cut_over((await stage_scope_repair()).id)
        names = {f"target:{a.id}": "A", f"target:{b.id}": "B"}
        nodes = {
            n.id: (names.get(n.source_scope or "", n.source_scope), n.kind, n.name)
            for n in await _nodes()
            if not (n.source_scope or "").startswith("legacy:")
        }
        async with get_sessionmaker()() as session:
            edges = (await session.execute(sa.select(GraphEdge))).scalars().all()
        return {("node", *v) for v in nodes.values()} | {
            ("edge", nodes[e.from_node_id], nodes[e.to_node_id], e.kind, e.last_seen is not None)
            for e in edges
            if e.from_node_id in nodes and e.to_node_id in nodes
        }

    first = await _scoped_shape(low, high)
    await _run_sql("TRUNCATE graph_edge, graph_edge_history, graph_node, graph_node_history")
    await _run_sql(*_RESET_SQL)
    second = await _scoped_shape(high, low)

    assert first == second
    assert ("node", "A", "namespace", "default") in first
    assert ("node", "B", "namespace", "default") in first


async def test_cutover_refuses_a_changed_snapshot_and_keeps_the_old_graph(scope_db: None) -> None:
    a, _b = await _legacy_graph()
    manifest = await stage_scope_repair()
    await _refresh(a, _cluster("cluster-a", "default", "only-a", "new-ns"))

    with pytest.raises(CutoverRefusedError) as excinfo:
        await cut_over(manifest.id)

    assert excinfo.value.reason == "snapshot_changed"
    assert await _mode() == "legacy"
    assert await _index_exists("graph_node_tenant_kind_name_idx")
    assert all(n.source_scope in (None, "global") for n in await _nodes())
    async with get_sessionmaker()() as session:
        stored = await load_manifest(session, manifest.id)
    assert stored is not None and (stored.status, stored.refusal_reason) == (
        "refused",
        "snapshot_changed",
    )
    closure = await find_dependents(_operator(), "cluster-a", kind="target")
    assert {n.name for n in closure} >= {"cluster-a", "default", "only-a", "new-ns"}


async def test_cutover_refuses_an_unavailable_or_deleted_target(scope_db: None) -> None:
    _a, b = await _legacy_graph()
    _FakeCluster.answers[b.id] = TimeoutError("cluster unreachable")
    incomplete = await stage_scope_repair()
    assert incomplete.status == "incomplete"
    assert [p.code for p in incomplete.plan.problems] == ["target_unavailable"]
    with pytest.raises(CutoverRefusedError) as excinfo:
        await cut_over(incomplete.id)
    assert excinfo.value.reason == "manifest_incomplete"

    _FakeCluster.answers[b.id] = _cluster("cluster-b", "default", "only-b")
    staged = await stage_scope_repair()
    await _run_sql(f"UPDATE targets SET deleted_at = now() WHERE id = '{b.id}'")
    with pytest.raises(CutoverRefusedError) as excinfo:
        await cut_over(staged.id)
    assert excinfo.value.reason == "target_unavailable"
    assert await _mode() == "legacy"
    assert await _index_exists("graph_node_tenant_kind_name_idx")


async def test_cutover_refuses_a_malformed_scope(scope_db: None) -> None:
    await _legacy_graph()
    manifest = await stage_scope_repair()
    body = manifest.plan.model_dump(mode="json")
    body["nodes"][0]["scope"] = "target:not-a-uuid"
    await _set_manifest(manifest.id, body=body)

    with pytest.raises(CutoverRefusedError) as excinfo:
        await cut_over(manifest.id)

    assert excinfo.value.reason == "malformed_scope"
    assert await _mode() == "legacy"
    assert all(n.source_scope in (None, "global") for n in await _nodes())


async def test_cutover_refuses_an_unresolved_edge(scope_db: None) -> None:
    a, _b = await _legacy_graph()
    _FakeCluster.answers[a.id] = _cluster("cluster-a", "default", "only-a", dangling=True)
    manifest = await stage_scope_repair()
    assert manifest.status == "incomplete"
    assert [p.code for p in manifest.plan.problems] == ["unresolved_edge"]
    await _set_manifest(manifest.id, status="staged")

    with pytest.raises(CutoverRefusedError) as excinfo:
        await cut_over(manifest.id)

    assert excinfo.value.reason == "unresolved_edge"
    assert await _mode() == "legacy"
    assert await _index_exists("graph_node_tenant_kind_name_idx")


async def test_interrupted_cutover_rolls_back_then_rolls_forward(
    scope_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _legacy_graph()
    before = {(n.id, n.source_scope, n.last_seen) for n in await _nodes()}
    manifest = await stage_scope_repair()

    async def _crash(session: Any) -> None:
        raise RuntimeError("connection lost mid cut-over")

    monkeypatch.setattr(cutover_module, "_finalise_schema", _crash)
    with pytest.raises(RuntimeError, match="connection lost"):
        await cut_over(manifest.id)

    assert {(n.id, n.source_scope, n.last_seen) for n in await _nodes()} == before
    assert await _mode() == "legacy"
    assert await _index_exists("graph_node_tenant_kind_name_idx")
    async with get_sessionmaker()() as session:
        stored = await load_manifest(session, manifest.id)
    assert stored is not None and stored.status == "staged"

    monkeypatch.undo()
    await cut_over(manifest.id)
    assert await _mode() == "scoped"


async def _seed(kind: str, name: str) -> GraphNode:
    async with get_sessionmaker()() as session:
        return (await create_or_get_node(session, _operator(), kind=kind, name=name)).node


async def test_soft_delete_after_cutover_stays_inside_the_target_scope(scope_db: None) -> None:
    a, b = await _legacy_graph()
    # A soft-deleted row a target still reports comes back at the cut-over;
    # one no target reports any more stays retired, set aside as legacy.
    await _run_sql("UPDATE graph_node SET last_seen = NULL WHERE name IN ('only-a', 'only-b')")
    _FakeCluster.answers[b.id] = _cluster("cluster-b", "default")
    await cut_over((await stage_scope_repair()).id)
    nodes = {(n.source_scope or "", n.name): n for n in await _nodes()}
    assert nodes[(f"target:{a.id}", "only-a")].last_seen is not None
    (only_b,) = [n for (scope, name), n in nodes.items() if name == "only-b"]
    assert only_b.source_scope == f"legacy:{only_b.id}" and only_b.last_seen is None

    await _refresh(a, _cluster("cluster-a", "only-a", "fresh"))

    nodes = {(n.source_scope or "", n.name): n for n in await _nodes()}
    assert nodes[(f"target:{a.id}", "default")].last_seen is None
    assert nodes[(f"target:{b.id}", "default")].last_seen is not None
    assert nodes[(f"target:{a.id}", "fresh")].source_scope_provenance == "refresh"
    assert (await _seed("vm", "jump-host")).source_scope == "global"
    with pytest.raises(AmbiguousNodeError):
        await _seed("namespace", "default")


async def test_rollback_guards_refuse_once_scoped(scope_db: None, async_pg_url: str) -> None:
    await _legacy_graph()
    manifest = await stage_scope_repair()
    await cut_over(manifest.id)

    with pytest.raises(CutoverRefusedError) as excinfo:
        await cut_over(manifest.id)
    assert excinfo.value.reason == "already_scoped"
    with pytest.raises(AlreadyScopedError):
        await stage_scope_repair()

    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_pg_url)
    with pytest.raises(RuntimeError, match="Refusing to downgrade 0109"):
        await asyncio.to_thread(command.downgrade, cfg, "0108")
    assert await _mode() == "scoped"


async def test_curated_edge_to_a_merged_row_is_quarantined_with_history(scope_db: None) -> None:
    await _legacy_graph()
    merged = next(n for n in await _nodes() if n.name == "default")
    seed = await _seed("vault-role", "ops")
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            GraphEdge(
                id=uuid.uuid4(),
                tenant_id=TENANT,
                from_node_id=seed.id,
                to_node_id=merged.id,
                kind="authenticates-via",
                source="curated",
                properties={"note": "operator link"},
                discovered_by="op-scope",
                last_seen=datetime.now(UTC),
            )
        )
    manifest = await stage_scope_repair()
    quarantined = [e for e in manifest.plan.edges if e.action == "quarantine"]
    assert {e.source for e in quarantined} == {"auto", "curated"}

    await cut_over(manifest.id)

    async with get_sessionmaker()() as session:
        curated = (
            await session.execute(sa.select(GraphEdge).where(GraphEdge.source == "curated"))
        ).scalar_one()
        history = (
            (
                await session.execute(
                    sa.select(GraphNodeHistory)
                    .where(GraphNodeHistory.node_id == merged.id)
                    .order_by(GraphNodeHistory.history_id)
                )
            )
            .scalars()
            .all()
        )
    assert curated.last_seen is not None and curated.to_node_id == merged.id
    assert history[-1].snapshot["after"]["source_scope"] == f"legacy:{merged.id}"
    assert len(history) >= 2, "the pre-cut-over history is kept"
    closure = await find_dependencies(_operator(), "ops", kind="vault-role")
    assert [n.name for n in closure] == ["ops"], "the legacy row is not a hop"
