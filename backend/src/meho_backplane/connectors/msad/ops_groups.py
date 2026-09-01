# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""msad group ops — ``list`` / ``get`` / ``members`` (safe) + membership writes.

``add-member`` / ``remove-member`` are ``caution`` (recoverable); ``delete`` is
``dangerous`` + ``requires_approval``. Groups are driven by the
``ActiveDirectory`` module cmdlets (``Get-ADGroup`` / ``Get-ADGroupMember`` /
``Add-ADGroupMember`` / ``Remove-ADGroupMember`` / ``Remove-ADGroup``) over the
shared PowerShell-over-SSH transport.

PowerShell injection safety
---------------------------

The group identity and each member identity are interpolated only inside
single-quoted PowerShell literals via
:func:`~meho_backplane.connectors._shared.pwsh.ps_single_quote`; the member list
renders as a PowerShell array literal ``@('a','b')`` of escaped literals. Every
write cmdlet that would otherwise prompt -- ``Add-ADGroupMember`` /
``Remove-ADGroupMember`` / ``Remove-ADGroup`` -- carries ``-Confirm:$false`` so
the cmdlet's own interactive prompt does not hang the non-interactive
``-EncodedCommand`` run (the approval gate is MEHO's, not AD's).

References
----------

* ``Get-ADGroup`` / ``Get-ADGroupMember`` / ``Add-ADGroupMember`` /
  ``Remove-ADGroupMember`` / ``Remove-ADGroup``:
  https://learn.microsoft.com/en-us/powershell/module/activedirectory/
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import ps_single_quote, pwsh_run
from meho_backplane.connectors.msad.ops import (
    SSH_TRANSPORT_NOTE,
    MsadOp,
    ad_list_read,
    validate_limit,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.msad.connector import MsadConnector

__all__ = [
    "GROUP_OPS",
    "msad_group_add_member",
    "msad_group_delete",
    "msad_group_get",
    "msad_group_list",
    "msad_group_members",
    "msad_group_remove_member",
]

_GROUP_SELECT: str = (
    "SamAccountName, Name, GroupCategory, GroupScope, DistinguishedName, Description"
)
_MEMBER_SELECT: str = "SamAccountName, Name, objectClass, distinguishedName"


def _members_literal(members: list[Any]) -> str:
    """Render *members* as a PowerShell array literal of escaped single-quoted strings."""
    if not members or not all(isinstance(m, str) and m.strip() for m in members):
        raise ValueError("members must be a non-empty list of non-blank identity strings")
    return "@(" + ", ".join(ps_single_quote(m) for m in members) + ")"


async def msad_group_list(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.group.list`` — ``Get-ADGroup -Filter *`` (list, capped)."""
    limit = validate_limit(params.get("limit"))
    return await ad_list_read(
        connector,
        target,
        pipeline=(
            f"Get-ADGroup -Filter * -ResultSetSize {limit} -Properties Description "
            f"| Select-Object {_GROUP_SELECT}"
        ),
        operator=operator,
    )


async def msad_group_get(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.group.get`` — ``Get-ADGroup -Identity`` (one group)."""
    identity = ps_single_quote(params["identity"])
    result = await ad_list_read(
        connector,
        target,
        pipeline=(
            f"Get-ADGroup -Identity {identity} -Properties Description "
            f"| Select-Object {_GROUP_SELECT}"
        ),
        operator=operator,
    )
    return {"identity": params["identity"], **result}


async def msad_group_members(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.group.members`` — ``Get-ADGroupMember -Identity`` (list)."""
    identity = ps_single_quote(params["identity"])
    result = await ad_list_read(
        connector,
        target,
        pipeline=(f"Get-ADGroupMember -Identity {identity} | Select-Object {_MEMBER_SELECT}"),
        operator=operator,
    )
    return {"identity": params["identity"], **result}


async def msad_group_add_member(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.group.add-member`` (caution) — ``Add-ADGroupMember``."""
    return await _member_write(
        connector, target, params, "Add-ADGroupMember", "add-member", operator
    )


async def msad_group_remove_member(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.group.remove-member`` (caution) — ``Remove-ADGroupMember``."""
    return await _member_write(
        connector, target, params, "Remove-ADGroupMember", "remove-member", operator
    )


async def msad_group_delete(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.group.delete`` (dangerous, resume path only) — ``Remove-ADGroup``.

    Runs ``Remove-ADGroup -Identity <group> -Confirm:$false``. Irreversible —
    ``requires_approval`` parks a dispatch for a human decision first.
    """
    identity = params["identity"]
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"Remove-ADGroup -Identity {ps_single_quote(identity)} -Confirm:$false; "
        "ConvertTo-Json -Compress -InputObject @{ ok = $true }"
    )
    await pwsh_run(connector, target, script, operator=operator)
    return {"identity": identity, "action": "delete", "op_class": "write"}


async def _member_write(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    cmdlet: str,
    action: str,
    operator: Operator | None,
) -> dict[str, Any]:
    """Run ``Add/Remove-ADGroupMember -Identity <group> -Members @(...) -Confirm:$false``."""
    identity = params["identity"]
    members: list[Any] = params["members"]
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"{cmdlet} -Identity {ps_single_quote(identity)} "
        f"-Members {_members_literal(members)} -Confirm:$false; "
        "ConvertTo-Json -Compress -InputObject @{ ok = $true }"
    )
    await pwsh_run(connector, target, script, operator=operator)
    return {"identity": identity, "action": action, "members": members, "op_class": "write"}


_GROUP_IDENTITY_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": (
        "The group identity — sAMAccountName, distinguished name, GUID, or SID "
        "(the ``-Identity`` operand, e.g. ``SQL-Admins``)."
    ),
}

_MEMBERS_PROP: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string", "minLength": 1, "pattern": "\\S"},
    "minItems": 1,
    "description": "Member identities (sAMAccountName / DN / GUID / SID) to add or remove.",
}

_LIMIT_PROP: dict[str, Any] = {
    "type": "integer",
    "minimum": 1,
    "description": "Max rows to return (maps to ``-ResultSetSize``). Default 500.",
}

_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {"type": "array", "items": {"type": "object"}},
        "total": {"type": "integer"},
    },
    "required": ["rows", "total"],
    "additionalProperties": True,
}

_IDENTITY_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "identity": {"type": "string"},
        "rows": {"type": "array", "items": {"type": "object"}},
        "total": {"type": "integer"},
    },
    "required": ["identity", "rows", "total"],
    "additionalProperties": True,
}

_MEMBER_WRITE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "identity": {"type": "string"},
        "action": {"type": "string"},
        "members": {"type": "array", "items": {"type": "string"}},
        "op_class": {"type": "string", "enum": ["write"]},
    },
    "required": ["identity", "action", "op_class"],
    "additionalProperties": True,
}


def _member_write_op(op_id: str, handler_attr: str, verb: str, cmdlet: str) -> MsadOp:
    """Build one caution-tier membership write op (add-member / remove-member)."""
    return MsadOp(
        op_id=op_id,
        handler_attr=handler_attr,
        summary=f"{verb.replace('-', ' ').capitalize()} an AD group via ``{cmdlet}`` (caution).",
        description=(
            f"Runs ``{cmdlet} -Identity <group> -Members @(...) -Confirm:$false`` "
            "on the domain controller. Recoverable (the inverse op restores the "
            "prior membership). safety_level=caution."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"identity": _GROUP_IDENTITY_PROP, "members": _MEMBERS_PROP},
            "required": ["identity", "members"],
            "additionalProperties": False,
        },
        response_schema=_MEMBER_WRITE_RESPONSE_SCHEMA,
        group_key="groups",
        tags=("write", "group", verb),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                f"{verb.replace('-', ' ').capitalize()} of an AD group (e.g. "
                "grant a service account into a role group). Recoverable; "
                "safety_level=caution. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "identity": "Required. The group identity.",
                "members": "Required. Non-empty list of member identities.",
            },
            "output_shape": f"{{'identity', 'action': '{verb}', 'members', 'op_class': 'write'}}.",
        },
    )


GROUP_OPS: tuple[MsadOp, ...] = (
    MsadOp(
        op_id="msad.group.list",
        handler_attr="msad_group_list",
        summary="List AD groups via ``Get-ADGroup -Filter *`` (capped by limit).",
        description=(
            "Runs ``Get-ADGroup -Filter *`` (capped at ``limit`` rows, default "
            "500) and returns one row per group (SamAccountName, Name, "
            "GroupCategory, GroupScope, DN, Description). Read-only."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"limit": _LIMIT_PROP},
            "additionalProperties": False,
        },
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="groups",
        tags=("read-only", "group", "inventory"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to enumerate AD groups (bounded by ``limit``) or find a "
                "group's exact identity. Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"limit": "Optional; max rows (default 500)."},
            "output_shape": (
                "{'rows': [{SamAccountName, Name, GroupScope, ...}], 'total': <int>}."
            ),
        },
    ),
    MsadOp(
        op_id="msad.group.get",
        handler_attr="msad_group_get",
        summary="Read one AD group by identity via ``Get-ADGroup -Identity``.",
        description=(
            "Runs ``Get-ADGroup -Identity <id>`` and returns the single "
            "matching group projection as a ``{rows, total}`` envelope plus the "
            "echoed ``identity``. A missing identity is a terminating error. "
            "Read-only."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"identity": _GROUP_IDENTITY_PROP},
            "required": ["identity"],
            "additionalProperties": False,
        },
        response_schema=_IDENTITY_LIST_RESPONSE_SCHEMA,
        group_key="groups",
        tags=("read-only", "group", "lookup"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to read one AD group's category / scope / description by "
                "identity. Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"identity": "Required. sAMAccountName / DN / GUID / SID."},
            "output_shape": "{'identity', 'rows': [<group>], 'total': <int>}.",
        },
    ),
    MsadOp(
        op_id="msad.group.members",
        handler_attr="msad_group_members",
        summary="List a group's members via ``Get-ADGroupMember -Identity``.",
        description=(
            "Runs ``Get-ADGroupMember -Identity <group>`` and returns one row "
            "per direct member (SamAccountName, Name, objectClass, DN) as a "
            "``{rows, total}`` envelope plus the echoed ``identity``. Read-only."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"identity": _GROUP_IDENTITY_PROP},
            "required": ["identity"],
            "additionalProperties": False,
        },
        response_schema=_IDENTITY_LIST_RESPONSE_SCHEMA,
        group_key="groups",
        tags=("read-only", "group", "members"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to list the direct members of an AD group (e.g. confirm a "
                "service account was granted). Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"identity": "Required. The group identity."},
            "output_shape": (
                "{'identity', 'rows': [{SamAccountName, objectClass, ...}], 'total': <int>}."
            ),
        },
    ),
    _member_write_op(
        "msad.group.add-member", "msad_group_add_member", "add-member", "Add-ADGroupMember"
    ),
    _member_write_op(
        "msad.group.remove-member",
        "msad_group_remove_member",
        "remove-member",
        "Remove-ADGroupMember",
    ),
    MsadOp(
        op_id="msad.group.delete",
        handler_attr="msad_group_delete",
        summary="Delete an AD group via ``Remove-ADGroup`` (dangerous, approval-gated).",
        description=(
            "Runs ``Remove-ADGroup -Identity <group> -Confirm:$false``. "
            "Irreversible — the group object and its SID are gone. "
            "safety_level=dangerous, requires_approval=True: a dispatch parks "
            "for a human to approve first."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"identity": _GROUP_IDENTITY_PROP},
            "required": ["identity"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "identity": {"type": "string"},
                "action": {"type": "string"},
                "op_class": {"type": "string", "enum": ["write"]},
            },
            "required": ["identity", "action", "op_class"],
            "additionalProperties": True,
        },
        group_key="groups",
        tags=("write", "group", "delete"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": (
                "Delete an AD group. Irreversible; approval-gated — parks for a "
                "human decision first. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"identity": "Required. The group identity to remove."},
            "output_shape": "{'identity', 'action': 'delete', 'op_class': 'write'}.",
        },
    ),
)
