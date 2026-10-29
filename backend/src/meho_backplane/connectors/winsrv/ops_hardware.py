# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""winsrv hardware read -- ``winsrv.system.hardware`` (safe).

The machine facts a migration inventory needs and the OS read does not
give: logical processors, installed memory, the SMBIOS product UUID, the
maker and model (which tell a VM from a physical server), the firmware type
(UEFI or legacy BIOS) and whether Secure Boot is on.

Sources, all read-only:

* ``Win32_ComputerSystem`` -- ``NumberOfLogicalProcessors``,
  ``TotalPhysicalMemory`` (bytes), ``Manufacturer``, ``Model``.
* ``Win32_ComputerSystemProduct.UUID`` -- the SMBIOS UUID. On a VM this is
  the BIOS UUID the hypervisor shows.
* ``Get-ComputerInfo -Property BiosFirmwareType`` -- ``Uefi`` or ``Bios``.
* ``Confirm-SecureBootUEFI`` -- ``True`` / ``False`` on a UEFI machine. It
  fails on a BIOS machine and without admin rights; the value is then
  ``null``.

A part that fails leaves its value ``null``; the read itself still answers.
The script is constant (no caller input), so it has no injection surface.

References
----------

* https://learn.microsoft.com/en-us/windows/win32/cimwin32prov/win32-computersystem
* https://learn.microsoft.com/en-us/windows/win32/cimwin32prov/win32-computersystemproduct
* https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.management/get-computerinfo
* https://learn.microsoft.com/en-us/powershell/module/secureboot/confirm-securebootuefi
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import pwsh_run
from meho_backplane.connectors.winsrv.ops import SSH_TRANSPORT_NOTE, WinsrvOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.winsrv.connector import WinsrvConnector

__all__ = [
    "HARDWARE_OPS",
    "winsrv_hardware",
]


_HARDWARE_SCRIPT: str = (
    "$cs = Get-CimInstance -ClassName Win32_ComputerSystem; "
    "$pr = Get-CimInstance -ClassName Win32_ComputerSystemProduct; "
    "$fw = $null; "
    'try { $fw = "$((Get-ComputerInfo -Property BiosFirmwareType).BiosFirmwareType)" } '
    "catch { $fw = $null }; "
    "$sb = $null; "
    "try { $sb = [bool](Confirm-SecureBootUEFI -ErrorAction Stop) } catch { $sb = $null }; "
    "ConvertTo-Json -Compress -InputObject @{ "
    "Hostname = [System.Net.Dns]::GetHostName(); "
    "Manufacturer = $cs.Manufacturer; "
    "Model = $cs.Model; "
    "LogicalProcessors = $cs.NumberOfLogicalProcessors; "
    "TotalPhysicalMemory = $cs.TotalPhysicalMemory; "
    "ProductUuid = if ($pr) { $pr.UUID } else { $null }; "
    "FirmwareType = $fw; "
    "SecureBoot = $sb }"
)


async def winsrv_hardware(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.system.hardware`` -- processors, memory, UUID, firmware."""
    del params  # declared empty in schema; intentionally ignored
    payload = await pwsh_run(connector, target, _HARDWARE_SCRIPT, operator=operator)
    return payload if isinstance(payload, dict) else {"value": payload}


def _value(type_: str, description: str) -> dict[str, Any]:
    return {"type": [type_, "null"], "description": description}


HARDWARE_OPS: tuple[WinsrvOp, ...] = (
    WinsrvOp(
        op_id="winsrv.system.hardware",
        handler_attr="winsrv_hardware",
        summary="Read processors, memory, product UUID, maker, model and firmware type.",
        description=(
            "Reads Win32_ComputerSystem, Win32_ComputerSystemProduct, the BIOS "
            "firmware type and the Secure Boot state. Returns logical processors, "
            "memory in bytes, the SMBIOS product UUID, maker and model, UEFI or "
            "BIOS, and Secure Boot on or off. Read-only."
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
                "Hostname": _value("string", "Host name."),
                "Manufacturer": _value("string", "System maker, for example 'VMware, Inc.'."),
                "Model": _value("string", "System model, for example 'Virtual Machine'."),
                "LogicalProcessors": _value("integer", "Logical processors."),
                "TotalPhysicalMemory": _value("integer", "Installed memory in bytes."),
                "ProductUuid": _value("string", "SMBIOS product UUID."),
                "FirmwareType": _value("string", "'Uefi' or 'Bios'; null when unknown."),
                "SecureBoot": _value("boolean", "Secure Boot on; null on BIOS or no rights."),
            },
            "additionalProperties": True,
        },
        group_key="system",
        tags=("read-only", "system", "hardware"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "Call for the size and identity of a Windows Server machine: how "
                "many processors and how much memory it has, its product UUID, "
                "whether it is a VM, and whether it boots with UEFI. Read-only. "
                + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{'Hostname', 'Manufacturer', 'Model', 'LogicalProcessors', "
                "'TotalPhysicalMemory' (bytes), 'ProductUuid', 'FirmwareType', "
                "'SecureBoot'}. A value that could not be read is null."
            ),
            "next_step": (
                "For the whole host in the common migration inventory shape, "
                "call winsrv.migration.inventory."
            ),
        },
    ),
)
