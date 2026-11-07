# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""pfSense subnets and DHCP leases on the topology map (#4131).

* :func:`build_topology_hints` turns interface rows and lease rows into
  map entries: one ``network`` node per IPv4 subnet, one ``host`` node
  per active lease, with ``source`` on every entry.
* Leases that are not active (expired, free, backup) are left out.
* :meth:`PfSenseConnector.discover_topology` runs the two reads and
  raises when one of them fails, so the map never reads "nothing there".
  A missing lease file (DHCP server off, or Kea) means no leases.
* Through the refresh service: a refresh adds the entries, and a lease
  that expired later is marked as no longer seen.

Addresses are from the documentation ranges (RFC 5737); MAC addresses
and names are made up.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest
from sqlalchemy import select

import meho_backplane.connectors.pfsense  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.pfsense import PfSenseConnector
from meho_backplane.connectors.pfsense._topology import (
    INTERFACE_SOURCE,
    LEASE_SOURCE,
    PfSenseTopologyReadError,
    build_topology_hints,
    rows_or_raise,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphEdge, GraphNode, Target, Tenant
from meho_backplane.operations._handler_resolve import reset_connector_instance_cache
from meho_backplane.settings import get_settings
from meho_backplane.topology.refresh import _has_populator, refresh_target_topology


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_connector_instance_cache()
    yield
    get_settings.cache_clear()
    reset_connector_instance_cache()


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str


_TARGET = _StubTarget(
    name="fw-test",
    host="fw.test.invalid",
    port=22,
    secret_ref="meho/testing/pfsense/fw-test",
)

_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def _iface(name: str, *inet: str, status: str | None = "active") -> dict[str, Any]:
    return {
        "name": name,
        "metric": 0,
        "mtu": 1500,
        "inet": list(inet),
        "inet6": [],
        "ether": "02:00:00:00:00:01",
        "status": status,
        "media": None,
    }


def _lease(ip: str, state: str | None = "active", **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ip": ip,
        "mac": "02:00:00:aa:bb:cc",
        "hostname": "laptop",
        "starts": "2026/08/12 10:00:00",
        "ends": "2026/08/12 22:00:00",
        "binding_state": state,
    }
    row.update(extra)
    return row


def _node(hints: Any, kind: str, name: str) -> Any:
    matches = [n for n in hints.nodes if n.kind == kind and n.name == name]
    assert len(matches) == 1, f"expected one {kind} {name}, got {matches}"
    return matches[0]


def _edge_keys(hints: Any) -> set[tuple[str, str, str, str, str]]:
    return {(e.from_kind, e.from_name, e.kind, e.to_kind, e.to_name) for e in hints.edges}


# ---------------------------------------------------------------------------
# build_topology_hints
# ---------------------------------------------------------------------------


def test_subnets_and_active_leases_become_nodes_and_edges() -> None:
    hints = build_topology_hints(
        "fw-test",
        [_iface("em0", "198.51.100.2/24"), _iface("em1", "192.0.2.1/24")],
        [_lease("192.0.2.50")],
        now=_NOW,
    )

    assert hints.discovered_at == _NOW
    assert _node(hints, "target", "fw-test").properties["source"] == INTERFACE_SOURCE

    lan = _node(hints, "network", "192.0.2.0/24")
    assert lan.properties["source"] == INTERFACE_SOURCE
    assert lan.properties["interfaces"] == [
        {"name": "em1", "address": "192.0.2.1", "ether": "02:00:00:00:00:01", "status": "active"}
    ]
    _node(hints, "network", "198.51.100.0/24")

    host = _node(hints, "host", "192.0.2.50")
    assert dict(host.properties) == {
        "source": LEASE_SOURCE,
        "address": "192.0.2.50",
        "mac": "02:00:00:aa:bb:cc",
        "hostname": "laptop",
        "lease_starts": "2026-08-12T10:00:00+00:00",
        "lease_ends": "2026-08-12T22:00:00+00:00",
        "binding_state": "active",
    }

    assert _edge_keys(hints) == {
        ("network", "192.0.2.0/24", "routes-through", "target", "fw-test"),
        ("network", "198.51.100.0/24", "routes-through", "target", "fw-test"),
        ("host", "192.0.2.50", "belongs-to", "network", "192.0.2.0/24"),
    }
    assert {e.properties["source"] for e in hints.edges} == {INTERFACE_SOURCE, LEASE_SOURCE}


@pytest.mark.parametrize("state", ["expired", "free", "backup", "abandoned", None])
def test_lease_that_is_not_active_is_left_out(state: str | None) -> None:
    hints = build_topology_hints(
        "fw-test", [_iface("em1", "192.0.2.1/24")], [_lease("192.0.2.60", state)], now=_NOW
    )
    assert [n for n in hints.nodes if n.kind == "host"] == []
    assert all(e.from_kind != "host" for e in hints.edges)


def test_lease_outside_every_subnet_routes_through_the_firewall() -> None:
    hints = build_topology_hints(
        "fw-test", [_iface("em1", "192.0.2.1/24")], [_lease("203.0.113.9")], now=_NOW
    )
    assert ("host", "203.0.113.9", "routes-through", "target", "fw-test") in _edge_keys(hints)


def test_lease_matches_the_most_specific_subnet() -> None:
    hints = build_topology_hints(
        "fw-test",
        [_iface("em1", "192.0.2.1/24"), _iface("em2", "192.0.2.129/25")],
        [_lease("192.0.2.200")],
        now=_NOW,
    )
    assert ("host", "192.0.2.200", "belongs-to", "network", "192.0.2.128/25") in _edge_keys(hints)


def test_loopback_single_address_bad_and_missing_addresses_are_skipped() -> None:
    hints = build_topology_hints(
        "fw-test",
        [
            _iface("lo0", "127.0.0.1/8"),
            _iface("em1", "192.0.2.1/24", "192.0.2.10/32", "192.0.2.11"),
            _iface("pflog0"),
            _iface("em9", "not-an-address", "300.1.1.1/24"),
            {"name": "odd", "inet": None},
        ],
        [],
        now=_NOW,
    )
    assert [n.name for n in hints.nodes if n.kind == "network"] == ["192.0.2.0/24"]


def test_one_subnet_on_two_interfaces_is_one_node() -> None:
    hints = build_topology_hints(
        "fw-test",
        [_iface("em1", "192.0.2.1/24"), _iface("em2", "192.0.2.2/24", status="no carrier")],
        [],
        now=_NOW,
    )
    lan = _node(hints, "network", "192.0.2.0/24")
    assert [i["name"] for i in lan.properties["interfaces"]] == ["em1", "em2"]
    assert len(hints.edges) == 1


def test_empty_reads_give_only_the_firewall_node() -> None:
    hints = build_topology_hints("fw-test", [], [], now=_NOW)
    assert [(n.kind, n.name) for n in hints.nodes] == [("target", "fw-test")]
    assert hints.edges == ()


def test_lease_edge_inputs() -> None:
    hints = build_topology_hints(
        "fw-test",
        [_iface("em1", "192.0.2.1/24")],
        [
            _lease("192.0.2.51", hostname=None, mac=None, ends="never", starts=None),
            _lease("192.0.2.52", ends="garbage"),
            _lease("", state="active"),
            {"binding_state": "active"},
        ],
        now=_NOW,
    )
    hosts = sorted((n for n in hints.nodes if n.kind == "host"), key=lambda n: n.name)
    assert [h.name for h in hosts] == ["192.0.2.51", "192.0.2.52"]
    assert hosts[0].properties["hostname"] is None
    assert hosts[0].properties["mac"] is None
    assert hosts[0].properties["lease_starts"] is None
    assert hosts[0].properties["lease_ends"] == "never"
    assert hosts[1].properties["lease_ends"] is None


def test_hints_are_json_serialisable() -> None:
    hints = build_topology_hints(
        "fw-test", [_iface("em1", "192.0.2.1/24")], [_lease("192.0.2.50")], now=_NOW
    )
    json.dumps(hints.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# rows_or_raise
# ---------------------------------------------------------------------------


def test_rows_or_raise_returns_rows() -> None:
    assert rows_or_raise("op", "fw", {"rows": [{"a": 1}], "total": 1}) == [{"a": 1}]
    assert rows_or_raise("op", "fw", {"rows": [], "total": 0}) == []


@pytest.mark.parametrize(
    "answer",
    [
        {"rows": [], "total": 0, "error": "ifconfig exit 1"},
        {"total": 0},
        {"rows": None},
    ],
)
def test_rows_or_raise_raises_on_a_failed_read(answer: dict[str, Any]) -> None:
    with pytest.raises(PfSenseTopologyReadError, match="topology map was not changed"):
        rows_or_raise("pfsense.interface.list", "fw-test", answer)


# ---------------------------------------------------------------------------
# PfSenseConnector.discover_topology
# ---------------------------------------------------------------------------

_IFCONFIG = (
    "em1: flags=8843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST> metric 0 mtu 1500\n"
    "\tether 02:00:00:00:00:01\n"
    "\tinet 192.0.2.1 netmask 0xffffff00 broadcast 192.0.2.255\n"
    "\tstatus: active\n"
    "lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> metric 0 mtu 16384\n"
    "\tinet 127.0.0.1 netmask 0xff000000\n"
)


def _ts(moment: datetime) -> str:
    return f"{moment.weekday()} {moment:%Y/%m/%d %H:%M:%S}"


def _lease_block(ip: str, starts: datetime, ends: datetime, state: str = "active") -> str:
    return (
        f"lease {ip} {{\n"
        f"  starts {_ts(starts)};\n"
        f"  ends {_ts(ends)};\n"
        f"  binding state {state};\n"
        "  hardware ethernet 02:00:00:aa:bb:cc;\n"
        '  client-hostname "laptop";\n'
        "}\n"
    )


def _proc(stdout: str = "", exit_status: int = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.exit_status = exit_status
    return proc


def _ssh_answers(ifconfig: Any, leases: Any, lease_file_check: Any = None) -> Any:
    """Answer the SSH commands. ``lease_file_check`` answers ``test -e``
    (default: the lease file exists)."""
    check = _proc() if lease_file_check is None else lease_file_check

    async def _run(target: Any, command: str, operator: Any = None) -> Any:
        if command == "ifconfig -a":
            answer = ifconfig
        elif command == "cat /var/dhcpd/var/db/dhcpd.leases":
            answer = leases
        elif command == "test -e /var/dhcpd/var/db/dhcpd.leases":
            answer = check
        else:  # pragma: no cover - a wrong command is a test failure
            raise AssertionError(f"unexpected command {command!r}")
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return _run


def _active_and_expired_leases() -> str:
    now = datetime.now(UTC)
    return _lease_block(
        "192.0.2.50", now - timedelta(hours=1), now + timedelta(hours=11)
    ) + _lease_block("192.0.2.60", now - timedelta(days=2), now - timedelta(days=1))


def test_pfsense_connector_has_a_topology_populator() -> None:
    assert _has_populator(PfSenseConnector)


@pytest.mark.asyncio
async def test_discover_topology_runs_both_reads_and_forwards_the_operator() -> None:
    connector = PfSenseConnector()
    operator = MagicMock(name="operator")
    run = AsyncMock(side_effect=_ssh_answers(_proc(_IFCONFIG), _proc(_active_and_expired_leases())))
    with patch.object(connector, "_run_command", run):
        hints = await connector.discover_topology(_TARGET, operator=operator)

    assert {c.args[1] for c in run.await_args_list} == {
        "ifconfig -a",
        "cat /var/dhcpd/var/db/dhcpd.leases",
    }
    assert all(c.kwargs["operator"] is operator for c in run.await_args_list)
    assert {(n.kind, n.name) for n in hints.nodes} == {
        ("target", "fw-test"),
        ("network", "192.0.2.0/24"),
        ("host", "192.0.2.50"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["ifconfig", "leases"])
async def test_discover_topology_raises_when_a_read_fails(failing: str) -> None:
    connector = PfSenseConnector()
    good_if, good_leases = _proc(_IFCONFIG), _proc(_active_and_expired_leases())
    answers = (
        (_proc("", exit_status=1), good_leases)
        if failing == "ifconfig"
        else (good_if, _proc("", exit_status=1))
    )
    with (
        patch.object(connector, "_run_command", AsyncMock(side_effect=_ssh_answers(*answers))),
        pytest.raises(PfSenseTopologyReadError, match="exit 1"),
    ):
        await connector.discover_topology(_TARGET)


@pytest.mark.asyncio
async def test_discover_topology_without_a_lease_file_still_maps_the_subnets() -> None:
    """DHCP server off or Kea: no lease file, so no hosts, but the subnets land."""
    connector = PfSenseConnector()
    operator = MagicMock(name="operator")
    run = AsyncMock(
        side_effect=_ssh_answers(
            _proc(_IFCONFIG), _proc("", exit_status=1), lease_file_check=_proc(exit_status=1)
        )
    )
    with patch.object(connector, "_run_command", run):
        hints = await connector.discover_topology(_TARGET, operator=operator)

    assert {(n.kind, n.name) for n in hints.nodes} == {
        ("target", "fw-test"),
        ("network", "192.0.2.0/24"),
    }
    assert [c.args[1] for c in run.await_args_list][-1] == (
        "test -e /var/dhcpd/var/db/dhcpd.leases"
    )
    assert all(c.kwargs["operator"] is operator for c in run.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("check_exit", [0, 2, 255])
async def test_discover_topology_raises_when_the_lease_file_is_there_but_unread(
    check_exit: int,
) -> None:
    """The file exists (or the check itself failed): the failed read still raises."""
    connector = PfSenseConnector()
    answers = _ssh_answers(
        _proc(_IFCONFIG), _proc("", exit_status=1), lease_file_check=_proc(exit_status=check_exit)
    )
    with (
        patch.object(connector, "_run_command", AsyncMock(side_effect=answers)),
        pytest.raises(PfSenseTopologyReadError, match="exit 1"),
    ):
        await connector.discover_topology(_TARGET)


@pytest.mark.asyncio
async def test_discover_topology_lets_an_ssh_error_through() -> None:
    connector = PfSenseConnector()
    answers = _ssh_answers(asyncssh.DisconnectError(11, "gone"), _proc(""))
    with (
        patch.object(connector, "_run_command", AsyncMock(side_effect=answers)),
        pytest.raises(asyncssh.DisconnectError),
    ):
        await connector.discover_topology(_TARGET)


# ---------------------------------------------------------------------------
# Through the refresh service
# ---------------------------------------------------------------------------


async def _seed_pfsense_target() -> tuple[uuid.UUID, Target]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id: uuid.UUID | None = await session.scalar(
            select(Tenant.id).where(Tenant.slug == "pfsense-topo-test")
        )
        if tenant_id is None:
            tenant_id = uuid.uuid4()
            session.add(Tenant(id=tenant_id, slug="pfsense-topo-test", name="Tenant"))
        target = Target(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            name="fw-test",
            aliases=[],
            product="pfsense",
            host="fw.test.invalid",
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


async def _live_nodes(tenant_id: uuid.UUID) -> dict[tuple[str, str], GraphNode]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            await session.scalars(select(GraphNode).where(GraphNode.tenant_id == tenant_id))
        ).all()
    return {(r.kind, r.name): r for r in rows}


async def _refresh_with(target: Target, operator: Operator, leases: str) -> Any:
    run = AsyncMock(side_effect=_ssh_answers(_proc(_IFCONFIG), _proc(leases)))
    with (
        patch("meho_backplane.topology.refresh.resolve_connector", return_value=PfSenseConnector),
        patch.object(PfSenseConnector, "_run_command", run),
    ):
        return await refresh_target_topology(target, operator)


@pytest.mark.asyncio
async def test_refresh_adds_entries_and_an_expired_lease_is_no_longer_live() -> None:
    tenant_id, target = await _seed_pfsense_target()
    operator = _operator(tenant_id)
    now = datetime.now(UTC)
    first = _lease_block("192.0.2.50", now - timedelta(hours=1), now + timedelta(hours=11))
    first += _lease_block("192.0.2.70", now - timedelta(hours=1), now + timedelta(hours=11))

    result = await _refresh_with(target, operator, first)
    assert result.no_populator_for_product is None
    assert result.added_nodes == 4  # firewall, subnet, two hosts
    assert result.added_edges == 3

    nodes = await _live_nodes(tenant_id)
    host = nodes[("host", "192.0.2.50")]
    assert host.last_seen is not None
    assert host.discovered_by == "pfsense"
    assert host.properties["source"] == LEASE_SOURCE
    assert host.properties["mac"] == "02:00:00:aa:bb:cc"
    assert nodes[("network", "192.0.2.0/24")].properties["source"] == INTERFACE_SOURCE

    # The second lease file renews .50 and lets .70 expire.
    second = first + _lease_block("192.0.2.70", now - timedelta(days=2), now - timedelta(minutes=5))
    result = await _refresh_with(target, operator, second)
    assert result.removed_nodes == 1
    assert result.removed_edges == 1

    nodes = await _live_nodes(tenant_id)
    assert nodes[("host", "192.0.2.70")].last_seen is None
    assert nodes[("host", "192.0.2.50")].last_seen is not None


@pytest.mark.asyncio
async def test_refresh_with_a_failed_lease_read_changes_nothing() -> None:
    tenant_id, target = await _seed_pfsense_target()
    operator = _operator(tenant_id)
    now = datetime.now(UTC)
    leases = _lease_block("192.0.2.50", now - timedelta(hours=1), now + timedelta(hours=11))
    await _refresh_with(target, operator, leases)
    before = {k: n.last_seen for k, n in (await _live_nodes(tenant_id)).items()}

    run = AsyncMock(side_effect=_ssh_answers(_proc(_IFCONFIG), _proc("", exit_status=1)))
    with (
        patch("meho_backplane.topology.refresh.resolve_connector", return_value=PfSenseConnector),
        patch.object(PfSenseConnector, "_run_command", run),
        pytest.raises(PfSenseTopologyReadError),
    ):
        await refresh_target_topology(target, operator)

    after = {k: n.last_seen for k, n in (await _live_nodes(tenant_id)).items()}
    assert after == before
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        edges = (
            await session.scalars(select(GraphEdge).where(GraphEdge.tenant_id == tenant_id))
        ).all()
    assert all(e.last_seen is not None for e in edges)
