# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.host.config.show``: one read of an ESXi host's settings.

Returns, for one host, its NTP servers and the ``ntpd`` start policy, its DNS
servers, search domains, host name and domain name, the remote syslog
targets, the lockdown mode, the SNMP agent settings and the active core dump
partition. It first reads the host's config managers in one call, then one
property per manager, and ``Syslog.global.logHost`` with
``OptionManager.QueryOptions``. The helpers live in
:mod:`~meho_backplane.connectors.vmware_rest.host_config`, shared with the
three changes that set NTP, DNS and syslog.

Each part is read on its own. When one part fails, the others still come
back; the failed part is ``null`` and ``read_notes`` says why. SNMP community
strings are passwords: the answer only counts them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest.host_config import (
    CONFIG_MANAGER_TYPES,
    PROPERTY_BY_TYPE,
    SYSLOG_LOG_HOST_KEY,
    HostConfigReadError,
    core_dump_state,
    dns_state,
    ntp_state,
    query_option,
    read_config_managers,
    read_property,
    resolve_host,
    snmp_state,
    syslog_state,
)
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "HOST_CONFIG_GROUP_KEY",
    "HOST_CONFIG_WHEN_TO_USE",
    "TYPED_OPS",
    "VMWARE_HOST_CONFIG_SHOW_OP",
    "WHEN_TO_USE_BY_GROUP",
    "host_config_show_impl",
]

_log = structlog.get_logger(__name__)

HOST_CONFIG_GROUP_KEY = "vmware-host-config"

_SECTIONS = ("ntp", "dns", "syslog", "lockdown_mode", "snmp", "core_dump")
_READ_ERRORS = (httpx.HTTPError, RuntimeError)


def _note(section: str, exc: Exception) -> str:
    if isinstance(exc, HostConfigReadError):
        return f"{section}: {exc}"
    return f"{section}: read failed with {type(exc).__name__}: {exc}"


@dataclass(frozen=True)
class _Ctx:
    """What every part reader needs: the session, the target and the managers."""

    connector: VmwareRestConnector
    operator: Operator
    target: VsphereTargetLike
    managers: dict[str, str]

    async def prop(self, manager: str) -> Any:
        moid = self.managers.get(manager)
        if moid is None:
            raise HostConfigReadError(f"the host has no configManager.{manager}")
        mo_type = CONFIG_MANAGER_TYPES[manager]
        return await read_property(
            self.connector, self.operator, self.target, mo_type, moid, PROPERTY_BY_TYPE[mo_type]
        )


async def _read_ntp(ctx: _Ctx, notes: list[str]) -> Any:
    info = await ctx.prop("dateTimeSystem")
    try:
        services = await ctx.prop("serviceSystem")
    except _READ_ERRORS as exc:
        notes.append(_note("ntp service", exc))
        services = None
    return ntp_state(info, services)


async def _read_dns(ctx: _Ctx, notes: list[str]) -> Any:
    return dns_state(await ctx.prop("networkSystem"))


async def _read_syslog(ctx: _Ctx, notes: list[str]) -> Any:
    moid = ctx.managers.get("advancedOption")
    if moid is None:
        raise HostConfigReadError("the host has no configManager.advancedOption")
    value = await query_option(ctx.connector, ctx.operator, ctx.target, moid, SYSLOG_LOG_HOST_KEY)
    return syslog_state(value)


async def _read_lockdown(ctx: _Ctx, notes: list[str]) -> Any:
    mode = await ctx.prop("hostAccessManager")
    return mode if isinstance(mode, str) else None


async def _read_snmp(ctx: _Ctx, notes: list[str]) -> Any:
    return snmp_state(await ctx.prop("snmpSystem"))


async def _read_core_dump(ctx: _Ctx, notes: list[str]) -> Any:
    return core_dump_state(await ctx.prop("diagnosticSystem"))


_READERS: dict[str, Callable[[_Ctx, list[str]], Awaitable[Any]]] = {
    "ntp": _read_ntp,
    "dns": _read_dns,
    "syslog": _read_syslog,
    "lockdown_mode": _read_lockdown,
    "snmp": _read_snmp,
    "core_dump": _read_core_dump,
}


async def _read_sections(ctx: _Ctx) -> tuple[dict[str, Any], list[str]]:
    """Read each part on its own; a failed part is ``None`` with a note."""
    sections: dict[str, Any] = dict.fromkeys(_SECTIONS)
    notes: list[str] = []
    for section, reader in _READERS.items():
        try:
            sections[section] = await reader(ctx, notes)
        except _READ_ERRORS as exc:
            notes.append(_note(section, exc))
    return sections, notes


async def host_config_show_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.host.config.show``."""
    host_moid, refusal = await resolve_host(connector, operator, target, params.get("host"))
    empty: dict[str, Any] = {"host_name": None, **dict.fromkeys(_SECTIONS), "read_notes": []}
    if refusal is not None:
        return {**empty, **refusal}
    assert host_moid is not None  # refusal is None => the host resolved (mypy narrowing).
    try:
        managers = await read_config_managers(
            connector, operator, target, host_moid, tuple(CONFIG_MANAGER_TYPES)
        )
    except _READ_ERRORS as exc:
        return {
            **empty,
            "status": "config_manager_unreadable",
            "host": host_moid,
            "read_notes": [_note("configManager", exc)],
        }
    sections, notes = await _read_sections(_Ctx(connector, operator, target, managers))
    dns = sections["dns"]
    _log.info(
        "vmware_host_config_read",
        target=target.name,
        host=host_moid,
        failed_parts=len(notes),
    )
    return {
        "status": "ok",
        "host": host_moid,
        "host_name": dns["host_name"] if isinstance(dns, dict) else None,
        **sections,
        "read_notes": notes,
    }


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

_STR_LIST = {"type": "array", "items": {"type": "string"}}
_NULL_STR = {"type": ["string", "null"]}
_NULL_BOOL = {"type": ["boolean", "null"]}
_NULL_INT = {"type": ["integer", "null"]}

HOST_PARAMETER: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Host display name or moid, such as '<host-name>' or 'host-21'. MEHO tries the "
        "name first, then the moid. Required on a vCenter target."
    ),
}

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"host": HOST_PARAMETER},
    "required": [],
    "additionalProperties": False,
    "examples": [{"host": "<host-name>"}],
}

NTP_STATE_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "description": "NTP settings; null when this part could not be read.",
    "properties": {
        "servers": {**_STR_LIST, "description": "NTP servers (ntpConfig.server)."},
        "time_zone": {**_NULL_STR, "description": "Time zone name of the host."},
        "protocol": {**_NULL_STR, "description": "Clock protocol: ntp or ptp."},
        "service_policy": {
            **_NULL_STR,
            "description": "Start policy of ntpd: on, automatic or off.",
        },
        "service_running": {**_NULL_BOOL, "description": "Whether ntpd runs now."},
    },
}

DNS_STATE_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "description": "DNS and name settings; null when this part could not be read.",
    "properties": {
        "dhcp": {**_NULL_BOOL, "description": "True when DHCP sets DNS (then names are fixed)."},
        "host_name": {**_NULL_STR, "description": "Host name, without the domain."},
        "domain_name": {**_NULL_STR, "description": "Domain name."},
        "servers": {**_STR_LIST, "description": "DNS server addresses, in order."},
        "search_domains": {**_STR_LIST, "description": "DNS search domains, in order."},
    },
}

SYSLOG_STATE_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "description": "Remote syslog settings; null when this part could not be read.",
    "properties": {
        "log_host": {
            "type": "string",
            "description": "Raw value of Syslog.global.logHost; empty when none is set.",
        },
        "targets": {
            **_STR_LIST,
            "description": "The comma-separated targets, such as 'udp://<log-server>:514'.",
        },
    },
}

_SNMP_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "description": "SNMP agent settings; community strings are counted, never shown.",
    "properties": {
        "enabled": {**_NULL_BOOL, "description": "Whether the SNMP agent is on."},
        "port": {**_NULL_INT, "description": "UDP port of the agent."},
        "read_only_community_count": {
            "type": "integer",
            "description": "How many read-only communities are set.",
        },
        "trap_targets": {
            "type": "array",
            "description": "Where traps go (no community string).",
            "items": {
                "type": "object",
                "properties": {
                    "host_name": {**_NULL_STR, "description": "Trap receiver."},
                    "port": {**_NULL_INT, "description": "Trap receiver port."},
                },
            },
        },
    },
}

_CORE_DUMP_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "description": (
        "Active core dump partition. partition_active false means no partition is active; "
        "a core dump file may still be set (this API does not show files)."
    ),
    "properties": {
        "partition_active": {"type": "boolean", "description": "A partition is active."},
        "disk_name": {**_NULL_STR, "description": "Disk of the partition."},
        "partition": {**_NULL_INT, "description": "Partition number on the disk."},
        "storage_type": {**_NULL_STR, "description": "Storage type, such as directAttached."},
        "slots": {**_NULL_INT, "description": "Number of dump slots."},
    },
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "ok",
                "host_required",
                "host_not_found",
                "ambiguous_host",
                "unsupported_host_target",
                "config_manager_unreadable",
            ],
            "description": "ok: the host was read; the others: nothing was read.",
        },
        "host": {**_NULL_STR, "description": "Host moid, or the input on a refusal."},
        "host_name": {**_NULL_STR, "description": "Host name from the DNS settings."},
        "ntp": NTP_STATE_SCHEMA,
        "dns": DNS_STATE_SCHEMA,
        "syslog": SYSLOG_STATE_SCHEMA,
        "lockdown_mode": {
            **_NULL_STR,
            "description": "lockdownDisabled, lockdownNormal or lockdownStrict.",
        },
        "snmp": _SNMP_SCHEMA,
        "core_dump": _CORE_DUMP_SCHEMA,
        "read_notes": {
            **_STR_LIST,
            "description": "One line per part that could not be read, with the reason.",
        },
        "candidate_hosts": {**_STR_LIST, "description": "Host moids on ambiguous_host."},
        "target_product": {**_NULL_STR, "description": "Product on unsupported_host_target."},
        "guidance": {**_NULL_STR, "description": "What to do on a refusal."},
    },
    "required": ["status", "host", "read_notes"],
}

#: Curated ``when_to_use`` text of the group.
HOST_CONFIG_WHEN_TO_USE = (
    "Use to read one ESXi host's base settings in one call: NTP servers and whether "
    "ntpd runs, DNS servers, search domains, host name and domain name, the remote "
    "syslog targets, lockdown mode, the SNMP agent and the core dump partition "
    "(host.config.show, safe). To change NTP, DNS or syslog, use the 'host' group "
    "(host.ntp.set, host.dns.set, host.syslog.set)."
)

VMWARE_HOST_CONFIG_SHOW_OP = VmwareTypedOp(
    op_id="vmware.host.config.show",
    handler_attr="host_config_show",
    summary="One ESXi host's NTP, DNS, host name, syslog, lockdown, SNMP and core dump settings.",
    description=(
        "Reads one host's base settings in one call: NTP servers and ntpd state, DNS servers, "
        "search domains, host and domain name, remote syslog targets, lockdown mode, SNMP "
        "agent and core dump partition. A part that fails is null with a note. Read-only."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=_RESPONSE_SCHEMA,
    group_key=HOST_CONFIG_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "host", "ntp", "dns", "syslog"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to check how a host is set up: time (NTP), name and DNS, where its logs "
            "go (syslog), lockdown mode, SNMP and core dump. Call it before and after "
            "host.ntp.set, host.dns.set or host.syslog.set."
        ),
        "parameter_hints": {"host": "Host display name or moid, e.g. 'host-21'."},
        "output_shape": (
            "{status, host, host_name, ntp: {servers, time_zone, protocol, service_policy, "
            "service_running}, dns: {dhcp, host_name, domain_name, servers, search_domains}, "
            "syslog: {log_host, targets}, lockdown_mode, snmp: {enabled, port, "
            "read_only_community_count, trap_targets}, core_dump: {partition_active, "
            "disk_name, partition, storage_type, slots}, read_notes}. A part that could not "
            "be read is null and has a line in read_notes."
        ),
        "next_step": (
            "To change NTP, call vmware.composite.host.ntp.set; DNS or host name, "
            "vmware.composite.host.dns.set; syslog targets, vmware.composite.host.syslog.set. "
            "On ambiguous_host, call again with one of candidate_hosts."
        ),
    },
)

TYPED_OPS: tuple[VmwareTypedOp, ...] = (VMWARE_HOST_CONFIG_SHOW_OP,)
WHEN_TO_USE_BY_GROUP: dict[str, str] = {HOST_CONFIG_GROUP_KEY: HOST_CONFIG_WHEN_TO_USE}
