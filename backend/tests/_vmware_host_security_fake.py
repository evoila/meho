# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A fake vCenter for the host security reads and changes.

It answers the calls the operations make on the connector session: the
``GET /vcenter/host`` lookup, ``RetrievePropertiesEx`` for the host's config
managers and their properties, the read methods (``QueryOptions``,
``QueryLockdownExceptions``, ``QueryAvailablePartition``), and the change
methods, which change the stored state so a read-back sees them. Every name,
address and secret is made up.
"""

from __future__ import annotations

import copy
from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest._mount import adapt_filter_params

HOST = "host-21"
MANAGERS = {
    "advancedOption": ("OptionManager", "EsxHostAdvSettings-21"),
    "firewallSystem": ("HostFirewallSystem", "firewallSystem-21"),
    "hostAccessManager": ("HostAccessManager", "hostAccessManager-21"),
    "snmpSystem": ("HostSnmpSystem", "snmpSystem-21"),
    "diagnosticSystem": ("HostDiagnosticSystem", "diagnosticSystem-21"),
}
OLD_COMMUNITY = "old-community-value"
SECRET_PASSWORD = "secret-password-value"
READS = {
    "RetrievePropertiesEx",
    "QueryOptions",
    "QueryLockdownExceptions",
    "QueryAvailablePartition",
}


def moref(mo_type: str, moid: str) -> dict[str, str]:
    return {"_typeName": "ManagedObjectReference", "type": mo_type, "value": moid}


def _option_def(key: str, option_type: dict[str, Any]) -> dict[str, Any]:
    return {
        "_typeName": "OptionDef",
        "key": key,
        "label": key,
        "summary": key,
        "optionType": option_type,
    }


def _partition(disk: str, number: int) -> dict[str, Any]:
    return {
        "_typeName": "HostDiagnosticPartition",
        "storageType": "directAttached",
        "diagnosticType": "singleHost",
        "slots": 1,
        "id": {"_typeName": "HostScsiDiskPartition", "diskName": disk, "partition": number},
    }


def _ruleset(key: str, enabled: bool, *, required: bool = False) -> dict[str, Any]:
    return {
        "_typeName": "HostFirewallRuleset",
        "key": key,
        "label": key.upper(),
        "required": required,
        "enabled": enabled,
        "service": None,
        "userControllable": not required,
        "ipListUserConfigurable": not required,
        "rule": [
            {
                "_typeName": "HostFirewallRule",
                "port": 22,
                "direction": "inbound",
                "portType": "dst",
                "protocol": "tcp",
            }
        ],
        "allowedHosts": {"_typeName": "HostFirewallRulesetIpList", "allIp": True},
    }


class HostSecurityVim:
    """Recording double for the connector session (VI-JSON and the host list)."""

    impl_id = "vmware-rest"
    version = "9.0"

    def __init__(self) -> None:
        self.hosts = [{"host": HOST, "name": "esxi-a"}]
        self.managers = dict(MANAGERS)
        #: key -> (VI-JSON type, value)
        self.options: dict[str, tuple[str, Any]] = {
            "Syslog.global.logDir": ("string", "[] /scratch/log"),
            "UserVars.SuppressShellWarning": ("long", 0),
            "Net.BlockGuestBPDU": ("int", 1),
            "Config.HostAgent.plugins.solo.enableMob": ("boolean", False),
            "UserVars.ExamplePassword": ("string", SECRET_PASSWORD),
            "Misc.ReadOnlyThing": ("string", "fixed"),
            # Password policy settings: they name a password but hold none.
            "Security.PasswordHistory": ("int", 0),
            "Security.PasswordQualityControl": ("string", "retry=3 min=disabled,disabled,7"),
        }
        self.definitions = [
            _option_def(
                "Syslog.global.logDir",
                {"_typeName": "StringOption", "defaultValue": "[] /scratch/log"},
            ),
            _option_def(
                "UserVars.SuppressShellWarning",
                {"_typeName": "LongOption", "min": 0, "max": 1, "defaultValue": 0},
            ),
            _option_def(
                "Net.BlockGuestBPDU",
                {"_typeName": "IntOption", "min": 0, "max": 1, "defaultValue": 0},
            ),
            _option_def(
                "Config.HostAgent.plugins.solo.enableMob",
                {"_typeName": "BoolOption", "supported": True, "defaultValue": False},
            ),
            _option_def(
                "UserVars.ExamplePassword", {"_typeName": "StringOption", "defaultValue": ""}
            ),
            _option_def(
                "Misc.ReadOnlyThing",
                {"_typeName": "StringOption", "defaultValue": "fixed", "valueIsReadonly": True},
            ),
            _option_def(
                "Security.PasswordHistory",
                {"_typeName": "IntOption", "min": 0, "max": 5, "defaultValue": 0},
            ),
            _option_def(
                "Security.PasswordQualityControl",
                {"_typeName": "StringOption", "defaultValue": "retry=3 min=disabled,disabled,7"},
            ),
        ]
        self.rulesets = [
            _ruleset("sshServer", False),
            _ruleset("vpxHeartbeats", True, required=True),
        ]
        self.lockdown = "lockdownDisabled"
        self.exceptions = ["svc-old"]
        self.snmp: dict[str, Any] = {
            "_typeName": "HostSnmpConfigSpec",
            "enabled": False,
            "port": 161,
            "readOnlyCommunities": [OLD_COMMUNITY],
            "trapTargets": [
                {
                    "_typeName": "HostSnmpDestination",
                    "hostName": "trap-old.example.invalid",
                    "port": 162,
                    "community": OLD_COMMUNITY,
                }
            ],
            "option": [{"_typeName": "KeyValue", "key": "syscontact", "value": "ops"}],
        }
        self.active: dict[str, Any] | None = _partition("disk-a", 7)
        self.available = [_partition("disk-a", 7), _partition("disk-b", 9)]
        #: method name -> exception the call raises.
        self.fail: dict[str, Exception] = {}
        #: When False, change calls are accepted but change nothing.
        self.apply = True
        #: When True, ``ReconfigureSnmpAgent`` applies everything but keeps the
        #: old communities (read-only and per trap target).
        self.keep_communities = False
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
        return [m for m in self.methods() if m not in READS]

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
        return self.answer(path, json)

    def answer(self, path: str, body: Any) -> Any:
        """The answer to one VI-JSON call (shared with the real-HTTP router)."""
        method = path.rsplit("/", 1)[-1]
        if method in self.fail:
            raise self.fail[method]
        if method == "RetrievePropertiesEx":
            return self._retrieve(body)
        if method == "QueryOptions":
            return self._query_options(body.get("name"))
        if method == "QueryLockdownExceptions":
            return list(self.exceptions)
        if method == "QueryAvailablePartition":
            return copy.deepcopy(self.available)
        if self.apply:
            self._change(method, body)
        return None

    def _query_options(self, name: str | None) -> list[dict[str, Any]]:
        keys = [
            k
            for k in self.options
            if name is None or k == name or (name.endswith(".") and k.startswith(name))
        ]
        return [
            {
                "_typeName": "OptionValue",
                "key": k,
                "value": {"_typeName": self.options[k][0], "_value": self.options[k][1]},
            }
            for k in keys
        ]

    def _change(self, method: str, body: Any) -> None:
        if method == "UpdateOptions":
            for item in body["changedValue"]:
                self.options[item["key"]] = (item["value"]["_typeName"], item["value"]["_value"])
        elif method in ("EnableRuleset", "DisableRuleset"):
            row = next(r for r in self.rulesets if r["key"] == body["id"])
            row["enabled"] = method == "EnableRuleset"
        elif method == "UpdateRuleset":
            row = next(r for r in self.rulesets if r["key"] == body["id"])
            row["allowedHosts"] = copy.deepcopy(body["spec"]["allowedHosts"])
        elif method == "ChangeLockdownMode":
            self.lockdown = body["mode"]
        elif method == "UpdateLockdownExceptions":
            self.exceptions = list(body["users"])
        elif method == "ReconfigureSnmpAgent":
            old_communities = self.snmp["readOnlyCommunities"]
            old_trap_communities = [t["community"] for t in self.snmp["trapTargets"]]
            self.snmp = copy.deepcopy(body["spec"])
            if self.keep_communities:
                self.snmp["readOnlyCommunities"] = old_communities
                for trap, community in zip(
                    self.snmp["trapTargets"], old_trap_communities, strict=False
                ):
                    trap["community"] = community
        elif method == "SelectActivePartition":
            part = body["partition"]
            self.active = _partition(part["diskName"], part["partition"])

    def _value(self, mo_type: str, prop: str) -> Any:
        if (mo_type, prop) == ("OptionManager", "supportedOption"):
            return {"_typeName": "ArrayOfOptionDef", "_value": copy.deepcopy(self.definitions)}
        if (mo_type, prop) == ("HostFirewallSystem", "firewallInfo"):
            return {
                "_typeName": "HostFirewallInfo",
                "defaultPolicy": {
                    "_typeName": "HostFirewallDefaultPolicy",
                    "incomingBlocked": True,
                    "outgoingBlocked": True,
                },
                "ruleset": copy.deepcopy(self.rulesets),
            }
        if (mo_type, prop) == ("HostAccessManager", "lockdownMode"):
            return {"_typeName": "HostLockdownMode", "_value": self.lockdown}
        if (mo_type, prop) == ("HostSnmpSystem", "configuration"):
            return copy.deepcopy(self.snmp)
        if (mo_type, prop) == ("HostDiagnosticSystem", "activePartition"):
            return copy.deepcopy(self.active)
        raise AssertionError(f"unexpected read {mo_type}.{prop}")

    def _retrieve(self, body: Any) -> dict[str, Any]:
        spec = body["specSet"][0]
        mo_type = spec["propSet"][0]["type"]
        paths = spec["propSet"][0]["pathSet"]
        moid = spec["objectSet"][0]["obj"]["value"]
        prop_set: list[dict[str, Any]] = []
        for path in paths:
            if mo_type == "HostSystem":
                name = path.removeprefix("configManager.")
                if name in self.managers:
                    prop_set.append({"name": path, "val": moref(*self.managers[name])})
                continue
            value = self._value(mo_type, path)
            if value is not None:
                prop_set.append({"name": path, "val": value})
        return {"objects": [{"obj": moref(mo_type, moid), "propSet": prop_set}]}
