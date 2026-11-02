# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared reads of the host security settings: advanced settings, firewall,
lockdown mode, SNMP and the core dump partition.

Used by the two reads (``vmware.host.advanced_settings.show``,
``vmware.host.firewall.show``) and the six changes in
:mod:`.composites._host_security_writes`, so a read and the read-back after a
change parse the host the same way.

Each setting sits on one of the host's config managers
(``HostSystem.configManager.<name>``, ``HostConfigManager`` in the pinned
``vi-json.yaml``):

* ``advancedOption`` -> ``OptionManager``: ``QueryOptions`` (values) and the
  ``supportedOption`` property (type, default, read-only flag);
* ``firewallSystem`` -> ``HostFirewallSystem.firewallInfo``;
* ``hostAccessManager`` -> ``HostAccessManager.lockdownMode`` and
  ``QueryLockdownExceptions``;
* ``snmpSystem`` -> ``HostSnmpSystem.configuration``;
* ``diagnosticSystem`` -> ``HostDiagnosticSystem.activePartition`` and
  ``QueryAvailablePartition``.

Every call goes through ``_post_vmomi_json`` (VI-JSON on vCenter). These
operations work through vCenter only: the SOAP path of a single ESXi host has
no builder for these methods yet, so a single-host target gets
``needs_vcenter`` before any call.

Secrets: SNMP community strings never leave this module (the answer counts
them), and an advanced setting that holds a secret shows ``<hidden>``
instead of its value (:func:`is_secret_key`).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.host_target import (
    HOST_FLAVOR_ESXI,
    classify_host_target,
)
from meho_backplane.connectors.vmware_rest.vim_body import (
    fault_type_name,
    retrieve_properties_body,
    unwrap_vim_value,
)
from meho_backplane.connectors.vmware_rest.vim_retrieve import RETRIEVE_PROPERTIES_PATH

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "HIDDEN_VALUE",
    "HOST_SECURITY_GROUP_KEY",
    "HOST_SECURITY_WHEN_TO_USE",
    "OP_QUERY_AVAILABLE_PARTITION",
    "OP_QUERY_LOCKDOWN_EXCEPTIONS",
    "OP_QUERY_OPTIONS",
    "OP_RETRIEVE_PROPERTIES",
    "PASSWORD_POLICY_KEYS",
    "HostSettingReadError",
    "call_query",
    "firewall_state",
    "is_secret_key",
    "locate",
    "option_definitions",
    "partition_row",
    "query_options",
    "read_property",
    "snmp_state",
]

#: The operation group of the two reads and the six changes, on both catalogs.
HOST_SECURITY_GROUP_KEY: Final = "vmware-host-security"

HOST_SECURITY_WHEN_TO_USE: Final = (
    "Use for the security and hardening settings of one ESXi host, through vCenter. "
    "Reads: the advanced settings, with the ones that differ from the default marked "
    "(vmware.host.advanced_settings.show), and the firewall rule sets with their ports "
    "and allowed IP addresses (vmware.host.firewall.show). Changes: set advanced "
    "settings (host.advanced_settings.set); turn a firewall rule set on or off or set "
    "its allowed IP addresses (host.firewall.ruleset.set); set lockdown mode "
    "(host.lockdown.set) and its exception users (host.lockdown.exceptions.set); set "
    "the SNMP agent (host.snmp.set); choose the core dump partition "
    "(host.coredump.set). Every change waits for approval."
)

#: VI-JSON method keys of the reads (the ``METHOD:/path`` form of ``vi-json.yaml``).
OP_RETRIEVE_PROPERTIES: Final = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"
OP_QUERY_OPTIONS: Final = "POST:/OptionManager/{moId}/QueryOptions"
OP_QUERY_LOCKDOWN_EXCEPTIONS: Final = "POST:/HostAccessManager/{moId}/QueryLockdownExceptions"
OP_QUERY_AVAILABLE_PARTITION: Final = "POST:/HostDiagnosticSystem/{moId}/QueryAvailablePartition"

#: Shown in place of the value of a setting that holds a secret.
HIDDEN_VALUE: Final = "<hidden>"
_SECRET_KEY = re.compile(r"passw|secret|token|community|private.?key", re.IGNORECASE)
#: A password policy setting names a password but holds a rule about
#: passwords, not one: ``Security.PasswordHistory``, ``Security.PasswordMaxDays``,
#: ``Security.PasswordQualityControl``.
_POLICY_KEY = re.compile(
    r"passw\w*(history|max|min|days|quality|control|policy|expir|length|lock|retry|complex|reuse)",
    re.IGNORECASE,
)
#: The password policy settings of ESXi. The answer of the change shows their
#: values (``register_plain_fields``): the default view of every answer would
#: hide them for their name.
PASSWORD_POLICY_KEYS: Final[tuple[str, ...]] = (
    "Security.PasswordHistory",
    "Security.PasswordMaxDays",
    "Security.PasswordQualityControl",
)

#: ``optionType._typeName`` -> the VI-JSON primitive type of the value.
_VALUE_TYPES: Final[dict[str, str]] = {
    "BoolOption": "boolean",
    "IntOption": "int",
    "LongOption": "long",
    "FloatOption": "float",
    "StringOption": "string",
    "ChoiceOption": "string",
}


class HostSettingReadError(RuntimeError):
    """vSphere could not read one property (it is in ``missingSet``).

    The message names the property and the fault class only.
    """


async def locate(
    connector: VmwareRestConnector,
    operator: Operator,
    target: Any,
    host: str | None,
    manager: str,
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """``(host moid, manager moid, None)``, or ``(None, None, refusal)``.

    The refusal is ``needs_vcenter`` on a single ESXi host target, or one of
    ``unsupported_host_target`` / ``host_required`` / ``host_not_found`` /
    ``ambiguous_host`` / ``config_manager_unreadable``.
    """
    # Imported here: ``typed_ops`` imports this module, and the composites
    # package imports ``typed_ops``, so a top-level import would be a cycle.
    from meho_backplane.connectors.vmware_rest.composites._host import (
        _resolve_host_and_manager,
    )

    flavor, _refusal = classify_host_target(target)
    if flavor == HOST_FLAVOR_ESXI:
        return (
            None,
            None,
            {
                "status": "needs_vcenter",
                "host": host,
                "guidance": "This operation works through a vCenter target only. Call it on "
                "the vCenter that manages the host.",
            },
        )
    host_moid, manager_moid, refusal = await _resolve_host_and_manager(
        connector, target, operator, host=host, prop=f"configManager.{manager}"
    )
    if refusal is not None:
        refusal.setdefault("guidance", _REFUSAL_TEXT.get(refusal.get("status", ""), None))
        return None, None, refusal
    return host_moid, manager_moid, None


_REFUSAL_TEXT: Final[dict[str, str]] = {
    "host_required": "Name the host (display name or moid).",
    "host_not_found": "vCenter has no host with this name or moid.",
    "ambiguous_host": "Several hosts have this name; call again with a moid from candidate_hosts.",
    "config_manager_unreadable": "vCenter does not offer this setting for the host. Nothing "
    "was changed.",
}


def _first_object(result: Any) -> dict[str, Any]:
    payload = unwrap_vim_value(result)
    if isinstance(payload, dict) and set(payload) == {"value"}:
        payload = payload["value"]
    objects = payload.get("objects", []) if isinstance(payload, dict) else payload
    if isinstance(objects, list):
        for obj in objects:
            if isinstance(obj, dict):
                return obj
    return {}


async def read_property(
    connector: VmwareRestConnector,
    operator: Operator,
    target: Any,
    mo_type: str,
    moid: str,
    prop: str,
) -> Any:
    """One property of one managed object, un-boxed; ``None`` when not set."""
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
            raise HostSettingReadError(f"{mo_type}.{prop} could not be read ({fault})")
    for entry in obj.get("propSet") or []:
        if isinstance(entry, dict) and entry.get("name") == prop:
            return entry.get("val")
    return None


async def call_query(
    connector: VmwareRestConnector, operator: Operator, target: Any, path: str
) -> Any:
    """Run one read method that takes no arguments (``QueryLockdownExceptions`` ...)."""
    result = await connector._post_vmomi_json(target, path, operator=operator, json={})
    return unwrap_vim_value(result)


async def query_options(
    connector: VmwareRestConnector,
    operator: Operator,
    target: Any,
    option_manager: str,
    name: str | None,
) -> dict[str, Any]:
    """``{key: value}`` from ``OptionManager.QueryOptions``.

    *name* ``None`` reads every option; a name ending in a dot reads that
    subtree; another name reads that one option (vSphere answers
    ``InvalidName`` when it does not exist).
    """
    body = {} if name is None else {"name": name}
    result = await connector._post_vmomi_json(
        target, f"/OptionManager/{option_manager}/QueryOptions", operator=operator, json=body
    )
    values = unwrap_vim_value(result)
    if isinstance(values, dict):
        values = [values]
    out: dict[str, Any] = {}
    for option in values if isinstance(values, list) else []:
        if isinstance(option, dict) and isinstance(option.get("key"), str):
            out[option["key"]] = option.get("value")
    return out


def is_secret_key(key: str, definition: dict[str, Any] | None = None) -> bool:
    """True when an advanced setting holds a secret.

    Its key names a password, secret, token or community, and it is not a
    password policy setting (``Security.PasswordHistory`` holds a count,
    ``Security.PasswordQualityControl`` a rule). With the setting's
    *definition* (:func:`option_definitions`), only a free-text setting can
    hold a secret: a number, a switch or a choice never does.
    """
    if definition is not None and (
        definition["value_type"] != "string" or definition["choices"] is not None
    ):
        return False
    return bool(_SECRET_KEY.search(key)) and not _POLICY_KEY.search(key)


def _default(option_type: dict[str, Any]) -> Any:
    if option_type.get("_typeName") == "ChoiceOption":
        choices = option_type.get("choiceInfo")
        index = option_type.get("defaultIndex")
        if isinstance(choices, list) and isinstance(index, int) and 0 <= index < len(choices):
            choice = choices[index]
            return choice.get("key") if isinstance(choice, dict) else None
        return None
    return option_type.get("defaultValue")


def _choices(option_type: dict[str, Any]) -> list[str] | None:
    raw = option_type.get("choiceInfo")
    if not isinstance(raw, list):
        return None
    return [c["key"] for c in raw if isinstance(c, dict) and isinstance(c.get("key"), str)]


def option_definitions(supported: Any) -> dict[str, dict[str, Any]]:
    """``{key: {value_type, default, read_only, min, max, choices, label}}``.

    Built from ``OptionManager.supportedOption``. ``value_type`` is the VI-JSON
    primitive type an ``UpdateOptions`` value must carry (``int``, ``long``,
    ``boolean``, ``float`` or ``string``); ``None`` for an unknown option type.
    """
    defs: dict[str, dict[str, Any]] = {}
    for item in supported if isinstance(supported, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            continue
        raw_type = item.get("optionType")
        option_type: dict[str, Any] = raw_type if isinstance(raw_type, dict) else {}
        defs[item["key"]] = {
            "value_type": _VALUE_TYPES.get(str(option_type.get("_typeName"))),
            "default": _default(option_type),
            "read_only": option_type.get("valueIsReadonly") is True,
            "min": option_type.get("min"),
            "max": option_type.get("max"),
            "choices": _choices(option_type),
            "label": item.get("label") if isinstance(item.get("label"), str) else None,
        }
    return defs


def _ip_list(raw: Any) -> dict[str, Any]:
    allowed = raw if isinstance(raw, dict) else {}
    addresses = allowed.get("ipAddress")
    networks = allowed.get("ipNetwork")
    return {
        "all_ip": allowed.get("allIp") is not False,
        "ip_addresses": [a for a in addresses if isinstance(a, str)]
        if isinstance(addresses, list)
        else [],
        "networks": [
            f"{n.get('network')}/{n.get('prefixLength')}"
            for n in (networks if isinstance(networks, list) else [])
            if isinstance(n, dict) and n.get("network") is not None
        ],
    }


def _rule(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "direction": raw.get("direction"),
        "protocol": raw.get("protocol"),
        "port": raw.get("port"),
        "end_port": raw.get("endPort"),
        "port_type": raw.get("portType"),
    }


def _ruleset_row(raw: dict[str, Any]) -> dict[str, Any]:
    rules = raw.get("rule")
    return {
        "key": raw.get("key"),
        "label": raw.get("label"),
        "enabled": raw.get("enabled") is True,
        "required": raw.get("required") is True,
        "service": raw.get("service"),
        "user_controllable": raw.get("userControllable") is not False,
        "ip_list_user_configurable": raw.get("ipListUserConfigurable") is not False,
        "allowed": _ip_list(raw.get("allowedHosts")),
        "rules": [_rule(r) for r in rules if isinstance(r, dict)]
        if isinstance(rules, list)
        else [],
    }


def firewall_state(info: Any) -> dict[str, Any]:
    """``{default_policy, rulesets}`` from ``HostFirewallSystem.firewallInfo``."""
    data = unwrap_vim_value(info)
    data = data if isinstance(data, dict) else {}
    raw_policy = data.get("defaultPolicy")
    policy: dict[str, Any] = raw_policy if isinstance(raw_policy, dict) else {}
    rulesets = data.get("ruleset")
    return {
        "default_policy": {
            "incoming_blocked": policy.get("incomingBlocked"),
            "outgoing_blocked": policy.get("outgoingBlocked"),
        },
        "rulesets": [_ruleset_row(r) for r in rulesets if isinstance(r, dict)]
        if isinstance(rulesets, list)
        else [],
    }


def snmp_state(configuration: Any) -> dict[str, Any]:
    """The SNMP agent settings. Community strings are counted, never shown."""
    raw = unwrap_vim_value(configuration)
    config: dict[str, Any] = raw if isinstance(raw, dict) else {}
    communities = config.get("readOnlyCommunities")
    traps = config.get("trapTargets")
    return {
        "enabled": config.get("enabled") is True,
        "port": config.get("port"),
        "read_only_community_count": len(
            [c for c in communities if isinstance(c, str) and c]
            if isinstance(communities, list)
            else []
        ),
        "trap_targets": [
            {"host_name": t.get("hostName"), "port": t.get("port")}
            for t in (traps if isinstance(traps, list) else [])
            if isinstance(t, dict) and t.get("hostName")
        ],
    }


def partition_row(raw: Any) -> dict[str, Any] | None:
    """``{disk_name, partition, storage_type, diagnostic_type, slots}`` of a partition."""
    data = unwrap_vim_value(raw)
    if not isinstance(data, dict):
        return None
    raw_id = data.get("id")
    ident: dict[str, Any] = raw_id if isinstance(raw_id, dict) else {}
    return {
        "disk_name": ident.get("diskName"),
        "partition": ident.get("partition"),
        "storage_type": data.get("storageType"),
        "diagnostic_type": data.get("diagnosticType"),
        "slots": data.get("slots"),
    }
