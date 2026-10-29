# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""hyperv guest op -- ``hyperv.vms.guest.list`` (safe).

One read for every VM on the host: the VM's BIOS GUID (what the guest sees
as its SMBIOS / BIOS UUID) and what the guest's integration services report
about the guest system (OS name, OS version, full host name). The migration
inventory uses it to find one machine seen through two sources (Hyper-V and
its guest OS) and to name the guest OS.

The data comes from the Hyper-V WMI provider (namespace
``root\\virtualization\\v2``):

* ``Msvm_ComputerSystem`` with ``Caption='Virtual Machine'`` -- one per VM;
  its ``Name`` is the VM id (the same id ``Get-VM`` returns).
* ``Msvm_VirtualSystemSettingData`` of type
  ``Microsoft:Hyper-V:System:Realized`` -- the running settings; its
  ``BIOSGUID`` is the guest's BIOS UUID.
* ``Msvm_KvpExchangeComponent.GuestIntrinsicExchangeItems`` -- the values
  the guest pushes up (``OSName``, ``OSVersion``,
  ``FullyQualifiedDomainName``). Empty when the VM is off or its
  integration services do not run; the fields are then ``null``.

The script is constant (no caller input), so it has no injection surface.

References
----------

* https://learn.microsoft.com/en-us/windows/win32/hyperv_v2/msvm-kvpexchangecomponent
* https://learn.microsoft.com/en-us/windows/win32/hyperv_v2/msvm-virtualsystemsettingdata
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.hyperv.ops import (
    SSH_TRANSPORT_NOTE,
    HypervOp,
    hyperv_list_read,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.hyperv.connector import HypervConnector

__all__ = [
    "GUEST_OPS",
    "hyperv_vms_guest_list",
]


#: One row per VM. ``GuestIntrinsicExchangeItems`` holds embedded
#: ``Msvm_KvpExchangeDataItem`` instances as XML; each has a ``Name`` and a
#: ``Data`` property.
_GUEST_PIPELINE: str = (
    "Get-CimInstance -Namespace 'root\\virtualization\\v2' -ClassName "
    "Msvm_ComputerSystem -Filter \"Caption='Virtual Machine'\" | ForEach-Object { "
    "$vm = $_; "
    "$s = Get-CimAssociatedInstance -InputObject $vm -ResultClassName "
    "Msvm_VirtualSystemSettingData | Where-Object { $_.VirtualSystemType -eq "
    "'Microsoft:Hyper-V:System:Realized' } | Select-Object -First 1; "
    "$k = Get-CimAssociatedInstance -InputObject $vm -ResultClassName "
    "Msvm_KvpExchangeComponent | Select-Object -First 1; "
    "$os = $null; $ver = $null; $fqdn = $null; "
    "if ($k -and $k.GuestIntrinsicExchangeItems) { "
    "foreach ($item in $k.GuestIntrinsicExchangeItems) { "
    "$p = ([xml]$item).INSTANCE.PROPERTY; "
    "$n = ($p | Where-Object { $_.NAME -eq 'Name' }).VALUE; "
    "$v = ($p | Where-Object { $_.NAME -eq 'Data' }).VALUE; "
    "if ($n -eq 'OSName') { $os = $v } "
    "elseif ($n -eq 'OSVersion') { $ver = $v } "
    "elseif ($n -eq 'FullyQualifiedDomainName') { $fqdn = $v } } }; "
    '[pscustomobject]@{ Id = "$($vm.Name)"; Name = $vm.ElementName; '
    'BIOSGUID = if ($s) { "$($s.BIOSGUID)" } else { $null }; '
    "OSName = $os; OSVersion = $ver; FullyQualifiedDomainName = $fqdn } }"
)


async def hyperv_vms_guest_list(
    connector: HypervConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``hyperv.vms.guest.list`` -- BIOS GUID and guest OS per VM (list)."""
    del params
    return await hyperv_list_read(connector, target, pipeline=_GUEST_PIPELINE, operator=operator)


def _text(description: str) -> dict[str, Any]:
    return {"type": ["string", "null"], "description": description}


_GUEST_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "Id": _text("VM id, the same as the Id of hyperv.vms.list."),
        "Name": _text("VM name."),
        "BIOSGUID": _text("BIOS GUID in braces; the guest sees it as its BIOS UUID."),
        "OSName": _text("Guest OS name; null when the VM is off or reports nothing."),
        "OSVersion": _text("Guest OS version; null when not reported."),
        "FullyQualifiedDomainName": _text("Guest host name; null when not reported."),
    },
}


GUEST_OPS: tuple[HypervOp, ...] = (
    HypervOp(
        op_id="hyperv.vms.guest.list",
        handler_attr="hyperv_vms_guest_list",
        summary="List every VM's BIOS GUID and the guest OS its integration services report.",
        description=(
            "Reads the Hyper-V WMI provider and returns one row per VM: Id, Name, "
            "BIOSGUID (the guest's BIOS UUID), and the guest's OSName, OSVersion "
            "and FullyQualifiedDomainName. Guest values are null when the VM is "
            "off or its integration services do not report. Read-only."
        ),
        parameter_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
            "examples": [{}],
        },
        response_schema={
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "items": _GUEST_ROW_SCHEMA,
                    "description": "One row per VM.",
                },
                "total": {"type": "integer", "description": "Number of rows."},
            },
            "required": ["rows", "total"],
            "additionalProperties": True,
        },
        group_key="vms",
        tags=("read-only", "vms", "inventory", "guest"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "Call to find which guest OS each VM runs, or to match a Hyper-V "
                "VM to the same machine read through its guest OS (by BIOS UUID). "
                "Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{'rows': [{Id, Name, BIOSGUID, OSName, OSVersion, "
                "FullyQualifiedDomainName}], 'total': <int>}."
            ),
            "next_step": (
                "For the full machine inventory in the common migration shape, "
                "call hyperv.migration.inventory instead."
            ),
        },
    ),
)
