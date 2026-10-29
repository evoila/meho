# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Response schemas of the Azure compute reads.

The AI reads these field descriptions to understand an answer. The field
names are a contract: the VM metrics read and the migration sizing build on
``azure.vm.list``, ``azure.vm.show`` and ``azure.vm.size.list``, so a name
here changes only together with its readers. The rows are built in
:mod:`.compute_rows`.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.azure._model import TEXT_OR_NULL

__all__ = [
    "DISK_LIST_RESPONSE",
    "IMAGE_LIST_RESPONSE",
    "VMSS_LIST_RESPONSE",
    "VM_LIST_RESPONSE",
    "VM_SHOW_RESPONSE",
    "VM_SIZE_LIST_RESPONSE",
]

_WHOLE: Final[list[str]] = ["integer", "null"]


def _field(kind: str | list[str], description: str) -> dict[str, Any]:
    return {"type": kind, "description": description}


def _texts(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


_NAME: Final[dict[str, Any]] = _field(TEXT_OR_NULL, "The name of the resource, as set in Azure.")
_ID: Final[dict[str, Any]] = _field(
    TEXT_OR_NULL, "The full Azure resource ID. Use it to find the resource in other reads."
)
_RESOURCE_GROUP: Final[dict[str, Any]] = _field(
    TEXT_OR_NULL, "The resource group, read from the resource ID."
)
_LOCATION: Final[dict[str, Any]] = _field(TEXT_OR_NULL, "The Azure region, for example westeurope.")
_ZONES: Final[dict[str, Any]] = _texts(
    "The availability zones of the resource, for example ['1']. Empty when it is not zonal."
)
_TAGS: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": {"type": "string"},
    "description": "The tags of the resource (name and value). Text from Azure.",
}
_OS_TYPE: Final[dict[str, Any]] = _field(TEXT_OR_NULL, "Windows or Linux.")
_PROVISIONING_STATE: Final[dict[str, Any]] = _field(
    TEXT_OR_NULL,
    "The state of the last change in Azure: Succeeded, Failed, Updating, Creating or Deleting.",
)

#: The paging fields of every list (``PagedRows.as_dict``).
_PAGING: Final[dict[str, Any]] = {
    "truncated": _field("boolean", "True when MEHO stopped reading pages at one of its limits."),
    "omission": _field(
        TEXT_OR_NULL, "What was left out and how to narrow the request, when truncated."
    ),
    "omission_kind": _field(
        TEXT_OR_NULL, "Which limit stopped the list: row_limit, page_limit or time_limit."
    ),
    "pages_read": _field("integer", "How many pages MEHO read from Azure."),
}


def _list_response(
    row: dict[str, Any], rows_text: str, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A list answer: the rows under ``value``, the paging fields, and *extra*."""
    return {
        "type": "object",
        "properties": {
            "value": {"type": "array", "items": row, "description": rows_text},
            **_PAGING,
            **(extra or {}),
        },
        "required": ["value", "truncated", "pages_read", *(extra or {})],
    }


def _row(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": ["name", "id"]}


_ROWS_TEXT: Final[str] = "One row per {what}. More than 50 rows come back as a stored-result link."

# ----------------------------------------------------------------------
# azure.vm.list
# ----------------------------------------------------------------------

_POWER_STATE: Final[dict[str, Any]] = _field(
    TEXT_OR_NULL,
    "running, deallocated, stopped, starting, stopping or deallocating, from the VM's "
    "PowerState status. stopped still costs compute; deallocated does not. null when "
    "Azure gave no power state.",
)
_SIZE: Final[dict[str, Any]] = _field(
    TEXT_OR_NULL,
    "The VM size, for example Standard_D2s_v5. azure.vm.size.list describes each size.",
)

_VM_ROW: Final[dict[str, Any]] = _row(
    {
        "name": _NAME,
        "id": _ID,
        "resource_group": _RESOURCE_GROUP,
        "location": _LOCATION,
        "zones": _ZONES,
        "size": _SIZE,
        "os_type": _OS_TYPE,
        "power_state": _POWER_STATE,
        "provisioning_state": _PROVISIONING_STATE,
        "os_disk_id": _field(TEXT_OR_NULL, "The resource ID of the OS disk (a managed disk)."),
        "data_disk_ids": _texts("The resource IDs of the data disks (managed disks)."),
        "tags": _TAGS,
    }
)

_POWER_STATE_READ: Final[dict[str, Any]] = {
    "type": "object",
    "description": (
        "How the power states were read: one extra read of the subscription's VM list "
        "with statusOnly=true. Stays inline when the rows go behind a stored-result link."
    ),
    "properties": {
        "vms_without_state": _field(
            "integer", "How many rows have power_state null. 0 means every VM has one."
        ),
        "pages_read": _field(
            "integer", "Pages of the status read. 0 when there was no VM to read a state for."
        ),
        "truncated": _field("boolean", "True when the status read stopped at a MEHO limit."),
        "omission": _field(TEXT_OR_NULL, "Why the status read stopped early, when it did."),
        "omission_kind": _field(
            TEXT_OR_NULL, "row_limit, page_limit or time_limit, when truncated."
        ),
    },
    "required": ["vms_without_state", "pages_read", "truncated"],
}

VM_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    _VM_ROW,
    _ROWS_TEXT.format(what="VM"),
    {"power_state_read": _POWER_STATE_READ},
)

# ----------------------------------------------------------------------
# azure.vm.show
# ----------------------------------------------------------------------

_DISK_ENTRY_FIELDS: Final[dict[str, Any]] = {
    "name": _field(TEXT_OR_NULL, "The disk name."),
    "id": _field(TEXT_OR_NULL, "The resource ID of the managed disk."),
    "size_gb": _field(_WHOLE, "The disk size in GB."),
    "sku": _field(TEXT_OR_NULL, "The disk type, for example Premium_LRS or StandardSSD_LRS."),
    "caching": _field(TEXT_OR_NULL, "The host caching: None, ReadOnly or ReadWrite."),
}

VM_SHOW_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "name": _NAME,
        "id": _ID,
        "vm_id": _field(TEXT_OR_NULL, "The unique ID Azure gave the VM (a GUID)."),
        "resource_group": _RESOURCE_GROUP,
        "location": _LOCATION,
        "zones": _ZONES,
        "size": _SIZE,
        "os_type": _OS_TYPE,
        "os_name": _field(
            TEXT_OR_NULL,
            "The guest OS name, as the VM agent reports it. null without a running agent.",
        ),
        "os_version": _field(TEXT_OR_NULL, "The guest OS version, as the VM agent reports it."),
        "computer_name": _field(TEXT_OR_NULL, "The host name inside the guest OS."),
        "hyperv_generation": _field(TEXT_OR_NULL, "V1 (BIOS boot) or V2 (UEFI boot)."),
        "power_state": _POWER_STATE,
        "provisioning_state": _PROVISIONING_STATE,
        "os_disk": {
            "type": "object",
            "description": "The OS disk.",
            "properties": _DISK_ENTRY_FIELDS,
        },
        "data_disks": {
            "type": "array",
            "description": "The data disks, with their slot number (lun).",
            "items": {
                "type": "object",
                "properties": {
                    "lun": _field(_WHOLE, "The slot of the disk on the VM (logical unit number)."),
                    **_DISK_ENTRY_FIELDS,
                },
            },
        },
        "network_interfaces": {
            "type": "array",
            "description": "The network interfaces of the VM.",
            "items": {
                "type": "object",
                "properties": {
                    "id": _field(TEXT_OR_NULL, "The resource ID of the network interface."),
                    "primary": _field(
                        ["boolean", "null"], "True for the primary interface of the VM."
                    ),
                },
            },
        },
        "availability_set_id": _field(
            TEXT_OR_NULL, "The resource ID of the VM's availability set, if it has one."
        ),
        "scale_set_id": _field(
            TEXT_OR_NULL, "The resource ID of the VM's scale set, if it has one."
        ),
        "tags": _TAGS,
    },
    "required": ["name", "id", "power_state", "os_disk", "data_disks", "network_interfaces"],
}

# ----------------------------------------------------------------------
# azure.disk.list, azure.image.list, azure.vmss.list
# ----------------------------------------------------------------------

_DISK_ROW: Final[dict[str, Any]] = _row(
    {
        "name": _NAME,
        "id": _ID,
        "resource_group": _RESOURCE_GROUP,
        "location": _LOCATION,
        "zones": _ZONES,
        "size_gb": _field(_WHOLE, "The disk size in GB."),
        "sku": _field(TEXT_OR_NULL, "The disk type, for example Premium_LRS or StandardSSD_LRS."),
        "disk_state": _field(
            TEXT_OR_NULL,
            "Attached, Unattached, Reserved, ActiveSAS and so on. An Unattached disk "
            "belongs to no VM but still costs money.",
        ),
        "owner_vm_id": _field(TEXT_OR_NULL, "The resource ID of the VM the disk is attached to."),
        "encryption_type": _field(
            TEXT_OR_NULL,
            "EncryptionAtRestWithPlatformKey, EncryptionAtRestWithCustomerKey or "
            "EncryptionAtRestWithPlatformAndCustomerKeys.",
        ),
        "os_type": _field(TEXT_OR_NULL, "Windows or Linux for an OS disk; null for a data disk."),
        "tags": _TAGS,
    }
)

DISK_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    _DISK_ROW, _ROWS_TEXT.format(what="managed disk")
)

_IMAGE_ROW: Final[dict[str, Any]] = _row(
    {
        "name": _NAME,
        "id": _ID,
        "resource_group": _RESOURCE_GROUP,
        "location": _LOCATION,
        "os_type": _OS_TYPE,
        "os_state": _field(TEXT_OR_NULL, "Generalized (ready for new VMs) or Specialized."),
        "hyperv_generation": _field(TEXT_OR_NULL, "V1 or V2: the VM generation the image boots."),
        "os_disk_size_gb": _field(_WHOLE, "The size of the image's OS disk in GB."),
        "data_disk_count": _field("integer", "How many data disks the image holds."),
        "source_vm_id": _field(TEXT_OR_NULL, "The resource ID of the VM the image was made from."),
        "provisioning_state": _PROVISIONING_STATE,
        "tags": _TAGS,
    }
)

IMAGE_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    _IMAGE_ROW, _ROWS_TEXT.format(what="managed image")
)

_VMSS_ROW: Final[dict[str, Any]] = _row(
    {
        "name": _NAME,
        "id": _ID,
        "resource_group": _RESOURCE_GROUP,
        "location": _LOCATION,
        "zones": _ZONES,
        "size": _field(
            TEXT_OR_NULL, "The VM size of the scale set's VMs, for example Standard_D2s_v5."
        ),
        "capacity": _field(_WHOLE, "How many VMs the scale set should run."),
        "orchestration_mode": _field(TEXT_OR_NULL, "Uniform or Flexible."),
        "upgrade_mode": _field(TEXT_OR_NULL, "Automatic, Manual or Rolling."),
        "os_type": _OS_TYPE,
        "provisioning_state": _PROVISIONING_STATE,
        "tags": _TAGS,
    }
)

VMSS_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    _VMSS_ROW, _ROWS_TEXT.format(what="scale set")
)

# ----------------------------------------------------------------------
# azure.vm.size.list
# ----------------------------------------------------------------------

_RESTRICTION: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "type": _field(
            TEXT_OR_NULL,
            "Location: the size cannot be used in this region by this subscription. "
            "Zone: it cannot be used in the zones listed.",
        ),
        "reason_code": _field(TEXT_OR_NULL, "NotAvailableForSubscription or QuotaId."),
        "zones": _texts("The zones the restriction covers. Empty for a Location restriction."),
    },
}

_SIZE_ROW: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "name": _field(TEXT_OR_NULL, "The size name, for example Standard_D2s_v5."),
        "family": _field(
            TEXT_OR_NULL, "The size family Azure counts quota in, for example standardDSv5Family."
        ),
        "vcpus": _field(_WHOLE, "The number of vCPUs."),
        "vcpus_available": _field(
            _WHOLE, "The vCPUs a VM of this size can use; lower than vcpus on constrained sizes."
        ),
        "memory_gb": _field(["number", "null"], "The memory in GB."),
        "max_data_disks": _field(_WHOLE, "The most data disks a VM of this size can have."),
        "premium_io": _field(["boolean", "null"], "True when the size supports Premium SSD disks."),
        "hyperv_generations": _texts("The VM generations the size runs: V1, V2 or both."),
        "acus": _field(_WHOLE, "Azure compute units: the CPU speed per vCPU, to compare sizes."),
        "cpu_architecture": _field(TEXT_OR_NULL, "x64 or Arm64."),
        "zones": _texts("The zones of this region in which Azure offers the size."),
        "restrictions": {
            "type": "array",
            "items": _RESTRICTION,
            "description": (
                "Why this subscription cannot use the size here. Empty when it can use it "
                "in the whole region."
            ),
        },
    },
    "required": ["name", "vcpus", "memory_gb", "restrictions"],
}

VM_SIZE_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    _SIZE_ROW,
    "One row per VM size of the region. A region has hundreds, so they come back as a "
    "stored-result link.",
    {"location": _field("string", "The region the sizes are for, in lower case.")},
)
