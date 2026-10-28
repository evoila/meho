# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""windows_dns zone settings -- read (``zone.get``) and change (``zone.set``).

Two settings of a primary zone matter when a new AD DNS server is built:

* who may update names in the zone (``Set-DnsServerPrimaryZone
  -DynamicUpdate``: ``None``, ``Secure`` or ``NonsecureAndSecure``);
* the minimum TTL of the zone's SOA record. Resolvers keep a "name does not
  exist" answer for this long (RFC 2308 negative caching).

``zone.get`` reads both, plus the zone type and the other SOA timers.
``zone.set`` changes one or both and then reads the zone back, so the result
shows the values the server now has. Setting a value it already has changes
nothing, so the call is safe to run twice.

The SOA change follows the documented ``Set-DnsServerResourceRecord`` pattern:
read the old record, clone it, change the clone, and pass both objects.

Injection safety: the zone name goes into the script only as a single-quoted
literal (``ps_single_quote``). The dynamic-update value comes from a fixed
list and the TTL is checked to be an ``int`` before it is written into the
script.

References
----------

* ``Set-DnsServerPrimaryZone``:
  https://learn.microsoft.com/en-us/powershell/module/dnsserver/set-dnsserverprimaryzone
* ``Set-DnsServerResourceRecord``:
  https://learn.microsoft.com/en-us/powershell/module/dnsserver/set-dnsserverresourcerecord
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import ps_single_quote, pwsh_run
from meho_backplane.connectors.windows_dns.ops import SSH_TRANSPORT_NOTE, WindowsDnsOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.windows_dns.connector import WindowsDnsConnector

__all__ = [
    "DYNAMIC_UPDATE_VALUES",
    "MAX_SOA_MINIMUM_TTL_SECONDS",
    "ZONE_SETTINGS_OPS",
    "windows_dns_zone_get",
    "windows_dns_zone_set",
]

#: The values ``Set-DnsServerPrimaryZone -DynamicUpdate`` accepts.
DYNAMIC_UPDATE_VALUES: tuple[str, ...] = ("None", "Secure", "NonsecureAndSecure")

#: Upper bound for the SOA minimum TTL (one day, the RFC 2308 advice).
MAX_SOA_MINIMUM_TTL_SECONDS: int = 86400


def _zone_read_script(zone_literal: str) -> str:
    """Return the statements that read one zone's settings as JSON."""
    return (
        f"$z = Get-DnsServerZone -Name {zone_literal}; "
        f"$soa = Get-DnsServerResourceRecord -ZoneName {zone_literal} -RRType Soa -Name '@' "
        "| Select-Object -First 1; "
        "$d = $soa.RecordData; "
        "ConvertTo-Json -Compress -InputObject @{ "
        "zone = [string]$z.ZoneName; "
        "zone_type = [string]$z.ZoneType; "
        "dynamic_update = [string]$z.DynamicUpdate; "
        "is_ds_integrated = [bool]$z.IsDsIntegrated; "
        "replication_scope = [string]$z.ReplicationScope; "
        "soa_primary_server = [string]$d.PrimaryServer; "
        "soa_serial_number = [int64]$d.SerialNumber; "
        "soa_minimum_ttl_seconds = [int64]$d.MinimumTimeToLive.TotalSeconds; "
        "soa_refresh_seconds = [int64]$d.RefreshInterval.TotalSeconds; "
        "soa_retry_seconds = [int64]$d.RetryDelay.TotalSeconds; "
        "soa_expire_seconds = [int64]$d.ExpireLimit.TotalSeconds }"
    )


def _zone_dict(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("the zone read did not return a JSON object")
    return payload


async def windows_dns_zone_get(
    connector: WindowsDnsConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``windns.zone.get`` -- one zone's settings and SOA timers."""
    script = "$ErrorActionPreference = 'Stop'; " + _zone_read_script(
        ps_single_quote(params["zone"])
    )
    return _zone_dict(await pwsh_run(connector, target, script, operator=operator))


def _check_ttl(raw: Any) -> int:
    if (
        not isinstance(raw, int)
        or isinstance(raw, bool)
        or not 0 <= raw <= MAX_SOA_MINIMUM_TTL_SECONDS
    ):
        raise ValueError(
            "soa_minimum_ttl_seconds must be a whole number from 0 to "
            f"{MAX_SOA_MINIMUM_TTL_SECONDS}; got {raw!r}"
        )
    return raw


async def windows_dns_zone_set(
    connector: WindowsDnsConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``windns.zone.set`` -- change dynamic update and/or the SOA minimum TTL."""
    dynamic_update = params.get("dynamic_update")
    ttl_raw = params.get("soa_minimum_ttl_seconds")
    if dynamic_update is None and ttl_raw is None:
        raise ValueError("give dynamic_update, soa_minimum_ttl_seconds, or both")
    if dynamic_update is not None and dynamic_update not in DYNAMIC_UPDATE_VALUES:
        raise ValueError(
            f"dynamic_update must be one of {list(DYNAMIC_UPDATE_VALUES)}; got {dynamic_update!r}"
        )

    zone_literal = ps_single_quote(params["zone"])
    parts = ["$ErrorActionPreference = 'Stop'; "]
    if dynamic_update is not None:
        parts.append(
            f"Set-DnsServerPrimaryZone -Name {zone_literal} -DynamicUpdate '{dynamic_update}'; "
        )
    if ttl_raw is not None:
        ttl = _check_ttl(ttl_raw)
        parts.append(
            f"$old = Get-DnsServerResourceRecord -ZoneName {zone_literal} -RRType Soa -Name '@' "
            "| Select-Object -First 1; "
            "$new = $old.Clone(); "
            f"$new.RecordData.MinimumTimeToLive = [System.TimeSpan]::FromSeconds({ttl}); "
            f"Set-DnsServerResourceRecord -ZoneName {zone_literal} "
            "-OldInputObject $old -NewInputObject $new; "
        )
    parts.append(_zone_read_script(zone_literal))
    result = _zone_dict(await pwsh_run(connector, target, "".join(parts), operator=operator))
    return {**result, "op_class": "write"}


_ZONE_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": "The zone name, for example example.test.",
}

_ZONE_RESPONSE_PROPERTIES: dict[str, Any] = {
    "zone": {"type": "string", "description": "The zone name."},
    "zone_type": {"type": "string", "description": "Primary, Secondary, Stub or Forwarder."},
    "dynamic_update": {
        "type": "string",
        "description": "Who may update names: None, Secure or NonsecureAndSecure.",
    },
    "is_ds_integrated": {"type": "boolean", "description": "The zone is stored in AD."},
    "replication_scope": {
        "type": "string",
        "description": (
            "Where AD copies the zone (Forest, Domain, Legacy, Custom); empty for a file zone."
        ),
    },
    "soa_primary_server": {"type": "string", "description": "The SOA primary server."},
    "soa_serial_number": {"type": "integer", "description": "The SOA serial number."},
    "soa_minimum_ttl_seconds": {
        "type": "integer",
        "description": "The SOA minimum TTL: how long a 'name does not exist' answer is kept.",
    },
    "soa_refresh_seconds": {"type": "integer", "description": "The SOA refresh interval."},
    "soa_retry_seconds": {"type": "integer", "description": "The SOA retry delay."},
    "soa_expire_seconds": {"type": "integer", "description": "The SOA expire limit."},
}


ZONE_SETTINGS_OPS: tuple[WindowsDnsOp, ...] = (
    WindowsDnsOp(
        op_id="windns.zone.get",
        handler_attr="windows_dns_zone_get",
        summary="Read one zone's settings: dynamic update, zone type and the SOA timers.",
        description=(
            "Runs Get-DnsServerZone and reads the zone's SOA record. Returns who may "
            "update names, the zone type and storage, and the SOA timers, including the "
            "minimum TTL that sets how long a 'name does not exist' answer is kept. Read-only."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"zone": _ZONE_PROP},
            "required": ["zone"],
            "additionalProperties": False,
            "examples": [{"zone": "example.test"}],
        },
        response_schema={
            "type": "object",
            "properties": _ZONE_RESPONSE_PROPERTIES,
            "required": ["zone", "dynamic_update", "soa_minimum_ttl_seconds"],
            "additionalProperties": True,
        },
        group_key="zone",
        tags=("read-only", "zone", "settings"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "Call to check a zone's settings, for example after a build or before "
                "windns.zone.set. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"zone": "Required. The zone name from windns.zone.list."},
            "output_shape": (
                "{zone, zone_type, dynamic_update, is_ds_integrated, replication_scope, "
                "soa_primary_server, soa_serial_number, soa_minimum_ttl_seconds, "
                "soa_refresh_seconds, soa_retry_seconds, soa_expire_seconds}."
            ),
            "next_step": "To change a value, call windns.zone.set with the same zone.",
        },
    ),
    WindowsDnsOp(
        op_id="windns.zone.set",
        handler_attr="windows_dns_zone_set",
        summary="Change who may update names in a zone and the zone's SOA minimum TTL.",
        description=(
            "Runs Set-DnsServerPrimaryZone -DynamicUpdate and/or changes the SOA minimum "
            "TTL with Set-DnsServerResourceRecord, then reads the zone back. Only for a "
            "primary zone. Safe to run twice. A person must approve it."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "zone": _ZONE_PROP,
                "dynamic_update": {
                    "type": "string",
                    "enum": list(DYNAMIC_UPDATE_VALUES),
                    "description": (
                        "Who may update names. Secure: only domain members (needs an AD "
                        "zone). NonsecureAndSecure: anyone. None: nobody."
                    ),
                },
                "soa_minimum_ttl_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_SOA_MINIMUM_TTL_SECONDS,
                    "description": (
                        "New SOA minimum TTL in seconds: how long resolvers keep a 'name "
                        "does not exist' answer. 0 to 86400."
                    ),
                },
            },
            "required": ["zone"],
            "anyOf": [
                {"required": ["dynamic_update"]},
                {"required": ["soa_minimum_ttl_seconds"]},
            ],
            "additionalProperties": False,
            "examples": [
                {"zone": "example.test", "dynamic_update": "Secure"},
                {"zone": "example.test", "soa_minimum_ttl_seconds": 300},
            ],
        },
        response_schema={
            "type": "object",
            "properties": {
                **_ZONE_RESPONSE_PROPERTIES,
                "op_class": {
                    "type": "string",
                    "enum": ["write"],
                    "description": "Always 'write'.",
                },
            },
            "required": ["zone", "dynamic_update", "soa_minimum_ttl_seconds", "op_class"],
            "additionalProperties": True,
        },
        group_key="zone",
        tags=("write", "zone", "settings"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_call": (
                "Call while building a DNS server to set who may update names in a zone, "
                "or how long a 'name does not exist' answer is kept. " + SSH_TRANSPORT_NOTE
            ),
            "preconditions": (
                "A person must approve the call. The zone must be a primary zone. Secure "
                "updates need a zone stored in AD. Read the current values first with "
                "windns.zone.get so you can set them back."
            ),
            "parameter_hints": {
                "zone": "Required. The zone name.",
                "dynamic_update": "Optional. None, Secure or NonsecureAndSecure.",
                "soa_minimum_ttl_seconds": "Optional. 0 to 86400 seconds.",
            },
            "output_shape": "The windns.zone.get fields after the change, plus op_class 'write'.",
            "next_step": "Compare the returned values with what you asked for.",
        },
    ),
)
