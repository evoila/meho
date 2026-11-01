# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameters, answers and AI guidance of the three VM device changes.

* ``vmware.composite.vm.disk.remove`` (``destructive``),
* ``vmware.composite.vm.nic.remove`` (``dangerous``),
* ``vmware.composite.vm.controller.add`` (``dangerous``).

The handlers live in :mod:`._vm_disk_remove` and :mod:`._vm_devices`.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest._vm_hardware_schemas import VM_MOID_PATTERN

__all__ = [
    "CONTROLLER_TYPES",
    "DISK_ID_PATTERN",
    "NIC_ID_PATTERN",
    "SCSI_CONTROLLER_TYPES",
    "VM_CONTROLLER_ADD_LLM_INSTRUCTIONS",
    "VM_CONTROLLER_ADD_PARAMETER_SCHEMA",
    "VM_CONTROLLER_ADD_RESPONSE_SCHEMA",
    "VM_DISK_REMOVE_LLM_INSTRUCTIONS",
    "VM_DISK_REMOVE_PARAMETER_SCHEMA",
    "VM_DISK_REMOVE_RESPONSE_SCHEMA",
    "VM_NIC_REMOVE_LLM_INSTRUCTIONS",
    "VM_NIC_REMOVE_PARAMETER_SCHEMA",
    "VM_NIC_REMOVE_RESPONSE_SCHEMA",
]

#: A disk id: the REST disk id, which is the VIM device key (``2000``).
DISK_ID_PATTERN: Final = r"^[0-9]+$(?!\n)"
#: A network adapter id as vCenter returns it (``4000``).
NIC_ID_PATTERN: Final = r"^[0-9]+$(?!\n)"

#: The SCSI models; the other kinds take no ``sharing``.
SCSI_CONTROLLER_TYPES: Final[tuple[str, ...]] = ("LSILOGICSAS", "PVSCSI")
#: Every controller kind the add takes.
CONTROLLER_TYPES: Final[tuple[str, ...]] = (*SCSI_CONTROLLER_TYPES, "NVME", "SATA", "USB_XHCI")

_NULLABLE_STR: Final = {"type": ["string", "null"]}
_NULLABLE_INT: Final = {"type": ["integer", "null"]}

_VM_ID_PROPERTY: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": VM_MOID_PATTERN,
    "description": (
        "VM id, such as 'vm-42'. Get it from vmware.vm.hardware.show or vmware.vm.list. "
        "A change takes no VM name, so after approval it runs on the VM the approver saw."
    ),
}

# ---------------------------------------------------------------------------
# vm.disk.remove
# ---------------------------------------------------------------------------

VM_DISK_REMOVE_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": _VM_ID_PROPERTY,
        "disk": {
            "type": "string",
            "pattern": DISK_ID_PATTERN,
            "description": (
                "Disk id, such as '2000' (the 'disk' field of vmware.vm.hardware.show)."
            ),
        },
        "delete_file": {
            "type": "boolean",
            "description": (
                "Also delete the disk's VMDK file from the datastore. Then the data is gone "
                "for good. Default false: the file stays and can be attached again."
            ),
        },
    },
    "required": ["vm", "disk"],
    "additionalProperties": False,
    "examples": [
        {"vm": "<vm-id>", "disk": "<disk-id>"},
        {"vm": "<vm-id>", "disk": "<disk-id>", "delete_file": True},
    ],
}

_DISK_OBJECT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "description": "The disk the call acts on, as read before the change.",
    "properties": {
        "kind": {"type": "string", "description": "Always 'vm_disk'."},
        "vm": {"type": "string", "description": "VM id."},
        "vm_name": {**_NULLABLE_STR, "description": "VM name."},
        "power_state": {**_NULLABLE_STR, "description": "VM power state before the change."},
        "disk": {"type": "string", "description": "Disk id."},
        "delete_file": {"type": "boolean", "description": "Whether the file is deleted too."},
        "label": {**_NULLABLE_STR, "description": "Device label, such as 'Hard disk 2'."},
        "capacity_bytes": {**_NULLABLE_INT, "description": "Disk size in bytes."},
        "vmdk_file": {**_NULLABLE_STR, "description": "Datastore path of the VMDK file."},
        "disk_uuid": {
            **_NULLABLE_STR,
            "description": (
                "UUID of the disk (VIM backing.uuid). The approved run checks it, so the "
                "removal runs on the disk the approver saw."
            ),
        },
        "sharing": {**_NULLABLE_STR, "description": "VIM sharing mode of the disk file."},
        "controller_key": {**_NULLABLE_INT, "description": "Key of the disk's controller."},
        "unit_number": {**_NULLABLE_INT, "description": "Unit number on that controller."},
    },
}

_DISK_ROW_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "disk": {"type": "string", "description": "Disk id."},
        "label": {**_NULLABLE_STR, "description": "Device label."},
        "capacity_bytes": {**_NULLABLE_INT, "description": "Disk size in bytes."},
        "vmdk_file": {**_NULLABLE_STR, "description": "Datastore path of the VMDK file."},
        "disk_uuid": {**_NULLABLE_STR, "description": "UUID of the disk (VIM backing.uuid)."},
    },
    "required": ["disk"],
}

VM_DISK_REMOVE_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "removed",
                "not_found",
                "invalid_request",
                "precondition_failed",
                "still_present",
                "timeout",
            ],
            "description": (
                "removed: the disk is gone from the VM; not_found: no such VM or disk, "
                "nothing sent; invalid_request: a bad id; precondition_failed: refused "
                "before any change (see blockers); still_present: vCenter finished but the "
                "disk still reads back; timeout: the task did not finish in time."
            ),
        },
        "object": _DISK_OBJECT_SCHEMA,
        "blockers": {
            "type": "array",
            "description": "Why the removal was refused before any change.",
            "items": {"type": "object"},
        },
        "task": {**_NULLABLE_STR, "description": "vCenter task id of the change."},
        "task_state": {**_NULLABLE_STR, "description": "Last state of that task."},
        "remaining_disks": {
            "type": ["array", "null"],
            "description": "The VM's disks read after the change; null before a change.",
            "items": _DISK_ROW_SCHEMA,
        },
        "guidance": {**_NULLABLE_STR, "description": "What happened and what to do next."},
    },
    "required": ["status", "object"],
}

VM_DISK_REMOVE_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to take one virtual disk off a VM. By default the VMDK file stays on the "
        "datastore. Set delete_file only when the data must go for good."
    ),
    "preconditions": (
        "Read the VM first with vmware.vm.hardware.show to get its id (vm) and the disk id "
        "(disk). Make sure the guest no longer uses the disk (unmount it in the OS). A "
        "shared (multi-writer) disk is refused with delete_file. A second person always "
        "approves, after a preview: call preview_operation with the same params, then "
        "call_operation with the preview_hash. An agent cannot run it; a person does."
    ),
    "parameter_hints": {
        "disk": "The 'disk' id from vmware.vm.hardware.show, such as '2000'.",
        "delete_file": "false keeps the VMDK file; true deletes it for good.",
    },
    "output_shape": (
        "{status: removed|not_found|invalid_request|precondition_failed|still_present|"
        "timeout, object: {vm, vm_name, power_state, disk, label, capacity_bytes, "
        "vmdk_file, disk_uuid, sharing, delete_file, ...}, blockers, task, task_state, "
        "remaining_disks: [{disk, label, capacity_bytes, vmdk_file, disk_uuid}], guidance}."
    ),
    "next_step": (
        "Not to be run twice blindly: after removed, a second call answers not_found. "
        "With the file kept, vmware.composite.vm.disk.attach can attach it again. Check "
        "the result with vmware.vm.hardware.show."
    ),
}

# ---------------------------------------------------------------------------
# vm.nic.remove
# ---------------------------------------------------------------------------

VM_NIC_REMOVE_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": _VM_ID_PROPERTY,
        "nic": {
            "type": "string",
            "pattern": NIC_ID_PATTERN,
            "description": (
                "Network adapter id, such as '4000'. List them with "
                "GET:/api/vcenter/vm/{vm}/hardware/ethernet or vmware.vm.info."
            ),
        },
    },
    "required": ["vm", "nic"],
    "additionalProperties": False,
    "examples": [{"vm": "<vm-id>", "nic": "<nic-id>"}],
}

_NIC_ROW_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "nic": {"type": "string", "description": "Network adapter id."},
        "label": {**_NULLABLE_STR, "description": "Device label, such as 'Network adapter 1'."},
        "type": {**_NULLABLE_STR, "description": "Adapter model, such as VMXNET3."},
        "mac_address": {**_NULLABLE_STR, "description": "MAC address."},
        "backing_type": {**_NULLABLE_STR, "description": "Network kind of the backing."},
        "network": {**_NULLABLE_STR, "description": "Network id of the backing."},
        "network_name": {**_NULLABLE_STR, "description": "Network name of the backing."},
        "state": {**_NULLABLE_STR, "description": "CONNECTED or NOT_CONNECTED."},
    },
    "required": ["nic"],
}

VM_NIC_REMOVE_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["removed", "not_found", "refused", "still_present"],
            "description": (
                "removed: the adapter is gone; not_found: no such VM or adapter, nothing "
                "sent; refused: vCenter said no, nothing changed; still_present: vCenter "
                "took the request but the adapter still reads back."
            ),
        },
        "vm": {"type": "string", "description": "VM id."},
        "nic": {"type": "string", "description": "Network adapter id."},
        "removed_nic": {
            **_NIC_ROW_SCHEMA,
            "type": ["object", "null"],
            "description": "The adapter as read before the change; null when not found.",
        },
        "remaining_nics": {
            "type": ["array", "null"],
            "description": "The VM's adapter ids after the change; null when not read.",
            "items": {"type": "string", "description": "Network adapter id."},
        },
        "error_type": {**_NULLABLE_STR, "description": "vCenter error type on refused."},
        "guidance": {**_NULLABLE_STR, "description": "What happened and what to do next."},
    },
    "required": ["status", "vm", "nic"],
}

VM_NIC_REMOVE_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to take one network adapter off a VM. To move an adapter to another "
        "network instead, use vmware.composite.vm.nic.repoint."
    ),
    "preconditions": (
        "Get the VM id (vm) and the adapter id (nic) first, for example with "
        "GET:/api/vcenter/vm/{vm}/hardware/ethernet. The guest loses that network at once. "
        "The change waits for approval; an agent needs a grant for dangerous changes."
    ),
    "parameter_hints": {"nic": "The adapter id, such as '4000'."},
    "output_shape": (
        "{status: removed|not_found|refused|still_present, vm, nic, removed_nic: {nic, "
        "label, type, mac_address, backing_type, network, network_name, state}, "
        "remaining_nics, error_type, guidance}."
    ),
    "next_step": (
        "Safe to run twice: when the adapter is gone, nothing is sent (not_found). On "
        "refused, read guidance (for example a guest that does not allow hot remove)."
    ),
}

# ---------------------------------------------------------------------------
# vm.controller.add
# ---------------------------------------------------------------------------

VM_CONTROLLER_ADD_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": _VM_ID_PROPERTY,
        "type": {
            "type": "string",
            "enum": list(CONTROLLER_TYPES),
            "description": (
                "Controller kind: LSILOGICSAS (LSI Logic SAS) or PVSCSI (VMware "
                "ParaVirtual) SCSI, NVME, SATA (AHCI), or USB_XHCI (USB 3)."
            ),
        },
        "bus": {
            "type": "integer",
            "minimum": 0,
            "maximum": 3,
            "description": (
                "Bus number for a SCSI, SATA or NVMe controller. Omit to let vCenter pick a "
                "free one. Not allowed with USB_XHCI."
            ),
        },
        "sharing": {
            "type": "string",
            "enum": ["NONE", "VIRTUAL", "PHYSICAL"],
            "description": (
                "SCSI bus sharing, only with LSILOGICSAS or PVSCSI. Default NONE. VIRTUAL or "
                "PHYSICAL shares the bus with other VMs (for guest clusters only)."
            ),
        },
    },
    "required": ["vm", "type"],
    "additionalProperties": False,
    "examples": [
        {"vm": "<vm-id>", "type": "PVSCSI"},
        {"vm": "<vm-id>", "type": "NVME", "bus": 1},
    ],
}

VM_CONTROLLER_ADD_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["added", "unchanged", "refused", "not_found", "invalid_request", "timeout"],
            "description": (
                "added: the controller exists; unchanged: the VM already has a USB 3 "
                "controller, nothing sent; refused: vCenter said no, nothing changed; "
                "not_found: no such VM, nothing sent; "
                "invalid_request: a bad combination of fields, nothing sent; timeout: the "
                "task did not finish in time."
            ),
        },
        "vm": {"type": "string", "description": "VM id."},
        "type": {"type": "string", "description": "The requested controller kind."},
        "requested": {"type": "object", "description": "The spec MEHO sent."},
        "controller": {
            "type": ["object", "null"],
            "description": "The new controller as read back; null when not read.",
            "properties": {
                "kind": {"type": "string", "description": "scsi, sata, nvme or usb."},
                "adapter": {"type": "string", "description": "Controller id (device key)."},
                "label": {**_NULLABLE_STR, "description": "Device label."},
                "type": {**_NULLABLE_STR, "description": "Model, such as PVSCSI or AHCI."},
                "bus": {**_NULLABLE_INT, "description": "Bus number."},
                "sharing": {**_NULLABLE_STR, "description": "SCSI bus sharing."},
                "pci_slot_number": {**_NULLABLE_INT, "description": "PCI slot."},
            },
        },
        "task": {**_NULLABLE_STR, "description": "vCenter task id (USB_XHCI only)."},
        "error_type": {**_NULLABLE_STR, "description": "vCenter error type on refused."},
        "guidance": {**_NULLABLE_STR, "description": "What happened and what to do next."},
    },
    "required": ["status", "vm", "type"],
}

VM_CONTROLLER_ADD_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to give a VM a new disk controller (SCSI, NVMe or SATA) or a USB 3 "
        "controller, for example before adding disks on a separate bus."
    ),
    "preconditions": (
        "Read the VM first with vmware.vm.hardware.show: it gives the VM id (vm) and the "
        "controllers it has (disk_controllers). A VM has at most four controllers of each "
        "disk kind and one USB 3 controller. vCenter may refuse a hot add while the VM is "
        "on (often for NVMe); then power it off first. The change waits for approval."
    ),
    "parameter_hints": {
        "type": "PVSCSI for fast storage, LSILOGICSAS for older guests, NVME, SATA, USB_XHCI.",
        "bus": "Omit unless a fixed bus number is needed.",
    },
    "output_shape": (
        "{status: added|unchanged|refused|not_found|invalid_request|timeout, vm, type, "
        "requested, "
        "controller: {kind, adapter, label, type, bus, sharing, pci_slot_number}, task, "
        "error_type, guidance}."
    ),
    "next_step": (
        "Not safe to run twice for a disk controller: each call adds one more. A USB 3 "
        "controller is added only once (unchanged after that). Check the result with "
        "vmware.vm.hardware.show."
    ),
}
