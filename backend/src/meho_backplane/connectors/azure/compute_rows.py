# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Rows of the Azure compute reads, built from Azure's answers.

Pure functions, no calls: :mod:`.compute_reads` calls Azure and passes each
answer here. Every row is built from a short list of named fields, so
nothing else from Azure's answer reaches the result: no admin user name, no
custom data, no blob URL. Text from Azure (names, tags, states) is untrusted
data.

Azure's answers are JSON from outside, so every value is checked for its
type before it is used. A field that is missing or has the wrong type
becomes ``None`` (or an empty list or object), never an error.
"""

from __future__ import annotations

import math
from typing import Any, Final

from meho_backplane.connectors.azure._paging import PagedRows

__all__ = [
    "disk_row",
    "image_row",
    "power_state_read",
    "power_states_by_id",
    "scale_set_row",
    "size_row",
    "vm_detail",
    "vm_row",
]

#: The start of a power state code in a VM's instance view, for example
#: ``PowerState/running``. Compared in lower case.
_POWER_STATE_PREFIX: Final[str] = "powerstate/"

#: The resource type of a VM size in the Resource SKUs answer. Compared in lower case.
_VM_SKU_TYPE: Final[str] = "virtualmachines"


def _object(value: object) -> dict[str, Any]:
    """*value* when it is a JSON object, else an empty one."""
    return value if isinstance(value, dict) else {}


def _items(value: object) -> list[Any]:
    """*value* when it is a JSON list, else an empty one."""
    return value if isinstance(value, list) else []


def _text(value: object) -> str | None:
    """*value* when it is a non-empty text, else ``None``."""
    return value if isinstance(value, str) and value else None


def _whole_number(value: object) -> int | None:
    """*value* when it is a whole number (not ``True`` or ``False``), else ``None``."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _texts(value: object) -> list[str]:
    """The texts in *value* when it is a list, else an empty list."""
    return [item for item in _items(value) if isinstance(item, str)]


def _tags(value: object) -> dict[str, str]:
    """The tags of a resource. Only keys and values that are texts are kept."""
    return {
        key: item
        for key, item in _object(value).items()
        if isinstance(key, str) and isinstance(item, str)
    }


def _id_of(value: object) -> str | None:
    """The ``id`` of a reference such as ``{"id": "/subscriptions/..."}``."""
    return _text(_object(value).get("id"))


def _resource_group(resource_id: str | None) -> str | None:
    """The resource group named in a resource ID, as Azure wrote it."""
    if resource_id is None:
        return None
    parts = resource_id.split("/")
    for index, part in enumerate(parts[:-1]):
        if part.lower() == "resourcegroups":
            return parts[index + 1] or None
    return None


def _power_state(instance_view: dict[str, Any]) -> str | None:
    """The power state in an instance view, for example ``running`` or ``deallocated``.

    Read from the status whose code starts with ``PowerState/``. When there
    are several, the last one counts, as in Azure PowerShell. ``None`` when
    there is none: Microsoft says the power state can be missing in rare
    cases.
    """
    state = None
    for status in _items(instance_view.get("statuses")):
        code = _text(_object(status).get("code"))
        if code is not None and code.lower().startswith(_POWER_STATE_PREFIX):
            state = code[len(_POWER_STATE_PREFIX) :].lower() or None
    return state


# ----------------------------------------------------------------------
# Rows
# ----------------------------------------------------------------------


def _common(item: dict[str, Any]) -> dict[str, Any]:
    """The fields every compute row starts with."""
    resource_id = _text(item.get("id"))
    return {
        "name": _text(item.get("name")),
        "id": resource_id,
        "resource_group": _resource_group(resource_id),
        "location": _text(item.get("location")),
    }


def _data_disk_ids(data_disks: object) -> list[str]:
    """The managed-disk IDs of a VM's data disks."""
    return [
        disk_id
        for disk_id in (_id_of(_object(disk).get("managedDisk")) for disk in _items(data_disks))
        if disk_id is not None
    ]


def vm_row(item: object, power_states: dict[str, str]) -> dict[str, Any] | None:
    """One row of ``azure.vm.list`` from one Azure ``VirtualMachine``."""
    if not isinstance(item, dict):
        return None
    properties = _object(item.get("properties"))
    storage = _object(properties.get("storageProfile"))
    os_disk = _object(storage.get("osDisk"))
    row = _common(item)
    vm_id = row["id"]
    row.update(
        {
            "zones": _texts(item.get("zones")),
            "size": _text(_object(properties.get("hardwareProfile")).get("vmSize")),
            "os_type": _text(os_disk.get("osType")),
            "power_state": power_states.get(vm_id.lower()) if vm_id is not None else None,
            "provisioning_state": _text(properties.get("provisioningState")),
            "os_disk_id": _id_of(os_disk.get("managedDisk")),
            "data_disk_ids": _data_disk_ids(storage.get("dataDisks")),
            "tags": _tags(item.get("tags")),
        }
    )
    return row


def power_states_by_id(items: list[Any]) -> dict[str, str]:
    """``{lower-case VM resource ID: power state}`` from a ``statusOnly`` list.

    Resource IDs are compared in lower case, because Azure does not keep the
    case of a resource group the same in every answer.
    """
    states: dict[str, str] = {}
    for item in items:
        entry = _object(item)
        vm_id = _text(entry.get("id"))
        state = _power_state(_object(_object(entry.get("properties")).get("instanceView")))
        if vm_id is not None and state is not None:
            states[vm_id.lower()] = state
    return states


def power_state_read(states: PagedRows, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """How the power states were read, kept next to the rows (an object)."""
    read = states.as_dict()
    return {
        "vms_without_state": sum(1 for row in rows if row["power_state"] is None),
        "pages_read": read["pages_read"],
        "truncated": read["truncated"],
        "omission": read["omission"],
        "omission_kind": read["omission_kind"],
    }


def _disk_entry(disk: object) -> dict[str, Any]:
    """The OS disk or one data disk of ``azure.vm.show``."""
    entry = _object(disk)
    managed = _object(entry.get("managedDisk"))
    return {
        "name": _text(entry.get("name")),
        "id": _text(managed.get("id")),
        "size_gb": _whole_number(entry.get("diskSizeGB")),
        "sku": _text(managed.get("storageAccountType")),
        "caching": _text(entry.get("caching")),
    }


def _data_disk_entry(disk: object) -> dict[str, Any]:
    return {"lun": _whole_number(_object(disk).get("lun")), **_disk_entry(disk)}


def _network_interfaces(network_profile: dict[str, Any]) -> list[dict[str, Any]]:
    """The network interfaces of a VM: their resource IDs, and which one is primary."""
    rows: list[dict[str, Any]] = []
    for interface in _items(network_profile.get("networkInterfaces")):
        entry = _object(interface)
        primary = _object(entry.get("properties")).get("primary")
        rows.append(
            {
                "id": _text(entry.get("id")),
                "primary": primary if isinstance(primary, bool) else None,
            }
        )
    return rows


def vm_detail(item: dict[str, Any]) -> dict[str, Any]:
    """The answer of ``azure.vm.show`` from one ``VirtualMachine`` with its instance view."""
    properties = _object(item.get("properties"))
    storage = _object(properties.get("storageProfile"))
    os_disk = _object(storage.get("osDisk"))
    view = _object(properties.get("instanceView"))
    detail = _common(item)
    detail.update(
        {
            "vm_id": _text(properties.get("vmId")),
            "zones": _texts(item.get("zones")),
            "size": _text(_object(properties.get("hardwareProfile")).get("vmSize")),
            "os_type": _text(os_disk.get("osType")),
            "os_name": _text(view.get("osName")),
            "os_version": _text(view.get("osVersion")),
            "computer_name": _text(view.get("computerName"))
            or _text(_object(properties.get("osProfile")).get("computerName")),
            "hyperv_generation": _text(view.get("hyperVGeneration")),
            "power_state": _power_state(view),
            "provisioning_state": _text(properties.get("provisioningState")),
            "os_disk": _disk_entry(os_disk),
            "data_disks": [_data_disk_entry(disk) for disk in _items(storage.get("dataDisks"))],
            "network_interfaces": _network_interfaces(_object(properties.get("networkProfile"))),
            "availability_set_id": _id_of(properties.get("availabilitySet")),
            "scale_set_id": _id_of(properties.get("virtualMachineScaleSet")),
            "tags": _tags(item.get("tags")),
        }
    )
    return detail


def disk_row(item: object) -> dict[str, Any] | None:
    """One row of ``azure.disk.list`` from one Azure ``Disk``."""
    if not isinstance(item, dict):
        return None
    properties = _object(item.get("properties"))
    row = _common(item)
    row.update(
        {
            "zones": _texts(item.get("zones")),
            "size_gb": _whole_number(properties.get("diskSizeGB")),
            "sku": _text(_object(item.get("sku")).get("name")),
            "disk_state": _text(properties.get("diskState")),
            "owner_vm_id": _text(item.get("managedBy")),
            "encryption_type": _text(_object(properties.get("encryption")).get("type")),
            "os_type": _text(properties.get("osType")),
            "tags": _tags(item.get("tags")),
        }
    )
    return row


def image_row(item: object) -> dict[str, Any] | None:
    """One row of ``azure.image.list`` from one Azure ``Image``."""
    if not isinstance(item, dict):
        return None
    properties = _object(item.get("properties"))
    storage = _object(properties.get("storageProfile"))
    os_disk = _object(storage.get("osDisk"))
    row = _common(item)
    row.update(
        {
            "os_type": _text(os_disk.get("osType")),
            "os_state": _text(os_disk.get("osState")),
            "hyperv_generation": _text(properties.get("hyperVGeneration")),
            "os_disk_size_gb": _whole_number(os_disk.get("diskSizeGB")),
            "data_disk_count": len(_items(storage.get("dataDisks"))),
            "source_vm_id": _id_of(properties.get("sourceVirtualMachine")),
            "provisioning_state": _text(properties.get("provisioningState")),
            "tags": _tags(item.get("tags")),
        }
    )
    return row


def scale_set_row(item: object) -> dict[str, Any] | None:
    """One row of ``azure.vmss.list`` from one Azure ``VirtualMachineScaleSet``."""
    if not isinstance(item, dict):
        return None
    properties = _object(item.get("properties"))
    sku = _object(item.get("sku"))
    profile = _object(properties.get("virtualMachineProfile"))
    os_disk = _object(_object(profile.get("storageProfile")).get("osDisk"))
    row = _common(item)
    row.update(
        {
            "zones": _texts(item.get("zones")),
            "size": _text(sku.get("name")),
            "capacity": _whole_number(sku.get("capacity")),
            "orchestration_mode": _text(properties.get("orchestrationMode")),
            "upgrade_mode": _text(_object(properties.get("upgradePolicy")).get("mode")),
            "os_type": _text(os_disk.get("osType")),
            "provisioning_state": _text(properties.get("provisioningState")),
            "tags": _tags(item.get("tags")),
        }
    )
    return row


# ----------------------------------------------------------------------
# VM sizes (Resource SKUs)
# ----------------------------------------------------------------------


def _capabilities(value: object) -> dict[str, str]:
    """``{name: value}`` from a SKU's list of capabilities. Azure sends every value as text."""
    found: dict[str, str] = {}
    for pair in _items(value):
        name, text = _object(pair).get("name"), _object(pair).get("value")
        if isinstance(name, str) and isinstance(text, str):
            found[name] = text
    return found


def _count(text: str | None) -> int | None:
    """A whole number written as text, for example ``"4"``. ``None`` when it is not one."""
    if text is None or not text.isascii() or not text.isdigit():
        return None
    return int(text)


def _amount(text: str | None) -> float | None:
    """A number written as text, for example ``"0.75"``. ``None`` when it is not one."""
    if text is None:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _yes_no(text: str | None) -> bool | None:
    """``"True"`` or ``"False"`` as a boolean; ``None`` for anything else."""
    if text is None:
        return None
    return {"true": True, "false": False}.get(text.lower())


def _generations(text: str | None) -> list[str]:
    """``"V1,V2"`` as ``["V1", "V2"]``: the Hyper-V generations a size runs."""
    if text is None:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _zones_in(location_info: object, location: str) -> list[str]:
    """The zones in which Azure offers a size in *location*, before restrictions."""
    for info in _items(location_info):
        entry = _object(info)
        where = _text(entry.get("location"))
        if where is not None and where.lower() == location and entry.get("type") is None:
            return _texts(entry.get("zones"))
    return []


def _names_region(regions: object, location: str) -> bool:
    """True when a list of regions is missing or empty, or names *location*.

    Azure's filter already asks for one region. This check makes sure a row
    or a restriction for another region never slips in, the way Microsoft's
    own SKU library reads the answer.
    """
    names = [name.lower() for name in _texts(regions)]
    return not names or location in names


def _restriction(value: object) -> dict[str, Any]:
    """One restriction of a size for this subscription: type, reason code and zones."""
    entry = _object(value)
    return {
        "type": _text(entry.get("type")),
        "reason_code": _text(entry.get("reasonCode")),
        "zones": _texts(_object(entry.get("restrictionInfo")).get("zones")),
    }


def size_row(item: object, location: str) -> dict[str, Any] | None:
    """One row of ``azure.vm.size.list``.

    ``None`` for a SKU that is not a VM size or that is for another region.
    """
    if not isinstance(item, dict):
        return None
    kind = _text(item.get("resourceType"))
    if kind is None or kind.lower() != _VM_SKU_TYPE:
        return None
    if not _names_region(item.get("locations"), location):
        return None
    caps = _capabilities(item.get("capabilities"))
    restrictions = [
        entry
        for entry in _items(item.get("restrictions"))
        if _names_region(_object(entry).get("values"), location)
    ]
    return {
        "name": _text(item.get("name")),
        "family": _text(item.get("family")),
        "vcpus": _count(caps.get("vCPUs")),
        "vcpus_available": _count(caps.get("vCPUsAvailable")),
        "memory_gb": _amount(caps.get("MemoryGB")),
        "max_data_disks": _count(caps.get("MaxDataDiskCount")),
        "premium_io": _yes_no(caps.get("PremiumIO")),
        "hyperv_generations": _generations(caps.get("HyperVGenerations")),
        "acus": _count(caps.get("ACUs")),
        "cpu_architecture": _text(caps.get("CpuArchitectureType")),
        "zones": _zones_in(item.get("locationInfo"), location),
        "restrictions": [_restriction(entry) for entry in restrictions],
    }
