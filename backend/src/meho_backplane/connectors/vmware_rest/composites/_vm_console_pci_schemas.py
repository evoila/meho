# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameters, answers and AI guidance of four VM changes.

The PCI passthrough add and remove (:mod:`._vm_pci_device`), the console
screenshot and the key send (:mod:`._vm_console`). No I/O here.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites.schemas import VM_MOID_PATTERN

__all__ = [
    "CONSOLE_KEYS",
    "SCREENSHOT_DEFAULT_MAX_BYTES",
    "SCREENSHOT_HARD_CAP_BYTES",
    "VM_KEYS_SEND_LLM_INSTRUCTIONS",
    "VM_KEYS_SEND_PARAMETER_SCHEMA",
    "VM_KEYS_SEND_RESPONSE_SCHEMA",
    "VM_PCI_DEVICE_ADD_LLM_INSTRUCTIONS",
    "VM_PCI_DEVICE_ADD_PARAMETER_SCHEMA",
    "VM_PCI_DEVICE_ADD_RESPONSE_SCHEMA",
    "VM_PCI_DEVICE_REMOVE_LLM_INSTRUCTIONS",
    "VM_PCI_DEVICE_REMOVE_PARAMETER_SCHEMA",
    "VM_PCI_DEVICE_REMOVE_RESPONSE_SCHEMA",
    "VM_SCREENSHOT_LLM_INSTRUCTIONS",
    "VM_SCREENSHOT_PARAMETER_SCHEMA",
    "VM_SCREENSHOT_RESPONSE_SCHEMA",
]

#: The only keys ``vm.keys.send`` sends: USB HID usage id and the modifier
#: keys held with it. No letters or digits, so the call cannot type a command.
CONSOLE_KEYS: Final[dict[str, tuple[int, tuple[str, ...]]]] = {
    "ctrl_alt_del": (0x4C, ("leftControl", "leftAlt")),
    "enter": (0x28, ()),
    "esc": (0x29, ()),
    "backspace": (0x2A, ()),
    "tab": (0x2B, ()),
    **{f"f{n}": (0x3A + n - 1, ()) for n in range(1, 13)},
    "right": (0x4F, ()),
    "left": (0x50, ()),
    "down": (0x51, ()),
    "up": (0x52, ()),
}

SCREENSHOT_DEFAULT_MAX_BYTES: Final = 2 * 1024 * 1024
SCREENSHOT_HARD_CAP_BYTES: Final = 8 * 1024 * 1024

_NULLABLE_STR: Final = {"type": ["string", "null"]}
_NULLABLE_INT: Final = {"type": ["integer", "null"]}

_VM_ID: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": VM_MOID_PATTERN,
    "description": (
        "VM id of the shape vm-<number>. Get it from vmware.vm.list or vmware.vm.info; do "
        "not guess it. A change takes no VM name, so after approval it runs on the VM the "
        "approver saw."
    ),
}

_COMMON_ANSWER: Final[dict[str, Any]] = {
    "vm": {"type": "string", "description": "VM id."},
    "vm_name": {**_NULLABLE_STR, "description": "VM name as vCenter reads it."},
    "task": {**_NULLABLE_STR, "description": "vCenter task id, once the change was sent."},
    "task_state": {**_NULLABLE_STR, "description": "Last state of the vCenter task."},
    "guidance": {**_NULLABLE_STR, "description": "What happened and what to do next."},
    "issues": {
        "type": "array",
        "description": "Present when the change is not confirmed (the task timed out).",
        "items": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Where the problem is."},
                "severity": {"type": "string", "description": "error, warning or info."},
                "message": {"type": "string", "description": "What happened."},
            },
        },
    },
}

# ---------------------------------------------------------------------------
# PCI passthrough
# ---------------------------------------------------------------------------

PCI_DEVICE_ROW_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "description": "One PCI passthrough device of the VM.",
    "properties": {
        "device_key": {**_NULLABLE_INT, "description": "Device key; remove takes it."},
        "label": {**_NULLABLE_STR, "description": "Label, such as 'PCI device 0'."},
        "kind": {
            "type": "string",
            "enum": ["directpath", "dynamic"],
            "description": "directpath: one fixed host device; dynamic: any free matching one.",
        },
        "pci_id": {
            **_NULLABLE_STR,
            "description": "Host PCI address (dynamic: only while the VM runs).",
        },
        "vendor_id": {**_NULLABLE_INT, "description": "PCI vendor id."},
        "device_id": {**_NULLABLE_INT, "description": "PCI device id (dynamic devices)."},
        "device_name": {**_NULLABLE_STR, "description": "Device name on the host."},
        "custom_label": {**_NULLABLE_STR, "description": "Label a dynamic device must carry."},
    },
}

_PCI_DEVICE_LIST: Final[dict[str, Any]] = {
    "type": ["array", "null"],
    "items": PCI_DEVICE_ROW_SCHEMA,
    "description": "Every PCI passthrough device of the VM, as read last.",
}

VM_PCI_DEVICE_ADD_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": _VM_ID,
        "pci_id": {
            "type": "string",
            "pattern": r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$",
            "description": (
                "DirectPath I/O: the host PCI address of the device, such as "
                "'0000:3b:00.0'. The host must have passthrough enabled for it."
            ),
        },
        "dynamic_device": {
            "type": "object",
            "description": (
                "Dynamic DirectPath: vCenter picks any free host device with these ids "
                "when the VM powers on."
            ),
            "properties": {
                "vendor_id": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 65535,
                    "description": "PCI vendor id, as a number.",
                },
                "device_id": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 65535,
                    "description": "PCI device id, as a number.",
                },
                "custom_label": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Only use host devices that carry this label.",
                },
            },
            "required": ["vendor_id", "device_id"],
            "additionalProperties": False,
        },
        "reserve_all_memory": {
            "type": "boolean",
            "description": (
                "Reserve all VM memory, which a passthrough VM needs to power on. Default true."
            ),
        },
    },
    "required": ["vm"],
    "oneOf": [{"required": ["pci_id"]}, {"required": ["dynamic_device"]}],
    "additionalProperties": False,
    "examples": [
        {"vm": "<vm-id>", "pci_id": "<pci-address>"},
        {
            "vm": "<vm-id>",
            "dynamic_device": {"vendor_id": "<vendor-id>", "device_id": "<device-id>"},
        },
    ],
}

VM_PCI_DEVICE_ADD_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "added",
                "unchanged",
                "refused",
                "not_found",
                "invalid_request",
                "timeout",
            ],
            "description": (
                "added: the device exists now; unchanged: the VM already has it, nothing "
                "sent; refused: the VM is not powered off; not_found: no such VM; "
                "invalid_request: the host does not offer the device; timeout: the task "
                "did not finish in time."
            ),
        },
        **_COMMON_ANSWER,
        "requested": {"type": "object", "description": "The device that was asked for."},
        "device": {
            **PCI_DEVICE_ROW_SCHEMA,
            "type": ["object", "null"],
            "description": "The new device as read back.",
        },
        "pci_devices": _PCI_DEVICE_LIST,
    },
    "required": ["status", "vm"],
}

VM_PCI_DEVICE_REMOVE_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": _VM_ID,
        "device_key": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Key of the passthrough device to remove. Read it from pci_devices in the "
                "answer of vmware.composite.vm.pci_device.add, or from vmware.vm.hardware.show."
            ),
        },
    },
    "required": ["vm", "device_key"],
    "additionalProperties": False,
    "examples": [{"vm": "<vm-id>", "device_key": "<device-key>"}],
}

VM_PCI_DEVICE_REMOVE_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "removed",
                "unchanged",
                "still_present",
                "refused",
                "not_found",
                "invalid_request",
                "timeout",
            ],
            "description": (
                "removed: the device is gone; unchanged: the VM has no such device, nothing "
                "sent; still_present: the task ended but the device still reads back; "
                "refused: the VM is not powered off; not_found: no such VM; timeout: the "
                "task did not finish in time."
            ),
        },
        **_COMMON_ANSWER,
        "device_key": {"type": "integer", "description": "The key that was asked for."},
        "device": {
            **PCI_DEVICE_ROW_SCHEMA,
            "type": ["object", "null"],
            "description": "The device that was removed.",
        },
        "pci_devices": _PCI_DEVICE_LIST,
    },
    "required": ["status", "vm"],
}

VM_PCI_DEVICE_ADD_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to give a VM direct access to a host PCI device, such as a GPU, a NIC or an "
        "accelerator card. Use pci_id for one fixed host device (DirectPath I/O), or "
        "dynamic_device to let vCenter pick any free device of that vendor and device id "
        "(dynamic DirectPath)."
    ),
    "preconditions": (
        "The VM must be powered off. The VM's host must have passthrough enabled for the "
        "device. A passthrough VM needs all its memory reserved; MEHO sets this unless "
        "reserve_all_memory is false. The change waits for approval. A wrong id makes the "
        "call answer invalid_request with the devices the host offers."
    ),
    "parameter_hints": {
        "pci_id": "'<domain>:<bus>:<slot>.<function>', such as '0000:3b:00.0'.",
        "dynamic_device": "{vendor_id, device_id} as numbers; custom_label is optional.",
    },
    "output_shape": (
        "{status, vm, vm_name, requested, device: {device_key, label, kind, pci_id, "
        "vendor_id, device_id, device_name, custom_label}, pci_devices: [same rows], task, "
        "task_state, guidance}."
    ),
    "next_step": (
        "Safe to run twice with pci_id: the second call answers unchanged. With "
        "dynamic_device a second call adds a second device. Power the VM on with "
        "vmware.composite.vm.power; it fails to start when the device is in use elsewhere."
    ),
}

VM_PCI_DEVICE_REMOVE_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": "Call to take one PCI passthrough device away from a VM.",
    "preconditions": (
        "The VM must be powered off. Get the device_key from the pci_devices list of "
        "vmware.composite.vm.pci_device.add or from vmware.vm.hardware.show. The change "
        "waits for approval. The VM keeps its full memory reservation."
    ),
    "output_shape": (
        "{status, vm, vm_name, device_key, device, pci_devices: [{device_key, label, kind, "
        "pci_id, ...}], task, task_state, guidance}."
    ),
    "next_step": (
        "Safe to run twice: when the device is gone, nothing is sent (unchanged). Power the "
        "VM on again with vmware.composite.vm.power when you are done."
    ),
}

# ---------------------------------------------------------------------------
# Console: screenshot and keys
# ---------------------------------------------------------------------------

VM_SCREENSHOT_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": _VM_ID,
        "max_bytes": {
            "type": "integer",
            "minimum": 1,
            "maximum": SCREENSHOT_HARD_CAP_BYTES,
            "description": (
                "Largest image MEHO reads, in bytes. A larger image is refused, not cut. "
                "Default 2 MiB, at most 8 MiB."
            ),
        },
    },
    "required": ["vm"],
    "additionalProperties": False,
    "examples": [{"vm": "<vm-id>"}],
}

VM_SCREENSHOT_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["taken", "refused", "not_found", "invalid_request", "timeout"],
            "description": (
                "taken: the image is in content_lines; refused: the VM is not powered on; "
                "not_found: no such VM; timeout: the task did not finish in time."
            ),
        },
        **_COMMON_ANSWER,
        "file": {
            **_NULLABLE_STR,
            "description": "Datastore path of the PNG file vCenter wrote, '[datastore] path'.",
        },
        "size_bytes": {**_NULLABLE_INT, "description": "Size of the PNG image."},
        "media_type": {**_NULLABLE_STR, "description": "Always 'image/png' when taken."},
        "encoding": {**_NULLABLE_STR, "description": "Always 'base64' when taken."},
        "content_lines": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": (
                "The PNG image, base64 in rows. Join all rows, then decode. A long list "
                "comes back as a result handle; read every row with result_query."
            ),
        },
    },
    "required": ["status", "vm"],
}

VM_KEYS_SEND_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": _VM_ID,
        "keys": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": {
                "type": "string",
                "enum": list(CONSOLE_KEYS),
                "description": "One key, or the Ctrl+Alt+Del combination.",
            },
            "description": (
                "Keys to press, in order. Only special keys: no letters or digits, so the "
                "call cannot type a command."
            ),
        },
    },
    "required": ["vm", "keys"],
    "additionalProperties": False,
    "examples": [
        {"vm": "<vm-id>", "keys": ["ctrl_alt_del"]},
        {"vm": "<vm-id>", "keys": ["down", "enter"]},
    ],
}

VM_KEYS_SEND_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["sent", "refused", "not_found", "invalid_request"],
            "description": (
                "sent: vCenter took the keys; refused: the VM is not powered on; not_found: "
                "no such VM; invalid_request: a key is not on the list, nothing sent."
            ),
        },
        **_COMMON_ANSWER,
        "keys": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": "The keys that were asked for.",
        },
        "keys_sent": {**_NULLABLE_INT, "description": "Key presses vCenter says it sent."},
        "power_state": {**_NULLABLE_STR, "description": "VM power state, read after the send."},
    },
    "required": ["status", "vm"],
}

VM_SCREENSHOT_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to see what a running VM's console shows, for example why a guest hangs at "
        "boot, shows an error, or has no network."
    ),
    "preconditions": (
        "The VM must be powered on. vCenter writes the PNG file into the VM's folder on "
        "its datastore and leaves it there. The call waits for approval."
    ),
    "output_shape": (
        "{status, vm, vm_name, file, size_bytes, media_type, encoding: base64, "
        "content_lines: [base64 rows], task, task_state, guidance}."
    ),
    "next_step": (
        "Join content_lines and decode the base64 to get the PNG. When the console asks "
        "for a key (a boot menu, a login prompt), send it with vmware.composite.vm.keys.send, "
        "then take a new screenshot."
    ),
}

VM_KEYS_SEND_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to press special keys on a running VM's console: Ctrl+Alt+Del, Enter, Esc, "
        "Tab, Backspace, F1 to F12 or the arrow keys. For example to pick a boot menu entry "
        "or to get past a prompt that waits for Enter."
    ),
    "preconditions": (
        "The VM must be powered on. Take a screenshot first with "
        "vmware.composite.vm.screenshot to see what the console waits for. Ctrl+Alt+Del can "
        "restart the guest. The change waits for approval."
    ),
    "parameter_hints": {"keys": "Names from the enum only, such as ['esc'] or ['down', 'enter']."},
    "output_shape": "{status, vm, vm_name, keys, keys_sent, power_state, guidance}.",
    "next_step": (
        "Not safe to run twice: each call presses the keys again. Take a new screenshot to "
        "see the result."
    ),
}
