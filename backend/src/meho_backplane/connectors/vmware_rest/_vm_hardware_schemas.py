# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameter and response schemas of the ``vmware.vm.hardware.show`` typed read.

The handler (:mod:`~meho_backplane.connectors.vmware_rest.typed_ops_vm_hardware`)
flattens the vSphere ``Cpu.Info``, ``Memory.Info``, ``Cdrom.Info``,
``Disk.Info``, the adapter ``Info`` objects and ``Installer.Info`` into
snake_case rows, so these schemas follow the handler code. Kept in their own
module so the handler module stays small. The CD drive row and the VM id
pattern are shared with the two changes in ``composites/_vm_cdrom_tools.py``.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "CDROM_ROW_SCHEMA",
    "DISK_ROW_SCHEMA",
    "VM_HARDWARE_PARAMETER_SCHEMA",
    "VM_HARDWARE_RESPONSE_SCHEMA",
    "VM_MOID_PATTERN",
    "VM_SELECTOR_PROPERTIES",
]

#: A vCenter VM id such as ``vm-42``. It goes into the URL path, so anything
#: else is refused. ``(?!\n)`` stops ``$`` from accepting a trailing newline.
VM_MOID_PATTERN: Final = r"^vm-[0-9]+$(?!\n)"

#: The ``vm`` / ``name`` selector of the read. The two changes take ``vm`` only.
VM_SELECTOR_PROPERTIES: Final[dict[str, Any]] = {
    "vm": {
        "type": "string",
        "pattern": VM_MOID_PATTERN,
        "description": "VM id, such as 'vm-42'. Give exactly one of 'vm' or 'name'.",
    },
    "name": {
        "type": "string",
        "minLength": 1,
        "description": (
            "VM name. MEHO looks up the VM id. An unknown name gives not_found; a name "
            "that fits two VMs is an error. Give exactly one of 'vm' or 'name'."
        ),
    },
}

_NULLABLE_BOOL: Final = {"type": ["boolean", "null"]}
_NULLABLE_INT: Final = {"type": ["integer", "null"]}
_NULLABLE_STR: Final = {"type": ["string", "null"]}


def _field(base: dict[str, Any], description: str) -> dict[str, Any]:
    return {**base, "description": description}


#: One CD drive row. Shared with the cdrom change's answer.
CDROM_ROW_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "cdrom": {"type": "string", "description": "CD drive id, such as '16000'."},
        "label": _field(_NULLABLE_STR, "Device label, such as 'CD/DVD drive 1'."),
        "type": _field(_NULLABLE_STR, "Controller kind the drive sits on: IDE or SATA."),
        "ide": {
            "type": ["object", "null"],
            "description": "IDE address ({primary, master}) when type is IDE.",
        },
        "sata": {
            "type": ["object", "null"],
            "description": "SATA address ({bus, unit}) when type is SATA.",
        },
        "backing_type": _field(_NULLABLE_STR, "ISO_FILE, HOST_DEVICE or CLIENT_DEVICE."),
        "iso_file": _field(_NULLABLE_STR, "Datastore path of the ISO file, for ISO_FILE."),
        "host_device": _field(_NULLABLE_STR, "Host device name, for HOST_DEVICE."),
        "device_access_type": _field(
            _NULLABLE_STR, "EMULATION, PASSTHRU or PASSTHRU_EXCLUSIVE (device backings)."
        ),
        "state": _field(
            _NULLABLE_STR,
            "CONNECTED, NOT_CONNECTED, RECOVERABLE_ERROR, UNRECOVERABLE_ERROR or UNKNOWN.",
        ),
        "connected": _field(_NULLABLE_BOOL, "True when state is CONNECTED."),
        "start_connected": _field(_NULLABLE_BOOL, "Connects when the VM powers on."),
        "allow_guest_control": _field(
            _NULLABLE_BOOL, "The guest can connect and disconnect the drive."
        ),
    },
    "required": ["cdrom"],
}

_ADDRESS_SCHEMA: Final = {
    "type": ["object", "null"],
    "description": "Controller address ({bus, unit}) when the device sits on that kind.",
}

#: One disk row.
DISK_ROW_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "disk": {"type": "string", "description": "Disk id, such as '2000'."},
        "label": _field(_NULLABLE_STR, "Device label, such as 'Hard disk 1'."),
        "type": _field(_NULLABLE_STR, "Controller kind the disk sits on: IDE, SCSI, SATA or NVME."),
        "ide": {
            "type": ["object", "null"],
            "description": "IDE address ({primary, master}) when type is IDE.",
        },
        "scsi": _ADDRESS_SCHEMA,
        "sata": _ADDRESS_SCHEMA,
        "nvme": _ADDRESS_SCHEMA,
        "capacity_bytes": _field(_NULLABLE_INT, "Disk size in bytes; null when not known."),
        "backing_type": _field(_NULLABLE_STR, "VMDK_FILE."),
        "vmdk_file": _field(_NULLABLE_STR, "Datastore path of the VMDK file."),
    },
    "required": ["disk"],
}

#: One device on a controller: a disk or a CD drive.
_CONTROLLER_DEVICE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "device_kind": {"type": "string", "description": "disk or cdrom."},
        "id": {"type": "string", "description": "Disk id or CD drive id."},
        "label": _field(_NULLABLE_STR, "Device label."),
        "unit": _field(_NULLABLE_INT, "Unit number on the controller's bus."),
        "capacity_bytes": _field(_NULLABLE_INT, "Disk size in bytes (disks only)."),
        "vmdk_file": _field(_NULLABLE_STR, "Datastore path of the VMDK file (disks only)."),
    },
    "required": ["device_kind", "id"],
}

VM_HARDWARE_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": VM_SELECTOR_PROPERTIES,
    "oneOf": [{"required": ["vm"]}, {"required": ["name"]}],
    "additionalProperties": False,
    "examples": [{"name": "<vm-name>"}, {"vm": "vm-42"}],
}

VM_HARDWARE_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": {"type": "string", "description": "VM id that was read."},
        "name": _field(_NULLABLE_STR, "VM name when the call named the VM, else null."),
        "cpu": {
            "type": "object",
            "description": "CPU settings.",
            "properties": {
                "count": _field(_NULLABLE_INT, "Number of virtual CPU cores."),
                "cores_per_socket": _field(_NULLABLE_INT, "Cores per socket."),
                "hot_add_enabled": _field(_NULLABLE_BOOL, "CPUs can be added while on."),
                "hot_remove_enabled": _field(_NULLABLE_BOOL, "CPUs can be removed while on."),
            },
        },
        "memory": {
            "type": "object",
            "description": "Memory settings.",
            "properties": {
                "size_mib": _field(_NULLABLE_INT, "Memory size in MiB."),
                "hot_add_enabled": _field(_NULLABLE_BOOL, "Memory can be added while on."),
                "hot_add_increment_size_mib": _field(
                    _NULLABLE_INT, "Step for adding memory while on, in MiB."
                ),
                "hot_add_limit_mib": _field(
                    _NULLABLE_INT, "Most memory the VM can have after a hot add, in MiB."
                ),
            },
        },
        "cdroms": {
            "type": ["array", "null"],
            "description": "Every CD drive; null when the list read failed.",
            "items": CDROM_ROW_SCHEMA,
        },
        "disks": {
            "type": ["array", "null"],
            "description": "Every disk; null when the list read failed.",
            "items": DISK_ROW_SCHEMA,
        },
        "disk_controllers": {
            "type": ["array", "null"],
            "description": "Every SCSI, SATA and NVMe controller; null when all reads failed.",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "description": "scsi, sata or nvme."},
                    "adapter": {"type": "string", "description": "Controller id."},
                    "label": _field(_NULLABLE_STR, "Device label."),
                    "type": _field(
                        _NULLABLE_STR, "Model: PVSCSI, LSILOGIC, LSILOGICSAS, BUSLOGIC, AHCI."
                    ),
                    "bus": _field(_NULLABLE_INT, "Bus number."),
                    "unit": _field(_NULLABLE_INT, "SCSI unit number of the controller."),
                    "sharing": _field(_NULLABLE_STR, "SCSI bus sharing: NONE, VIRTUAL, PHYSICAL."),
                    "pci_slot_number": _field(_NULLABLE_INT, "PCI slot; null before power on."),
                    "devices": {
                        "type": "array",
                        "description": (
                            "The disks and CD drives on this controller (same kind and bus "
                            "number), by unit. Empty when the disk and CD drive lists failed."
                        ),
                        "items": _CONTROLLER_DEVICE_SCHEMA,
                    },
                },
                "required": ["kind", "adapter", "devices"],
            },
        },
        "tools_installer_mounted": _field(
            _NULLABLE_BOOL, "True when the VMware Tools installer is in a CD drive."
        ),
        "read_notes": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "One line per part that could not be read, including a single CD drive "
                "or disk whose list worked but whose own read failed."
            ),
        },
    },
    "required": ["vm", "cpu", "memory", "read_notes"],
}
