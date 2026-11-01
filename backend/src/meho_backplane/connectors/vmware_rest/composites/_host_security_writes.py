# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Two host changes: set advanced settings, and set a firewall rule set.

* ``vmware.composite.host.advanced_settings.set`` -- ``OptionManager.UpdateOptions``.
  Before it sends anything it reads ``supportedOption`` and checks each key:
  the setting must exist, must not be read-only, and the value must have the
  setting's type (and be inside its range or one of its choices). The value
  is sent with the type vSphere expects (``int``, ``long``, ``boolean``,
  ``float`` or ``string``); a ``long`` setting refuses an ``int`` value.
  A setting whose key names a secret (a password, token or community) takes
  its value only from Vault (``value_secret_ref``); a plain ``value`` for it
  is refused before anything is read. A value from Vault goes only into the
  vCenter request body and shows as ``<hidden>`` in every answer. The op is
  pinned ``credential_write`` (``broadcast/events.py``), so the flight
  recorder keeps no body (the read-back answers hold the value too), the
  broadcast keeps no params, and a waiting approval keeps its params in the
  encrypted handoff store.
* ``vmware.composite.host.firewall.ruleset.set`` --
  ``HostFirewallSystem.UpdateRuleset`` (allowed IP addresses), then
  ``EnableRuleset`` or ``DisableRuleset``. A required rule set is never turned
  off.

Both are ``dangerous`` and wait for approval. The shared flow is in
:mod:`._host_security_flow`.
"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._host_security_flow import (
    Step,
    envelope,
    finish,
    refused,
    run_steps,
    stop,
    unchanged,
)
from meho_backplane.connectors.vmware_rest.host_security import (
    HIDDEN_VALUE,
    OP_QUERY_OPTIONS,
    OP_RETRIEVE_PROPERTIES,
    firewall_state,
    is_secret_key,
    locate,
    option_definitions,
    query_options,
    read_property,
)
from meho_backplane.connectors.vmware_rest.vcenter_deployment import (
    SecretValueError,
    read_secret_param,
)
from meho_backplane.connectors.vmware_rest.vim_body import unwrap_vim_value

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "OP_ID_ADVANCED_SETTINGS_SET",
    "OP_ID_FIREWALL_RULESET_SET",
    "VIM_SUB_OPS_ADVANCED_SETTINGS_SET",
    "VIM_SUB_OPS_FIREWALL_RULESET_SET",
    "hidden_keys",
    "host_advanced_settings_set_composite",
    "host_firewall_ruleset_set_composite",
    "read_ruleset",
    "read_settings",
    "requested_settings",
    "settings_view",
]

OP_ID_ADVANCED_SETTINGS_SET: Final = "vmware.composite.host.advanced_settings.set"
OP_ID_FIREWALL_RULESET_SET: Final = "vmware.composite.host.firewall.ruleset.set"

_OP_UPDATE_OPTIONS: Final = "POST:/OptionManager/{moId}/UpdateOptions"
_OP_UPDATE_RULESET: Final = "POST:/HostFirewallSystem/{moId}/UpdateRuleset"
_OP_ENABLE_RULESET: Final = "POST:/HostFirewallSystem/{moId}/EnableRuleset"
_OP_DISABLE_RULESET: Final = "POST:/HostFirewallSystem/{moId}/DisableRuleset"

#: Every vim call of each change (reads and writes). The reconcile test checks
#: them against the pinned vSphere API files, and ``_governed_subops`` lists
#: them for the grant set.
VIM_SUB_OPS_ADVANCED_SETTINGS_SET: Final[tuple[str, ...]] = (
    OP_RETRIEVE_PROPERTIES,
    OP_QUERY_OPTIONS,
    _OP_UPDATE_OPTIONS,
)
VIM_SUB_OPS_FIREWALL_RULESET_SET: Final[tuple[str, ...]] = (
    OP_RETRIEVE_PROPERTIES,
    _OP_UPDATE_RULESET,
    _OP_ENABLE_RULESET,
    _OP_DISABLE_RULESET,
)


# ---------------------------------------------------------------------------
# host.advanced_settings.set
# ---------------------------------------------------------------------------


def _coerce(value: Any, definition: dict[str, Any]) -> tuple[Any, str | None]:
    """``(value, None)`` in the setting's type, or ``(None, reason)``."""
    kind = definition["value_type"]
    if kind == "boolean":
        ok = isinstance(value, bool)
    elif kind in ("int", "long"):
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "float":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        value = float(value) if ok else value
    elif kind == "string":
        ok = isinstance(value, str)
    else:
        return None, "vSphere gives no type for this setting"
    if not ok:
        return None, f"the value must be of type {kind}"
    low, high = definition["min"], definition["max"]
    if kind in ("int", "long", "float") and (
        (low is not None and value < low) or (high is not None and value > high)
    ):
        return None, f"the value must be between {low} and {high}"
    choices = definition["choices"]
    if choices is not None and value not in choices:
        return None, f"the value must be one of {choices}"
    return value, None


_SECRET_REF: Final = "value_secret_ref"
_DEFAULT_VALUE_KEY: Final = "value"


def _is_hidden(setting: dict[str, Any]) -> bool:
    """A setting whose value never shows: a secret key, or a value from Vault."""
    return is_secret_key(setting["key"]) or _SECRET_REF in setting


def hidden_keys(settings: Any) -> set[str]:
    """The keys of *settings* (raw params) whose value never shows."""
    return {
        s["key"]
        for s in (settings if isinstance(settings, list) else [])
        if isinstance(s, dict) and isinstance(s.get("key"), str) and _is_hidden(s)
    }


def settings_view(values: dict[str, Any], hidden: set[str]) -> dict[str, Any]:
    """``{key: value}``, with ``<hidden>`` for each key in *hidden*."""
    return {key: HIDDEN_VALUE if key in hidden else value for key, value in values.items()}


def requested_settings(settings: Any) -> dict[str, Any]:
    """What the call asks for, from the raw params: ``{key: value}``, secrets hidden."""
    rows = [
        s
        for s in (settings if isinstance(settings, list) else [])
        if isinstance(s, dict) and isinstance(s.get("key"), str)
    ]
    return settings_view({s["key"]: s.get("value") for s in rows}, hidden_keys(rows))


def _check_settings(
    settings: list[dict[str, Any]], definitions: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], tuple[str, str, list[str]] | None]:
    """The wanted plain values by key, or ``(status, guidance, keys)`` of the first problem.

    A setting given with ``value_secret_ref`` is checked (it must exist, be
    writable and be a string setting) but its value is read from Vault later.
    """
    wanted: dict[str, Any] = {}
    keys = [s["key"] for s in settings]
    repeated = sorted({key for key in keys if keys.count(key) > 1})
    if repeated:
        return wanted, ("invalid_value", "Give each key only once.", repeated)
    both = [s["key"] for s in settings if ("value" in s) == (_SECRET_REF in s)]
    if both:
        return wanted, ("invalid_value", "Give value or value_secret_ref, one of them.", both)
    plain_secret = [s["key"] for s in settings if "value" in s and is_secret_key(s["key"])]
    if plain_secret:
        return wanted, (
            "secret_value_required",
            "This setting holds a secret. Store the value in Vault and give value_secret_ref.",
            plain_secret,
        )
    unknown = [s["key"] for s in settings if s["key"] not in definitions]
    if unknown:
        return wanted, ("unknown_setting", "The host has no setting with this key.", unknown)
    read_only = [s["key"] for s in settings if definitions[s["key"]]["read_only"]]
    if read_only:
        return wanted, ("read_only_setting", "These settings cannot be changed.", read_only)
    not_text = [
        s["key"]
        for s in settings
        if _SECRET_REF in s and definitions[s["key"]]["value_type"] != "string"
    ]
    if not_text:
        return wanted, (
            "invalid_value",
            "A value from Vault is text, so value_secret_ref fits only a string setting.",
            not_text,
        )
    for setting in settings:
        if _SECRET_REF in setting:
            continue
        value, reason = _coerce(setting["value"], definitions[setting["key"]])
        if reason is not None:
            return wanted, ("invalid_value", f"{setting['key']}: {reason}.", [setting["key"]])
        wanted[setting["key"]] = value
    return wanted, None


async def _read_secret_values(
    operator: Operator, target: Any, settings: list[dict[str, Any]]
) -> tuple[dict[str, str], tuple[str, str] | None]:
    """The Vault values by key, or ``(key, reason)`` of the first unusable secret."""
    values: dict[str, str] = {}
    for setting in settings:
        if _SECRET_REF not in setting:
            continue
        secret_params = {**setting}
        secret_params.setdefault("value_secret_key", _DEFAULT_VALUE_KEY)
        try:
            values[setting["key"]] = await read_secret_param(
                operator, target, secret_params, prefix="value"
            )
        except SecretValueError as exc:
            return values, (setting["key"], str(exc).removesuffix(" Nothing was sent."))
    return values, None


async def read_settings(
    connector: VmwareRestConnector,
    operator: Operator,
    target: Any,
    option_manager: str,
    keys: list[str],
) -> dict[str, Any]:
    """``{key: value}`` of *keys*, one ``QueryOptions`` each."""
    values: dict[str, Any] = {}
    for key in keys:
        found = await query_options(connector, operator, target, option_manager, key)
        values[key] = found.get(key)
    return values


async def host_advanced_settings_set_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Set one or more advanced settings of one host; a secret comes from Vault."""
    settings = list(params["settings"])
    hidden = hidden_keys(settings)
    requested = {"settings": requested_settings(settings)}
    host_moid, manager, refusal = await locate(
        connector, operator, target, params.get("host"), "advancedOption"
    )
    if refusal is not None:
        return refused(refusal, requested)
    assert host_moid is not None and manager is not None
    supported = await read_property(
        connector, operator, target, "OptionManager", manager, "supportedOption"
    )
    definitions = option_definitions(unwrap_vim_value(supported))
    wanted, problem = _check_settings(settings, definitions)
    if problem is not None:
        status, guidance, keys = problem
        return stop(envelope(host_moid, requested, None), status, guidance, keys=keys)
    secret_values, unusable = await _read_secret_values(operator, target, settings)
    if unusable is not None:
        key, reason = unusable
        return stop(
            envelope(host_moid, requested, None),
            "value_secret_unusable",
            f"{key}: {reason}",
            keys=[key],
        )
    keys = [s["key"] for s in settings]
    wanted = {key: {**wanted, **secret_values}[key] for key in keys}
    before = await read_settings(connector, operator, target, manager, keys)
    base = envelope(host_moid, requested, settings_view(before, hidden))
    changed = {key: value for key, value in wanted.items() if before.get(key) != value}
    if not changed:
        return unchanged(base)
    body = {
        "changedValue": [
            {
                "_typeName": "OptionValue",
                "key": key,
                "value": {"_typeName": definitions[key]["value_type"], "_value": value},
            }
            for key, value in changed.items()
        ]
    }
    step: Step = (
        _OP_UPDATE_OPTIONS,
        f"/OptionManager/{manager}/UpdateOptions",
        body,
        {"host": host_moid, "settings": settings_view(changed, hidden)},
    )
    secrets = tuple(secret_values.values())
    stopped = await run_steps(connector, operator, target, [step], base, secrets)
    if stopped is not None:
        return stopped

    async def read() -> dict[str, Any]:
        return await read_settings(connector, operator, target, manager, keys)

    return await finish(
        base,
        read,
        lambda after: all(after[k] == wanted[k] for k in keys),
        shown=lambda after: settings_view(after, hidden),
    )


# ---------------------------------------------------------------------------
# host.firewall.ruleset.set
# ---------------------------------------------------------------------------


def _normal_allowed(allowed: dict[str, Any]) -> dict[str, Any]:
    """The allowed hosts in one canonical form, so two lists compare equal.

    With ``all_ip`` the lists do not matter, so they are left empty.
    """
    if allowed.get("all_ip"):
        return {"all_ip": True, "ip_addresses": [], "networks": []}
    return {
        "all_ip": False,
        "ip_addresses": sorted(
            str(ipaddress.ip_address(a)) for a in allowed.get("ip_addresses") or []
        ),
        "networks": sorted(
            str(ipaddress.ip_network(n, strict=False)) for n in allowed.get("networks") or []
        ),
    }


def _allowed_problem(allowed: dict[str, Any]) -> str | None:
    try:
        normal = _normal_allowed({**allowed, "all_ip": False})
        normal["all_ip"] = bool(allowed.get("all_ip"))
    except ValueError as exc:
        return f"An address or network is not valid: {exc}."
    if allowed.get("all_ip") and (allowed.get("ip_addresses") or allowed.get("networks")):
        return "With all_ip true, leave ip_addresses and networks empty."
    if not normal["all_ip"] and not (normal["ip_addresses"] or normal["networks"]):
        return "Give at least one address or network, or set all_ip true."
    return None


def _ip_list_body(allowed: dict[str, Any]) -> dict[str, Any]:
    networks = [ipaddress.ip_network(n, strict=False) for n in allowed["networks"]]
    return {
        "_typeName": "HostFirewallRulesetIpList",
        "allIp": allowed["all_ip"],
        "ipAddress": allowed["ip_addresses"],
        "ipNetwork": [
            {
                "_typeName": "HostFirewallRulesetIpNetwork",
                "network": str(net.network_address),
                "prefixLength": net.prefixlen,
            }
            for net in networks
        ],
    }


async def read_ruleset(
    connector: VmwareRestConnector, operator: Operator, target: Any, firewall: str, key: str
) -> dict[str, Any] | None:
    """The row of rule set *key* (see :func:`.host_security.firewall_state`), or ``None``."""
    info = await read_property(
        connector, operator, target, "HostFirewallSystem", firewall, "firewallInfo"
    )
    for row in firewall_state(info)["rulesets"]:
        if row["key"] == key:
            return {"key": key, "enabled": row["enabled"], **_flags(row)}
    return None


def _flags(row: dict[str, Any]) -> dict[str, Any]:
    allowed = row["allowed"]
    return {
        "allowed": _normal_allowed(allowed),
        "required": row["required"],
        "user_controllable": row["user_controllable"],
        "ip_list_user_configurable": row["ip_list_user_configurable"],
    }


def _ruleset_refusal(before: dict[str, Any], params: dict[str, Any]) -> tuple[str, str] | None:
    enabled = params.get("enabled")
    if enabled is False and before["required"]:
        return "ruleset_required", "This rule set is required and cannot be turned off."
    if enabled is not None and not before["user_controllable"]:
        return "not_user_controllable", "This rule set cannot be turned on or off."
    allowed = params.get("allowed")
    if allowed is not None:
        if not before["ip_list_user_configurable"]:
            return (
                "ip_list_not_configurable",
                "The allowed IP addresses of this rule set are fixed.",
            )
        problem = _allowed_problem(allowed)
        if problem is not None:
            return "invalid_value", problem
    return None


def _ruleset_steps(
    host_moid: str, firewall: str, before: dict[str, Any], params: dict[str, Any]
) -> list[Step]:
    key = params["ruleset"]
    steps: list[Step] = []
    allowed = params.get("allowed")
    if allowed is not None:
        normal = _normal_allowed(allowed)
        if normal != before["allowed"]:
            spec = {
                "_typeName": "HostFirewallRulesetRulesetSpec",
                "allowedHosts": _ip_list_body(normal),
            }
            steps.append(
                (
                    _OP_UPDATE_RULESET,
                    f"/HostFirewallSystem/{firewall}/UpdateRuleset",
                    {"id": key, "spec": spec},
                    {"host": host_moid, "ruleset": key, "allowed": normal},
                )
            )
    enabled = params.get("enabled")
    if enabled is not None and enabled != before["enabled"]:
        op_id, path = (
            (_OP_ENABLE_RULESET, f"/HostFirewallSystem/{firewall}/EnableRuleset")
            if enabled
            else (_OP_DISABLE_RULESET, f"/HostFirewallSystem/{firewall}/DisableRuleset")
        )
        steps.append(
            (
                op_id,
                path,
                {"id": key},
                {"host": host_moid, "ruleset": key, "enabled": enabled},
            )
        )
    return steps


async def host_firewall_ruleset_set_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Turn one firewall rule set on or off and set its allowed IP addresses."""
    key = params["ruleset"]
    requested: dict[str, Any] = {
        k: params[k] for k in ("ruleset", "enabled", "allowed") if k in params
    }
    host_moid, firewall, refusal = await locate(
        connector, operator, target, params.get("host"), "firewallSystem"
    )
    if refusal is not None:
        return refused(refusal, requested)
    assert host_moid is not None and firewall is not None

    async def read() -> dict[str, Any] | None:
        return await read_ruleset(connector, operator, target, firewall, key)

    before = await read()
    base = envelope(host_moid, requested, before)
    if before is None:
        return stop(base, "ruleset_not_found", f"The host has no rule set {key!r}.")
    problem = _ruleset_refusal(before, params)
    if problem is not None:
        return stop(base, problem[0], problem[1])
    steps = _ruleset_steps(host_moid, firewall, before, params)
    if not steps:
        return unchanged(base)
    stopped = await run_steps(connector, operator, target, steps, base)
    if stopped is not None:
        return stopped

    def matches(after: dict[str, Any] | None) -> bool:
        if after is None:
            return False
        if "enabled" in params and after["enabled"] != params["enabled"]:
            return False
        return "allowed" not in params or after["allowed"] == _normal_allowed(params["allowed"])

    return await finish(base, read, matches)
