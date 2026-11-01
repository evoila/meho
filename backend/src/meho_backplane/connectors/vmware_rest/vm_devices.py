# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The disks, network adapters and guest file systems of one VM, for ``vmware.vm.info``.

``vmware.vm.info`` reads two more VIM properties in its one property read:

* ``config.hardware.device`` -- every virtual device. MEHO keeps the disks
  (``VirtualDisk``: size, file, thin or thick, controller) and the network
  adapters (every ``VirtualEthernetCard`` type: port group, MAC address,
  connected or not). The controller of a disk is found by its ``controllerKey``
  in the same list.
* ``guest.disk`` -- the guest file systems VMware Tools reports (mount point,
  capacity, free space). It is empty when Tools does not run.

The REST VM detail (``GET /api/vcenter/vm/{vm}``) has disks and network
adapters too, but no thin or thick flag; the VIM device list has it, and it
comes in the same read, so no second call is needed. A distributed port group
is named by its moid only; :func:`portgroup_moids` lists those moids so the
caller can read their names.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "DISKS_SCHEMA",
    "FILESYSTEMS_SCHEMA",
    "NICS_SCHEMA",
    "parse_disks",
    "parse_filesystems",
    "parse_nics",
    "portgroup_moids",
]

#: The VIM types of a network adapter (subtypes of ``VirtualEthernetCard``).
_NIC_TYPES: Final = frozenset(
    {
        "VirtualE1000",
        "VirtualE1000e",
        "VirtualPCNet32",
        "VirtualVmxnet",
        "VirtualVmxnet2",
        "VirtualVmxnet3",
        "VirtualVmxnet3Vrdma",
        "VirtualSriovEthernetCard",
    }
)
_DVS_BACKING: Final = "VirtualEthernetCardDistributedVirtualPortBackingInfo"
_OPAQUE_BACKING: Final = "VirtualEthernetCardOpaqueNetworkBackingInfo"


def _list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = [value]
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _label(device: dict[str, Any]) -> str | None:
    info = device.get("deviceInfo")
    return _text(info.get("label")) if isinstance(info, dict) else None


def _moid(ref: Any) -> str | None:
    return _text(ref.get("value")) if isinstance(ref, dict) else None


def _provisioning(backing: dict[str, Any]) -> str | None:
    thin = backing.get("thinProvisioned")
    if thin is True:
        return "thin"
    if thin is False:
        return "thick_eager_zeroed" if backing.get("eagerlyScrub") is True else "thick_lazy_zeroed"
    return None


def parse_disks(devices: Any) -> list[dict[str, Any]]:
    """One row per ``VirtualDisk`` of ``config.hardware.device``."""
    all_devices = _list(devices)
    by_key = {d.get("key"): d for d in all_devices if isinstance(d.get("key"), int)}
    rows: list[dict[str, Any]] = []
    for device in all_devices:
        if device.get("_typeName") != "VirtualDisk":
            continue
        backing = _dict(device.get("backing"))
        controller = _dict(by_key.get(device.get("controllerKey")))
        capacity = _int(device.get("capacityInBytes"))
        if capacity is None:
            kb = _int(device.get("capacityInKB"))
            capacity = None if kb is None else kb * 1024
        rows.append(
            {
                "key": _int(device.get("key")),
                "label": _label(device),
                "capacity_bytes": capacity,
                "file": _text(backing.get("fileName")),
                "datastore": _moid(backing.get("datastore")),
                "provisioning": _provisioning(backing),
                "controller": _label(controller) if controller else None,
                "controller_type": _text(controller.get("_typeName")) if controller else None,
                "unit_number": _int(device.get("unitNumber")),
            }
        )
    return rows


def _is_nic(device: dict[str, Any]) -> bool:
    return device.get("_typeName") in _NIC_TYPES or "macAddress" in device


def parse_nics(devices: Any, portgroup_names: dict[str, str]) -> list[dict[str, Any]]:
    """One row per network adapter of ``config.hardware.device``."""
    rows: list[dict[str, Any]] = []
    for device in _list(devices):
        if not _is_nic(device):
            continue
        backing = _dict(device.get("backing"))
        backing_type = backing.get("_typeName")
        network: str | None
        if backing_type == _DVS_BACKING:
            port = _dict(backing.get("port"))
            network = _text(port.get("portgroupKey"))
            network_name = portgroup_names.get(network or "")
        elif backing_type == _OPAQUE_BACKING:
            network = _text(backing.get("opaqueNetworkId"))
            network_name = None
        else:
            network = _moid(backing.get("network"))
            network_name = _text(backing.get("deviceName"))
        connectable = _dict(device.get("connectable"))
        connected = connectable.get("connected")
        start_connected = connectable.get("startConnected")
        rows.append(
            {
                "key": _int(device.get("key")),
                "label": _label(device),
                "adapter_type": _text(device.get("_typeName")),
                "mac_address": _text(device.get("macAddress")),
                "network": network,
                "network_name": network_name,
                "connected": connected if isinstance(connected, bool) else None,
                "start_connected": start_connected if isinstance(start_connected, bool) else None,
            }
        )
    return rows


def portgroup_moids(devices: Any) -> list[str]:
    """Moids of the distributed port groups the network adapters use."""
    moids: set[str] = set()
    for device in _list(devices):
        backing = device.get("backing")
        if (
            _is_nic(device)
            and isinstance(backing, dict)
            and backing.get("_typeName") == _DVS_BACKING
        ):
            port = backing.get("port")
            key = _text(port.get("portgroupKey")) if isinstance(port, dict) else None
            if key:
                moids.add(key)
    return sorted(moids)


def parse_filesystems(guest_disks: Any) -> list[dict[str, Any]]:
    """One row per ``GuestDiskInfo`` of ``guest.disk``."""
    rows: list[dict[str, Any]] = []
    for disk in _list(guest_disks):
        rows.append(
            {
                "mount_point": _text(disk.get("diskPath")),
                "filesystem_type": _text(disk.get("filesystemType")),
                "capacity_bytes": _int(disk.get("capacity")),
                "free_bytes": _int(disk.get("freeSpace")),
            }
        )
    return rows


DISKS_SCHEMA: Final[dict[str, Any]] = {
    "type": "array",
    "description": "The virtual disks.",
    "items": {
        "type": "object",
        "properties": {
            "key": {"type": ["integer", "null"], "description": "Device key of the disk."},
            "label": {"type": ["string", "null"], "description": "E.g. 'Hard disk 1'."},
            "capacity_bytes": {"type": ["integer", "null"], "description": "Disk size in bytes."},
            "file": {
                "type": ["string", "null"],
                "description": "The disk file, e.g. '[datastore] vm/vm.vmdk'.",
            },
            "datastore": {"type": ["string", "null"], "description": "Datastore moid."},
            "provisioning": {
                "type": ["string", "null"],
                "enum": ["thin", "thick_lazy_zeroed", "thick_eager_zeroed", None],
                "description": "Thin or thick; null for a disk without that setting (e.g. RDM).",
            },
            "controller": {
                "type": ["string", "null"],
                "description": "Label of the disk controller, e.g. 'SCSI controller 0'.",
            },
            "controller_type": {
                "type": ["string", "null"],
                "description": "VIM type of the controller, e.g. 'ParaVirtualSCSIController'.",
            },
            "unit_number": {
                "type": ["integer", "null"],
                "description": "Unit number of the disk on its controller.",
            },
        },
        "required": ["label", "capacity_bytes", "file", "provisioning", "controller"],
    },
}

NICS_SCHEMA: Final[dict[str, Any]] = {
    "type": "array",
    "description": "The network adapters.",
    "items": {
        "type": "object",
        "properties": {
            "key": {"type": ["integer", "null"], "description": "Device key of the adapter."},
            "label": {"type": ["string", "null"], "description": "E.g. 'Network adapter 1'."},
            "adapter_type": {
                "type": ["string", "null"],
                "description": "VIM type, e.g. 'VirtualVmxnet3'.",
            },
            "mac_address": {"type": ["string", "null"], "description": "MAC address."},
            "network": {
                "type": ["string", "null"],
                "description": "Moid of the port group or network (for NSX, the network id).",
            },
            "network_name": {
                "type": ["string", "null"],
                "description": "Name of the port group or network, when known.",
            },
            "connected": {
                "type": ["boolean", "null"],
                "description": "Whether the adapter is connected now.",
            },
            "start_connected": {
                "type": ["boolean", "null"],
                "description": "Whether it connects when the VM powers on.",
            },
        },
        "required": ["label", "mac_address", "network", "connected"],
    },
}

FILESYSTEMS_SCHEMA: Final[dict[str, Any]] = {
    "type": "array",
    "description": "Guest file systems from VMware Tools; empty when Tools does not run.",
    "items": {
        "type": "object",
        "properties": {
            "mount_point": {
                "type": ["string", "null"],
                "description": "Mount point or drive, e.g. '/' or 'C:\\\\'.",
            },
            "filesystem_type": {
                "type": ["string", "null"],
                "description": "E.g. 'ext4' or 'NTFS', when Tools reports it.",
            },
            "capacity_bytes": {"type": ["integer", "null"], "description": "Size in bytes."},
            "free_bytes": {"type": ["integer", "null"], "description": "Free space in bytes."},
        },
        "required": ["mount_point", "capacity_bytes", "free_bytes"],
    },
}
