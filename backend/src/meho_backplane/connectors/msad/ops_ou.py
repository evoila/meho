# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""msad organizational-unit ops — ``list`` (safe) + ``create`` / ``move`` (caution).

OU inventory / provisioning via the ``ActiveDirectory`` module cmdlets
(``Get-ADOrganizationalUnit`` / ``New-ADOrganizationalUnit`` / ``Move-ADObject``)
over the shared PowerShell-over-SSH transport. ``create`` and ``move`` are
``caution`` (recoverable — an OU can be moved back / removed). OU deletion is out
of scope for this connector cut.

PowerShell injection safety
---------------------------

The OU name, distinguished names, and target path are interpolated only inside
single-quoted PowerShell literals via
:func:`~meho_backplane.connectors._shared.pwsh.ps_single_quote`; the
``protected`` toggle renders as a ``$true`` / ``$false`` literal.

References
----------

* ``Get-ADOrganizationalUnit`` / ``New-ADOrganizationalUnit`` / ``Move-ADObject``:
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
    "OU_OPS",
    "msad_ou_create",
    "msad_ou_list",
    "msad_ou_move",
]

_OU_SELECT: str = "Name, DistinguishedName, Description, ProtectedFromAccidentalDeletion"


async def msad_ou_list(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.ou.list`` — ``Get-ADOrganizationalUnit -Filter *`` (list, capped)."""
    limit = validate_limit(params.get("limit"))
    return await ad_list_read(
        connector,
        target,
        pipeline=(
            f"Get-ADOrganizationalUnit -Filter * -ResultSetSize {limit} "
            f"-Properties Description,ProtectedFromAccidentalDeletion "
            f"| Select-Object {_OU_SELECT}"
        ),
        operator=operator,
    )


async def msad_ou_create(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.ou.create`` (caution) — ``New-ADOrganizationalUnit``.

    Creates an OU under the optional parent ``path`` (defaults to the domain
    root). ``protected`` maps to ``-ProtectedFromAccidentalDeletion`` (default
    true, matching the cmdlet default). Recoverable. safety_level=caution.
    """
    name = params["name"]
    clauses = [f"New-ADOrganizationalUnit -Name {ps_single_quote(name)}"]
    if params.get("path") is not None:
        clauses.append(f"-Path {ps_single_quote(params['path'])}")
    protected = params.get("protected")
    if protected is not None:
        clauses.append(f"-ProtectedFromAccidentalDeletion ${'true' if protected else 'false'}")
    new_expr = " ".join(clauses)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"{new_expr} -PassThru "
        f"| Select-Object {_OU_SELECT} "
        "| ConvertTo-Json -Depth 3 -Compress"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    ou = payload if isinstance(payload, dict) else None
    return {"name": name, "action": "create", "ou": ou, "op_class": "write"}


async def msad_ou_move(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.ou.move`` (caution) — ``Move-ADObject``.

    Moves the object at ``identity`` (a distinguished name) under the new parent
    ``target_path`` (a distinguished name). Recoverable (move it back).
    safety_level=caution.
    """
    identity = params["identity"]
    target_path = params["target_path"]
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"Move-ADObject -Identity {ps_single_quote(identity)} "
        f"-TargetPath {ps_single_quote(target_path)}; "
        "ConvertTo-Json -Compress -InputObject @{ ok = $true }"
    )
    await pwsh_run(connector, target, script, operator=operator)
    return {
        "identity": identity,
        "target_path": target_path,
        "action": "move",
        "op_class": "write",
    }


_LIMIT_PROP: dict[str, Any] = {
    "type": "integer",
    "minimum": 1,
    "description": "Max rows to return (maps to ``-ResultSetSize``). Default 500.",
}

_DN_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": "A distinguished name (e.g. ``OU=Servers,DC=c1sql,DC=lab``).",
}


OU_OPS: tuple[MsadOp, ...] = (
    MsadOp(
        op_id="msad.ou.list",
        handler_attr="msad_ou_list",
        summary="List OUs via ``Get-ADOrganizationalUnit -Filter *`` (capped by limit).",
        description=(
            "Runs ``Get-ADOrganizationalUnit -Filter *`` (capped at ``limit`` "
            "rows, default 500) and returns one row per OU (Name, "
            "DistinguishedName, Description, ProtectedFromAccidentalDeletion). "
            "Read-only."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"limit": _LIMIT_PROP},
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "rows": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            },
            "required": ["rows", "total"],
            "additionalProperties": True,
        },
        group_key="ou",
        tags=("read-only", "ou", "inventory"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to enumerate the OU tree (find the DN of an OU to create "
                "objects in or move objects to). Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"limit": "Optional; max rows (default 500)."},
            "output_shape": (
                "{'rows': [{Name, DistinguishedName, ProtectedFromAccidentalDeletion}], "
                "'total': <int>}."
            ),
        },
    ),
    MsadOp(
        op_id="msad.ou.create",
        handler_attr="msad_ou_create",
        summary="Create an OU via ``New-ADOrganizationalUnit`` (caution).",
        description=(
            "Runs ``New-ADOrganizationalUnit -Name <name>`` under the optional "
            "parent ``path`` (defaults to the domain root). ``protected`` maps "
            "to ``-ProtectedFromAccidentalDeletion`` (default true). "
            "Recoverable. safety_level=caution."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": "\\S",
                    "description": "The OU name (``-Name``).",
                },
                "path": {
                    "type": "string",
                    "description": "Optional parent OU / container distinguished name.",
                },
                "protected": {
                    "type": "boolean",
                    "description": "``-ProtectedFromAccidentalDeletion``. Default true.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "action": {"type": "string"},
                "ou": {"type": ["object", "null"]},
                "op_class": {"type": "string", "enum": ["write"]},
            },
            "required": ["name", "action", "op_class"],
            "additionalProperties": True,
        },
        group_key="ou",
        tags=("write", "ou", "create"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Create an OU (optionally under a parent OU). Recoverable; "
                "safety_level=caution. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "name": "Required. The OU name.",
                "path": "Optional. Parent OU/container DN (defaults to domain root).",
            },
            "output_shape": "{'name', 'action': 'create', 'ou': <created>, 'op_class': 'write'}.",
        },
    ),
    MsadOp(
        op_id="msad.ou.move",
        handler_attr="msad_ou_move",
        summary="Move an object / OU to a new parent via ``Move-ADObject`` (caution).",
        description=(
            "Runs ``Move-ADObject -Identity <dn> -TargetPath <parent-dn>`` — "
            "moves the object at ``identity`` under the parent at "
            "``target_path``. Recoverable (move it back). safety_level=caution. "
            "Note: a ``ProtectedFromAccidentalDeletion`` object refuses the "
            "move until the protection is cleared."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "identity": {
                    **_DN_PROP,
                    "description": "The distinguished name of the object / OU to move.",
                },
                "target_path": {
                    **_DN_PROP,
                    "description": "The distinguished name of the new parent OU / container.",
                },
            },
            "required": ["identity", "target_path"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "identity": {"type": "string"},
                "target_path": {"type": "string"},
                "action": {"type": "string"},
                "op_class": {"type": "string", "enum": ["write"]},
            },
            "required": ["identity", "target_path", "action", "op_class"],
            "additionalProperties": True,
        },
        group_key="ou",
        tags=("write", "ou", "move"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Move an object or OU to a new parent OU (both given as "
                "distinguished names). Recoverable; safety_level=caution. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "identity": "Required. DN of the object/OU to move.",
                "target_path": "Required. DN of the new parent OU/container.",
            },
            "output_shape": ("{'identity', 'target_path', 'action': 'move', 'op_class': 'write'}."),
        },
    ),
)
