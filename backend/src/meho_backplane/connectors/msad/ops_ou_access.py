# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""msad access rules on a directory folder -- grant and remove "may create <class> here".

A build often gives one group the right to create computer objects in one OU
(the ``dsacls "<OU>" /I:T /G "<group>:CC;computer"`` step). These two ops do
that with a fixed, checked script instead of a free ``dsacls`` command line:

* ``msad.ou.grant-create-child`` adds an "allow create child" access rule for
  one object class (computer, user or group) to the OU or container, for this
  object and all objects below it (``/I:T``). When a rule with the same scope
  already holds that right, nothing changes (``changed: false``), so the call
  is safe to run twice.
* ``msad.ou.revoke-create-child`` takes exactly that right away again (the
  undo). It touches only rules with the same group, object class, inheritance
  and inherited object type. A matched rule that also holds other rights (for
  example "delete child") keeps those: the rule is removed and written back
  without the "create child" bit.

Both ops read the permissions back after the save and fail when the saved
state is not what the op set. The answer carries ``rule_present``: whether a
rule with that scope and the "create child" right is in the saved
permissions.

What makes two rules "the same scope": the group SID, the object class GUID
(``ObjectType``), the allow type, the inheritance (``All`` = this object and
all below), and the inherited object type (``InheritedObjectType``, empty =
every child type). A rule that only reaches one child type is narrower, so it
is not the rule these ops manage.

The script uses the ActiveDirectory module's ``AD:`` drive with ``Get-Acl`` /
``Set-Acl`` and a
:class:`System.DirectoryServices.ActiveDirectoryAccessRule`. The schema GUID
of the class is read from the schema partition on the domain controller, not
hard-coded. The revoke does not use ``RemoveAccessRule``: its removal
algorithm "might add access control rules in addition to removing them" and
can return ``false`` on complex inheritance, so the split is done by hand with
``RemoveAccessRuleSpecific`` plus ``AddAccessRule`` of the remaining rights.

Injection safety: the folder DN and the group identity go into the script
only as single-quoted literals (``ps_single_quote``). The class name comes
from a fixed list.

References
----------

* ``ActiveDirectoryAccessRule`` constructors (the six-argument form takes
  ``objectType``, ``inheritanceType`` and ``inheritedObjectType``):
  https://learn.microsoft.com/en-us/dotnet/api/system.directoryservices.activedirectoryaccessrule.-ctor
* ``RemoveAccessRule`` vs ``RemoveAccessRuleSpecific``:
  https://learn.microsoft.com/en-us/dotnet/api/system.security.accesscontrol.directoryobjectsecurity.removeaccessrule
* ``Get-Acl`` / ``Set-Acl`` (``-LiteralPath``):
  https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.security/set-acl
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import ps_single_quote, pwsh_run
from meho_backplane.connectors.msad.ops import SSH_TRANSPORT_NOTE, MsadOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.msad.connector import MsadConnector

__all__ = [
    "CHILD_CLASSES",
    "OU_ACCESS_OPS",
    "msad_ou_grant_create_child",
    "msad_ou_revoke_create_child",
]

#: The object classes a group may be allowed to create.
CHILD_CLASSES: tuple[str, ...] = ("computer", "user", "group")


def _prelude(params: dict[str, Any]) -> str:
    """Return the statements that resolve the folder, group SID, class GUID and ACL.

    ``$find`` returns the explicit rules of an ACL with the managed scope
    (group, class GUID, allow, inherit to all below, every child type). The
    rights of a matched rule are not part of the scope: a rule may hold the
    "create child" bit together with other bits.
    """
    object_class = params.get("object_class", "computer")
    if object_class not in CHILD_CLASSES:
        raise ValueError(f"object_class must be one of {list(CHILD_CLASSES)}; got {object_class!r}")
    path = ps_single_quote(params["path"])
    group = ps_single_quote(params["group"])
    return (
        "$ErrorActionPreference = 'Stop'; "
        "Import-Module ActiveDirectory; "
        f"$obj = Get-ADObject -Identity {path}; "
        "$sid = [System.Security.Principal.SecurityIdentifier]"
        f"(Get-ADGroup -Identity {group}).SID; "
        f"$cls = '{object_class}'; "
        "$schema = Get-ADObject -SearchBase (Get-ADRootDSE).schemaNamingContext "
        '-LDAPFilter "(lDAPDisplayName=$cls)" -Properties schemaIDGUID; '
        "$guid = [Guid]::new([byte[]]$schema.schemaIDGUID); "
        "$adPath = 'AD:\\' + $obj.DistinguishedName; "
        "$rights = [System.DirectoryServices.ActiveDirectoryRights]::CreateChild; "
        "$inh = [System.DirectoryServices.ActiveDirectorySecurityInheritance]::All; "
        "$anyChild = [Guid]::Empty; "
        "$find = { param($a) @($a.GetAccessRules($true, $false, "
        "[System.Security.Principal.SecurityIdentifier]) | Where-Object { "
        "$_.IdentityReference -eq $sid -and $_.ObjectType -eq $guid -and "
        "$_.InheritedObjectType -eq $anyChild -and "
        "$_.AccessControlType -eq 'Allow' -and $_.InheritanceType -eq $inh }) }; "
        "$hasRight = { param($r) "
        "([int]$r.ActiveDirectoryRights -band [int]$rights) -eq [int]$rights }; "
        "$acl = Get-Acl -LiteralPath $adPath; "
        "$rules = & $find $acl; "
    )


_READ_BACK: str = (
    "$after = & $find (Get-Acl -LiteralPath $adPath); "
    "$present = @($after | Where-Object { & $hasRight $_ }).Count -gt 0; "
)

_RESULT_JSON: str = (
    "ConvertTo-Json -Compress -InputObject @{ "
    "path = [string]$obj.DistinguishedName; "
    "group_sid = $sid.Value; "
    "object_class = $cls; "
    "changed = $changed; "
    "rule_present = $present }"
)


def _result(payload: Any, params: dict[str, Any], action: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("the access rule script did not return a JSON object")
    return {**payload, "group": params["group"], "action": action, "op_class": "write"}


async def msad_ou_grant_create_child(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.ou.grant-create-child`` (caution, approval)."""
    script = (
        _prelude(params) + "$changed = $false; "
        "$have = @($rules | Where-Object { & $hasRight $_ }); "
        "if ($have.Count -eq 0) { "
        "$rule = New-Object System.DirectoryServices.ActiveDirectoryAccessRule("
        "$sid, $rights, 'Allow', $guid, $inh, $anyChild); "
        "$acl.AddAccessRule($rule); "
        "Set-Acl -LiteralPath $adPath -AclObject $acl; "
        "$changed = $true }; " + _READ_BACK + "if (-not $present) { "
        "throw 'the access rule is not in the saved permissions' }; " + _RESULT_JSON
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    return _result(payload, params, "grant-create-child")


async def msad_ou_revoke_create_child(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.ou.revoke-create-child`` (caution, approval).

    Only the "create child" bit goes away. A matched rule with more rights is
    removed as it is and added back without that bit, so the other rights
    stay.
    """
    script = (
        _prelude(params) + "$changed = $false; "
        "foreach ($r in @($rules | Where-Object { & $hasRight $_ })) { "
        "[void]$acl.RemoveAccessRuleSpecific($r); "
        "$rest = [int]$r.ActiveDirectoryRights -band (-bnot [int]$rights); "
        "if ($rest -ne 0) { "
        "$keep = New-Object System.DirectoryServices.ActiveDirectoryAccessRule("
        "$sid, [System.DirectoryServices.ActiveDirectoryRights]$rest, 'Allow', "
        "$r.ObjectType, $r.InheritanceType, $r.InheritedObjectType); "
        "$acl.AddAccessRule($keep) }; "
        "$changed = $true }; "
        "if ($changed) { Set-Acl -LiteralPath $adPath -AclObject $acl }; "
        + _READ_BACK
        + "if ($present) { throw 'the access rule is still in the saved permissions' }; "
        + _RESULT_JSON
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    return _result(payload, params, "revoke-create-child")


_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Distinguished name of the OU or container, for example "
                "OU=Servers,DC=example,DC=test."
            ),
        },
        "group": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "The group that gets the right: sAMAccountName, DN, GUID or SID.",
        },
        "object_class": {
            "type": "string",
            "enum": list(CHILD_CLASSES),
            "description": "What the group may create: computer (the default), user or group.",
        },
    },
    "required": ["path", "group"],
    "additionalProperties": False,
    "examples": [
        {
            "path": "OU=Servers,DC=example,DC=test",
            "group": "Server-Joiners",
            "object_class": "computer",
        }
    ],
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The DN of the OU or container."},
        "group": {"type": "string", "description": "The group identity you gave."},
        "group_sid": {"type": "string", "description": "The SID of the group."},
        "object_class": {"type": "string", "description": "The object class of the rule."},
        "changed": {
            "type": "boolean",
            "description": "false when the right was already there (grant) or not there (revoke).",
        },
        "rule_present": {
            "type": "boolean",
            "description": (
                "Read back after the save: true when a rule with this group, class and "
                "scope holds the 'create child' right. Always true after a grant and "
                "false after a revoke."
            ),
        },
        "action": {"type": "string", "description": "grant-create-child or revoke-create-child."},
        "op_class": {"type": "string", "enum": ["write"], "description": "Always 'write'."},
    },
    "required": [
        "path",
        "group_sid",
        "object_class",
        "changed",
        "rule_present",
        "action",
        "op_class",
    ],
    "additionalProperties": True,
}

_HINTS: dict[str, str] = {
    "path": "Required. The DN from msad.ou.list.",
    "group": "Required. The group identity.",
    "object_class": "Optional. computer (default), user or group.",
}


OU_ACCESS_OPS: tuple[MsadOp, ...] = (
    MsadOp(
        op_id="msad.ou.grant-create-child",
        handler_attr="msad_ou_grant_create_child",
        summary="Let a group create computers (or users or groups) in an OU and below it.",
        description=(
            "Adds an 'allow create child' access rule for one object class to the OU "
            "or container, for this object and all below it (like dsacls /I:T /G "
            "group:CC;computer). Reads the permissions back after the save. Safe to "
            "run twice. A person must approve it."
        ),
        parameter_schema=_PARAMETER_SCHEMA,
        response_schema=_RESPONSE_SCHEMA,
        group_key="ou",
        tags=("write", "ou", "access-rule"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_call": (
                "Call while building a domain to let a group create computer accounts "
                "in one OU, for example when the join limit is 0. " + SSH_TRANSPORT_NOTE
            ),
            "preconditions": (
                "A person must approve the call. The OU and the group must exist "
                "(msad.ou.list, msad.group.get). The right reaches every object below "
                "the OU."
            ),
            "parameter_hints": _HINTS,
            "output_shape": (
                "{path, group, group_sid, object_class, changed, rule_present, action: "
                "'grant-create-child', op_class: 'write'}."
            ),
            "next_step": "To take the right away again, call msad.ou.revoke-create-child.",
        },
    ),
    MsadOp(
        op_id="msad.ou.revoke-create-child",
        handler_attr="msad_ou_revoke_create_child",
        summary="Remove a group's right to create computers (or users or groups) in an OU.",
        description=(
            "Takes the 'create child' right that msad.ou.grant-create-child adds away "
            "again, for the same group, class, OU and scope. Other rules and other "
            "rights of the same rule stay. Reads the permissions back after the save. "
            "Safe to run twice. A person must approve it."
        ),
        parameter_schema=_PARAMETER_SCHEMA,
        response_schema=_RESPONSE_SCHEMA,
        group_key="ou",
        tags=("write", "ou", "access-rule"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_call": (
                "Call to undo msad.ou.grant-create-child, for example when a group no "
                "longer joins computers in that OU. " + SSH_TRANSPORT_NOTE
            ),
            "preconditions": (
                "A person must approve the call. Members of the group can no longer "
                "create that class of object in the OU afterwards."
            ),
            "parameter_hints": _HINTS,
            "output_shape": (
                "{path, group, group_sid, object_class, changed, rule_present, action: "
                "'revoke-create-child', op_class: 'write'}."
            ),
            "next_step": "changed false means no rule with that scope held the right.",
        },
    ),
)
