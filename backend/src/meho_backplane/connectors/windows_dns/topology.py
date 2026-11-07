# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Windows DNS on the topology map: the A, AAAA and CNAME records of its zones.

:meth:`WindowsDnsConnector.discover_topology` calls
:func:`discover_windows_dns_topology`. It uses the same reads as
``windns.zone.list`` and ``windns.record.get``:

* the zone list comes from the ``windns.zone.list`` handler
  (``Get-DnsServerZone``, which fails on any error);
* the records of each zone come from ``Get-DnsServerResourceRecord``, the
  cmdlet of ``windns.record.get``, without a name, so one call reads the
  whole zone. Unlike ``windns.record.get`` it stops on an error instead of
  returning an empty list, so a failed read never looks like an empty
  zone.

Only forward ``Primary`` and ``Secondary`` zones that Windows did not
create by itself are read. A zone the account may not read (PowerShell
error category ``PermissionDenied``, or an access-denied error) is
skipped and named on the target node with reason ``not_allowed``. Any
other error raises, so the refresh stores nothing.

The snapshot itself is built by
:func:`~meho_backplane.connectors._shared.dns_topology.build_dns_topology_hints`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.dns_topology import (
    DnsRecord,
    DnsZoneSkip,
    build_dns_topology_hints,
)
from meho_backplane.connectors._shared.pwsh import ps_single_quote, pwsh_run
from meho_backplane.connectors.schemas import TopologyHints
from meho_backplane.connectors.windows_dns.ops_zone import (
    normalise_json_rows,
    windows_dns_zone_list,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.windows_dns.connector import WindowsDnsConnector

__all__ = [
    "WindowsDnsTopologyReadError",
    "build_zone_records_script",
    "discover_windows_dns_topology",
    "zones_to_read",
]

_SOURCE = "windows-dns"
_DATA_ZONE_TYPES = frozenset({"primary", "secondary"})
#: A large zone can take longer than the default 30 seconds.
_ZONE_READ_TIMEOUT_SECONDS = 120.0

#: Reads one zone. Kept as a brace-heavy raw literal (PowerShell script
#: blocks and hashtables); the zone name is put in with ``replace``, never
#: ``str.format``. The ``catch`` answers with the error category only, never
#: the error text.
_ZONE_RECORDS_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "try { "
    "$recs = @(Get-DnsServerResourceRecord -ZoneName __ZONE__ | Where-Object { "
    "@('A', 'AAAA', 'CNAME') -contains $_.RecordType.ToString() }); "
    "$rows = @($recs | ForEach-Object { "
    "$rd = $_.RecordData; $val = $null; "
    "switch ($_.RecordType.ToString()) { "
    "'A' { $val = $rd.IPv4Address.IPAddressToString } "
    "'AAAA' { $val = $rd.IPv6Address.IPAddressToString } "
    "'CNAME' { $val = $rd.HostNameAlias } "
    "} "
    '@{ name = $_.HostName; type = $_.RecordType.ToString(); value = "$val" } '
    "}); "
    "ConvertTo-Json -Depth 4 -InputObject @{ ok = $true; rows = $rows } "
    "} catch { "
    "$e = $_; "
    "$denied = ($e.CategoryInfo.Category.ToString() -eq 'PermissionDenied') "
    "-or ($e.Exception -is [System.UnauthorizedAccessException]) "
    "-or ($e.Exception.HResult -eq -2147024891) "
    "-or (\"$($e.Exception.NativeErrorCode)\" -eq 'AccessDenied'); "
    "ConvertTo-Json -InputObject @{ ok = $false; denied = $denied; "
    "category = $e.CategoryInfo.Category.ToString() } "
    "}"
)


class WindowsDnsTopologyReadError(RuntimeError):
    """A read for the topology map failed, so the refresh stores nothing."""


def zones_to_read(zone_rows: list[dict[str, Any]]) -> list[str]:
    """Return the names of the forward Primary / Secondary zones to read."""
    out: list[str] = []
    for row in zone_rows:
        name = row.get("ZoneName")
        if not isinstance(name, str) or not name:
            continue
        if str(row.get("ZoneType") or "").lower() not in _DATA_ZONE_TYPES:
            continue
        if row.get("IsReverseLookupZone") is True or row.get("IsAutoCreated") is True:
            continue
        out.append(name)
    return out


def build_zone_records_script(zone: str) -> str:
    """Return the script that reads the A, AAAA and CNAME records of *zone*."""
    return _ZONE_RECORDS_SCRIPT.replace("__ZONE__", ps_single_quote(zone))


def _full_name(host_name: Any, zone: str) -> str | None:
    """Turn a relative ``HostName`` (``@`` is the zone itself) into a full name."""
    if not isinstance(host_name, str) or not host_name.strip():
        return None
    host = host_name.strip().rstrip(".")
    return zone if host == "@" else f"{host}.{zone}"


async def _read_zone(
    connector: WindowsDnsConnector,
    target: Any,
    zone: str,
    operator: Operator,
) -> list[DnsRecord] | DnsZoneSkip:
    """Read one zone; return its records, or why it was skipped."""
    payload = await pwsh_run(
        connector,
        target,
        build_zone_records_script(zone),
        operator=operator,
        timeout=_ZONE_READ_TIMEOUT_SECONDS,
    )
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        if isinstance(payload, dict) and payload.get("denied") is True:
            return DnsZoneSkip(zone=zone, reason="not_allowed")
        category = payload.get("category") if isinstance(payload, dict) else None
        raise WindowsDnsTopologyReadError(
            f"reading the records of zone {zone!r} failed (error category {category!r})"
        )
    records: list[DnsRecord] = []
    for row in normalise_json_rows(payload.get("rows")):
        name = _full_name(row.get("name"), zone)
        if name is None:
            continue
        records.append(
            DnsRecord(
                name=name,
                type=str(row.get("type") or ""),
                value=str(row.get("value") or ""),
                zone=zone,
            )
        )
    return records


async def discover_windows_dns_topology(
    connector: WindowsDnsConnector,
    target: Any,
    operator: Operator,
) -> TopologyHints:
    """Read every data zone of *target* and build its topology snapshot."""
    zone_list = await windows_dns_zone_list(connector, target, {}, operator)
    records: list[DnsRecord] = []
    zones_read: list[tuple[str, str | None]] = []
    skipped: list[DnsZoneSkip] = []
    for zone in zones_to_read(zone_list["rows"]):
        result = await _read_zone(connector, target, zone, operator)
        if isinstance(result, DnsZoneSkip):
            skipped.append(result)
            continue
        zones_read.append((zone, None))
        records.extend(result)
    return build_dns_topology_hints(
        target_name=target.name,
        source=_SOURCE,
        records=records,
        zones_read=zones_read,
        zones_skipped=skipped,
    )
