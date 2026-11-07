# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Topology map entries for :class:`HetznerRobotConnector`.

:meth:`HetznerRobotConnector.discover_topology` calls
:func:`discover_robot_topology`. It reads the account with GET calls only
and turns the answers into a :class:`TopologyHints` snapshot:

* one ``target`` node for the Robot account (the registered target);
* one ``host`` node per dedicated server, with its main addresses
  (``server_ip``, ``server_ipv6_net``, ``ip``) in ``properties``;
* one ``network`` node per subnet, named by its CIDR (``<ip>/<mask>``);
* one ``network`` node per vSwitch, named ``vswitch-<id>`` (the id is
  stable; the vSwitch name is free text and goes in ``properties``).

Edges:

* server, subnet and vSwitch ``belongs-to`` the account;
* a subnet ``routes-through`` the server it is routed to;
* a member server ``routes-through`` its vSwitch;
* a subnet on a vSwitch ``routes-through`` that vSwitch.

The answer shapes follow the Robot Webservice reference
(https://robot.hetzner.com/doc/webservice/en.html): ``GET /server`` and
``GET /subnet`` wrap each entry under a ``server`` / ``subnet`` key;
``GET /vswitch`` lists the vSwitches without their members, so each
vSwitch is read once more with ``GET /vswitch/{id}``.

A failed or odd read raises. The refresh then rolls back, so a failure
never looks like "the account has no servers". The one exception is the
documented "nothing there" answer: ``GET /server`` answers 404
``SERVER_NOT_FOUND`` and ``GET /subnet`` answers 404 ``NOT_FOUND`` when
the account has none.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from meho_backplane.connectors.schemas import EdgeHint, EdgeKind, NodeHint, NodeKind, TopologyHints

__all__ = [
    "build_topology_hints",
    "discover_robot_topology",
    "server_node_names",
    "subnet_node_name",
    "vswitch_node_name",
]

_log = structlog.get_logger(__name__)

RobotGet = Callable[[str], Awaitable[Any]]
"""Reads one Robot path and returns the parsed JSON body."""

_SERVER_KEYS = (
    "server_ip",
    "server_ipv6_net",
    "ip",
    "product",
    "dc",
    "status",
    "cancelled",
)
_SUBNET_KEYS = ("gateway", "server_ip", "server_number", "failover", "locked")


def _unwrap_list(payload: Any, key: str, path: str) -> list[dict[str, Any]]:
    """Return the entries of a Robot list answer, unwrapping ``{key: {...}}``.

    Raises :exc:`RuntimeError` when the answer is not a list of objects, so
    an odd body never reads as an empty account.
    """
    if not isinstance(payload, list):
        raise RuntimeError(
            f"Hetzner Robot GET {path} did not return a list "
            f"(got {type(payload).__name__}); the topology refresh stopped."
        )
    entries: list[dict[str, Any]] = []
    for entry in payload:
        inner = entry.get(key, entry) if isinstance(entry, Mapping) else entry
        if not isinstance(inner, Mapping):
            raise RuntimeError(
                f"Hetzner Robot GET {path} returned an entry that is not an object "
                f"(got {type(inner).__name__}); the topology refresh stopped."
            )
        entries.append(dict(inner))
    return entries


def _is_documented_empty(exc: httpx.HTTPStatusError, code: str) -> bool:
    """Return ``True`` when *exc* is Robot's documented 404 "none found" answer."""
    if exc.response.status_code != 404:
        return False
    try:
        body = exc.response.json()
    except ValueError:
        return False
    error = body.get("error") if isinstance(body, Mapping) else None
    return isinstance(error, Mapping) and error.get("code") == code


async def _read_list(
    get_json: RobotGet, path: str, key: str, empty_code: str | None
) -> list[dict[str, Any]]:
    try:
        payload = await get_json(path)
    except httpx.HTTPStatusError as exc:
        if empty_code is not None and _is_documented_empty(exc, empty_code):
            return []
        raise
    return _unwrap_list(payload, key, path)


def _int_or_none(value: Any) -> int | None:
    # bool is an int subclass; a flag is never an id.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


async def discover_robot_topology(
    target_name: str,
    get_json: RobotGet,
    *,
    now: datetime | None = None,
) -> TopologyHints:
    """Read the account with GET calls only and build its :class:`TopologyHints`."""
    servers = await _read_list(get_json, "/server", "server", "SERVER_NOT_FOUND")
    subnets = await _read_list(get_json, "/subnet", "subnet", "NOT_FOUND")
    vswitch_list = await _read_list(get_json, "/vswitch", "vswitch", None)
    vswitches: list[dict[str, Any]] = []
    for entry in vswitch_list:
        vswitch_id = _int_or_none(entry.get("id"))
        if vswitch_id is None:
            raise RuntimeError(
                "Hetzner Robot GET /vswitch returned a vSwitch without a numeric id; "
                "the topology refresh stopped."
            )
        detail = await get_json(f"/vswitch/{vswitch_id}")
        if isinstance(detail, Mapping) and isinstance(detail.get("vswitch"), Mapping):
            detail = detail["vswitch"]
        if not isinstance(detail, Mapping):
            raise RuntimeError(
                f"Hetzner Robot GET /vswitch/{vswitch_id} did not return an object; "
                "the topology refresh stopped."
            )
        vswitches.append(dict(detail))
    return build_topology_hints(target_name, servers, subnets, vswitches, now=now)


def server_node_names(servers: list[dict[str, Any]]) -> dict[int, str]:
    """Map each server number to its node name.

    The name is ``server_name``, or ``server-<number>`` when it is empty.
    Robot does not keep server names unique, so a name that two servers
    share gets `` #<number>`` on both.
    """
    base: dict[int, str] = {}
    for server in servers:
        number = _int_or_none(server.get("server_number"))
        if number is None:
            continue
        raw_name = server.get("server_name")
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        base[number] = name or f"server-{number}"
    counts = Counter(base.values())
    return {
        number: name if counts[name] == 1 else f"{name} #{number}" for number, name in base.items()
    }


def subnet_node_name(subnet: Mapping[str, Any]) -> str | None:
    """Return ``<ip>/<mask>`` for a subnet entry, or ``None`` when a part is missing."""
    ip = subnet.get("ip")
    mask = subnet.get("mask")
    if not isinstance(ip, str) or not ip or mask is None or isinstance(mask, bool):
        return None
    return f"{ip}/{mask}"


def vswitch_node_name(vswitch_id: int) -> str:
    """Return the node name of a vSwitch: ``vswitch-<id>``."""
    return f"vswitch-{vswitch_id}"


class _Snapshot:
    """Collects nodes and edges once each, in insertion order."""

    def __init__(self) -> None:
        self.nodes: dict[tuple[str, str], NodeHint] = {}
        self.edges: dict[tuple[str, str, str, str, str], EdgeHint] = {}

    def node(self, kind: NodeKind, name: str, properties: Mapping[str, Any]) -> None:
        self.nodes.setdefault((kind, name), NodeHint(kind=kind, name=name, properties=properties))

    def edge(
        self,
        from_kind: NodeKind,
        from_name: str,
        kind: EdgeKind,
        to_kind: NodeKind,
        to_name: str,
        properties: Mapping[str, Any] | None = None,
    ) -> None:
        key = (from_kind, from_name, kind, to_kind, to_name)
        self.edges.setdefault(
            key,
            EdgeHint(
                from_kind=from_kind,
                from_name=from_name,
                to_kind=to_kind,
                to_name=to_name,
                kind=kind,
                properties=properties or {},
            ),
        )


def _subnet_ip_version(ip: str) -> int:
    return 6 if ":" in ip else 4


def _add_servers(
    snap: _Snapshot, target_name: str, servers: list[dict[str, Any]]
) -> dict[int, str]:
    names = server_node_names(servers)
    for server in servers:
        number = _int_or_none(server.get("server_number"))
        if number is None:
            _log.warning("hetzner_robot_topology_server_skipped", target=target_name)
            continue
        properties: dict[str, Any] = {"server_number": number}
        properties.update({k: server[k] for k in _SERVER_KEYS if k in server})
        own_subnets = [
            name
            for s in server.get("subnet") or []
            if isinstance(s, Mapping) and (name := subnet_node_name(s))
        ]
        if own_subnets:
            properties["subnets"] = own_subnets
        snap.node("host", names[number], properties)
        snap.edge("host", names[number], "belongs-to", "target", target_name)
    return names


def _add_subnets(
    snap: _Snapshot, target_name: str, subnets: list[dict[str, Any]], names: Mapping[int, str]
) -> set[str]:
    subnet_names: set[str] = set()
    for subnet in subnets:
        name = subnet_node_name(subnet)
        if name is None:
            _log.warning("hetzner_robot_topology_subnet_skipped", target=target_name)
            continue
        subnet_names.add(name)
        properties = {k: subnet[k] for k in _SUBNET_KEYS if k in subnet}
        properties["ip_version"] = _subnet_ip_version(name)
        snap.node("network", name, properties)
        snap.edge("network", name, "belongs-to", "target", target_name)
        server_number = _int_or_none(subnet.get("server_number"))
        if server_number is not None and server_number in names:
            snap.edge("network", name, "routes-through", "host", names[server_number])
    return subnet_names


def _add_vswitch_links(
    snap: _Snapshot,
    vs_name: str,
    vswitch: Mapping[str, Any],
    names: Mapping[int, str],
    subnet_names: set[str],
) -> None:
    for member in vswitch.get("server") or []:
        if not isinstance(member, Mapping):
            continue
        number = _int_or_none(member.get("server_number"))
        if number is not None and number in names:
            snap.edge(
                "host",
                names[number],
                "routes-through",
                "network",
                vs_name,
                {"status": member.get("status")},
            )
    for vs_subnet in vswitch.get("subnet") or []:
        if not isinstance(vs_subnet, Mapping):
            continue
        sub_name = subnet_node_name(vs_subnet)
        if sub_name is not None and sub_name in subnet_names:
            snap.edge("network", sub_name, "routes-through", "network", vs_name)


def build_topology_hints(
    target_name: str,
    servers: list[dict[str, Any]],
    subnets: list[dict[str, Any]],
    vswitches: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> TopologyHints:
    """Build the snapshot from already-read Robot answers (unwrapped entries).

    An entry without the field that names it (``server_number``, subnet
    ``ip`` / ``mask``, vSwitch ``id``) is left out and logged.
    """
    snap = _Snapshot()
    snap.node("target", target_name, {"vendor": "hetzner", "product": "robot-webservice"})
    names = _add_servers(snap, target_name, servers)
    subnet_names = _add_subnets(snap, target_name, subnets, names)
    for vswitch in vswitches:
        vswitch_id = _int_or_none(vswitch.get("id"))
        if vswitch_id is None:
            _log.warning("hetzner_robot_topology_vswitch_skipped", target=target_name)
            continue
        vs_name = vswitch_node_name(vswitch_id)
        snap.node(
            "network",
            vs_name,
            {
                "vswitch_id": vswitch_id,
                "vswitch_name": vswitch.get("name"),
                "vlan": vswitch.get("vlan"),
                "cancelled": vswitch.get("cancelled"),
            },
        )
        snap.edge("network", vs_name, "belongs-to", "target", target_name)
        _add_vswitch_links(snap, vs_name, vswitch, names, subnet_names)

    return TopologyHints(
        nodes=tuple(snap.nodes.values()),
        edges=tuple(snap.edges.values()),
        discovered_at=datetime.now(UTC) if now is None else now,
    )
