# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""hyperv host ops — ``host.info`` / ``host.numa`` / ``host.vswitch.list`` (all safe).

Read-only Hyper-V host facts via ``Get-VMHost`` (logical processors, memory
capacity, NUMA spanning, default VM / VHD paths), ``Get-VMHostNumaNode`` (the
host's NUMA topology), and ``Get-VMSwitch`` (the virtual-switch inventory),
routed through the shared PowerShell-over-SSH transport
(:func:`~meho_backplane.connectors._shared.pwsh.pwsh_run`). No operator input is
interpolated into any of these scripts (they are constants), so the host-read
group has no injection surface.

References
----------

* ``Get-VMHost`` (output ``Microsoft.HyperV.PowerShell.VMHost``):
  https://learn.microsoft.com/en-us/powershell/module/hyper-v/get-vmhost
* ``Get-VMHostNumaNode`` (output ``Microsoft.HyperV.PowerShell.VMHostNumaNode``):
  https://learn.microsoft.com/en-us/powershell/module/hyper-v/get-vmhostnumanode
* ``Get-VMSwitch`` (output ``Microsoft.HyperV.PowerShell.VMSwitch``;
  ``SwitchType`` is External / Internal / Private):
  https://learn.microsoft.com/en-us/powershell/module/hyper-v/get-vmswitch
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import pwsh_run
from meho_backplane.connectors.hyperv.ops import (
    SSH_TRANSPORT_NOTE,
    HypervOp,
    hyperv_list_read,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.hyperv.connector import HypervConnector

__all__ = [
    "HOST_OPS",
    "hyperv_host_info",
    "hyperv_host_numa",
    "hyperv_host_vswitch_list",
]


#: Host projection — a flat dict of ``Get-VMHost`` fields the migration
#: assessment needs. Constant script (no operator input). ``MemoryCapacity`` is
#: bytes; the default paths anchor where new / exported artifacts land.
_HOST_INFO_SCRIPT: str = (
    "$ErrorActionPreference = 'Stop'; "
    "$h = Get-VMHost; "
    "ConvertTo-Json -Depth 3 -Compress -InputObject @{ "
    "Hostname = [System.Net.Dns]::GetHostName(); "
    "LogicalProcessorCount = $h.LogicalProcessorCount; "
    "MemoryCapacity = $h.MemoryCapacity; "
    "NumaSpanningEnabled = $h.NumaSpanningEnabled; "
    "VirtualHardDiskPath = $h.VirtualHardDiskPath; "
    "VirtualMachinePath = $h.VirtualMachinePath; "
    "MaximumStorageMigrations = $h.MaximumStorageMigrations; "
    "MaximumVirtualMachineMigrations = $h.MaximumVirtualMachineMigrations }"
)

#: NUMA topology — one row per node. ``ProcessorsAvailability`` is an array (per
#: logical processor), so ``-Depth 4`` in the list helper keeps it intact.
_NUMA_PIPELINE: str = (
    "Get-VMHostNumaNode | Select-Object "
    "NodeId, ProcessorsAvailability, MemoryAvailable, MemoryTotal"
)

#: Virtual-switch inventory — one row per switch. ``SwitchType`` is stringified
#: (the enum renders as its integer under Windows PowerShell 5.1 ConvertTo-Json
#: otherwise).
_VSWITCH_PIPELINE: str = (
    "Get-VMSwitch | Select-Object "
    "Name, @{N='SwitchType';E={\"$($_.SwitchType)\"}}, "
    "NetAdapterInterfaceDescription, AllowManagementOS, "
    "@{N='Id';E={$_.Id.ToString()}}, EmbeddedTeamingEnabled"
)


async def hyperv_host_info(
    connector: HypervConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``hyperv.host.info`` — the ``Get-VMHost`` projection.

    Reads logical processor count, memory capacity (bytes), NUMA spanning, and
    the host's default VM / VHD paths. Read-only.
    """
    del params  # declared empty in schema; intentionally ignored
    payload = await pwsh_run(connector, target, _HOST_INFO_SCRIPT, operator=operator)
    return payload if isinstance(payload, dict) else {"value": payload}


async def hyperv_host_numa(
    connector: HypervConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``hyperv.host.numa`` — ``Get-VMHostNumaNode`` (list-shaped)."""
    del params
    return await hyperv_list_read(connector, target, pipeline=_NUMA_PIPELINE, operator=operator)


async def hyperv_host_vswitch_list(
    connector: HypervConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``hyperv.host.vswitch.list`` — ``Get-VMSwitch`` (list-shaped)."""
    del params
    return await hyperv_list_read(connector, target, pipeline=_VSWITCH_PIPELINE, operator=operator)


_EMPTY_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
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


HOST_OPS: tuple[HypervOp, ...] = (
    HypervOp(
        op_id="hyperv.host.info",
        handler_attr="hyperv_host_info",
        summary="Read Hyper-V host facts via ``Get-VMHost`` (CPUs, memory, NUMA, default paths).",
        description=(
            "Runs ``Get-VMHost`` and returns the host's logical processor "
            "count, memory capacity (bytes), whether NUMA spanning is enabled, "
            "the default virtual-machine and virtual-hard-disk paths, and the "
            "storage / live-migration limits. Read-only; the host-capacity "
            "context a migration plan sizes against."
        ),
        parameter_schema=_EMPTY_PARAMS_SCHEMA,
        response_schema={
            "type": "object",
            "properties": {
                "Hostname": {"type": ["string", "null"]},
                "LogicalProcessorCount": {"type": ["integer", "null"]},
                "MemoryCapacity": {"type": ["integer", "null"]},
                "NumaSpanningEnabled": {"type": ["boolean", "null"]},
                "VirtualHardDiskPath": {"type": ["string", "null"]},
                "VirtualMachinePath": {"type": ["string", "null"]},
            },
            "additionalProperties": True,
        },
        group_key="host",
        tags=("read-only", "host", "capacity"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call for the Hyper-V host's capacity facts — logical CPUs, "
                "memory, NUMA spanning, default VM / VHD paths. Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "Flat dict (LogicalProcessorCount, MemoryCapacity, "
                "NumaSpanningEnabled, VirtualHardDiskPath, VirtualMachinePath, "
                "...)."
            ),
        },
    ),
    HypervOp(
        op_id="hyperv.host.numa",
        handler_attr="hyperv_host_numa",
        summary="List the host's NUMA topology via ``Get-VMHostNumaNode`` (list-shaped).",
        description=(
            "Runs ``Get-VMHostNumaNode`` and returns one row per NUMA node "
            "(NodeId, per-processor availability, available / total memory). "
            "Read-only; NUMA topology informs how a large source VM's vCPU / "
            "memory maps onto the target host."
        ),
        parameter_schema=_EMPTY_PARAMS_SCHEMA,
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="host",
        tags=("read-only", "host", "numa"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to read the Hyper-V host's NUMA node topology. "
                "Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{'rows': [{NodeId, ProcessorsAvailability, MemoryAvailable, "
                "MemoryTotal}], 'total': <int>}."
            ),
        },
    ),
    HypervOp(
        op_id="hyperv.host.vswitch.list",
        handler_attr="hyperv_host_vswitch_list",
        summary="List virtual switches via ``Get-VMSwitch`` (list-shaped).",
        description=(
            "Runs ``Get-VMSwitch`` and returns one row per virtual switch "
            "(Name, SwitchType — External / Internal / Private, bound physical "
            "adapter description, whether the management OS shares it). "
            "Read-only; the source networking a migration must reproduce on the "
            "target port groups."
        ),
        parameter_schema=_EMPTY_PARAMS_SCHEMA,
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="host",
        tags=("read-only", "host", "networking"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to enumerate the Hyper-V host's virtual switches (the "
                "source networking to reproduce on the target). Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{'rows': [{Name, SwitchType, NetAdapterInterfaceDescription, "
                "AllowManagementOS, Id}], 'total': <int>}."
            ),
        },
    ),
)
