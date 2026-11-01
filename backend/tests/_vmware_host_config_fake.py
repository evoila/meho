# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A fake vCenter for the host settings read and the NTP, DNS and syslog changes.

It answers the calls the operations make on the connector session: the
``GET /vcenter/host`` lookup, ``RetrievePropertiesEx`` for the host's config
managers and their properties, ``OptionManager.QueryOptions``, and the six
change methods, which change the stored settings so a read-back sees them.
Every name and address is made up.
"""

from __future__ import annotations

import copy
from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest._mount import adapt_filter_params

HOST = "host-21"
MANAGERS = {
    "dateTimeSystem": ("HostDateTimeSystem", "dateTimeSystem-21"),
    "serviceSystem": ("HostServiceSystem", "serviceSystem-21"),
    "networkSystem": ("HostNetworkSystem", "networkSystem-21"),
    "advancedOption": ("OptionManager", "EsxHostAdvSettings-21"),
    "hostAccessManager": ("HostAccessManager", "hostAccessManager-21"),
    "snmpSystem": ("HostSnmpSystem", "snmpSystem-21"),
    "diagnosticSystem": ("HostDiagnosticSystem", "diagnosticSystem-21"),
}
SECRET_COMMUNITY = "community-secret-value"


def _moref(mo_type: str, moid: str) -> dict[str, str]:
    return {"_typeName": "ManagedObjectReference", "type": mo_type, "value": moid}


def _service(key: str, running: bool, policy: str) -> dict[str, Any]:
    return {
        "_typeName": "HostService",
        "key": key,
        "label": key.upper(),
        "required": False,
        "uninstallable": False,
        "running": running,
        "policy": policy,
    }


class HostConfigVim:
    """Recording double for the connector session (VI-JSON and the host list)."""

    impl_id = "vmware-rest"
    version = "9.0"

    def __init__(self) -> None:
        self.hosts = [{"host": HOST, "name": "esxi-a"}]
        self.managers = dict(MANAGERS)
        self.ntp_servers = ["old-ntp.example.invalid"]
        self.services = {
            "ntpd": _service("ntpd", True, "off"),
            "snmpd": _service("snmpd", False, "off"),
        }
        self.dns: dict[str, Any] = {
            "_typeName": "HostDnsConfig",
            "dhcp": False,
            "hostName": "esxi-a",
            "domainName": "example.invalid",
            "address": ["old-dns.example.invalid"],
            "searchDomain": ["example.invalid"],
        }
        self.log_host = "udp://old-log.example.invalid:514"
        #: method name -> exception the call raises.
        self.fail: dict[str, Exception] = {}
        #: property paths answered in missingSet (vSphere could not read them).
        self.unreadable: set[str] = set()
        #: When False, change calls are accepted but change nothing.
        self.apply = True
        self.calls: list[tuple[str, Any]] = []

    # -- REST host lookup --------------------------------------------------
    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"/api{path}"

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        return adapt_filter_params("/api", query)

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        params = params or {}
        if params.get("names"):
            return [h for h in self.hosts if h["name"] in params["names"]]
        if params.get("hosts"):
            return [h for h in self.hosts if h["host"] in params["hosts"]]
        return list(self.hosts)

    # -- helpers for tests -------------------------------------------------
    def methods(self) -> list[str]:
        return [path.rsplit("/", 1)[-1] for path, _ in self.calls]

    def writes(self) -> list[str]:
        reads = {"RetrievePropertiesEx", "QueryOptions"}
        return [m for m in self.methods() if m not in reads]

    def body_of(self, method: str) -> Any:
        return next(body for path, body in self.calls if path.endswith(f"/{method}"))

    # -- VI-JSON -----------------------------------------------------------
    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: Any = None,
        promote_managed_object_not_found: bool = False,
        session_used: Any = None,
        session_token: str | None = None,
    ) -> Any:
        self.calls.append((path, copy.deepcopy(json)))
        _mo_type, _moid, method = path.strip("/").split("/")
        if method in self.fail:
            raise self.fail[method]
        if method == "RetrievePropertiesEx":
            return self._retrieve(json)
        if method == "QueryOptions":
            return [
                {
                    "_typeName": "OptionValue",
                    "key": json["name"],
                    "value": {"_typeName": "string", "_value": self.log_host},
                }
            ]
        if self.apply:
            self._change(method, json)
        return None

    def _change(self, method: str, body: Any) -> None:
        if method == "UpdateDateTimeConfig":
            self.ntp_servers = list(body["config"]["ntpConfig"]["server"])
        elif method == "UpdateServicePolicy":
            self.services[body["id"]]["policy"] = body["policy"]
        elif method in ("StartService", "RestartService"):
            self.services[body["id"]]["running"] = True
        elif method == "StopService":
            self.services[body["id"]]["running"] = False
        elif method == "UpdateDnsConfig":
            self.dns = copy.deepcopy(body["config"])
        elif method == "UpdateOptions":
            self.log_host = body["changedValue"][0]["value"]["_value"]

    def _value(self, mo_type: str, prop: str) -> Any:
        if mo_type == "HostDateTimeSystem":
            return {
                "_typeName": "HostDateTimeInfo",
                "timeZone": {"_typeName": "HostDateTimeSystemTimeZone", "name": "UTC"},
                "systemClockProtocol": "ntp",
                "ntpConfig": {
                    "_typeName": "HostNtpConfig",
                    "server": {"_typeName": "ArrayOfString", "_value": list(self.ntp_servers)},
                },
            }
        if mo_type == "HostServiceSystem":
            return {"_typeName": "HostServiceInfo", "service": list(self.services.values())}
        if mo_type == "HostNetworkSystem":
            return copy.deepcopy(self.dns)
        if mo_type == "HostAccessManager":
            return {"_typeName": "HostLockdownMode", "_value": "lockdownDisabled"}
        if mo_type == "HostSnmpSystem":
            return {
                "_typeName": "HostSnmpConfigSpec",
                "enabled": True,
                "port": 161,
                "readOnlyCommunities": [SECRET_COMMUNITY],
                "trapTargets": [
                    {
                        "_typeName": "HostSnmpDestination",
                        "hostName": "trap.example.invalid",
                        "port": 162,
                        "community": SECRET_COMMUNITY,
                    }
                ],
            }
        if mo_type == "HostDiagnosticSystem":
            return {
                "_typeName": "HostDiagnosticPartition",
                "storageType": "directAttached",
                "diagnosticType": "singleHost",
                "slots": 1,
                "id": {"_typeName": "HostScsiDiskPartition", "diskName": "disk-a", "partition": 7},
            }
        raise AssertionError(f"unexpected read {mo_type}.{prop}")

    def _retrieve(self, body: Any) -> dict[str, Any]:
        spec = body["specSet"][0]
        mo_type = spec["propSet"][0]["type"]
        paths = spec["propSet"][0]["pathSet"]
        moid = spec["objectSet"][0]["obj"]["value"]
        prop_set: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        for path in paths:
            if path in self.unreadable:
                missing.append({"path": path, "fault": {"_typeName": "NoPermission"}})
                continue
            if mo_type == "HostSystem":
                name = path.removeprefix("configManager.")
                if name in self.managers:
                    prop_set.append({"name": path, "val": _moref(*self.managers[name])})
                continue
            prop_set.append({"name": path, "val": self._value(mo_type, path)})
        entry: dict[str, Any] = {"obj": _moref(mo_type, moid), "propSet": prop_set}
        if missing:
            entry["missingSet"] = missing
        return {"objects": [entry]}
