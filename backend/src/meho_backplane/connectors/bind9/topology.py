# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""bind9 on the topology map: the A, AAAA and CNAME records of its zones.

:meth:`Bind9Connector.discover_topology` calls :func:`discover_bind9_topology`.
It runs the same reads as ``bind9.zone.list`` and ``bind9.zone.read``
(``named-checkconf -p``, then ``cat`` of each zone file) and parses them
with the same parsers. It does not run those operations, so no result
limit shortens a zone.

* Only zones that hold data are read: type ``master`` / ``primary`` /
  ``slave`` / ``secondary`` with a ``file``, and not a reverse zone
  (``in-addr.arpa`` / ``ip6.arpa``, which holds no A, AAAA or CNAME
  records a system is named by).
* A zone file the SSH user may not read (``Permission denied``) is
  skipped and named on the target node with reason ``not_allowed``. A
  missing file is skipped with ``file_missing``, and a file that is not
  a text zone file (for example the ``raw`` format of a secondary zone)
  with ``not_text``.
* Any other failure (SSH, a failed ``named-checkconf``, another ``cat``
  error) raises, so the refresh stores nothing. A failed read never
  looks like an empty zone.

The snapshot itself is built by
:func:`~meho_backplane.connectors._shared.dns_topology.build_dns_topology_hints`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import dns.exception

from meho_backplane.connectors._shared.dns_topology import (
    DnsRecord,
    DnsZoneSkip,
    build_dns_topology_hints,
)
from meho_backplane.connectors.bind9.ops_zone import parse_named_checkconf_zones, parse_zonefile
from meho_backplane.connectors.schemas import TopologyHints

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.bind9.connector import Bind9Connector

__all__ = ["Bind9TopologyReadError", "discover_bind9_topology", "zones_to_read"]

_SOURCE = "bind9"
_DATA_ZONE_TYPES = frozenset({"master", "primary", "slave", "secondary"})
_REVERSE_SUFFIXES = (".in-addr.arpa", ".ip6.arpa")


class Bind9TopologyReadError(RuntimeError):
    """A read for the topology map failed, so the refresh stores nothing."""


def zones_to_read(zone_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the zones that hold A, AAAA or CNAME data and have a file."""
    out: list[dict[str, Any]] = []
    for row in zone_rows:
        name = str(row.get("name") or "").rstrip(".").lower()
        if not name or not row.get("file"):
            continue
        if str(row.get("type") or "").lower() not in _DATA_ZONE_TYPES:
            continue
        if name.endswith(_REVERSE_SUFFIXES) or name in ("in-addr.arpa", "ip6.arpa"):
            continue
        out.append(row)
    return out


def _skip_reason(stderr: str) -> str | None:
    """Map a failed ``cat`` to a skip reason, or ``None`` when it must fail."""
    text = stderr.lower()
    if "permission denied" in text:
        return "not_allowed"
    if "no such file" in text:
        return "file_missing"
    return None


async def _read_zone(
    connector: Bind9Connector,
    target: Any,
    zone: dict[str, Any],
    operator: Operator,
) -> list[DnsRecord] | DnsZoneSkip:
    """Read one zone file; return its records, or why it was skipped."""
    zone_name = str(zone["name"])
    view = zone.get("view")
    path = str(zone["file"])
    quoted_path = "'" + path.replace("'", "'\\''") + "'"
    try:
        proc = await connector._run_command(target, f"cat {quoted_path}", operator=operator)
    except UnicodeDecodeError:
        return DnsZoneSkip(zone=zone_name, view=view, reason="not_text")
    exit_status = getattr(proc, "exit_status", None)
    if exit_status != 0:
        reason = _skip_reason(str(getattr(proc, "stderr", "") or ""))
        if reason is None:
            raise Bind9TopologyReadError(
                f"reading the file of zone {zone_name!r} failed with exit status {exit_status!r}"
            )
        return DnsZoneSkip(zone=zone_name, view=view, reason=reason)
    text = proc.stdout if isinstance(proc.stdout, str) else ""
    origin = zone_name if zone_name.endswith(".") else zone_name + "."
    try:
        rows = parse_zonefile(text, origin=origin)
    except dns.exception.DNSException:
        return DnsZoneSkip(zone=zone_name, view=view, reason="not_text")
    return [
        DnsRecord(
            name=row["name"],
            type=row["type"],
            value=row["rdata"],
            zone=zone_name,
            view=view,
        )
        for row in rows
        if row["type"] in ("A", "AAAA", "CNAME")
    ]


async def discover_bind9_topology(
    connector: Bind9Connector,
    target: Any,
    operator: Operator,
) -> TopologyHints:
    """Read every data zone of *target* and build its topology snapshot."""
    checkconf = await connector._run_command(target, "named-checkconf -p", operator=operator)
    exit_status = getattr(checkconf, "exit_status", None)
    if exit_status != 0:
        raise Bind9TopologyReadError(
            f"named-checkconf -p failed with exit status {exit_status!r}; the zone list is unknown"
        )
    output = checkconf.stdout if isinstance(checkconf.stdout, str) else ""
    records: list[DnsRecord] = []
    zones_read: list[tuple[str, str | None]] = []
    skipped: list[DnsZoneSkip] = []
    for zone in zones_to_read(parse_named_checkconf_zones(output)):
        result = await _read_zone(connector, target, zone, operator)
        if isinstance(result, DnsZoneSkip):
            skipped.append(result)
            continue
        zones_read.append((str(zone["name"]), zone.get("view")))
        records.extend(result)
    return build_dns_topology_hints(
        target_name=target.name,
        source=_SOURCE,
        records=records,
        zones_read=zones_read,
        zones_skipped=skipped,
    )
