# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""hyperv checkpoints ops — ``list`` (safe) + ``create`` (caution) + guarded writes.

Checkpoint (snapshot) management via the Hyper-V module cmdlets over the shared
PowerShell-over-SSH transport
(:func:`~meho_backplane.connectors._shared.pwsh.pwsh_run`):

* ``list``   — ``Get-VMSnapshot -VMName`` (read-only, ``safe``).
* ``create`` — ``Checkpoint-VM`` (``caution`` — recoverable).
* ``revert`` — ``Restore-VMSnapshot`` (``dangerous`` + ``requires_approval``): a
  revert discards everything written to the VM since the checkpoint.
* ``delete`` — ``Remove-VMSnapshot`` (``dangerous`` + ``requires_approval``):
  irreversible removal.

``revert`` / ``delete`` follow the rke2 approval-parked-write mold: the
dispatcher's policy gate parks a dispatch at ``needs-approval`` for a human
decision, and the handler runs only on the ``_approved=True`` resume path. Per
the Initiative #3259 satellite table a destructive op is satellite-EXCLUDED by
the tier ladder — central-dial / on-site only. ``-Confirm:$false`` suppresses
the cmdlet's own interactive prompt (it would hang non-interactively); the
approval gate is MEHO's, not Hyper-V's.

PowerShell injection safety
---------------------------

The operator-supplied ``vm_name`` / ``checkpoint_name`` are interpolated only
inside single-quoted literals via
:func:`~meho_backplane.connectors._shared.pwsh.ps_single_quote`.

References
----------

* ``Get-VMSnapshot`` / ``Checkpoint-VM`` / ``Restore-VMSnapshot`` /
  ``Remove-VMSnapshot`` (Hyper-V module; snapshots were renamed *checkpoints* in
  Windows Server 2012 R2 — the cmdlet nouns kept ``Snapshot``):
  https://learn.microsoft.com/en-us/powershell/module/hyper-v/get-vmsnapshot
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import ps_single_quote, pwsh_run
from meho_backplane.connectors.hyperv.ops import (
    SSH_TRANSPORT_NOTE,
    HypervOp,
    hyperv_list_read,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.hyperv.connector import HypervConnector

__all__ = [
    "CHECKPOINT_OPS",
    "hyperv_checkpoints_create",
    "hyperv_checkpoints_delete",
    "hyperv_checkpoints_list",
    "hyperv_checkpoints_revert",
]


#: Checkpoint projection (per ``Get-VMSnapshot`` row). ``SnapshotType`` is an
#: enum, ``CreationTime`` a DateTime, ``Id`` a Guid → all stringified.
_CHECKPOINT_SELECT: str = (
    "Name, VMName, @{N='SnapshotType';E={\"$($_.SnapshotType)\"}}, "
    "@{N='CreationTime';E={$_.CreationTime.ToString('o')}}, "
    "ParentSnapshotName, @{N='Id';E={$_.Id.ToString()}}"
)


async def hyperv_checkpoints_list(
    connector: HypervConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``hyperv.checkpoints.list`` — ``Get-VMSnapshot -VMName`` (list)."""
    vm_name = params["vm_name"]
    result = await hyperv_list_read(
        connector,
        target,
        prelude=f"$n = {ps_single_quote(vm_name)}; ",
        pipeline=f"Get-VMSnapshot -VMName $n | Select-Object {_CHECKPOINT_SELECT}",
        operator=operator,
    )
    return {"vm_name": vm_name, **result}


async def hyperv_checkpoints_create(
    connector: HypervConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``hyperv.checkpoints.create`` (caution) — ``Checkpoint-VM``.

    Creates a checkpoint of the VM. When ``checkpoint_name`` is omitted Hyper-V
    auto-names it (VM name + timestamp). ``-Passthru`` returns the created
    checkpoint so the handler echoes its resolved name.
    """
    vm_name = params["vm_name"]
    n = ps_single_quote(vm_name)
    name_clause = ""
    checkpoint_name = params.get("checkpoint_name")
    if checkpoint_name is not None:
        name_clause = f" -SnapshotName {ps_single_quote(checkpoint_name)}"
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$n = {n}; "
        f"$s = Checkpoint-VM -Name $n{name_clause} -Passthru; "
        "ConvertTo-Json -Compress -InputObject @{ ok = $true; vm = $n; checkpoint = $s.Name }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    data = payload if isinstance(payload, dict) else {}
    return {
        "vm_name": vm_name,
        "checkpoint_name": data.get("checkpoint"),
        "action": "create",
        "op_class": "write",
    }


async def hyperv_checkpoints_revert(
    connector: HypervConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``hyperv.checkpoints.revert`` (dangerous, resume path only).

    Runs ``Restore-VMSnapshot -Name <checkpoint_name> -VMName <vm_name>
    -Confirm:$false``. Discards state written since the checkpoint —
    ``requires_approval`` so a dispatch parks for a human decision first.
    """
    return await _checkpoint_action(
        connector, target, params, cmdlet="Restore-VMSnapshot", action="revert", operator=operator
    )


async def hyperv_checkpoints_delete(
    connector: HypervConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``hyperv.checkpoints.delete`` (dangerous, resume path only).

    Runs ``Remove-VMSnapshot -Name <checkpoint_name> -VMName <vm_name>
    -Confirm:$false``. Irreversible — ``requires_approval`` so a dispatch parks
    for a human decision first.
    """
    return await _checkpoint_action(
        connector, target, params, cmdlet="Remove-VMSnapshot", action="delete", operator=operator
    )


async def _checkpoint_action(
    connector: HypervConnector,
    target: Any,
    params: dict[str, Any],
    *,
    cmdlet: str,
    action: str,
    operator: Operator | None,
) -> dict[str, Any]:
    """Run a ``-Name <checkpoint> -VMName <vm> -Confirm:$false`` checkpoint write."""
    vm_name = params["vm_name"]
    checkpoint_name = params["checkpoint_name"]
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"{cmdlet} -Name {ps_single_quote(checkpoint_name)} "
        f"-VMName {ps_single_quote(vm_name)} -Confirm:$false; "
        "ConvertTo-Json -Compress -InputObject @{ ok = $true }"
    )
    await pwsh_run(connector, target, script, operator=operator)
    return {
        "vm_name": vm_name,
        "checkpoint_name": checkpoint_name,
        "action": action,
        "op_class": "write",
    }


_VM_NAME_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": "The virtual machine name (the ``-VMName`` / ``-Name`` VM operand).",
}

_CHECKPOINT_NAME_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": "The checkpoint (snapshot) name.",
}

_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm_name": {"type": "string"},
        "rows": {"type": "array", "items": {"type": "object"}},
        "total": {"type": "integer"},
    },
    "required": ["vm_name", "rows", "total"],
    "additionalProperties": True,
}

_WRITE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm_name": {"type": "string"},
        "checkpoint_name": {"type": ["string", "null"]},
        "action": {"type": "string"},
        "op_class": {"type": "string", "enum": ["write"]},
    },
    "required": ["vm_name", "action", "op_class"],
    "additionalProperties": True,
}


def _checkpoint_write_op(
    op_id: str,
    handler_attr: str,
    verb: str,
    cmdlet: str,
    *,
    safety_level: str,
    requires_approval: bool,
) -> HypervOp:
    """Build one identity-based checkpoint write op (revert / delete)."""
    note = (
        "Destructive; approval-gated — parks for a human decision first."
        if requires_approval
        else "Recoverable."
    )
    return HypervOp(
        op_id=op_id,
        handler_attr=handler_attr,
        summary=f"{verb.capitalize()} a VM checkpoint via ``{cmdlet}``.",
        description=(
            f"Runs ``{cmdlet} -Name <checkpoint_name> -VMName <vm_name> "
            f"-Confirm:$false``. {note} safety_level={safety_level}."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"vm_name": _VM_NAME_PROP, "checkpoint_name": _CHECKPOINT_NAME_PROP},
            "required": ["vm_name", "checkpoint_name"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_RESPONSE_SCHEMA,
        group_key="checkpoints",
        tags=("write", "checkpoint", verb),
        safety_level=safety_level,  # type: ignore[arg-type]
        requires_approval=requires_approval,
        llm_instructions={
            "when_to_use": (
                f"{verb.capitalize()} a named VM checkpoint. {note} " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "vm_name": "Required. The VM name.",
                "checkpoint_name": "Required. The checkpoint name.",
            },
            "output_shape": (
                f"{{'vm_name', 'checkpoint_name', 'action': '{verb}', 'op_class': 'write'}}."
            ),
        },
    )


CHECKPOINT_OPS: tuple[HypervOp, ...] = (
    HypervOp(
        op_id="hyperv.checkpoints.list",
        handler_attr="hyperv_checkpoints_list",
        summary="List a VM's checkpoints via ``Get-VMSnapshot -VMName`` (list).",
        description=(
            "Runs ``Get-VMSnapshot -VMName <vm_name>`` and returns one row per "
            "checkpoint (Name, VMName, SnapshotType, CreationTime, "
            "ParentSnapshotName, Id). Read-only; a VM with a live checkpoint "
            "tree carries differencing (.avhdx) disks that complicate a "
            "migration."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"vm_name": _VM_NAME_PROP},
            "required": ["vm_name"],
            "additionalProperties": False,
        },
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="checkpoints",
        tags=("read-only", "checkpoint", "inventory"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to enumerate a VM's checkpoints (snapshots). Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"vm_name": "Required. The VM name."},
            "output_shape": (
                "{'vm_name', 'rows': [{Name, SnapshotType, CreationTime, "
                "ParentSnapshotName, Id}], 'total': <int>}."
            ),
        },
    ),
    HypervOp(
        op_id="hyperv.checkpoints.create",
        handler_attr="hyperv_checkpoints_create",
        summary="Create a VM checkpoint via ``Checkpoint-VM`` (caution).",
        description=(
            "Runs ``Checkpoint-VM -Name <vm_name>`` (optionally ``-SnapshotName "
            "<checkpoint_name>``; omit to let Hyper-V auto-name it). "
            "safety_level=caution — recoverable. Take a checkpoint before a "
            "risky migration cutover step so the source can be rolled back."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "vm_name": _VM_NAME_PROP,
                "checkpoint_name": {
                    **_CHECKPOINT_NAME_PROP,
                    "description": (
                        "Optional checkpoint name (``-SnapshotName``); omit to "
                        "let Hyper-V auto-name it (VM name + timestamp)."
                    ),
                },
            },
            "required": ["vm_name"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_RESPONSE_SCHEMA,
        group_key="checkpoints",
        tags=("write", "checkpoint", "create"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Create a checkpoint of a VM (e.g. before a cutover step). "
                "Recoverable; safety_level=caution. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "vm_name": "Required. The VM name.",
                "checkpoint_name": "Optional. The checkpoint name; auto-named if omitted.",
            },
            "output_shape": (
                "{'vm_name', 'checkpoint_name': <resolved>, 'action': 'create', "
                "'op_class': 'write'}."
            ),
        },
    ),
    _checkpoint_write_op(
        "hyperv.checkpoints.revert",
        "hyperv_checkpoints_revert",
        "revert",
        "Restore-VMSnapshot",
        safety_level="dangerous",
        requires_approval=True,
    ),
    _checkpoint_write_op(
        "hyperv.checkpoints.delete",
        "hyperv_checkpoints_delete",
        "delete",
        "Remove-VMSnapshot",
        safety_level="dangerous",
        requires_approval=True,
    ),
)
