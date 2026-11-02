# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read the time, name and logging settings of one ESXi host.

Shared by the read ``vmware.host.config.show`` and the three changes
``vmware.composite.host.ntp.set`` / ``.dns.set`` / ``.syslog.set``, so the
read and the read-back after a change parse the host the same way.

The settings sit on the host's config managers, not on ``HostSystem``
itself. Each manager is a ``ManagedObjectReference`` under
``HostSystem.configManager`` (``HostConfigManager`` in the pinned
``vi-json.yaml``):

* ``dateTimeSystem`` -> ``HostDateTimeSystem.dateTimeInfo`` (NTP servers,
  time zone, clock protocol);
* ``serviceSystem`` -> ``HostServiceSystem.serviceInfo`` (the ``ntpd``
  start policy and whether it runs);
* ``networkSystem`` -> ``HostNetworkSystem.dnsConfig`` (DNS servers, search
  domains, host name, domain name);
* ``advancedOption`` -> ``OptionManager.QueryOptions`` for
  ``Syslog.global.logHost``;
* ``hostAccessManager`` -> ``HostAccessManager.lockdownMode``;
* ``snmpSystem`` -> ``HostSnmpSystem.configuration``;
* ``diagnosticSystem`` -> ``HostDiagnosticSystem.activePartition``.

Every read goes through ``_post_vmomi_json`` (VI-JSON on vCenter). SNMP
community strings are passwords, so they never leave this module: the
answer only counts them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.host_target import (
    HOST_FLAVOR_ESXI,
    STANDALONE_ESXI_HOST_MOID,
    classify_host_target,
)
from meho_backplane.connectors.vmware_rest.typed_ops import _unwrap_value
from meho_backplane.connectors.vmware_rest.typed_ops_host_storage_devices import (
    _int_or_none,
    _list_host_moids,
    _moref_value,
)
from meho_backplane.connectors.vmware_rest.vim_body import (
    fault_type_name,
    retrieve_properties_body,
    unwrap_vim_value,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "CONFIG_MANAGER_TYPES",
    "HOST_SYSTEM_MO_TYPE",
    "NTP_SERVICE_KEY",
    "OP_QUERY_OPTIONS",
    "OP_RETRIEVE_PROPERTIES",
    "PROPERTY_BY_TYPE",
    "SYSLOG_LOG_HOST_KEY",
    "HostConfigReadError",
    "core_dump_state",
    "dns_state",
    "find_service",
    "ntp_state",
    "query_option",
    "read_config_managers",
    "read_property",
    "resolve_host",
    "snmp_state",
    "syslog_state",
]

HOST_SYSTEM_MO_TYPE: Final = "HostSystem"
RETRIEVE_PROPERTIES_PATH: Final = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"

#: ``HostConfigManager`` field -> the managed object type it refers to.
CONFIG_MANAGER_TYPES: Final[dict[str, str]] = {
    "dateTimeSystem": "HostDateTimeSystem",
    "serviceSystem": "HostServiceSystem",
    "networkSystem": "HostNetworkSystem",
    "advancedOption": "OptionManager",
    "hostAccessManager": "HostAccessManager",
    "snmpSystem": "HostSnmpSystem",
    "diagnosticSystem": "HostDiagnosticSystem",
}

#: Managed object type -> the one property the read takes from it.
PROPERTY_BY_TYPE: Final[dict[str, str]] = {
    "HostDateTimeSystem": "dateTimeInfo",
    "HostServiceSystem": "serviceInfo",
    "HostNetworkSystem": "dnsConfig",
    "HostAccessManager": "lockdownMode",
    "HostSnmpSystem": "configuration",
    "HostDiagnosticSystem": "activePartition",
}

#: The advanced option that holds the remote syslog targets.
SYSLOG_LOG_HOST_KEY: Final = "Syslog.global.logHost"
#: ``HostService.key`` of the NTP daemon.
NTP_SERVICE_KEY: Final = "ntpd"

#: VI-JSON method keys of the reads (the ``METHOD:/path`` form of ``vi-json.yaml``).
OP_RETRIEVE_PROPERTIES: Final = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"
OP_QUERY_OPTIONS: Final = "POST:/OptionManager/{moId}/QueryOptions"


class HostConfigReadError(RuntimeError):
    """vSphere answered, but could not read one property (a ``missingSet`` fault).

    The message names the property and the fault class only, never fault text.
    """


async def resolve_host(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    host: str | None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Find the ``HostSystem`` moid, or return a refusal ``{status, host, ...}``.

    On a vCenter target *host* is a display name (tried first) or a moid,
    looked up with ``GET /vcenter/host``. Several hosts with that name give
    ``ambiguous_host`` with ``candidate_hosts``. A standalone ESXi target is
    its own host (``ha-host``).
    """
    flavor, refusal = classify_host_target(target)
    if refusal is not None:
        return None, {**refusal, "host": host}
    if flavor == HOST_FLAVOR_ESXI:
        return STANDALONE_ESXI_HOST_MOID, None
    if not host:
        return None, {
            "status": "host_required",
            "host": host,
            "guidance": "On a vCenter target, name the host (display name or moid).",
        }
    by_name = await _list_host_moids(connector, operator, target, {"filter.names": [host]})
    if len(by_name) == 1:
        return by_name[0], None
    if len(by_name) > 1:
        return None, {"status": "ambiguous_host", "host": host, "candidate_hosts": by_name}
    by_moid = await _list_host_moids(connector, operator, target, {"filter.hosts": [host]})
    if len(by_moid) == 1:
        return by_moid[0], None
    return None, {"status": "host_not_found", "host": host}


def _first_object(result: Any) -> dict[str, Any]:
    payload = _unwrap_value(result)
    objects = payload.get("objects", []) if isinstance(payload, dict) else payload
    if isinstance(objects, list):
        for obj in objects:
            if isinstance(obj, dict):
                return obj
    return {}


def _props(obj: dict[str, Any]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for entry in obj.get("propSet") or []:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            props[entry["name"]] = unwrap_vim_value(entry.get("val"))
    return props


async def read_config_managers(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    host_moid: str,
    names: tuple[str, ...],
) -> dict[str, str]:
    """Read ``configManager.<name>`` for each of *names* in one call.

    Returns ``{name: manager moid}`` for the managers the host has. A manager
    the host does not offer (or vSphere could not read) is left out.
    """
    paths = [f"configManager.{name}" for name in names]
    result = await connector._post_vmomi_json(
        target,
        RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=retrieve_properties_body(HOST_SYSTEM_MO_TYPE, [host_moid], paths),
    )
    props = _props(_first_object(result))
    managers: dict[str, str] = {}
    for name in names:
        moid = _moref_value(props.get(f"configManager.{name}"))
        if moid is not None:
            managers[name] = moid
    return managers


async def read_property(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    mo_type: str,
    moid: str,
    prop: str,
) -> Any:
    """Read one property of one managed object; ``None`` when it is not set.

    Raises :class:`HostConfigReadError` when vSphere could not read it (the
    property is in ``missingSet``). A transport error is raised unchanged.
    """
    result = await connector._post_vmomi_json(
        target,
        RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=retrieve_properties_body(mo_type, [moid], [prop]),
    )
    obj = _first_object(result)
    for entry in obj.get("missingSet") or []:
        if isinstance(entry, dict) and entry.get("path") == prop:
            fault = fault_type_name(entry.get("fault")) or "unknown fault"
            raise HostConfigReadError(f"{mo_type}.{prop} could not be read ({fault})")
    return _props(obj).get(prop)


async def query_option(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    option_manager_moid: str,
    key: str,
) -> Any:
    """The value of one advanced option (``OptionManager.QueryOptions``), or ``None``."""
    result = await connector._post_vmomi_json(
        target,
        f"/OptionManager/{option_manager_moid}/QueryOptions",
        operator=operator,
        json={"name": key},
    )
    values = unwrap_vim_value(_unwrap_value(result))
    if isinstance(values, dict):
        values = [values]
    if not isinstance(values, list):
        return None
    for option in values:
        if isinstance(option, dict) and option.get("key") == key:
            return option.get("value")
    return None


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def find_service(service_info: Any, key: str) -> dict[str, Any] | None:
    """``{key, label, running, policy}`` of service *key* in ``serviceInfo``, or ``None``."""
    services = service_info.get("service") if isinstance(service_info, dict) else None
    if isinstance(services, dict):
        services = [services]
    for service in services if isinstance(services, list) else []:
        if isinstance(service, dict) and service.get("key") == key:
            return {
                "key": key,
                "label": _str_or_none(service.get("label")),
                "running": _bool_or_none(service.get("running")),
                "policy": _str_or_none(service.get("policy")),
            }
    return None


def ntp_state(date_time_info: Any, service_info: Any | None) -> dict[str, Any]:
    """The NTP part of the answer from ``dateTimeInfo`` and the ``ntpd`` service."""
    info = date_time_info if isinstance(date_time_info, dict) else {}
    raw_ntp = info.get("ntpConfig")
    ntp_config: dict[str, Any] = raw_ntp if isinstance(raw_ntp, dict) else {}
    time_zone = info.get("timeZone")
    service = find_service(service_info, NTP_SERVICE_KEY) if service_info is not None else None
    return {
        "servers": _str_list(ntp_config.get("server")),
        "time_zone": _str_or_none(time_zone.get("name")) if isinstance(time_zone, dict) else None,
        "protocol": _str_or_none(info.get("systemClockProtocol")),
        "service_policy": service["policy"] if service else None,
        "service_running": service["running"] if service else None,
    }


def dns_state(dns_config: Any) -> dict[str, Any]:
    """The DNS part of the answer from ``HostNetworkSystem.dnsConfig``."""
    config = dns_config if isinstance(dns_config, dict) else {}
    return {
        "dhcp": _bool_or_none(config.get("dhcp")),
        "host_name": _str_or_none(config.get("hostName")),
        "domain_name": _str_or_none(config.get("domainName")),
        "servers": _str_list(config.get("address")),
        "search_domains": _str_list(config.get("searchDomain")),
    }


def syslog_state(log_host: Any) -> dict[str, Any]:
    """The syslog part: the raw option value and its comma-separated targets."""
    text = log_host if isinstance(log_host, str) else ""
    return {
        "log_host": text,
        "targets": [part.strip() for part in text.split(",") if part.strip()],
    }


def snmp_state(configuration: Any) -> dict[str, Any]:
    """The SNMP part. Community strings are counted, never shown."""
    config = configuration if isinstance(configuration, dict) else {}
    traps = config.get("trapTargets")
    if isinstance(traps, dict):
        traps = [traps]
    trap_rows = [
        {"host_name": _str_or_none(t.get("hostName")), "port": _int_or_none(t.get("port"))}
        for t in (traps if isinstance(traps, list) else [])
        if isinstance(t, dict)
    ]
    return {
        "enabled": _bool_or_none(config.get("enabled")),
        "port": _int_or_none(config.get("port")),
        "read_only_community_count": len(_str_list(config.get("readOnlyCommunities"))),
        "trap_targets": trap_rows,
    }


def core_dump_state(partition: Any) -> dict[str, Any]:
    """The core dump part from ``HostDiagnosticSystem.activePartition``."""
    if not isinstance(partition, dict):
        return {
            "partition_active": False,
            "disk_name": None,
            "partition": None,
            "storage_type": None,
            "slots": None,
        }
    raw_id = partition.get("id")
    ident: dict[str, Any] = raw_id if isinstance(raw_id, dict) else {}
    return {
        "partition_active": True,
        "disk_name": _str_or_none(ident.get("diskName")),
        "partition": _int_or_none(ident.get("partition")),
        "storage_type": _str_or_none(partition.get("storageType")),
        "slots": _int_or_none(partition.get("slots")),
    }
