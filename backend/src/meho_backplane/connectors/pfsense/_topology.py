# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Topology map entries for :class:`PfSenseConnector` (#4131).

:meth:`PfSenseConnector.discover_topology` reads two things the
connector already reads, and turns them into map entries:

* ``pfsense.interface.list`` (``ifconfig -a``): each IPv4 subnet the
  firewall has an address in becomes a ``network`` node named by the
  subnet (for example ``<subnet>/24``). The node lists the firewall
  interfaces on that subnet. A ``routes-through`` edge goes from the
  subnet to the firewall's own ``target`` node.
* ``pfsense.dhcp.leases`` (the ISC dhcpd lease file): each **active**
  lease becomes a ``host`` node named by its IP address, with the MAC
  address, the hostname and the lease times. A ``belongs-to`` edge goes
  from the host to the subnet that holds its address. A lease outside
  every known subnet gets a ``routes-through`` edge to the firewall
  instead.

A lease that is not active (expired, free, backup, abandoned) is left
out. The refresh service then marks the host node it added earlier as
no longer seen, so an expired lease is never shown as a live system.

Every node and edge carries ``source``: the operation whose answer it
came from. The time MEHO saw it is the row's ``last_seen``, which the
refresh service sets; the lease's own times are in ``lease_starts`` and
``lease_ends``.

A failed read raises :class:`PfSenseTopologyReadError`. It never returns
an empty map, because the refresh service would read an empty map as
"everything is gone" and mark every node as no longer seen. One
exception: when the lease file (:data:`LEASE_FILE`) does not exist, the
DHCP server is off or the firewall uses the Kea DHCP server. Then there
are no leases, and the subnets still go on the map.

Left out on purpose: loopback addresses, single-address (``/32``)
entries such as virtual IPs, interfaces without an IPv4 address, and
IPv6 (``ifconfig`` gives no IPv6 prefix length here, and the lease file
holds only DHCPv4 leases).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from meho_backplane.connectors.pfsense.ops_read import _lease_datetime
from meho_backplane.connectors.schemas import EdgeHint, NodeHint, TopologyHints

__all__ = [
    "INTERFACE_SOURCE",
    "LEASE_FILE",
    "LEASE_SOURCE",
    "PfSenseTopologyReadError",
    "build_topology_hints",
    "rows_or_raise",
]

#: ``source`` value on entries that come from the interface list.
INTERFACE_SOURCE = "pfsense.interface.list"
#: ``source`` value on entries that come from the DHCP lease table.
LEASE_SOURCE = "pfsense.dhcp.leases"
#: The ISC dhcpd lease file that ``pfsense.dhcp.leases`` reads.
LEASE_FILE = "/var/dhcpd/var/db/dhcpd.leases"

_Network = ipaddress.IPv4Network | ipaddress.IPv6Network


class PfSenseTopologyReadError(RuntimeError):
    """A read that the topology map needs failed, so the refresh stops."""


def rows_or_raise(op_id: str, target_name: str, answer: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the ``rows`` of an op answer, or raise when the read failed.

    The pfSense read handlers answer a failed command with
    ``{"rows": [], "total": 0, "error": ...}``. That must not reach the
    map as "no rows".
    """
    error = answer.get("error")
    rows = answer.get("rows")
    if error or not isinstance(rows, list):
        reason = error or "the answer had no row list"
        raise PfSenseTopologyReadError(
            f"pfSense target {target_name!r}: {op_id} failed ({reason}); "
            "the topology map was not changed"
        )
    return rows


def _iso_lease_time(value: str | None) -> str | None:
    """Turn a lease time (``YYYY/MM/DD HH:MM:SS`` UTC) into ISO 8601.

    ``"never"`` stays ``"never"``. A value that is missing or cannot be
    read gives ``None``.
    """
    if value is None or value == "never":
        return value
    parsed = _lease_datetime(value)
    return parsed.isoformat() if parsed is not None else None


def _subnets(interfaces: Iterable[Mapping[str, Any]]) -> dict[_Network, list[dict[str, Any]]]:
    """Group the firewall's IPv4 addresses by subnet.

    Returns ``{subnet: [{name, address, ether, status}, ...]}``. Loopback
    and single-address entries are skipped, and so is any address that is
    not a valid ``addr/prefix`` string.
    """
    subnets: dict[_Network, list[dict[str, Any]]] = {}
    for iface in interfaces:
        for raw in iface.get("inet") or ():
            try:
                addr = ipaddress.ip_interface(str(raw))
            except ValueError:
                continue
            if addr.ip.is_loopback or addr.network.num_addresses == 1:
                continue
            subnets.setdefault(addr.network, []).append(
                {
                    "name": iface.get("name"),
                    "address": str(addr.ip),
                    "ether": iface.get("ether"),
                    "status": iface.get("status"),
                }
            )
    return subnets


def _subnet_for(ip: str, subnets: Iterable[_Network]) -> _Network | None:
    """Return the most specific known subnet that holds *ip*, or ``None``."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    matches = [net for net in subnets if address.version == net.version and address in net]
    return max(matches, key=lambda net: net.prefixlen, default=None)


def _lease_node(lease: Mapping[str, Any]) -> NodeHint:
    return NodeHint(
        kind="host",
        name=str(lease["ip"]),
        properties={
            "source": LEASE_SOURCE,
            "address": lease["ip"],
            "mac": lease.get("mac"),
            "hostname": lease.get("hostname"),
            "lease_starts": _iso_lease_time(lease.get("starts")),
            "lease_ends": _iso_lease_time(lease.get("ends")),
            "binding_state": lease.get("binding_state"),
        },
    )


def _lease_edge(host_name: str, subnet: _Network | None, target_name: str) -> EdgeHint:
    """Link a lease host to its subnet, or to the firewall when no subnet holds it."""
    if subnet is not None:
        return EdgeHint(
            from_kind="host",
            from_name=host_name,
            to_kind="network",
            to_name=str(subnet),
            kind="belongs-to",
            properties={"source": LEASE_SOURCE},
        )
    return EdgeHint(
        from_kind="host",
        from_name=host_name,
        to_kind="target",
        to_name=target_name,
        kind="routes-through",
        properties={"source": LEASE_SOURCE},
    )


def build_topology_hints(
    target_name: str,
    interfaces: Iterable[Mapping[str, Any]],
    leases: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> TopologyHints:
    """Build the map entries for one pfSense firewall.

    *interfaces* are rows of ``pfsense.interface.list``; *leases* are rows
    of ``pfsense.dhcp.leases``. Only leases with
    ``binding_state == "active"`` are added (the lease parser already
    reports an active lease whose end time has passed as ``expired``).
    """
    discovered_at = datetime.now(UTC) if now is None else now
    subnets = _subnets(interfaces)

    nodes: list[NodeHint] = [
        NodeHint(kind="target", name=target_name, properties={"source": INTERFACE_SOURCE})
    ]
    edges: list[EdgeHint] = []

    for network in sorted(subnets, key=lambda net: (net.version, net)):
        name = str(network)
        nodes.append(
            NodeHint(
                kind="network",
                name=name,
                properties={"source": INTERFACE_SOURCE, "interfaces": subnets[network]},
            )
        )
        edges.append(
            EdgeHint(
                from_kind="network",
                from_name=name,
                to_kind="target",
                to_name=target_name,
                kind="routes-through",
                properties={"source": INTERFACE_SOURCE},
            )
        )

    active = [row for row in leases if row.get("binding_state") == "active" and row.get("ip")]
    for lease in sorted(active, key=lambda row: str(row["ip"])):
        host = _lease_node(lease)
        nodes.append(host)
        edges.append(_lease_edge(host.name, _subnet_for(host.name, subnets), target_name))

    return TopologyHints(nodes=tuple(nodes), edges=tuple(edges), discovered_at=discovered_at)
