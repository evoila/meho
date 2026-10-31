# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Two reads of one ESXi host: its advanced settings and its firewall.

* ``vmware.host.advanced_settings.show`` -- ``OptionManager.QueryOptions``
  for the values (all, or one key prefix), and the ``supportedOption``
  property for each setting's type, default and read-only flag. Each row says
  whether the value differs from the default.
* ``vmware.host.firewall.show`` -- ``HostFirewallSystem.firewallInfo``: the
  default policy and one row per rule set (on or off, ports, allowed IP
  addresses).

Both read through vCenter on the connector's own session, so they work with
no spec ingest. The shared parsers are in :mod:`.host_security`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest.host_security import (
    HIDDEN_VALUE,
    HOST_SECURITY_GROUP_KEY,
    HostSettingReadError,
    firewall_state,
    is_secret_key,
    locate,
    option_definitions,
    query_options,
    read_property,
)
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp
from meho_backplane.connectors.vmware_rest.vim_body import fault_type_name, unwrap_vim_value

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "DEFAULT_POLICY",
    "HOST_PARAMETER",
    "HOST_SECURITY_TYPED_OPS",
    "KEY_PATTERN",
    "RULESET_ROW",
    "VMWARE_HOST_ADVANCED_SETTINGS_SHOW_OP",
    "VMWARE_HOST_FIREWALL_SHOW_OP",
    "advanced_settings_show_impl",
    "firewall_show_impl",
]

_READ_ERRORS = (httpx.HTTPError, RuntimeError)
#: A key or a key prefix: letters, digits, dots, dashes and underscores.
KEY_PATTERN = r"^[A-Za-z0-9_.\-]+$(?!\n)"


def _fault_class(exc: httpx.HTTPStatusError) -> str | None:
    try:
        return fault_type_name(unwrap_vim_value(exc.response.json()))
    except ValueError:
        return None


def _setting_row(key: str, value: Any, definition: dict[str, Any] | None) -> dict[str, Any]:
    known = definition is not None
    default = definition["default"] if definition else None
    row = {
        "key": key,
        "value": value,
        "default": default,
        "is_default": (value == default) if known and default is not None else None,
        "value_type": definition["value_type"] if definition else None,
        "read_only": definition["read_only"] if definition else None,
        "label": definition["label"] if definition else None,
    }
    if is_secret_key(key):
        row["value"] = HIDDEN_VALUE
        row["default"] = HIDDEN_VALUE
    return row


async def advanced_settings_show_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.host.advanced_settings.show``."""
    prefix = params.get("prefix")
    changed_only = bool(params.get("changed_only", False))
    empty: dict[str, Any] = {"prefix": prefix, "settings": [], "count": 0, "read_notes": []}
    host_moid, manager, refusal = await locate(
        connector, operator, target, params.get("host"), "advancedOption"
    )
    if refusal is not None:
        return {**empty, **refusal}
    assert manager is not None  # refusal is None => the manager resolved (mypy narrowing).
    try:
        values = await query_options(connector, operator, target, manager, prefix)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 500 or _fault_class(exc) != "InvalidName":
            raise
        return {
            **empty,
            "status": "no_such_setting",
            "host": host_moid,
            "guidance": f"The host has no setting or subtree named {prefix!r}. End a prefix "
            "with a dot, such as 'Syslog.'.",
        }
    notes: list[str] = []
    try:
        supported = await read_property(
            connector, operator, target, "OptionManager", manager, "supportedOption"
        )
        definitions = option_definitions(unwrap_vim_value(supported))
    except _READ_ERRORS as exc:
        definitions = {}
        notes.append(f"defaults: read failed with {type(exc).__name__}: {exc}")
    rows = [_setting_row(key, values[key], definitions.get(key)) for key in sorted(values)]
    if changed_only:
        rows = [row for row in rows if row["is_default"] is False]
    return {
        **empty,
        "status": "ok",
        "host": host_moid,
        "settings": rows,
        "count": len(rows),
        "read_notes": notes,
    }


async def firewall_show_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.host.firewall.show``."""
    wanted = params.get("ruleset")
    enabled_only = bool(params.get("enabled_only", False))
    empty: dict[str, Any] = {"default_policy": None, "rulesets": [], "count": 0}
    host_moid, manager, refusal = await locate(
        connector, operator, target, params.get("host"), "firewallSystem"
    )
    if refusal is not None:
        return {**empty, **refusal}
    assert manager is not None
    try:
        info = await read_property(
            connector, operator, target, "HostFirewallSystem", manager, "firewallInfo"
        )
    except HostSettingReadError as exc:
        return {**empty, "status": "firewall_unreadable", "host": host_moid, "guidance": str(exc)}
    state = firewall_state(info)
    rows = state["rulesets"]
    if wanted:
        rows = [row for row in rows if row["key"] == wanted]
    if enabled_only:
        rows = [row for row in rows if row["enabled"]]
    status = "ok" if rows or not wanted else "ruleset_not_found"
    return {
        "status": status,
        "host": host_moid,
        "default_policy": state["default_policy"],
        "rulesets": rows,
        "count": len(rows),
    }


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

_NULL_STR: dict[str, Any] = {"type": ["string", "null"]}
_STR_LIST: dict[str, Any] = {"type": "array", "items": {"type": "string"}}

HOST_PARAMETER: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 255,
    "description": (
        "Host display name or moid, such as '<host-name>' or '<host-moid>'. MEHO tries the "
        "name first, then the moid."
    ),
}

#: The refusals both reads share (nothing was read).
_REFUSALS = [
    "needs_vcenter",
    "host_required",
    "host_not_found",
    "ambiguous_host",
    "unsupported_host_target",
    "config_manager_unreadable",
]
_REFUSAL_FIELDS: dict[str, Any] = {
    "host": {**_NULL_STR, "description": "Host moid, or the input on a refusal."},
    "candidate_hosts": {**_STR_LIST, "description": "Host moids on ambiguous_host."},
    "guidance": {**_NULL_STR, "description": "What to do on a refusal."},
}

_SETTING_ROW: dict[str, Any] = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "description": "Setting name, such as 'Syslog.global.logDir'."},
        "value": {"description": "Current value; '<hidden>' when the key names a secret."},
        "default": {"description": "Default value; null when unknown."},
        "is_default": {
            "type": ["boolean", "null"],
            "description": "False when the value differs from the default; null when unknown.",
        },
        "value_type": {
            **_NULL_STR,
            "description": "boolean, int, long, float or string.",
        },
        "read_only": {"type": ["boolean", "null"], "description": "The value cannot be set."},
        "label": {**_NULL_STR, "description": "Short label from vSphere."},
    },
}

_ADVANCED_SETTINGS_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["ok", "no_such_setting", *_REFUSALS],
            "description": "ok: read. no_such_setting: no key or subtree with that name.",
        },
        "prefix": {**_NULL_STR, "description": "The prefix asked for."},
        "settings": {
            "type": "array",
            "items": _SETTING_ROW,
            "description": "One row per setting, sorted by key.",
        },
        "count": {"type": "integer", "description": "Number of rows."},
        "read_notes": {**_STR_LIST, "description": "Parts that could not be read."},
        **_REFUSAL_FIELDS,
    },
    "required": ["status", "settings"],
}

VMWARE_HOST_ADVANCED_SETTINGS_SHOW_OP = VmwareTypedOp(
    op_id="vmware.host.advanced_settings.show",
    handler_attr="host_advanced_settings_show",
    summary="Advanced settings of one ESXi host, with the ones that differ from the default.",
    description=(
        "Lists the advanced settings of one host: key, value, default, type and whether the "
        "value differs from the default. Filter by a key prefix such as 'Syslog.', or show "
        "only changed settings. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "host": HOST_PARAMETER,
            "prefix": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "pattern": KEY_PATTERN,
                "description": (
                    "One setting key, or a key prefix that ends with a dot (such as "
                    "'UserVars.'). Omit for all settings."
                ),
            },
            "changed_only": {
                "type": "boolean",
                "default": False,
                "description": "Only rows whose value differs from the default.",
            },
        },
        "required": ["host"],
        "additionalProperties": False,
        "examples": [{"host": "<host-name>", "prefix": "Syslog.", "changed_only": True}],
    },
    response_schema=_ADVANCED_SETTINGS_RESPONSE,
    group_key=HOST_SECURITY_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "host", "advanced-settings", "vi-json"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to read ESXi advanced settings, like 'govc host.option.ls' or "
            "Get-AdvancedSetting: one key, a subtree, or all; or to find which settings "
            "were changed from the default (changed_only)."
        ),
        "parameter_hints": {
            "prefix": "A key ('Syslog.global.logDir') or a subtree ending in a dot ('Net.').",
        },
        "output_shape": (
            "{status, host, prefix, settings: [{key, value, default, is_default, value_type, "
            "read_only, label}], count, read_notes}. All settings are about 1000 rows, so a "
            "long list comes back as a result handle."
        ),
        "next_step": (
            "To change a setting, call vmware.composite.host.advanced_settings.set with the "
            "key and a value of the row's value_type."
        ),
    },
)

_RULE_ROW: dict[str, Any] = {
    "type": "object",
    "properties": {
        "direction": {**_NULL_STR, "description": "inbound or outbound."},
        "protocol": {**_NULL_STR, "description": "tcp or udp."},
        "port": {"type": ["integer", "null"], "description": "Port, or first port of a range."},
        "end_port": {"type": ["integer", "null"], "description": "Last port of a range."},
        "port_type": {**_NULL_STR, "description": "src or dst."},
    },
}

_ALLOWED: dict[str, Any] = {
    "type": "object",
    "description": "Who may connect: all_ip true means any address.",
    "properties": {
        "all_ip": {"type": "boolean", "description": "Any IP address may connect."},
        "ip_addresses": {**_STR_LIST, "description": "Allowed single addresses."},
        "networks": {**_STR_LIST, "description": "Allowed networks, such as '<net>/24'."},
    },
}

RULESET_ROW: dict[str, Any] = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "description": "Rule set id, such as 'sshServer'."},
        "label": {**_NULL_STR, "description": "Display name."},
        "enabled": {"type": "boolean", "description": "The rule set is on."},
        "required": {"type": "boolean", "description": "Cannot be turned off."},
        "service": {**_NULL_STR, "description": "Host service the rule set belongs to."},
        "user_controllable": {"type": "boolean", "description": "Can be turned on or off."},
        "ip_list_user_configurable": {
            "type": "boolean",
            "description": "The allowed IP addresses can be set.",
        },
        "allowed": _ALLOWED,
        "rules": {"type": "array", "items": _RULE_ROW, "description": "Ports of the rule set."},
    },
}

DEFAULT_POLICY: dict[str, Any] = {
    "type": ["object", "null"],
    "description": "What the firewall does with traffic no rule set allows.",
    "properties": {
        "incoming_blocked": {"type": ["boolean", "null"], "description": "Blocks incoming."},
        "outgoing_blocked": {"type": ["boolean", "null"], "description": "Blocks outgoing."},
    },
}

VMWARE_HOST_FIREWALL_SHOW_OP = VmwareTypedOp(
    op_id="vmware.host.firewall.show",
    handler_attr="host_firewall_show",
    summary="Firewall rule sets of one ESXi host: on or off, ports and allowed IP addresses.",
    description=(
        "Lists the firewall rule sets of one host: on or off, required, ports and the "
        "allowed IP addresses, plus the default policy. Filter by one rule set or by the "
        "ones that are on. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "host": HOST_PARAMETER,
            "ruleset": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "pattern": KEY_PATTERN,
                "description": "One rule set id, such as 'sshServer'. Omit for all.",
            },
            "enabled_only": {
                "type": "boolean",
                "default": False,
                "description": "Only the rule sets that are on.",
            },
        },
        "required": ["host"],
        "additionalProperties": False,
        "examples": [{"host": "<host-name>", "enabled_only": True}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["ok", "ruleset_not_found", "firewall_unreadable", *_REFUSALS],
                "description": "ok: read. ruleset_not_found: no rule set with that id.",
            },
            "default_policy": DEFAULT_POLICY,
            "rulesets": {"type": "array", "items": RULESET_ROW, "description": "Rule sets."},
            "count": {"type": "integer", "description": "Number of rule sets."},
            **_REFUSAL_FIELDS,
        },
        "required": ["status", "rulesets"],
    },
    group_key=HOST_SECURITY_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "host", "firewall", "vi-json"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see the ESXi firewall, like 'esxcli network firewall ruleset list' or "
            "Get-VMHostFirewallException: which rule sets are on, their ports and who may "
            "connect."
        ),
        "parameter_hints": {"ruleset": "A rule set id from an earlier call, e.g. 'sshServer'."},
        "output_shape": (
            "{status, host, default_policy: {incoming_blocked, outgoing_blocked}, rulesets: "
            "[{key, label, enabled, required, service, user_controllable, "
            "ip_list_user_configurable, allowed: {all_ip, ip_addresses, networks}, rules: "
            "[{direction, protocol, port, end_port, port_type}]}], count}."
        ),
        "next_step": (
            "To turn a rule set on or off or limit who may connect, call "
            "vmware.composite.host.firewall.ruleset.set with its key."
        ),
    },
)

HOST_SECURITY_TYPED_OPS: tuple[VmwareTypedOp, ...] = (
    VMWARE_HOST_ADVANCED_SETTINGS_SHOW_OP,
    VMWARE_HOST_FIREWALL_SHOW_OP,
)
