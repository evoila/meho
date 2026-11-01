# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""msad domain-wide settings -- one read and two changes.

* ``msad.domain.settings`` (safe) -- the default password rules of the domain
  (``Get-ADDefaultDomainPasswordPolicy``) and the join limit: how many
  computers a normal user may join to the domain (the
  ``ms-DS-MachineAccountQuota`` attribute of the domain object).
* ``msad.domain.password-policy.set`` (caution, approval) --
  ``Set-ADDefaultDomainPasswordPolicy`` with the values the caller gives.
  Reversible encryption is deliberately not offered.
* ``msad.domain.machine-account-quota.set`` (caution, approval) --
  ``Set-ADDomain -Replace @{'ms-DS-MachineAccountQuota' = <n>}``.

Both changes read the settings back and return them. The same values give the
same result, so both are safe to run twice. The password-policy op sets rules
for passwords; it never takes or returns a password.

Injection safety: no operator string reaches these scripts. Every value is an
``int`` (checked against its range) or a ``bool`` rendered as ``$true`` /
``$false``.

References
----------

* ``Get-ADDefaultDomainPasswordPolicy`` / ``Set-ADDefaultDomainPasswordPolicy``:
  https://learn.microsoft.com/en-us/powershell/module/activedirectory/set-addefaultdomainpasswordpolicy
* ``Set-ADDomain`` (``-Replace``):
  https://learn.microsoft.com/en-us/powershell/module/activedirectory/set-addomain
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import pwsh_run
from meho_backplane.connectors.msad.ops import SSH_TRANSPORT_NOTE, MsadOp
from meho_backplane.operations import register_plain_fields

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.msad.connector import MsadConnector

__all__ = [
    "DOMAIN_CONFIG_OPS",
    "MAX_MACHINE_ACCOUNT_QUOTA",
    "PASSWORD_POLICY_FIELDS",
    "msad_domain_machine_account_quota_set",
    "msad_domain_password_policy_set",
    "msad_domain_settings",
    "password_policy_arguments",
]

#: Highest join limit one call may set (the AD default is 10).
MAX_MACHINE_ACCOUNT_QUOTA: int = 1000

#: Password-policy fields: name -> (minimum, maximum, cmdlet parameter, kind).
#: ``kind`` is how the value is written: ``int`` as is, ``days`` and
#: ``minutes`` as a ``[TimeSpan]``.
PASSWORD_POLICY_FIELDS: dict[str, tuple[int, int, str, str]] = {
    "min_length": (0, 128, "MinPasswordLength", "int"),
    "history_count": (0, 24, "PasswordHistoryCount", "int"),
    "max_age_days": (0, 999, "MaxPasswordAge", "days"),
    "min_age_days": (0, 998, "MinPasswordAge", "days"),
    "lockout_threshold": (0, 999, "LockoutThreshold", "int"),
    "lockout_duration_minutes": (0, 99999, "LockoutDuration", "minutes"),
    "lockout_window_minutes": (0, 99999, "LockoutObservationWindow", "minutes"),
}

_READ_SCRIPT: str = (
    "$d = Get-ADDomain; "
    "$p = Get-ADDefaultDomainPasswordPolicy -Identity $d.DistinguishedName; "
    "$q = (Get-ADObject -Identity $d.DistinguishedName "
    "-Properties 'ms-DS-MachineAccountQuota').'ms-DS-MachineAccountQuota'; "
    "ConvertTo-Json -Compress -Depth 3 -InputObject @{ "
    "domain = [string]$d.DNSRoot; "
    "machine_account_quota = [int]$q; "
    "password_policy = @{ "
    "min_length = [int]$p.MinPasswordLength; "
    "history_count = [int]$p.PasswordHistoryCount; "
    "max_age_days = [int][math]::Floor($p.MaxPasswordAge.TotalDays); "
    "min_age_days = [int][math]::Floor($p.MinPasswordAge.TotalDays); "
    "complexity_enabled = [bool]$p.ComplexityEnabled; "
    "reversible_encryption_enabled = [bool]$p.ReversibleEncryptionEnabled; "
    "lockout_threshold = [int]$p.LockoutThreshold; "
    "lockout_duration_minutes = [int][math]::Floor($p.LockoutDuration.TotalMinutes); "
    "lockout_window_minutes = [int][math]::Floor($p.LockoutObservationWindow.TotalMinutes) } }"
)


def _bounded_int(name: str, raw: Any, low: int, high: int) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or not low <= raw <= high:
        raise ValueError(f"{name} must be a whole number from {low} to {high}; got {raw!r}")
    return raw


def password_policy_arguments(params: dict[str, Any]) -> str:
    """Return the checked ``Set-ADDefaultDomainPasswordPolicy`` arguments for *params*."""
    args: list[str] = []
    for name, (low, high, parameter, kind) in PASSWORD_POLICY_FIELDS.items():
        if params.get(name) is None:
            continue
        value = _bounded_int(name, params[name], low, high)
        if kind == "days":
            args.append(f"-{parameter} ([System.TimeSpan]::FromDays({value}))")
        elif kind == "minutes":
            args.append(f"-{parameter} ([System.TimeSpan]::FromMinutes({value}))")
        else:
            args.append(f"-{parameter} {value}")
    complexity = params.get("complexity_enabled")
    if complexity is not None:
        if not isinstance(complexity, bool):
            raise ValueError("complexity_enabled must be true or false")
        args.append("-ComplexityEnabled " + ("$true" if complexity else "$false"))
    if not args:
        raise ValueError("give at least one password rule to change")
    return " ".join(args)


def _settings_dict(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("the domain settings read did not return a JSON object")
    return payload


async def msad_domain_settings(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.domain.settings`` -- password rules and join limit. Read-only."""
    del params
    script = "$ErrorActionPreference = 'Stop'; " + _READ_SCRIPT
    return _settings_dict(await pwsh_run(connector, target, script, operator=operator))


async def msad_domain_password_policy_set(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.domain.password-policy.set`` (caution, approval)."""
    arguments = password_policy_arguments(params)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "$dn = (Get-ADDomain).DistinguishedName; "
        f"Set-ADDefaultDomainPasswordPolicy -Identity $dn {arguments}; " + _READ_SCRIPT
    )
    result = _settings_dict(await pwsh_run(connector, target, script, operator=operator))
    return {**result, "op_class": "write"}


async def msad_domain_machine_account_quota_set(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.domain.machine-account-quota.set`` (caution, approval)."""
    quota = _bounded_int("quota", params.get("quota"), 0, MAX_MACHINE_ACCOUNT_QUOTA)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "$dn = (Get-ADDomain).DistinguishedName; "
        f"Set-ADDomain -Identity $dn -Replace @{{'ms-DS-MachineAccountQuota' = {quota}}}; "
        + _READ_SCRIPT
    )
    result = _settings_dict(await pwsh_run(connector, target, script, operator=operator))
    return {**result, "op_class": "write"}


_POLICY_DESCRIPTIONS: dict[str, str] = {
    "min_length": "Shortest password allowed, in characters.",
    "history_count": "How many old passwords a user may not use again.",
    "max_age_days": "Days until a password expires. 0 means it never expires.",
    "min_age_days": "Days a user must wait before changing the password again.",
    "lockout_threshold": "Failed logins before the account is locked. 0 means never.",
    "lockout_duration_minutes": "Minutes an account stays locked. Must be at least the window.",
    "lockout_window_minutes": "Minutes after which the failed-login count goes back to 0.",
}

_POLICY_PROPERTIES: dict[str, Any] = {
    name: {"type": "integer", "description": _POLICY_DESCRIPTIONS[name]}
    for name in PASSWORD_POLICY_FIELDS
}
_POLICY_PROPERTIES["complexity_enabled"] = {
    "type": "boolean",
    "description": "A password needs three of: upper case, lower case, digits, other signs.",
}

_SETTINGS_PROPERTIES: dict[str, Any] = {
    "domain": {"type": "string", "description": "The DNS name of the domain."},
    "machine_account_quota": {
        "type": "integer",
        "description": "How many computers a normal user may join to the domain.",
    },
    "password_policy": {
        "type": "object",
        "description": "The default password rules of the domain.",
        "properties": {
            **_POLICY_PROPERTIES,
            "reversible_encryption_enabled": {
                "type": "boolean",
                "description": "Passwords are stored with reversible encryption (should be false).",
            },
        },
    },
}

_WRITE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_SETTINGS_PROPERTIES,
        "op_class": {"type": "string", "enum": ["write"], "description": "Always 'write'."},
    },
    "required": ["domain", "machine_account_quota", "password_policy", "op_class"],
    "additionalProperties": True,
}

_SETTINGS_OUTPUT_SHAPE: str = (
    "{domain, machine_account_quota, password_policy: {min_length, history_count, "
    "max_age_days, min_age_days, complexity_enabled, reversible_encryption_enabled, "
    "lockout_threshold, lockout_duration_minutes, lockout_window_minutes}}"
)


def _policy_param(name: str) -> dict[str, Any]:
    low, high, _, _ = PASSWORD_POLICY_FIELDS[name]
    return {
        "type": "integer",
        "minimum": low,
        "maximum": high,
        "description": f"{_POLICY_DESCRIPTIONS[name]} {low} to {high}.",
    }


DOMAIN_CONFIG_OPS: tuple[MsadOp, ...] = (
    MsadOp(
        op_id="msad.domain.settings",
        handler_attr="msad_domain_settings",
        summary="Read the domain's password rules and its join limit in one answer.",
        description=(
            "Runs Get-ADDefaultDomainPasswordPolicy and reads ms-DS-MachineAccountQuota "
            "from the domain object. Returns the default password rules and how many "
            "computers a normal user may join. Read-only."
        ),
        parameter_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
            "examples": [{}],
        },
        response_schema={
            "type": "object",
            "properties": _SETTINGS_PROPERTIES,
            "required": ["domain", "machine_account_quota", "password_policy"],
            "additionalProperties": True,
        },
        group_key="domain-config",
        tags=("read-only", "domain", "settings"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "Call to check the domain-wide settings of a new or existing domain in "
                "one call. For DNS settings use the windns connector, for the time "
                "source winsrv.time.get. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": _SETTINGS_OUTPUT_SHAPE + ".",
            "next_step": (
                "To change a value, call msad.domain.password-policy.set or "
                "msad.domain.machine-account-quota.set."
            ),
        },
    ),
    MsadOp(
        op_id="msad.domain.password-policy.set",
        handler_attr="msad_domain_password_policy_set",
        summary="Change the domain's default password rules (length, age, history, lockout).",
        description=(
            "Runs Set-ADDefaultDomainPasswordPolicy with the rules you give, then reads "
            "the settings back. Sets rules only; it never takes a password. Safe to run "
            "twice. A person must approve it."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                **{name: _policy_param(name) for name in PASSWORD_POLICY_FIELDS},
                "complexity_enabled": _POLICY_PROPERTIES["complexity_enabled"],
            },
            "minProperties": 1,
            "additionalProperties": False,
            "examples": [
                {"min_length": 14, "max_age_days": 0, "complexity_enabled": True},
                {
                    "lockout_threshold": 10,
                    "lockout_duration_minutes": 15,
                    "lockout_window_minutes": 15,
                },
            ],
        },
        response_schema=_WRITE_RESPONSE_SCHEMA,
        group_key="domain-config",
        tags=("write", "domain", "password-policy"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_call": (
                "Call while building a domain to set its password rules. Give only the "
                "rules you want to change. " + SSH_TRANSPORT_NOTE
            ),
            "preconditions": (
                "A person must approve the call. The rules apply to every user of the "
                "domain. Read the current rules first with msad.domain.settings, so you "
                "can set them back. The lockout duration must be at least the lockout "
                "window."
            ),
            "parameter_hints": {
                name: _POLICY_DESCRIPTIONS[name] for name in PASSWORD_POLICY_FIELDS
            },
            "output_shape": _SETTINGS_OUTPUT_SHAPE + ", plus op_class 'write'.",
            "next_step": "Check that password_policy shows the values you sent.",
        },
    ),
    MsadOp(
        op_id="msad.domain.machine-account-quota.set",
        handler_attr="msad_domain_machine_account_quota_set",
        summary="Set how many computers a normal user may join to the domain (the join limit).",
        description=(
            "Runs Set-ADDomain -Replace on ms-DS-MachineAccountQuota, then reads the "
            "settings back. 0 means only users with a granted right may join computers. "
            "Safe to run twice. A person must approve it."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "quota": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_MACHINE_ACCOUNT_QUOTA,
                    "description": (
                        "How many computers a normal user may join. 0 to "
                        f"{MAX_MACHINE_ACCOUNT_QUOTA}. The AD default is 10."
                    ),
                },
            },
            "required": ["quota"],
            "additionalProperties": False,
            "examples": [{"quota": 0}],
        },
        response_schema=_WRITE_RESPONSE_SCHEMA,
        group_key="domain-config",
        tags=("write", "domain", "join-limit"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_call": (
                "Call while building a domain to stop normal users from joining "
                "computers (quota 0), or to change the limit. " + SSH_TRANSPORT_NOTE
            ),
            "preconditions": (
                "A person must approve the call. With 0, give a group the right to "
                "create computers with msad.ou.grant-create-child, or prestage computer "
                "accounts with msad.computer.join-prestage."
            ),
            "parameter_hints": {"quota": "Required. 0 to 1000."},
            "output_shape": _SETTINGS_OUTPUT_SHAPE + ", plus op_class 'write'.",
            "next_step": "Check that machine_account_quota shows the value you sent.",
        },
    ),
)


# ``password_policy`` holds the domain's rules (lengths, ages, lockout), never a password.
for _op in DOMAIN_CONFIG_OPS:
    register_plain_fields(_op.op_id, ("password_policy",))
