# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""hyperv vms ops — ``list`` / ``get`` / ``config`` / ``state`` (all safe).

The migration-assessment surface. Read-only VM facts via ``Get-VM`` (inventory,
identity, deep configuration, runtime state) and — for a Generation 2 VM —
``Get-VMFirmware`` (secure-boot state), routed through the shared
PowerShell-over-SSH transport
(:func:`~meho_backplane.connectors._shared.pwsh.pwsh_run`). ``Generation`` and
secure-boot decide the target-side VMware firmware (Gen 1 → BIOS, Gen 2 → EFI,
secure-boot On → EFI Secure Boot); ``IntegrationServicesVersion`` flags a guest
whose integration components need refreshing before / after the move.

Enum, ``Guid``, ``TimeSpan`` and ``Version`` properties are stringified with
``Select-Object`` calculated properties / ``"$(...)"`` because Windows
PowerShell 5.1 ``ConvertTo-Json`` renders an enum as its integer value and a
compound object as a nested map — a stringified projection keeps the
assessment surface readable and stable.

PowerShell injection safety
---------------------------

The operator-supplied ``vm_name`` is interpolated only inside a single-quoted
literal via :func:`~meho_backplane.connectors._shared.pwsh.ps_single_quote`
(assigned to ``$n`` in a prelude, then referenced as a variable). The list op
takes no operator input (constant script).

References
----------

* ``Get-VM`` (output ``Microsoft.HyperV.PowerShell.VirtualMachine``):
  https://learn.microsoft.com/en-us/powershell/module/hyper-v/get-vm
* ``Get-VMFirmware`` (Generation 2 secure-boot state):
  https://learn.microsoft.com/en-us/powershell/module/hyper-v/get-vmfirmware
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
    "VMS_OPS",
    "hyperv_vms_config",
    "hyperv_vms_get",
    "hyperv_vms_list",
    "hyperv_vms_state",
]


#: The bounded projection every VM inventory read returns — a stable, JSON-safe
#: subset with enum / Guid / TimeSpan / Version fields stringified.
_VM_SELECT: str = (
    "Name, @{N='Id';E={$_.Id.ToString()}}, @{N='State';E={\"$($_.State)\"}}, "
    "Status, CPUUsage, MemoryAssigned, @{N='Uptime';E={$_.Uptime.ToString()}}, "
    "@{N='Version';E={\"$($_.Version)\"}}, Generation, ProcessorCount, "
    "@{N='IntegrationServicesVersion';E={if ($_.IntegrationServicesVersion) "
    "{ $_.IntegrationServicesVersion.ToString() } else { $null }}}, "
    "IntegrationServicesState, Path"
)


async def hyperv_vms_list(
    connector: HypervConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``hyperv.vms.list`` — ``Get-VM`` (list-shaped, JSONFlux-reduced)."""
    del params
    return await hyperv_list_read(
        connector, target, pipeline=f"Get-VM | Select-Object {_VM_SELECT}", operator=operator
    )


async def hyperv_vms_get(
    connector: HypervConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``hyperv.vms.get`` — ``Get-VM -Name`` (one VM)."""
    vm_name = params["vm_name"]
    result = await hyperv_list_read(
        connector,
        target,
        prelude=f"$n = {ps_single_quote(vm_name)}; ",
        pipeline=f"Get-VM -Name $n | Select-Object {_VM_SELECT}",
        operator=operator,
    )
    return {"vm_name": vm_name, **result}


async def hyperv_vms_config(
    connector: HypervConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``hyperv.vms.config`` — deep configuration of one VM.

    Reads memory (startup / min / max, dynamic), processor count, generation,
    automatic start / stop actions, checkpoint type, integration-services
    version, and — for a Generation 2 VM — the secure-boot state via
    ``Get-VMFirmware`` (guarded: a Generation 1 VM is BIOS and has none, so
    ``SecureBoot`` is ``null``). Read-only.
    """
    n = ps_single_quote(params["vm_name"])
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$n = {n}; "
        "$vm = Get-VM -Name $n; "
        "$secureboot = $null; "
        "if ($vm.Generation -eq 2) { try { "
        '$secureboot = "$((Get-VMFirmware -VMName $n).SecureBoot)" } '
        "catch { $secureboot = $null } }; "
        "ConvertTo-Json -Depth 4 -Compress -InputObject @{ "
        "Name = $vm.Name; "
        "Generation = $vm.Generation; "
        'Version = "$($vm.Version)"; '
        "ProcessorCount = $vm.ProcessorCount; "
        "MemoryStartup = $vm.MemoryStartup; "
        "MemoryMinimum = $vm.MemoryMinimum; "
        "MemoryMaximum = $vm.MemoryMaximum; "
        "DynamicMemoryEnabled = $vm.DynamicMemoryEnabled; "
        'AutomaticStartAction = "$($vm.AutomaticStartAction)"; '
        'AutomaticStopAction = "$($vm.AutomaticStopAction)"; '
        'CheckpointType = "$($vm.CheckpointType)"; '
        "IntegrationServicesVersion = if ($vm.IntegrationServicesVersion) "
        "{ $vm.IntegrationServicesVersion.ToString() } else { $null }; "
        "ConfigurationLocation = $vm.ConfigurationLocation; "
        "SecureBoot = $secureboot }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    return payload if isinstance(payload, dict) else {"value": payload}


async def hyperv_vms_state(
    connector: HypervConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``hyperv.vms.state`` — runtime state of one VM.

    Reads State / Status / Uptime / CPU + memory usage / Heartbeat (``null``
    when the guest integration services are not reporting). Read-only.
    """
    n = ps_single_quote(params["vm_name"])
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$n = {n}; "
        "$vm = Get-VM -Name $n; "
        "ConvertTo-Json -Compress -InputObject @{ "
        "Name = $vm.Name; "
        'State = "$($vm.State)"; '
        "Status = $vm.Status; "
        "Uptime = $vm.Uptime.ToString(); "
        "CPUUsage = $vm.CPUUsage; "
        "MemoryAssigned = $vm.MemoryAssigned; "
        "MemoryDemand = $vm.MemoryDemand; "
        'Heartbeat = if ($vm.Heartbeat) { "$($vm.Heartbeat)" } else { $null } }'
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    return payload if isinstance(payload, dict) else {"value": payload}


_VM_NAME_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": "The virtual machine name (the ``Get-VM -Name`` operand, e.g. ``sql-01``).",
}

_VM_NAME_PARAM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"vm_name": _VM_NAME_PROP},
    "required": ["vm_name"],
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


VMS_OPS: tuple[HypervOp, ...] = (
    HypervOp(
        op_id="hyperv.vms.list",
        handler_attr="hyperv_vms_list",
        summary="List all VMs via ``Get-VM`` (list-shaped, JSONFlux-reduced).",
        description=(
            "Runs ``Get-VM`` and returns one row per VM (Name, Id, State, "
            "Status, CPUUsage, MemoryAssigned, Uptime, configuration Version, "
            "Generation, ProcessorCount, IntegrationServicesVersion / State, "
            "config Path). Read-only; the migration-assessment surface — the "
            "starting inventory of source VMs. The ``{rows, total}`` envelope is "
            "JSONFlux-reduced to a handle for large result sets."
        ),
        parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="vms",
        tags=("read-only", "vms", "inventory"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to enumerate every VM on the Hyper-V host — the source "
                "inventory for a migration assessment (state, generation, "
                "integration-services version). Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{'rows': [{Name, Id, State, Generation, ProcessorCount, "
                "MemoryAssigned, IntegrationServicesVersion, ...}], "
                "'total': <int>}."
            ),
        },
    ),
    HypervOp(
        op_id="hyperv.vms.get",
        handler_attr="hyperv_vms_get",
        summary="Read one VM by name via ``Get-VM -Name``.",
        description=(
            "Runs ``Get-VM -Name <vm_name>`` and returns the single matching VM "
            "projection as a ``{rows, total}`` envelope plus the echoed "
            "``vm_name``. An unknown VM is a terminating error. Read-only."
        ),
        parameter_schema=_VM_NAME_PARAM_SCHEMA,
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
        group_key="vms",
        tags=("read-only", "vms", "lookup"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to read one VM's identity, state, and generation by name. "
                "Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"vm_name": "Required. The VM name."},
            "output_shape": "{'vm_name', 'rows': [<vm>], 'total': <int>}.",
        },
    ),
    HypervOp(
        op_id="hyperv.vms.config",
        handler_attr="hyperv_vms_config",
        summary="Read a VM's deep configuration (memory / processors / generation / firmware).",
        description=(
            "Runs ``Get-VM -Name <vm_name>`` (and, for a Generation 2 VM, "
            "``Get-VMFirmware``) and returns the migration-relevant "
            "configuration: memory (startup / min / max, dynamic), processor "
            "count, generation, automatic start / stop actions, checkpoint "
            "type, integration-services version, and secure-boot state "
            "(``null`` on a Generation 1 / BIOS VM). Read-only. Generation + "
            "secure-boot decide the target VMware firmware (BIOS vs EFI / EFI "
            "Secure Boot)."
        ),
        parameter_schema=_VM_NAME_PARAM_SCHEMA,
        response_schema={
            "type": "object",
            "properties": {
                "Name": {"type": ["string", "null"]},
                "Generation": {"type": ["integer", "null"]},
                "ProcessorCount": {"type": ["integer", "null"]},
                "MemoryStartup": {"type": ["integer", "null"]},
                "DynamicMemoryEnabled": {"type": ["boolean", "null"]},
                "SecureBoot": {"type": ["string", "null"]},
            },
            "additionalProperties": True,
        },
        group_key="vms",
        tags=("read-only", "vms", "config"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call for a VM's deep configuration before planning its move — "
                "memory, processors, generation, firmware / secure-boot. "
                "Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"vm_name": "Required. The VM name."},
            "output_shape": (
                "Flat dict (Generation, ProcessorCount, MemoryStartup, "
                "MemoryMinimum, MemoryMaximum, DynamicMemoryEnabled, SecureBoot, "
                "...)."
            ),
        },
    ),
    HypervOp(
        op_id="hyperv.vms.state",
        handler_attr="hyperv_vms_state",
        summary="Read a VM's runtime state (State / Status / Uptime / Heartbeat).",
        description=(
            "Runs ``Get-VM -Name <vm_name>`` and returns the runtime state: "
            "State, Status, Uptime, CPU + memory usage, and guest Heartbeat "
            "(``null`` when the integration services are not reporting). "
            "Read-only; the pre-cutover check that the source VM is quiesced / "
            "reporting healthy."
        ),
        parameter_schema=_VM_NAME_PARAM_SCHEMA,
        response_schema={
            "type": "object",
            "properties": {
                "Name": {"type": ["string", "null"]},
                "State": {"type": ["string", "null"]},
                "Status": {"type": ["string", "null"]},
                "Uptime": {"type": ["string", "null"]},
                "Heartbeat": {"type": ["string", "null"]},
            },
            "additionalProperties": True,
        },
        group_key="vms",
        tags=("read-only", "vms", "state"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to check a VM's runtime state (running / off), uptime, and "
                "guest heartbeat before a cutover. Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"vm_name": "Required. The VM name."},
            "output_shape": "{'Name', 'State', 'Status', 'Uptime', 'Heartbeat', ...}.",
        },
    ),
)
