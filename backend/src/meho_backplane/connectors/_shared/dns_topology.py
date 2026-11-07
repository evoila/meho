# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Build the topology map snapshot of a DNS server from its records.

Shared by the bind9 and the Windows DNS connectors. Each connector reads
its zones its own way and hands the A, AAAA and CNAME records here as
:class:`DnsRecord` rows. :func:`build_dns_topology_hints` turns them into:

* the DNS server's own ``target`` node, with the zones it read and the
  zones it skipped (and why);
* one ``dns-record`` node per DNS name, with its records (type, value,
  zone, view) and the source (``bind9`` or ``windows-dns``), linked to the
  target with ``belongs-to``;
* a ``resolves-to`` edge from a CNAME name to the name it points at, when
  the server holds that name too;
* links (:class:`~meho_backplane.connectors.schemas.LinkHint`) that ask
  the refresh to connect each name to an existing ``vm``, ``host`` or
  ``node`` of the map with the same full name, the same address, or the
  same first label. The refresh never creates a node for a link.

The time is not in the properties: the map stores when a refresh first
and last saw each row, so a property that changed on every refresh would
only fill the history.
"""

from __future__ import annotations

import ipaddress
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from meho_backplane.connectors.schemas import (
    EdgeHint,
    LinkHint,
    NodeHint,
    NodeKind,
    TopologyHints,
)

__all__ = [
    "LINK_TO_KINDS",
    "DnsRecord",
    "DnsZoneSkip",
    "build_dns_topology_hints",
    "normalise_dns_name",
]

#: The map node kinds a DNS name is linked to: the systems.
LINK_TO_KINDS: tuple[NodeKind, ...] = ("vm", "host", "node")

_ADDRESS_TYPES = frozenset({"A", "AAAA"})
_READ_TYPES = frozenset({"A", "AAAA", "CNAME"})


@dataclass(frozen=True)
class DnsRecord:
    """One A, AAAA or CNAME record as a DNS server holds it."""

    name: str
    type: str
    value: str
    zone: str
    view: str | None = None


@dataclass(frozen=True)
class DnsZoneSkip:
    """A zone the refresh did not read, and why (for example ``not_allowed``)."""

    zone: str
    reason: str
    view: str | None = None


def normalise_dns_name(name: str) -> str:
    """Return *name* in lower case without the trailing dot."""
    return name.strip().rstrip(".").lower()


def _normalise_record(record: DnsRecord) -> DnsRecord | None:
    """Return the record with a clean name and value, or ``None`` to drop it.

    Drops other record types, wildcard names (``*.example.com`` is not one
    system), empty names and values that are not an address of the type.
    """
    record_type = record.type.upper()
    name = normalise_dns_name(record.name)
    if record_type not in _READ_TYPES or not name or name.startswith("*"):
        return None
    if record_type in _ADDRESS_TYPES:
        try:
            address = ipaddress.ip_address(record.value.strip())
        except ValueError:
            return None
        if (address.version == 4) != (record_type == "A"):
            return None
        value = str(address)
    else:
        value = normalise_dns_name(record.value)
        if not value:
            return None
    return DnsRecord(
        name=name,
        type=record_type,
        value=value,
        zone=normalise_dns_name(record.zone),
        view=record.view,
    )


def _record_row(record: DnsRecord) -> dict[str, Any]:
    row: dict[str, Any] = {"type": record.type, "value": record.value, "zone": record.zone}
    if record.view is not None:
        row["view"] = record.view
    return row


def _sort_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (row["type"], row["value"], row["zone"], row.get("view") or "")


def _links_for(name: str, records: list[DnsRecord]) -> list[LinkHint]:
    """Ask the refresh to link *name* by full name, by address, then by first label."""
    links = [
        LinkHint(
            from_kind="dns-record",
            from_name=name,
            to_kinds=LINK_TO_KINDS,
            match_names=(name,),
            properties={"matched_on": "name"},
        )
    ]
    addresses = sorted({r.value for r in records if r.type in _ADDRESS_TYPES})
    if addresses:
        links.append(
            LinkHint(
                from_kind="dns-record",
                from_name=name,
                to_kinds=LINK_TO_KINDS,
                match_names=tuple(addresses),
                properties={"matched_on": "address"},
            )
        )
    short_name = name.split(".", 1)[0]
    if short_name != name:
        links.append(
            LinkHint(
                from_kind="dns-record",
                from_name=name,
                to_kinds=LINK_TO_KINDS,
                match_names=(short_name,),
                properties={"matched_on": "short-name"},
            )
        )
    return links


def build_dns_topology_hints(
    *,
    target_name: str,
    source: str,
    records: list[DnsRecord],
    zones_read: list[tuple[str, str | None]],
    zones_skipped: list[DnsZoneSkip],
    now: datetime | None = None,
) -> TopologyHints:
    """Build the :class:`TopologyHints` of one DNS server.

    *source* is the product that read the records (``bind9`` or
    ``windows-dns``). *zones_read* lists ``(zone, view)`` pairs; *view* is
    ``None`` when the server has no views.
    """
    by_name: dict[str, list[DnsRecord]] = defaultdict(list)
    for raw in records:
        record = _normalise_record(raw)
        if record is not None:
            by_name[record.name].append(record)

    target_properties: dict[str, Any] = {
        "dns_source": source,
        "zones_read": sorted(
            (
                {"zone": normalise_dns_name(z), **({"view": v} if v is not None else {})}
                for z, v in zones_read
            ),
            key=lambda row: (row["zone"], row.get("view") or ""),
        ),
        "zones_skipped": sorted(
            (
                {
                    "zone": normalise_dns_name(s.zone),
                    "reason": s.reason,
                    **({"view": s.view} if s.view is not None else {}),
                }
                for s in zones_skipped
            ),
            key=lambda row: (row["zone"], row.get("view") or ""),
        ),
    }
    nodes: list[NodeHint] = [
        NodeHint(kind="target", name=target_name, properties=target_properties)
    ]
    edges: list[EdgeHint] = []
    links: list[LinkHint] = []
    for name in sorted(by_name):
        name_records = by_name[name]
        rows = sorted({tuple(sorted(_record_row(r).items())) for r in name_records})
        nodes.append(
            NodeHint(
                kind="dns-record",
                name=name,
                properties={
                    "source": source,
                    "records": sorted((dict(row) for row in rows), key=_sort_key),
                },
            )
        )
        edges.append(
            EdgeHint(
                from_kind="dns-record",
                from_name=name,
                to_kind="target",
                to_name=target_name,
                kind="belongs-to",
            )
        )
        for alias_target in sorted({r.value for r in name_records if r.type == "CNAME"}):
            if alias_target in by_name and alias_target != name:
                edges.append(
                    EdgeHint(
                        from_kind="dns-record",
                        from_name=name,
                        to_kind="dns-record",
                        to_name=alias_target,
                        kind="resolves-to",
                        properties={"matched_on": "cname"},
                    )
                )
        links.extend(_links_for(name, name_records))
    return TopologyHints(
        nodes=tuple(nodes),
        edges=tuple(edges),
        links=tuple(links),
        discovered_at=datetime.now(UTC) if now is None else now,
    )
