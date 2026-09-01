# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""windows_dns record ops -- read (``record.get``) + writes (``record.add`` / ``record.remove``).

Mirrors :mod:`~meho_backplane.connectors.bind9.ops_record`'s record
surface -- one read op and the two symmetric write ops -- but swaps the
BIND9 ``dig`` / zonefile-transform machinery for the Windows
``DnsServer`` module cmdlets driven over PowerShell-over-SSH
(:func:`~meho_backplane.connectors._shared.pwsh.pwsh_run`):

* ``windns.record.get``    -- ``Get-DnsServerResourceRecord`` (safe).
* ``windns.record.add``    -- ``Add-DnsServerResourceRecordA`` (A) /
  ``Add-DnsServerResourceRecordCName`` (CNAME), selected by ``type``
  (caution).
* ``windns.record.remove`` -- ``Remove-DnsServerResourceRecord ... -Force``
  (caution).

Safety levels mirror bind9 exactly: ``record.get`` is ``safe``;
``record.add`` / ``record.remove`` are ``caution`` (a DNS change is
global -- no per-caller scoping -- so it carries the same production-path
policy weight bind9's record writes do).

PowerShell injection safety
---------------------------

Operator-supplied string values (zone / name / IP / CNAME target) are
interpolated into the PowerShell script text inside **single-quoted**
PowerShell string literals with any embedded ``'`` doubled -- the exact
convention the Holodeck ``pod.info`` handler uses
(``value.replace("'", "''")``). Inside a single-quoted PowerShell string
the only metacharacter is ``'`` itself, so doubling it is complete
escaping. The transport is ``powershell -EncodedCommand`` (base64 UTF-16LE), so
the *shell* never parses the script -- there is no ``sh -c`` layer to
escape for, only the PowerShell parser. Integer values (TTL seconds) are
validated to ``int`` before interpolation. A/CNAME writes prefix
``$ErrorActionPreference = 'Stop'`` so a cmdlet failure terminates the
pwsh process with a non-zero exit that
:func:`~meho_backplane.connectors._shared.pwsh.pwsh_run` maps to a
:class:`~meho_backplane.connectors._shared.pwsh.PwshRunError`.

References
----------

* ``Get-DnsServerResourceRecord`` /
  ``Add-DnsServerResourceRecordA`` / ``Add-DnsServerResourceRecordCName`` /
  ``Remove-DnsServerResourceRecord``:
  https://learn.microsoft.com/en-us/powershell/module/dnsserver/
* Record-surface sibling: :mod:`meho_backplane.connectors.bind9.ops_record`.
"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import ps_single_quote, pwsh_run
from meho_backplane.connectors.windows_dns.ops import WindowsDnsOp
from meho_backplane.connectors.windows_dns.ops_zone import normalise_json_rows

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.windows_dns.connector import WindowsDnsConnector

__all__ = [
    "RECORD_OPS",
    "WINDOWS_DNS_RECORD_ADD_LLM_INSTRUCTIONS",
    "WINDOWS_DNS_RECORD_ADD_PARAMETER_SCHEMA",
    "WINDOWS_DNS_RECORD_GET_LLM_INSTRUCTIONS",
    "WINDOWS_DNS_RECORD_GET_PARAMETER_SCHEMA",
    "WINDOWS_DNS_RECORD_REMOVE_LLM_INSTRUCTIONS",
    "WINDOWS_DNS_RECORD_REMOVE_PARAMETER_SCHEMA",
    "windows_dns_record_add",
    "windows_dns_record_get",
    "windows_dns_record_remove",
]


# Supported ``-RRType`` values for the read / remove ops -- the
# operator-relevant subset of the DnsServer record-type surface.
_GET_SUPPORTED_TYPES: frozenset[str] = frozenset(
    {"A", "AAAA", "CNAME", "MX", "TXT", "PTR", "NS", "SRV", "SOA"}
)
_REMOVE_SUPPORTED_TYPES: frozenset[str] = frozenset(
    {"A", "AAAA", "CNAME", "MX", "TXT", "PTR", "NS", "SRV"}
)
# ``record.add`` mirrors bind9's A-plus-one-alias-type write surface: A
# (via Add-DnsServerResourceRecordA) and CNAME (via
# Add-DnsServerResourceRecordCName). Other record types are out of scope
# for the mirror.
_ADD_SUPPORTED_TYPES: frozenset[str] = frozenset({"A", "CNAME"})


async def windows_dns_record_get(
    connector: WindowsDnsConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``windns.record.get``.

    Runs ``Get-DnsServerResourceRecord -ZoneName <zone> [-Name <name>]
    [-RRType <type>]`` on the Windows host and returns
    ``{zone, name, type, rows, total}``. ``rows`` is empty when no
    record matches -- an empty match is a legitimate result, not an
    error (mirrors bind9's NXDOMAIN handling). The script wraps the
    cmdlet output in a ``@{ rows = ...; total = ... }`` hashtable so the
    pwsh helper always sees a non-empty JSON object even for a
    zero-match read (a bare cmdlet with no output would trip the
    empty-stdout guard).
    """
    zone: str = params["zone"]
    name: str | None = params.get("name")
    record_type: str | None = params.get("type")
    if record_type is not None:
        record_type = record_type.upper()
        if record_type not in _GET_SUPPORTED_TYPES:
            raise ValueError(
                f"unsupported record type {record_type!r}; "
                f"expected one of {sorted(_GET_SUPPORTED_TYPES)}"
            )

    cmd_parts = [f"Get-DnsServerResourceRecord -ZoneName {ps_single_quote(zone)}"]
    if name is not None:
        cmd_parts.append(f"-Name {ps_single_quote(name)}")
    if record_type is not None:
        cmd_parts.append(f"-RRType {ps_single_quote(record_type)}")
    get_expr = " ".join(cmd_parts)
    # SilentlyContinue so a missing zone / no-match read yields an empty
    # array rather than a terminating error; the hashtable envelope keeps
    # stdout non-empty and JSON-shaped in every case.
    script = (
        "$ErrorActionPreference = 'SilentlyContinue'; "
        f"$recs = @({get_expr}); "
        "ConvertTo-Json -Depth 4 -InputObject @{ rows = $recs; total = $recs.Count }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    raw_rows = payload.get("rows") if isinstance(payload, dict) else payload
    rows = normalise_json_rows(raw_rows)
    return {
        "zone": zone,
        "name": name,
        "type": record_type,
        "rows": rows,
        "total": len(rows),
    }


async def windows_dns_record_add(
    connector: WindowsDnsConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``windns.record.add`` -- add an A or CNAME record.

    Sequence:

    1. Validate ``type`` (A / CNAME) and the type-specific value
       (``ip`` must be a valid IPv4 address for A; ``target`` must be a
       non-empty CNAME alias for CNAME).
    2. Build the ``Add-DnsServerResourceRecordA`` /
       ``Add-DnsServerResourceRecordCName`` script (with an optional
       ``-TimeToLive (New-TimeSpan -Seconds <ttl>)``) under
       ``$ErrorActionPreference = 'Stop'`` so a cmdlet failure exits
       non-zero.
    3. Run it via the PowerShell helper; a ``@{ ok = $true }`` tail keeps
       stdout JSON-shaped on success.

    Returns ``{zone, name, type, value, ttl, op_class}`` with
    ``op_class="write"`` (the dual signal bind9's write ops set for the
    audit-replay path).
    """
    zone: str = params["zone"]
    name: str = params["name"]
    record_type: str = params.get("type", "A").upper()
    ttl: int | None = params.get("ttl")

    if record_type not in _ADD_SUPPORTED_TYPES:
        raise ValueError(
            f"record.add only supports A / CNAME; got type={record_type!r}. "
            "Other record types are out of scope for this connector."
        )

    ttl_clause = ""
    if ttl is not None:
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 0:
            raise ValueError(f"ttl must be a non-negative integer number of seconds; got {ttl!r}")
        ttl_clause = f" -TimeToLive (New-TimeSpan -Seconds {ttl})"

    if record_type == "A":
        ip: str | None = params.get("ip")
        if not ip:
            raise ValueError("record.add type=A requires an 'ip' parameter (IPv4 address)")
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError as exc:
            raise ValueError(f"invalid IP address {ip!r}: {exc}") from exc
        if not isinstance(addr, ipaddress.IPv4Address):
            raise ValueError(f"record type A expects an IPv4 address; got {ip!r}")
        value = ip
        add_expr = (
            f"Add-DnsServerResourceRecordA -ZoneName {ps_single_quote(zone)} "
            f"-Name {ps_single_quote(name)} -IPv4Address {ps_single_quote(ip)}{ttl_clause}"
        )
    else:  # CNAME
        alias: str | None = params.get("target")
        if not alias:
            raise ValueError(
                "record.add type=CNAME requires a 'target' parameter (the HostNameAlias FQDN)"
            )
        value = alias
        add_expr = (
            f"Add-DnsServerResourceRecordCName -ZoneName {ps_single_quote(zone)} "
            f"-Name {ps_single_quote(name)} -HostNameAlias {ps_single_quote(alias)}{ttl_clause}"
        )

    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"{add_expr}; "
        "ConvertTo-Json -Compress -InputObject @{ ok = $true }"
    )
    await pwsh_run(connector, target, script, operator=operator)
    return {
        "zone": zone,
        "name": name,
        "type": record_type,
        "value": value,
        "ttl": ttl,
        "op_class": "write",
    }


async def windows_dns_record_remove(
    connector: WindowsDnsConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``windns.record.remove`` -- remove matching records.

    Runs ``Remove-DnsServerResourceRecord -ZoneName <zone> -Name <name>
    -RRType <type> -Force`` under ``$ErrorActionPreference = 'Stop'``.
    ``-Force`` suppresses the interactive confirmation prompt (mandatory
    for the non-interactive transport). Returns
    ``{zone, name, type, op_class}`` with ``op_class="write"``.
    """
    zone: str = params["zone"]
    name: str = params["name"]
    record_type: str = params["type"].upper()
    if record_type not in _REMOVE_SUPPORTED_TYPES:
        raise ValueError(
            f"unsupported record type {record_type!r}; "
            f"expected one of {sorted(_REMOVE_SUPPORTED_TYPES)}"
        )

    remove_expr = (
        f"Remove-DnsServerResourceRecord -ZoneName {ps_single_quote(zone)} "
        f"-Name {ps_single_quote(name)} -RRType {ps_single_quote(record_type)} -Force"
    )
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"{remove_expr}; "
        "ConvertTo-Json -Compress -InputObject @{ ok = $true }"
    )
    await pwsh_run(connector, target, script, operator=operator)
    return {
        "zone": zone,
        "name": name,
        "type": record_type,
        "op_class": "write",
    }


# ---------------------------------------------------------------------------
# Parameter schemas + LLM instructions
# ---------------------------------------------------------------------------


_ZONE_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": "The DNS zone name, e.g. ``evba.lab``.",
}
_NAME_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": (
        "The owner (host) name relative to the zone, e.g. ``www`` for "
        "``www.evba.lab``, or ``@`` for the zone apex."
    ),
}


WINDOWS_DNS_RECORD_GET_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "zone": _ZONE_PROP,
        "name": {
            **_NAME_PROP,
            "description": (
                "Optional. The owner name to filter on. Omit to return every record in the zone."
            ),
        },
        "type": {
            "type": "string",
            "enum": sorted(_GET_SUPPORTED_TYPES),
            "description": (
                "Optional ``-RRType`` filter. Omit to return every "
                "record type at the matched name(s)."
            ),
        },
    },
    "required": ["zone"],
    "additionalProperties": False,
}


_WINDOWS_DNS_RECORD_GET_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "zone": {"type": "string"},
        "name": {"type": ["string", "null"]},
        "type": {"type": ["string", "null"]},
        "rows": {"type": "array", "items": {"type": "object"}},
        "total": {"type": "integer"},
    },
    "required": ["zone", "rows", "total"],
    "additionalProperties": False,
}


WINDOWS_DNS_RECORD_GET_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call when the operator asks 'what records exist at <name> in "
        "zone <zone>?' or 'list the records in zone <zone>'. Read-only; "
        "runs ``Get-DnsServerResourceRecord``. Empty ``rows`` means no "
        "matching record -- a legitimate result, not an error."
    ),
    "parameter_hints": {
        "zone": "Required. The DNS zone name.",
        "name": "Optional. Owner name filter; omit for the whole zone.",
        "type": "Optional. One of A / AAAA / CNAME / MX / TXT / PTR / NS / SRV / SOA.",
    },
    "output_shape": (
        "{'zone', 'name', 'type', 'rows': [<DnsServerResourceRecord objects>], 'total': <int>}."
    ),
}


_ADD_WARNING = (
    "WARNING: this change is global. DNS has no per-caller scoping -- on "
    "success the record is live for every consumer of this server. "
    "``safety_level`` is ``caution`` (the production-path gate is G7/G10 "
    "policy territory keyed on this value)."
)


WINDOWS_DNS_RECORD_ADD_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "zone": _ZONE_PROP,
        "name": _NAME_PROP,
        "type": {
            "type": "string",
            "enum": sorted(_ADD_SUPPORTED_TYPES),
            "default": "A",
            "description": "Record type: ``A`` (default) or ``CNAME``.",
        },
        "ip": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "Required for ``type=A``. The IPv4 address, e.g. ``10.5.50.9``.",
        },
        "target": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Required for ``type=CNAME``. The HostNameAlias -- the "
                "canonical FQDN the alias points at, e.g. "
                "``www.evba.lab``."
            ),
        },
        "ttl": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Optional TTL in seconds; rendered as "
                "``-TimeToLive (New-TimeSpan -Seconds <ttl>)``. Omit to "
                "use the zone default."
            ),
        },
    },
    "required": ["zone", "name"],
    "additionalProperties": False,
}


_WINDOWS_DNS_RECORD_ADD_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "zone": {"type": "string"},
        "name": {"type": "string"},
        "type": {"type": "string"},
        "value": {"type": "string"},
        "ttl": {"type": ["integer", "null"]},
        "op_class": {"type": "string", "enum": ["write"]},
    },
    "required": ["zone", "name", "type", "value", "op_class"],
    "additionalProperties": False,
}


WINDOWS_DNS_RECORD_ADD_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Add a forward A record (``type=A``, ``ip=<IPv4>``) or a CNAME "
        "alias (``type=CNAME``, ``target=<FQDN>``) to a zone the Windows "
        "DNS server hosts. " + _ADD_WARNING + " Use ``windns.record.get`` "
        "first to confirm the name is not already in use."
    ),
    "parameter_hints": {
        "zone": "Required. The DNS zone name.",
        "name": "Required. Owner name relative to the zone.",
        "type": "Optional. ``A`` (default) or ``CNAME``.",
        "ip": "Required for type=A. IPv4 address.",
        "target": "Required for type=CNAME. The HostNameAlias FQDN.",
        "ttl": "Optional. TTL in seconds.",
    },
    "output_shape": (
        "{'zone', 'name', 'type', 'value': <ip-or-alias>, 'ttl', 'op_class': 'write'}."
    ),
}


WINDOWS_DNS_RECORD_REMOVE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "zone": _ZONE_PROP,
        "name": _NAME_PROP,
        "type": {
            "type": "string",
            "enum": sorted(_REMOVE_SUPPORTED_TYPES),
            "description": ("The ``-RRType`` of the record(s) to remove, e.g. ``A`` or ``CNAME``."),
        },
    },
    "required": ["zone", "name", "type"],
    "additionalProperties": False,
}


_WINDOWS_DNS_RECORD_REMOVE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "zone": {"type": "string"},
        "name": {"type": "string"},
        "type": {"type": "string"},
        "op_class": {"type": "string", "enum": ["write"]},
    },
    "required": ["zone", "name", "type", "op_class"],
    "additionalProperties": False,
}


WINDOWS_DNS_RECORD_REMOVE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Remove the record(s) matching (zone, name, RRType) from the "
        "Windows DNS server via ``Remove-DnsServerResourceRecord ... "
        "-Force``. " + _ADD_WARNING + " Use ``windns.record.get`` first "
        "to confirm the current state."
    ),
    "parameter_hints": {
        "zone": "Required. The DNS zone name.",
        "name": "Required. Owner name relative to the zone.",
        "type": "Required. The RRType to remove (A / AAAA / CNAME / ...).",
    },
    "output_shape": "{'zone', 'name', 'type', 'op_class': 'write'}.",
}


# ---------------------------------------------------------------------------
# Op metadata table
# ---------------------------------------------------------------------------


RECORD_OPS: tuple[WindowsDnsOp, ...] = (
    WindowsDnsOp(
        op_id="windns.record.get",
        handler_attr="windows_dns_record_get",
        summary="Read records via ``Get-DnsServerResourceRecord`` (optional name/type filter).",
        description=(
            "Runs ``Get-DnsServerResourceRecord -ZoneName <zone> "
            "[-Name <name>] [-RRType <type>]`` on the Windows host and "
            "returns one row per matching record. ``name`` and ``type`` "
            "are optional filters; omit both to dump the whole zone. "
            "Read-only. Empty ``rows`` is a legitimate no-match result, "
            "not an error."
        ),
        parameter_schema=WINDOWS_DNS_RECORD_GET_PARAMETER_SCHEMA,
        response_schema=_WINDOWS_DNS_RECORD_GET_RESPONSE_SCHEMA,
        group_key="record",
        tags=("read-only", "record", "lookup"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=WINDOWS_DNS_RECORD_GET_LLM_INSTRUCTIONS,
    ),
    WindowsDnsOp(
        op_id="windns.record.add",
        handler_attr="windows_dns_record_add",
        summary="Add an A or CNAME record via the Add-DnsServerResourceRecord* cmdlets.",
        description=(
            "Adds a forward A record "
            "(``Add-DnsServerResourceRecordA -IPv4Address``) or a CNAME "
            "alias (``Add-DnsServerResourceRecordCName -HostNameAlias``), "
            "selected by ``type``. Optional ``ttl`` (seconds) renders as "
            "``-TimeToLive (New-TimeSpan -Seconds <ttl>)``. " + _ADD_WARNING
        ),
        parameter_schema=WINDOWS_DNS_RECORD_ADD_PARAMETER_SCHEMA,
        response_schema=_WINDOWS_DNS_RECORD_ADD_RESPONSE_SCHEMA,
        group_key="record",
        tags=("write", "record"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions=WINDOWS_DNS_RECORD_ADD_LLM_INSTRUCTIONS,
    ),
    WindowsDnsOp(
        op_id="windns.record.remove",
        handler_attr="windows_dns_record_remove",
        summary="Remove matching records via ``Remove-DnsServerResourceRecord ... -Force``.",
        description=(
            "Removes the record(s) matching (zone, name, RRType) via "
            "``Remove-DnsServerResourceRecord ... -Force``. " + _ADD_WARNING
        ),
        parameter_schema=WINDOWS_DNS_RECORD_REMOVE_PARAMETER_SCHEMA,
        response_schema=_WINDOWS_DNS_RECORD_REMOVE_RESPONSE_SCHEMA,
        group_key="record",
        tags=("write", "record"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions=WINDOWS_DNS_RECORD_REMOVE_LLM_INSTRUCTIONS,
    ),
)
