# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameters, answers and AI guidance of four host security changes.

Advanced settings, firewall rule set, lockdown mode and lockdown exception
users. The SNMP and core dump changes are in :mod:`._host_access_schemas`.
The handlers are in :mod:`._host_security_writes` and
:mod:`._host_access_writes`.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.typed_ops_host_security import (
    HOST_PARAMETER,
    KEY_PATTERN,
)

__all__ = [
    "ADVANCED_SETTINGS_SET",
    "FIREWALL_RULESET_SET",
    "LOCKDOWN_EXCEPTIONS_SET",
    "LOCKDOWN_SET",
    "NEXT_STEP_TAIL",
    "NULL_STR",
    "op_parts",
    "response_schema",
]

#: A user name, optionally with a domain ('DOMAIN\\user' or 'user@domain').
_USER_PATTERN: Final = r"^[A-Za-z0-9_.@\\\-]+$(?!\n)"

NULL_STR: Final = {"type": ["string", "null"]}
_ISSUES: Final = {
    "type": "array",
    "description": "Problems; an entry with severity error marks the call as failed.",
    "items": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "Where the problem is."},
            "severity": {"type": "string", "description": "error, warning or info."},
            "message": {"type": "string", "description": "What happened."},
        },
    },
}
_COMMON_STATUSES: Final = [
    "applied",
    "unchanged",
    "failed",
    "not_verified",
    "needs_vcenter",
    "host_required",
    "host_not_found",
    "ambiguous_host",
    "unsupported_host_target",
    "config_manager_unreadable",
]
_STATUS_TEXT: Final = (
    "applied: changed and read back. unchanged: the host already had it, nothing sent. "
    "failed: vSphere refused a call (see fault). not_verified: the read-back does not show "
    "the change. Any other status: nothing was sent."
)
NEXT_STEP_TAIL: Final = (
    "Safe to run twice: unchanged sends nothing. On failed, read fault. On not_verified, "
    "read the setting again."
)


def response_schema(
    state: dict[str, Any],
    extra_statuses: list[str],
    extra_properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The answer of a change: status, host, before, after, steps, fault, guidance."""
    return {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": [*_COMMON_STATUSES, *extra_statuses],
                "description": _STATUS_TEXT,
            },
            "host": {**NULL_STR, "description": "Host moid, or the input on a refusal."},
            "requested": {"type": ["object", "null"], "description": "What the call asked for."},
            "before": {**state, "description": "The setting before the change."},
            "after": {**state, "description": "The setting read back after the change."},
            "applied_steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The vSphere calls that ran, in order.",
            },
            "fault": {**NULL_STR, "description": "vSphere fault class and text on failed."},
            "candidate_hosts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Host moids on ambiguous_host.",
            },
            "guidance": {**NULL_STR, "description": "What happened and what to do next."},
            "issues": _ISSUES,
            **(extra_properties or {}),
        },
        "required": ["status", "host"],
    }


def op_parts(
    *,
    parameters: dict[str, Any],
    response: dict[str, Any],
    llm: dict[str, Any],
) -> dict[str, Any]:
    """The three schema parts of one registry row."""
    return {"parameter_schema": parameters, "response_schema": response, "llm_instructions": llm}


# ---------------------------------------------------------------------------
# host.advanced_settings.set
# ---------------------------------------------------------------------------

_SETTINGS_STATE: Final = {
    "type": ["object", "null"],
    "description": "Value per key; '<hidden>' for a key that names a secret.",
    "additionalProperties": True,
}

ADVANCED_SETTINGS_SET: Final[dict[str, Any]] = op_parts(
    parameters={
        "type": "object",
        "properties": {
            "host": HOST_PARAMETER,
            "settings": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "description": "The settings to set. Settings not named keep their value.",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 255,
                            "pattern": KEY_PATTERN,
                            "description": "Setting key, such as 'UserVars.SuppressShellWarning'.",
                        },
                        "value": {
                            "anyOf": [
                                {"type": "boolean"},
                                {"type": "integer"},
                                {"type": "number"},
                                {"type": "string", "maxLength": 4096},
                            ],
                            "description": (
                                "New value, of the setting's value_type from "
                                "vmware.host.advanced_settings.show. Not for a setting that "
                                "holds a secret: give value_secret_ref instead."
                            ),
                        },
                        "value_secret_ref": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 512,
                            "description": (
                                "Vault path of the secret that holds the new value. Required "
                                "for a setting whose key names a password, secret, token or "
                                "community; allowed for any string setting. Give value or "
                                "value_secret_ref, not both. Never put the secret itself here."
                            ),
                        },
                        "value_secret_mount": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                            "description": (
                                "Vault KV version 2 mount of the secret (default 'secret')."
                            ),
                        },
                        "value_secret_key": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                            "description": (
                                "Key in the secret that holds the value (default 'value')."
                            ),
                        },
                    },
                    "required": ["key"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["host", "settings"],
        "additionalProperties": False,
        "examples": [
            {"host": "<host-name>", "settings": [{"key": "Example.Setting.Key", "value": 1}]},
            {
                "host": "<host-name>",
                "settings": [
                    {
                        "key": "Example.Setting.Password",
                        "value_secret_ref": "<vault-path-of-the-value>",
                    }
                ],
            },
        ],
    },
    response=response_schema(
        _SETTINGS_STATE,
        [
            "unknown_setting",
            "read_only_setting",
            "invalid_value",
            "secret_value_required",
            "value_secret_unusable",
        ],
        {
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The keys that stopped the call on a refusal.",
            }
        },
    ),
    llm={
        "when_to_call": (
            "Call to set ESXi advanced settings, like 'govc host.option.set' or "
            "Set-AdvancedSetting."
        ),
        "preconditions": (
            "Read the settings first with vmware.host.advanced_settings.show to learn each "
            "key's value_type and range. MEHO checks every key before it sends anything: an "
            "unknown key, a read-only setting, a wrong value type or a key given twice stops "
            "the whole call. A setting that holds a secret (its key names a password, secret, "
            "token or community) takes its value from Vault: store it first and give "
            "value_secret_ref; a plain value for it is refused. "
            "Some settings need a host reboot to take effect. The change waits for approval."
        ),
        "parameter_hints": {
            "settings": (
                "e.g. [{'key': '<setting-key>', 'value': <value of its value_type>}] or "
                "[{'key': '<secret-setting-key>', 'value_secret_ref': '<vault-path>'}]."
            ),
        },
        "output_shape": (
            "{status: applied|unchanged|unknown_setting|read_only_setting|invalid_value|"
            "secret_value_required|value_secret_unusable|failed|not_verified|<host refusal>, "
            "host, requested: {settings}, before and after: {<key>: <value>}, keys (on a "
            "refusal), applied_steps, fault, guidance, issues}. A secret setting shows "
            "'<hidden>' instead of its value."
        ),
        "next_step": NEXT_STEP_TAIL,
    },
)
# ---------------------------------------------------------------------------
# host.firewall.ruleset.set
# ---------------------------------------------------------------------------

_ALLOWED_STATE: Final = {
    "type": "object",
    "description": "Who may connect.",
    "properties": {
        "all_ip": {"type": "boolean", "description": "Any IP address may connect."},
        "ip_addresses": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Allowed single addresses.",
        },
        "networks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Allowed networks, such as '<net>/24'.",
        },
    },
}
_RULESET_STATE: Final = {
    "type": ["object", "null"],
    "description": "One rule set.",
    "properties": {
        "key": {"type": "string", "description": "Rule set id."},
        "enabled": {"type": "boolean", "description": "The rule set is on."},
        "allowed": _ALLOWED_STATE,
        "required": {"type": "boolean", "description": "Cannot be turned off."},
        "user_controllable": {"type": "boolean", "description": "Can be turned on or off."},
        "ip_list_user_configurable": {
            "type": "boolean",
            "description": "The allowed IP addresses can be set.",
        },
    },
}

FIREWALL_RULESET_SET: Final[dict[str, Any]] = op_parts(
    parameters={
        "type": "object",
        "properties": {
            "host": HOST_PARAMETER,
            "ruleset": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "pattern": KEY_PATTERN,
                "description": "Rule set id from vmware.host.firewall.show, such as 'sshServer'.",
            },
            "enabled": {"type": "boolean", "description": "true turns it on, false turns it off."},
            "allowed": {
                "type": "object",
                "description": "Who may connect. Replaces the current list.",
                "properties": {
                    "all_ip": {
                        "type": "boolean",
                        "description": "true: any address may connect (leave the lists empty).",
                    },
                    "ip_addresses": {
                        "type": "array",
                        "maxItems": 64,
                        "items": {
                            "type": "string",
                            "maxLength": 45,
                            "description": "One IPv4 or IPv6 address.",
                        },
                        "description": "Allowed single addresses.",
                    },
                    "networks": {
                        "type": "array",
                        "maxItems": 64,
                        "items": {
                            "type": "string",
                            "maxLength": 50,
                            "description": "One network with its prefix, such as '<net>/24'.",
                        },
                        "description": "Allowed networks.",
                    },
                },
                "required": ["all_ip"],
                "additionalProperties": False,
            },
        },
        "required": ["host", "ruleset"],
        "anyOf": [{"required": ["enabled"]}, {"required": ["allowed"]}],
        "additionalProperties": False,
        "examples": [
            {
                "host": "<host-name>",
                "ruleset": "exampleRuleset",
                "enabled": True,
                "allowed": {"all_ip": False, "networks": ["<network>/24"]},
            }
        ],
    },
    response=response_schema(
        _RULESET_STATE,
        [
            "ruleset_not_found",
            "ruleset_required",
            "not_user_controllable",
            "ip_list_not_configurable",
            "invalid_value",
        ],
    ),
    llm={
        "when_to_call": (
            "Call to turn one ESXi firewall rule set on or off, or to limit which addresses "
            "may connect to it, like 'esxcli network firewall ruleset set' or "
            "Set-VMHostFirewallException."
        ),
        "preconditions": (
            "Read the rule set first with vmware.host.firewall.show. A required rule set is "
            "never turned off. Limiting the allowed addresses of a rule set vCenter uses can "
            "cut the host off from vCenter: keep vCenter's address in the list. The change "
            "waits for approval."
        ),
        "parameter_hints": {
            "allowed": "{'all_ip': false, 'ip_addresses': ['<ip>'], 'networks': ['<net>/24']}.",
        },
        "output_shape": (
            "{status: applied|unchanged|ruleset_not_found|ruleset_required|"
            "not_user_controllable|ip_list_not_configurable|invalid_value|failed|not_verified|"
            "<host refusal>, host, requested, before and after: {key, enabled, allowed: "
            "{all_ip, ip_addresses, networks}, required, user_controllable, "
            "ip_list_user_configurable}, applied_steps, fault, guidance, issues}."
        ),
        "next_step": NEXT_STEP_TAIL,
    },
)

# ---------------------------------------------------------------------------
# host.lockdown.set and host.lockdown.exceptions.set
# ---------------------------------------------------------------------------

LOCKDOWN_SET: Final[dict[str, Any]] = op_parts(
    parameters={
        "type": "object",
        "properties": {
            "host": HOST_PARAMETER,
            "mode": {
                "type": "string",
                "enum": ["disabled", "normal", "strict"],
                "description": (
                    "disabled: no lockdown. normal: only vCenter and the exception users; the "
                    "host console (DCUI) still works. strict: the console stops too."
                ),
            },
        },
        "required": ["host", "mode"],
        "additionalProperties": False,
        "examples": [{"host": "<host-name>", "mode": "normal"}],
    },
    response=response_schema(
        {
            "type": ["object", "null"],
            "description": "Lockdown mode.",
            "properties": {
                "mode": {**NULL_STR, "description": "disabled, normal or strict."},
            },
        },
        [],
    ),
    llm={
        "when_to_call": (
            "Call to turn lockdown mode on (normal or strict) or off for one ESXi host, like "
            "the Lockdown Mode setting in the vSphere Client."
        ),
        "preconditions": (
            "The host must stay managed by this vCenter: in lockdown only vCenter and the "
            "exception users reach the host. In strict mode the host console stops too, so "
            "if vCenter is lost, only the exception users can get in. Check the exception "
            "users first. The change waits for approval."
        ),
        "parameter_hints": {"mode": "disabled, normal or strict."},
        "output_shape": (
            "{status: applied|unchanged|failed|not_verified|<host refusal>, host, requested: "
            "{mode}, before and after: {mode}, applied_steps, fault, guidance, issues}."
        ),
        "next_step": NEXT_STEP_TAIL,
    },
)

LOCKDOWN_EXCEPTIONS_SET: Final[dict[str, Any]] = op_parts(
    parameters={
        "type": "object",
        "properties": {
            "host": HOST_PARAMETER,
            "users": {
                "type": "array",
                "maxItems": 20,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 255,
                    "pattern": _USER_PATTERN,
                    "description": "One user name, such as '<user>' or '<user>@<domain>'.",
                },
                "description": (
                    "Users who keep access in lockdown mode. Replaces the list; [] removes all."
                ),
            },
        },
        "required": ["host", "users"],
        "additionalProperties": False,
        "examples": [{"host": "<host-name>", "users": ["example-user"]}],
    },
    response=response_schema(
        {
            "type": ["object", "null"],
            "description": "The exception users.",
            "properties": {
                "users": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "User names, sorted.",
                },
            },
        },
        [],
    ),
    llm={
        "when_to_call": (
            "Call to set which users keep access to one ESXi host when lockdown mode is on."
        ),
        "preconditions": (
            "The list replaces the current one. Keep the service accounts that must reach the "
            "host directly. The change waits for approval."
        ),
        "parameter_hints": {"users": "e.g. ['<user-name>']; [] removes all."},
        "output_shape": (
            "{status: applied|unchanged|failed|not_verified|<host refusal>, host, requested: "
            "{users}, before and after: {users}, applied_steps, fault, guidance, issues}."
        ),
        "next_step": NEXT_STEP_TAIL,
    },
)
