# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A refresh links snapshot nodes to nodes other targets found (``LinkHint``).

The DNS connectors send one link per name: "connect this DNS name to the
``vm`` / ``host`` / ``node`` with this name or address". These tests pin
what :mod:`meho_backplane.topology.refresh_links` does with them:

* exactly one match gives one ``resolves-to`` edge;
* no match, or more than one, gives no edge and never a new node;
* soft-deleted and ``legacy:`` nodes are never matched;
* a later refresh without the link, or after the far node is gone, marks
  the edge as gone; a curated edge stays;
* a refresh with the same links changes nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select, update

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import (
    EdgeHint,
    LinkHint,
    NodeHint,
    TopologyHints,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphEdge, GraphNode, Target, Tenant
from meho_backplane.operations._handler_resolve import reset_connector_instance_cache
from meho_backplane.settings import get_settings
from meho_backplane.topology.refresh import refresh_target_topology


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    reset_connector_instance_cache()
    register_connector_v2(product="fakelinks", version="", impl_id="", cls=_FakeConnector)
    yield
    clear_registry()
    reset_connector_instance_cache()


class _FakeConnector(Connector):
    """Returns the snapshot stored for the target's name."""

    product = "fakelinks"
    hints_by_target: dict[str, TopologyHints] = {}  # noqa: RUF012 - test fake

    async def fingerprint(self, target: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def discover_topology(self, target: Any) -> TopologyHints:
        return type(self).hints_by_target[target.name]


async def _seed(names: list[str]) -> tuple[uuid.UUID, dict[str, Target]]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = uuid.uuid4()
        session.add(Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[:8]}", name="Links tenant"))
        targets = {
            name: Target(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                name=name,
                aliases=[],
                product="fakelinks",
                host=f"{name}.example.test",
            )
            for name in names
        }
        session.add_all(targets.values())
        await session.commit()
        for target in targets.values():
            await session.refresh(target)
    return tenant_id, targets


def _operator(tenant_id: uuid.UUID) -> Operator:
    return Operator(
        sub="op-1",
        name="Op One",
        email=None,
        raw_jwt="",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


def _systems(*nodes: tuple[str, str]) -> TopologyHints:
    return TopologyHints(
        discovered_at=datetime.now(UTC),
        nodes=(
            NodeHint(kind="target", name="vc"),
            *(NodeHint(kind=kind, name=name) for kind, name in nodes),  # type: ignore[arg-type]
        ),
    )


def _dns(*links: tuple[str, tuple[str, ...], str]) -> TopologyHints:
    names = sorted({name for name, _, _ in links})
    return TopologyHints(
        discovered_at=datetime.now(UTC),
        nodes=(
            NodeHint(kind="target", name="dns"),
            *(NodeHint(kind="dns-record", name=name) for name in names),
        ),
        edges=tuple(
            EdgeHint(
                from_kind="dns-record",
                from_name=name,
                to_kind="target",
                to_name="dns",
                kind="belongs-to",
            )
            for name in names
        ),
        links=tuple(
            LinkHint(
                from_kind="dns-record",
                from_name=name,
                to_kinds=("vm", "host", "node"),
                match_names=match,
                properties={"matched_on": how},
            )
            for name, match, how in links
        ),
    )


async def _resolves_to(tenant_id: uuid.UUID, *, live_only: bool = True) -> set[tuple[str, str]]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        edges = (
            (
                await session.execute(
                    select(GraphEdge).where(
                        GraphEdge.tenant_id == tenant_id, GraphEdge.kind == "resolves-to"
                    )
                )
            )
            .scalars()
            .all()
        )
        nodes = {
            n.id: n
            for n in (
                await session.execute(select(GraphNode).where(GraphNode.tenant_id == tenant_id))
            )
            .scalars()
            .all()
        }
    return {
        (nodes[e.from_node_id].name, f"{nodes[e.to_node_id].kind}:{nodes[e.to_node_id].name}")
        for e in edges
        if e.last_seen is not None or not live_only
    }


async def _node_names(tenant_id: uuid.UUID) -> set[tuple[str, str]]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(GraphNode.kind, GraphNode.name).where(GraphNode.tenant_id == tenant_id)
            )
        ).all()
    return {(kind, name) for kind, name in rows}


@pytest.mark.asyncio
async def test_links_match_by_name_address_and_short_name() -> None:
    tenant_id, targets = await _seed(["vc", "dns"])
    _FakeConnector.hints_by_target = {
        "vc": _systems(("host", "ESX01.example.com"), ("vm", "web01"), ("host", "192.0.2.20")),
        "dns": _dns(
            ("esx01.example.com", ("esx01.example.com",), "name"),
            ("store.example.com", ("192.0.2.20",), "address"),
            ("web01.example.com", ("web01",), "short-name"),
            ("nobody.example.com", ("nobody.example.com", "192.0.2.99"), "name"),
        ),
    }
    op = _operator(tenant_id)
    await refresh_target_topology(targets["vc"], op)
    before = await _node_names(tenant_id)
    result = await refresh_target_topology(targets["dns"], op)

    assert await _resolves_to(tenant_id) == {
        ("esx01.example.com", "host:ESX01.example.com"),
        ("store.example.com", "host:192.0.2.20"),
        ("web01.example.com", "vm:web01"),
    }
    # The unmatched name made no system node: only the DNS target and names.
    after = await _node_names(tenant_id)
    assert after - before == {
        ("target", "dns"),
        ("dns-record", "esx01.example.com"),
        ("dns-record", "store.example.com"),
        ("dns-record", "web01.example.com"),
        ("dns-record", "nobody.example.com"),
    }
    assert result.added_edges == 4 + 3  # four belongs-to, three links

    again = await refresh_target_topology(targets["dns"], op)
    assert (again.added_edges, again.updated_edges, again.removed_edges) == (0, 0, 0)


@pytest.mark.asyncio
async def test_ambiguous_name_links_nothing() -> None:
    tenant_id, targets = await _seed(["vc", "dns"])
    _FakeConnector.hints_by_target = {
        "vc": _systems(("vm", "db"), ("host", "DB")),
        "dns": _dns(("db.example.com", ("db",), "short-name")),
    }
    op = _operator(tenant_id)
    await refresh_target_topology(targets["vc"], op)
    await refresh_target_topology(targets["dns"], op)
    assert await _resolves_to(tenant_id) == set()


@pytest.mark.asyncio
async def test_gone_and_legacy_nodes_are_not_matched() -> None:
    tenant_id, targets = await _seed(["vc", "dns"])
    _FakeConnector.hints_by_target = {
        "vc": _systems(("vm", "old"), ("vm", "set-aside")),
        "dns": _dns(
            ("old.example.com", ("old",), "short-name"),
            ("set-aside.example.com", ("set-aside",), "short-name"),
        ),
    }
    op = _operator(tenant_id)
    await refresh_target_topology(targets["vc"], op)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        await session.execute(
            update(GraphNode)
            .where(GraphNode.tenant_id == tenant_id, GraphNode.name == "old")
            .values(last_seen=None)
        )
        node = (
            await session.execute(
                select(GraphNode).where(
                    GraphNode.tenant_id == tenant_id, GraphNode.name == "set-aside"
                )
            )
        ).scalar_one()
        node.source_scope = f"legacy:{node.id}"
    await refresh_target_topology(targets["dns"], op)
    assert await _resolves_to(tenant_id) == set()


@pytest.mark.asyncio
async def test_link_edge_is_marked_gone_when_link_or_far_node_goes() -> None:
    tenant_id, targets = await _seed(["vc", "dns"])
    _FakeConnector.hints_by_target = {
        "vc": _systems(("vm", "web01"), ("vm", "web02")),
        "dns": _dns(
            ("web01.example.com", ("web01",), "short-name"),
            ("web02.example.com", ("web02",), "short-name"),
        ),
    }
    op = _operator(tenant_id)
    await refresh_target_topology(targets["vc"], op)
    await refresh_target_topology(targets["dns"], op)
    assert len(await _resolves_to(tenant_id)) == 2

    # vCenter no longer has web02; the DNS server no longer has web01.
    _FakeConnector.hints_by_target["vc"] = _systems(("vm", "web01"))
    _FakeConnector.hints_by_target["dns"] = _dns(("web02.example.com", ("web02",), "short-name"))
    await refresh_target_topology(targets["vc"], op)
    result = await refresh_target_topology(targets["dns"], op)
    assert await _resolves_to(tenant_id) == set()
    # Two links and the web01 belongs-to edge.
    assert result.removed_edges == 3
    # The rows stay for history, marked as gone.
    assert len(await _resolves_to(tenant_id, live_only=False)) == 2


@pytest.mark.asyncio
async def test_curated_resolves_to_edge_is_kept() -> None:
    tenant_id, targets = await _seed(["vc", "dns"])
    _FakeConnector.hints_by_target = {
        "vc": _systems(("vm", "web01")),
        "dns": _dns(("web01.example.com", ("web01",), "short-name")),
    }
    op = _operator(tenant_id)
    await refresh_target_topology(targets["vc"], op)
    await refresh_target_topology(targets["dns"], op)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        await session.execute(
            update(GraphEdge)
            .where(GraphEdge.tenant_id == tenant_id, GraphEdge.kind == "resolves-to")
            .values(source="curated")
        )
    _FakeConnector.hints_by_target["dns"] = TopologyHints(
        discovered_at=datetime.now(UTC),
        nodes=(
            NodeHint(kind="target", name="dns"),
            NodeHint(kind="dns-record", name="web01.example.com"),
        ),
    )
    result = await refresh_target_topology(targets["dns"], op)
    assert result.removed_edges == 1  # only the belongs-to edge
    assert await _resolves_to(tenant_id) == {("web01.example.com", "vm:web01")}


@pytest.mark.asyncio
async def test_link_from_a_node_not_in_the_snapshot_is_ignored() -> None:
    tenant_id, targets = await _seed(["vc", "dns"])
    _FakeConnector.hints_by_target = {
        "vc": _systems(("vm", "web01")),
        "dns": TopologyHints(
            discovered_at=datetime.now(UTC),
            nodes=(NodeHint(kind="target", name="dns"),),
            links=(
                LinkHint(
                    from_kind="dns-record",
                    from_name="missing.example.com",
                    to_kinds=("vm",),
                    match_names=("web01",),
                ),
            ),
        ),
    }
    op = _operator(tenant_id)
    await refresh_target_topology(targets["vc"], op)
    result = await refresh_target_topology(targets["dns"], op)
    assert result.added_edges == 0
    assert await _resolves_to(tenant_id) == set()
