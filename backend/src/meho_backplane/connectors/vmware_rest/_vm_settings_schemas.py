# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schemas of the two typed reads ``vmware.vm.boot.show`` and ``vmware.vm.advanced_settings.show``.

Kept apart from the handlers so the handler modules stay small. The boot order
row and the advanced-setting row are shared with the changes in
``composites/_vm_boot.py`` and ``composites/_vm_advanced_settings.py``.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest._vm_hardware_schemas import VM_SELECTOR_PROPERTIES

__all__ = [
    "ADVANCED_SETTING_ROW_SCHEMA",
    "BOOT_ORDER_ENTRY_SCHEMA",
    "BOOT_STATE_PROPERTIES",
    "VM_ADVANCED_SETTINGS_PARAMETER_SCHEMA",
    "VM_ADVANCED_SETTINGS_RESPONSE_SCHEMA",
    "VM_BOOT_RESPONSE_SCHEMA",
    "VM_READ_PARAMETER_SCHEMA",
]

_NULLABLE_BOOL: Final = {"type": ["boolean", "null"]}
_NULLABLE_INT: Final = {"type": ["integer", "null"]}
_NULLABLE_STR: Final = {"type": ["string", "null"]}

BOOT_DEVICE_TYPES: Final[tuple[str, ...]] = ("CDROM", "DISK", "ETHERNET", "FLOPPY")


def _field(base: dict[str, Any], description: str) -> dict[str, Any]:
    return {**base, "description": description}


#: The ``vm`` / ``name`` selector of a read.
VM_READ_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": VM_SELECTOR_PROPERTIES,
    "oneOf": [{"required": ["vm"]}, {"required": ["name"]}],
    "additionalProperties": False,
    "examples": [{"name": "<vm-name>"}, {"vm": "vm-42"}],
}

#: One boot order entry, as the read returns it.
BOOT_ORDER_ENTRY_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "type": _field(_NULLABLE_STR, "Boot device kind: CDROM, DISK, ETHERNET or FLOPPY."),
        "nic": _field(_NULLABLE_STR, "Network adapter id, for ETHERNET."),
        "disks": {
            "type": ["array", "null"],
            "items": {"type": "string", "description": "Disk id, such as '2000'."},
            "description": "Disk ids in boot order, for DISK.",
        },
    },
}

#: The boot fields the read and the boot change both return.
BOOT_STATE_PROPERTIES: Final[dict[str, Any]] = {
    "vm": {"type": "string", "description": "VM id."},
    "vm_name": _field(_NULLABLE_STR, "VM name, or null when the VIM read failed."),
    "firmware": _field(_NULLABLE_STR, "BIOS or EFI."),
    "efi_secure_boot": _field(_NULLABLE_BOOL, "EFI secure boot on or off."),
    "efi_legacy_boot": _field(_NULLABLE_BOOL, "EFI legacy boot mode, for EFI."),
    "network_protocol": _field(_NULLABLE_STR, "IPV4 or IPV6, for a network boot."),
    "delay_ms": _field(_NULLABLE_INT, "Wait before the boot starts, in milliseconds."),
    "retry": _field(_NULLABLE_BOOL, "Retry the boot after a failure."),
    "retry_delay_ms": _field(_NULLABLE_INT, "Wait before a boot retry, in milliseconds."),
    "enter_setup_mode": _field(
        _NULLABLE_BOOL, "Enter the firmware setup on the next boot (resets after it)."
    ),
    "boot_order": {
        "type": ["array", "null"],
        "items": BOOT_ORDER_ENTRY_SCHEMA,
        "description": "Boot devices in order; empty means vCenter's default order.",
    },
}

VM_BOOT_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        **BOOT_STATE_PROPERTIES,
        "read_notes": {
            "type": "array",
            "items": {"type": "string", "description": "One part that could not be read."},
            "description": "Why a part is null.",
        },
    },
    "required": ["vm", "firmware", "read_notes"],
}

#: One advanced setting row.
ADVANCED_SETTING_ROW_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "description": "Setting key, such as 'disk.EnableUUID'."},
        "value": _field(_NULLABLE_STR, "Setting value as text; null when masked."),
        "masked": {
            "type": "boolean",
            "description": "True when the value may hold a secret and is not returned.",
        },
    },
    "required": ["key", "value", "masked"],
}

VM_ADVANCED_SETTINGS_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        **VM_SELECTOR_PROPERTIES,
        "key_prefix": {
            "type": "string",
            "minLength": 1,
            "description": "Return only keys that start with this text, such as 'guestinfo.'.",
        },
    },
    "oneOf": [{"required": ["vm"]}, {"required": ["name"]}],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42"}, {"name": "<vm-name>", "key_prefix": "isolation."}],
}

VM_ADVANCED_SETTINGS_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": {"type": "string", "description": "VM id."},
        "vm_name": _field(_NULLABLE_STR, "VM name."),
        "key_prefix": _field(_NULLABLE_STR, "The filter used, or null."),
        "setting_count": {"type": "integer", "description": "Number of rows."},
        "masked_count": {"type": "integer", "description": "Rows whose value is masked."},
        "settings": {
            "type": "array",
            "items": ADVANCED_SETTING_ROW_SCHEMA,
            "description": "The settings, sorted by key.",
        },
    },
    "required": ["vm", "setting_count", "settings"],
}
