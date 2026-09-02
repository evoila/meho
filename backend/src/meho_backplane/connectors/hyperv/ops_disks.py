# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""hyperv disks ops — ``disks.vm.list`` / ``disks.vhd.get`` / ``disks.vhd.chain`` (all safe).

The VHDX→VMDK planning input. Read-only virtual-disk facts via
``Get-VMHardDiskDrive`` (a VM's attached disks) and ``Get-VHD`` (one VHD/VHDX's
format / size / parent / fragmentation), routed through the shared
PowerShell-over-SSH transport
(:func:`~meho_backplane.connectors._shared.pwsh.pwsh_run`).

``disks.vhd.chain`` walks a differencing disk's parent chain from the leaf
(``.avhdx``) to the base disk by following ``ParentPath`` — a differencing chain
must be merged before a clean single-file VMDK conversion, so surfacing the full
chain is the load-bearing migration read.

``Get-VHD`` note: when a VHD is attached to a running VM on shared storage it can
only be read from the host currently using it (the cmdlet errors elsewhere); the
handler runs under ``$ErrorActionPreference = 'Stop'`` so that surfaces as a
real :class:`~meho_backplane.connectors._shared.pwsh.PwshRunError`, not a
false-empty read.

PowerShell injection safety
---------------------------

The operator-supplied ``vm_name`` / ``path`` are interpolated only inside
single-quoted literals via
:func:`~meho_backplane.connectors._shared.pwsh.ps_single_quote` (assigned to a
prelude variable, then referenced).

References
----------

* ``Get-VMHardDiskDrive`` (output ``Microsoft.HyperV.PowerShell.HardDiskDrive``):
  https://learn.microsoft.com/en-us/powershell/module/hyper-v/get-vmharddiskdrive
* ``Get-VHD`` (output ``Microsoft.Vhd.PowerShell.VirtualHardDisk``):
  https://learn.microsoft.com/en-us/powershell/module/hyper-v/get-vhd
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import ps_single_quote, pwsh_run
from meho_backplane.connectors.hyperv.ops import (
    SSH_TRANSPORT_NOTE,
    HypervOp,
    hyperv_list_read,
    normalise_json_rows,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.hyperv.connector import HypervConnector

__all__ = [
    "DISK_OPS",
    "hyperv_disks_vhd_chain",
    "hyperv_disks_vhd_get",
    "hyperv_disks_vm_list",
]


#: Attached-disk projection (per ``Get-VMHardDiskDrive`` row). ``ControllerType``
#: is an enum (IDE / SCSI) → stringified.
_VMDISK_SELECT: str = (
    "Path, @{N='ControllerType';E={\"$($_.ControllerType)\"}}, ControllerNumber, ControllerLocation"
)


async def hyperv_disks_vm_list(
    connector: HypervConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``hyperv.disks.vm.list`` — ``Get-VMHardDiskDrive -VMName`` (list)."""
    vm_name = params["vm_name"]
    result = await hyperv_list_read(
        connector,
        target,
        prelude=f"$n = {ps_single_quote(vm_name)}; ",
        pipeline=f"Get-VMHardDiskDrive -VMName $n | Select-Object {_VMDISK_SELECT}",
        operator=operator,
    )
    return {"vm_name": vm_name, **result}


async def hyperv_disks_vhd_get(
    connector: HypervConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``hyperv.disks.vhd.get`` — ``Get-VHD -Path`` (one disk's facts).

    Returns the format (VHD / VHDX), type (Fixed / Dynamic / Differencing),
    virtual size, on-disk file size, minimum size, parent path (for a
    differencing disk), fragmentation percentage, and identifiers. Read-only.
    """
    p = ps_single_quote(params["path"])
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$v = Get-VHD -Path {p}; "
        "ConvertTo-Json -Depth 3 -Compress -InputObject @{ "
        "Path = $v.Path; "
        'VhdFormat = "$($v.VhdFormat)"; '
        'VhdType = "$($v.VhdType)"; '
        "Size = $v.Size; "
        "FileSize = $v.FileSize; "
        "MinimumSize = $v.MinimumSize; "
        "ParentPath = $v.ParentPath; "
        "FragmentationPercentage = $v.FragmentationPercentage; "
        "Attached = $v.Attached; "
        "DiskIdentifier = $v.DiskIdentifier; "
        "BlockSize = $v.BlockSize; "
        "LogicalSectorSize = $v.LogicalSectorSize; "
        "PhysicalSectorSize = $v.PhysicalSectorSize }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    return payload if isinstance(payload, dict) else {"value": payload}


async def hyperv_disks_vhd_chain(
    connector: HypervConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``hyperv.disks.vhd.chain`` — the differencing parent chain (list).

    Walks ``ParentPath`` from the given leaf disk to the base, returning one row
    per link (Path, VhdFormat, VhdType, Size, FileSize, ParentPath) ordered
    leaf → base. A single-file (non-differencing) disk yields a one-row chain.
    Read-only.
    """
    p = ps_single_quote(params["path"])
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$p = {p}; "
        "$chain = @(); "
        "while ($p) { "
        "$v = Get-VHD -Path $p; "
        "$chain += @{ Path = $v.Path; "
        'VhdFormat = "$($v.VhdFormat)"; '
        'VhdType = "$($v.VhdType)"; '
        "Size = $v.Size; FileSize = $v.FileSize; ParentPath = $v.ParentPath }; "
        "$p = $v.ParentPath }; "
        "ConvertTo-Json -Depth 4 -InputObject @{ rows = $chain; total = $chain.Count }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    raw_rows = payload.get("rows") if isinstance(payload, dict) else payload
    rows = normalise_json_rows(raw_rows)
    return {"path": params["path"], "rows": rows, "total": len(rows)}


_VM_NAME_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": "The virtual machine name (the ``Get-VMHardDiskDrive -VMName`` operand).",
}

_PATH_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": (
        "The path to the VHD / VHDX file on the Hyper-V host (the ``Get-VHD "
        "-Path`` operand, e.g. ``C:\\\\VMs\\\\sql-01\\\\disk0.vhdx``)."
    ),
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


DISK_OPS: tuple[HypervOp, ...] = (
    HypervOp(
        op_id="hyperv.disks.vm.list",
        handler_attr="hyperv_disks_vm_list",
        summary="List a VM's attached virtual disks via ``Get-VMHardDiskDrive`` (list).",
        description=(
            "Runs ``Get-VMHardDiskDrive -VMName <vm_name>`` and returns one row "
            "per attached virtual disk (Path, ControllerType — IDE / SCSI, "
            "ControllerNumber, ControllerLocation). Read-only; maps a VM to the "
            "VHD/VHDX files that feed ``hyperv.disks.vhd.get`` / ``.chain``."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"vm_name": _VM_NAME_PROP},
            "required": ["vm_name"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "vm_name": {"type": "string"},
                "rows": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            },
            "required": ["vm_name", "rows", "total"],
            "additionalProperties": True,
        },
        group_key="disks",
        tags=("read-only", "disks", "inventory"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to enumerate a VM's attached virtual disks (controller + "
                "file path) before reading each VHD/VHDX's facts. Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"vm_name": "Required. The VM name."},
            "output_shape": (
                "{'vm_name', 'rows': [{Path, ControllerType, ControllerNumber, "
                "ControllerLocation}], 'total': <int>}."
            ),
        },
    ),
    HypervOp(
        op_id="hyperv.disks.vhd.get",
        handler_attr="hyperv_disks_vhd_get",
        summary="Read one VHD/VHDX's facts via ``Get-VHD -Path`` (format / size / parent).",
        description=(
            "Runs ``Get-VHD -Path <path>`` and returns the disk's format (VHD / "
            "VHDX), type (Fixed / Dynamic / Differencing), virtual size, on-disk "
            "file size, minimum size, parent path (for a differencing disk), "
            "fragmentation percentage, and identifiers. Read-only; the primary "
            "VHDX→VMDK planning input."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"path": _PATH_PROP},
            "required": ["path"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "Path": {"type": ["string", "null"]},
                "VhdFormat": {"type": ["string", "null"]},
                "VhdType": {"type": ["string", "null"]},
                "Size": {"type": ["integer", "null"]},
                "FileSize": {"type": ["integer", "null"]},
                "ParentPath": {"type": ["string", "null"]},
                "FragmentationPercentage": {"type": ["integer", "null"]},
            },
            "additionalProperties": True,
        },
        group_key="disks",
        tags=("read-only", "disks", "vhd"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to read one VHD/VHDX file's format, size, parent, and "
                "fragmentation — the input to a VHDX→VMDK conversion plan. "
                "Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"path": "Required. The VHD/VHDX file path on the Hyper-V host."},
            "output_shape": (
                "Flat dict (VhdFormat, VhdType, Size, FileSize, MinimumSize, "
                "ParentPath, FragmentationPercentage, ...)."
            ),
        },
    ),
    HypervOp(
        op_id="hyperv.disks.vhd.chain",
        handler_attr="hyperv_disks_vhd_chain",
        summary="Walk a differencing disk's parent chain (leaf → base; list).",
        description=(
            "Follows ``ParentPath`` from the given leaf VHD/VHDX to the base "
            "disk and returns one row per link (Path, VhdFormat, VhdType, Size, "
            "FileSize, ParentPath), ordered leaf → base. A single-file "
            "(non-differencing) disk yields a one-row chain. Read-only; a "
            "differencing chain must be merged before a clean single-file VMDK "
            "conversion."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"path": _PATH_PROP},
            "required": ["path"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "rows": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            },
            "required": ["path", "rows", "total"],
            "additionalProperties": True,
        },
        group_key="disks",
        tags=("read-only", "disks", "vhd", "chain"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to see a differencing disk's full parent chain to the base "
                "(the links that must be merged before a single-file VMDK "
                "conversion). Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"path": "Required. The leaf VHD/VHDX file path."},
            "output_shape": (
                "{'path', 'rows': [{Path, VhdType, ParentPath, ...}] leaf→base, 'total': <int>}."
            ),
        },
    ),
)
