# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Hetzner Robot topology map entries (#4132).

``HetznerRobotConnector.discover_topology`` reads servers, subnets and
vSwitches with GET calls only and returns them as topology hints. The
answer shapes follow the Robot Webservice reference. All addresses are
from the documentation ranges (RFC 5737, RFC 3849).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.hetzner_robot import HetznerRobotConnector
from meho_backplane.connectors.hetzner_robot._topology import (
    build_topology_hints,
    server_node_names,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel, TopologyHints
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphEdge, GraphNode, Target, Tenant
from meho_backplane.operations._handler_resolve import reset_connector_instance_cache
from meho_backplane.settings import get_settings
from meho_backplane.topology.refresh import refresh_target_topology

_BASE = "https://robot-ws.example.test"
_DEFAULT_TENANT = UUID(int=0)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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
    register_connector_v2(
        product=HetznerRobotConnector.product,
        version="",
        impl_id="",
        cls=HetznerRobotConnector,
    )
    yield
    clear_registry()
    reset_connector_instance_cache()


@dataclass
class _StubTarget:
    name: str = "robot-account"
    host: str = "robot-ws.example.test"
    port: int | None = 443
    secret_ref: str = "hetzner/robot-account"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid.uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _operator(tenant_id: UUID = _DEFAULT_TENANT) -> Operator:
    return Operator(
        sub="op-topology",
        name=None,
        email=None,
        raw_jwt="op.topology.jwt",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


class _RecordingLoader:
    def __init__(self) -> None:
        self.operators: list[Operator] = []

    async def __call__(self, _target: Any, operator: Operator) -> dict[str, str]:
        self.operators.append(operator)
        return {"username": "made-up-user", "password": "made-up-password"}


# Vendor answer shapes (Robot Webservice reference, GET /server, /subnet,
# /vswitch, /vswitch/{id}).
_SERVERS = [
    {
        "server": {
            "server_ip": "192.0.2.10",
            "server_ipv6_net": "2001:db8:10::",
            "server_number": 321,
            "server_name": "web-1",
            "product": "DS 3000",
            "dc": "DC-A",
            "traffic": "5 TB",
            "status": "ready",
            "cancelled": False,
            "paid_until": "2030-01-01",
            "ip": ["192.0.2.10"],
            "subnet": [{"ip": "2001:db8:10::", "mask": "64"}],
        }
    },
    {
        "server": {
            "server_ip": "192.0.2.11",
            "server_ipv6_net": "2001:db8:11::",
            "server_number": 421,
            "server_name": "",
            "product": "X5",
            "dc": "DC-B",
            "status": "ready",
            "cancelled": False,
            "ip": ["192.0.2.11"],
            "subnet": None,
        }
    },
]
_SUBNETS = [
    {
        "subnet": {
            "ip": "198.51.100.0",
            "mask": 29,
            "gateway": "198.51.100.1",
            "server_ip": "192.0.2.10",
            "server_number": 321,
            "failover": False,
            "locked": False,
            "traffic_warnings": False,
        }
    },
    {
        "subnet": {
            "ip": "203.0.113.48",
            "mask": 29,
            "gateway": "203.0.113.49",
            "server_ip": None,
            "server_number": 999,
            "failover": False,
            "locked": False,
        }
    },
]
_VSWITCH_LIST = [{"id": 4321, "name": "backend lan", "vlan": 4000, "cancelled": False}]
_VSWITCH_DETAIL = {
    "id": 4321,
    "name": "backend lan",
    "vlan": 4000,
    "cancelled": False,
    "server": [
        {"server_ip": "192.0.2.10", "server_number": 321, "status": "ready"},
        {"server_ip": "192.0.2.11", "server_number": 421, "status": "in process"},
        {"server_ip": "192.0.2.99", "server_number": 777, "status": "ready"},
    ],
    "subnet": [{"ip": "203.0.113.48", "mask": 29, "gateway": "203.0.113.49"}],
    "cloud_network": [],
}


def _mock_account(mock: respx.MockRouter) -> None:
    mock.get("/server").respond(200, json=_SERVERS)
    mock.get("/subnet").respond(200, json=_SUBNETS)
    mock.get("/vswitch").respond(200, json=_VSWITCH_LIST)
    mock.get("/vswitch/4321").respond(200, json=_VSWITCH_DETAIL)


def _edges(hints: TopologyHints) -> set[tuple[str, str, str, str, str]]:
    return {(e.from_kind, e.from_name, e.kind, e.to_kind, e.to_name) for e in hints.edges}


# ---------------------------------------------------------------------------
# discover_topology against the mocked Robot API
# ---------------------------------------------------------------------------


async def test_discover_topology_adds_servers_subnets_and_vswitch_links() -> None:
    loader = _RecordingLoader()
    connector = HetznerRobotConnector(credentials_loader=loader)
    target = _StubTarget()
    operator = _operator()

    async with respx.mock(base_url=_BASE, assert_all_called=True) as mock:
        _mock_account(mock)
        hints = await connector.discover_topology(target, operator=operator)

    nodes = {(n.kind, n.name): dict(n.properties) for n in hints.nodes}
    assert set(nodes) == {
        ("target", "robot-account"),
        ("host", "web-1"),
        ("host", "server-421"),
        ("network", "198.51.100.0/29"),
        ("network", "203.0.113.48/29"),
        ("network", "vswitch-4321"),
    }
    web = nodes[("host", "web-1")]
    assert web["server_number"] == 321
    assert web["server_ip"] == "192.0.2.10"
    assert web["server_ipv6_net"] == "2001:db8:10::"
    assert web["ip"] == ["192.0.2.10"]
    assert web["subnets"] == ["2001:db8:10::/64"]
    assert "paid_until" not in web
    assert nodes[("network", "198.51.100.0/29")]["gateway"] == "198.51.100.1"
    assert nodes[("network", "198.51.100.0/29")]["ip_version"] == 4
    assert nodes[("network", "vswitch-4321")] == {
        "vswitch_id": 4321,
        "vswitch_name": "backend lan",
        "vlan": 4000,
        "cancelled": False,
    }

    assert _edges(hints) == {
        ("host", "web-1", "belongs-to", "target", "robot-account"),
        ("host", "server-421", "belongs-to", "target", "robot-account"),
        ("network", "198.51.100.0/29", "belongs-to", "target", "robot-account"),
        ("network", "198.51.100.0/29", "routes-through", "host", "web-1"),
        ("network", "203.0.113.48/29", "belongs-to", "target", "robot-account"),
        ("network", "vswitch-4321", "belongs-to", "target", "robot-account"),
        ("host", "web-1", "routes-through", "network", "vswitch-4321"),
        ("host", "server-421", "routes-through", "network", "vswitch-4321"),
        ("network", "203.0.113.48/29", "routes-through", "network", "vswitch-4321"),
    }
    member = next(
        e for e in hints.edges if e.from_name == "server-421" and e.kind == "routes-through"
    )
    assert dict(member.properties) == {"status": "in process"}
    # The acting operator's login reads the secret, not the system operator.
    assert loader.operators and all(op is operator for op in loader.operators)


async def test_discover_topology_makes_no_write_call() -> None:
    connector = HetznerRobotConnector(credentials_loader=_RecordingLoader())

    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_account(mock)
        await connector.discover_topology(_StubTarget(), operator=_operator())
        # respx clears its call list when the mock context exits.
        methods = {call.request.method for call in mock.calls}
        paths = [call.request.url.path for call in mock.calls]

    assert methods == {"GET"}
    assert paths == ["/server", "/subnet", "/vswitch", "/vswitch/4321"]


async def test_discover_topology_empty_account_uses_documented_404() -> None:
    connector = HetznerRobotConnector(credentials_loader=_RecordingLoader())

    async with respx.mock(base_url=_BASE) as mock:
        mock.get("/server").respond(
            404, json={"error": {"status": 404, "code": "SERVER_NOT_FOUND", "message": "x"}}
        )
        mock.get("/subnet").respond(
            404, json={"error": {"status": 404, "code": "NOT_FOUND", "message": "x"}}
        )
        mock.get("/vswitch").respond(200, json=[])
        hints = await connector.discover_topology(_StubTarget(), operator=_operator())

    assert [(n.kind, n.name) for n in hints.nodes] == [("target", "robot-account")]
    assert hints.edges == ()


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (404, {"error": {"status": 404, "code": "SOMETHING_ELSE", "message": "x"}}),
        (404, None),
        (500, {"error": {"status": 500, "code": "INTERNAL_ERROR", "message": "x"}}),
    ],
)
async def test_discover_topology_failed_server_read_raises(status: int, body: Any) -> None:
    connector = HetznerRobotConnector(credentials_loader=_RecordingLoader())

    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        if body is None:
            mock.get("/server").respond(status, text="not json")
        else:
            mock.get("/server").respond(status, json=body)
        with pytest.raises(httpx.HTTPStatusError):
            await connector.discover_topology(_StubTarget(), operator=_operator())


async def test_discover_topology_failed_vswitch_detail_raises() -> None:
    connector = HetznerRobotConnector(credentials_loader=_RecordingLoader())

    async with respx.mock(base_url=_BASE) as mock:
        mock.get("/server").respond(200, json=_SERVERS)
        mock.get("/subnet").respond(200, json=_SUBNETS)
        mock.get("/vswitch").respond(200, json=_VSWITCH_LIST)
        mock.get("/vswitch/4321").respond(503)
        with pytest.raises(httpx.HTTPStatusError):
            await connector.discover_topology(_StubTarget(), operator=_operator())


async def test_discover_topology_401_stops_after_one_request() -> None:
    connector = HetznerRobotConnector(credentials_loader=_RecordingLoader())

    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        route = mock.get("/server").respond(401)
        with pytest.raises(RuntimeError, match="auth_failed"):
            await connector.discover_topology(_StubTarget(), operator=_operator())
        call_count = len(mock.calls)

    assert route.call_count == 1
    assert call_count == 1


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/server", {"servers": []}),
        ("/server", ["not-an-object"]),
        ("/subnet", "text"),
        ("/vswitch", [{"name": "no id"}]),
    ],
)
async def test_discover_topology_odd_body_raises(path: str, body: Any) -> None:
    connector = HetznerRobotConnector(credentials_loader=_RecordingLoader())
    answers: dict[str, Any] = {"/server": _SERVERS, "/subnet": _SUBNETS, "/vswitch": []}
    answers[path] = body

    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        for p, b in answers.items():
            mock.get(p).respond(200, json=b)
        with pytest.raises(RuntimeError):
            await connector.discover_topology(_StubTarget(), operator=_operator())


async def test_discover_topology_without_operator_uses_system_operator() -> None:
    loader = _RecordingLoader()
    connector = HetznerRobotConnector(credentials_loader=loader)

    async with respx.mock(base_url=_BASE) as mock:
        _mock_account(mock)
        await connector.discover_topology(_StubTarget())

    assert loader.operators
    assert all(op.raw_jwt != "op.topology.jwt" for op in loader.operators)


# ---------------------------------------------------------------------------
# Pure builder edge cases
# ---------------------------------------------------------------------------


def test_server_node_names_disambiguates_shared_and_empty_names() -> None:
    servers = [
        {"server_number": 1, "server_name": "db"},
        {"server_number": 2, "server_name": "db"},
        {"server_number": 3, "server_name": None},
        {"server_number": 4, "server_name": "  "},
        {"server_number": "5", "server_name": "app"},
        {"server_name": "no-number"},
    ]
    assert server_node_names(servers) == {
        1: "db #1",
        2: "db #2",
        3: "server-3",
        4: "server-4",
        5: "app",
    }


def test_build_topology_hints_skips_entries_without_identity() -> None:
    hints = build_topology_hints(
        "acct",
        servers=[{"server_name": "no-number"}, {"server_number": True}],
        subnets=[{"ip": "198.51.100.0"}, {"mask": 29}, {"ip": "", "mask": 29}],
        vswitches=[{"name": "no id"}],
    )
    assert [(n.kind, n.name) for n in hints.nodes] == [("target", "acct")]
    assert hints.edges == ()


def test_build_topology_hints_tolerates_null_lists_and_odd_members() -> None:
    hints = build_topology_hints(
        "acct",
        servers=[{"server_number": 7, "server_name": "a", "subnet": None}],
        subnets=[{"ip": "2001:db8:7::", "mask": 64, "server_number": 7}],
        vswitches=[
            {"id": 9, "server": None, "subnet": ["odd", None]},
            {"id": 10, "server": ["odd"]},
        ],
    )
    nodes = {(n.kind, n.name): dict(n.properties) for n in hints.nodes}
    assert "subnets" not in nodes[("host", "a")]
    assert nodes[("network", "2001:db8:7::/64")]["ip_version"] == 6
    assert ("network", "vswitch-9") in nodes
    assert ("network", "vswitch-10") in nodes
    assert ("network", "2001:db8:7::/64", "routes-through", "host", "a") in _edges(hints)


def test_build_topology_hints_dedupes_repeated_entries() -> None:
    server = {"server_number": 7, "server_name": "a"}
    subnet = {"ip": "198.51.100.0", "mask": 29, "server_number": 7}
    hints = build_topology_hints("acct", [server, server], [subnet, subnet], [])
    names = [(n.kind, n.name) for n in hints.nodes]
    assert len(names) == len(set(names))
    keys = [(e.from_kind, e.from_name, e.kind, e.to_kind, e.to_name) for e in hints.edges]
    assert len(keys) == len(set(keys))


def test_hetzner_robot_overrides_discover_topology() -> None:
    assert HetznerRobotConnector.discover_topology is not Connector.discover_topology


# ---------------------------------------------------------------------------
# Refresh end to end: rows carry source and time
# ---------------------------------------------------------------------------


async def _seed_target() -> tuple[UUID, Target]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = uuid.uuid4()
        session.add(Tenant(id=tenant_id, slug=f"robot-{tenant_id.hex[:8]}", name="Robot tenant"))
        target = Target(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            name="robot-account",
            aliases=[],
            product="hetzner",
            host="robot-ws.example.test",
            port=443,
            secret_ref="hetzner/robot-account",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
    return tenant_id, target


async def test_refresh_writes_robot_nodes_with_source_and_time() -> None:
    tenant_id, target = await _seed_target()
    loader = _RecordingLoader()
    operator = _operator(tenant_id)

    with patch(
        "meho_backplane.connectors.hetzner_robot.connector.load_credentials_from_vault",
        new=loader,
    ):
        reset_connector_instance_cache()
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            _mock_account(mock)
            result = await refresh_target_topology(target, operator)

    assert result.no_populator_for_product is None
    assert result.added_nodes == 6
    assert result.added_edges == 9
    assert all(op is operator for op in loader.operators)

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
    robot_nodes = [n for n in nodes if n.target_id == target.id]
    assert {(n.kind, n.name) for n in robot_nodes} >= {
        ("host", "web-1"),
        ("network", "198.51.100.0/29"),
        ("network", "vswitch-4321"),
    }
    for node in robot_nodes:
        assert node.source == "auto"
        assert node.discovered_by == "hetzner"
        assert node.first_seen is not None
        assert node.last_seen is not None
    robot_ids = {n.id for n in robot_nodes}
    robot_edges = [e for e in edges if e.from_node_id in robot_ids]
    assert len(robot_edges) == 9
    for edge in robot_edges:
        assert edge.source == "auto"
        assert edge.discovered_by == "hetzner"
        assert edge.last_seen is not None
