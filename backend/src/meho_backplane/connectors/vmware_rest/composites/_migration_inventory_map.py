# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Turn vCenter VM properties into migration inventory records.

Pure functions, no vCenter calls: :mod:`._migration_inventory` reads the
properties and hands them here. Everything a record cannot know is ``None``
with a reason in ``missing`` (the inventory rule: unknown is never zero).

Text from vCenter (VM names, guest host names, OS names) is untrusted data.
It is copied into string fields only and never used to build a request.

The BIOS UUID is stored in the form the guest OS reports, so a vCenter record
and a record read inside the guest name the same machine. From virtual
hardware version 13 on, the VM's firmware reports SMBIOS 2.7, and the guest
(``dmidecode``, Windows WMI) shows the first three groups of the UUID with
their bytes reversed against vCenter's ``config.uuid`` (vendor article
53609). :func:`guest_bios_uuid` applies that rule.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal

from meho_backplane.connectors.inventory.contracts import (
    Disk,
    IdentityHints,
    MissingReason,
    Nic,
    OperatingSystem,
    ServerRecord,
    Stat,
    Utilization,
)
from meho_backplane.connectors.vmware_rest.vim_body import fault_type_name, unwrap_vim_value

__all__ = [
    "DETAIL_PROPERTIES",
    "LIST_PROPERTIES",
    "VmProps",
    "as_dict",
    "extract_objects",
    "guest_bios_uuid",
    "nearest_rank_stat",
    "server_record",
]

#: Read for every VM in scope, to count and sort them. Small, so one paged read
#: covers a large vCenter.
LIST_PROPERTIES: Final[tuple[str, ...]] = ("name", "config.template")

#: Read per batch of VMs for the record itself.
DETAIL_PROPERTIES: Final[tuple[str, ...]] = (
    "name",
    "config.template",
    "config.uuid",
    "config.instanceUuid",
    "config.version",
    "config.guestFullName",
    "config.guestId",
    "config.firmware",
    "config.bootOptions.efiSecureBootEnabled",
    "config.hardware.numCPU",
    "config.hardware.memoryMB",
    "config.hardware.device",
    "guest.guestFullName",
    "guest.guestFamily",
    "guest.hostName",
    "guest.net",
    "runtime.powerState",
    "runtime.host",
    "snapshot",
)

_SECURE_BOOT_PROPERTY: Final = "config.bootOptions.efiSecureBootEnabled"
#: First virtual hardware version whose firmware reports SMBIOS 2.7, where the
#: guest reads the first three UUID groups in reversed byte order.
_UUID_SWAP_HARDWARE_VERSION: Final = 13
_HARDWARE_VERSION: Final = re.compile(r"^vmx-(\d+)$")
_UUID: Final = re.compile(r"^([0-9a-f]{8})-([0-9a-f]{4})-([0-9a-f]{4})-([0-9a-f]{4}-[0-9a-f]{12})$")
_GIB: Final = 1024**3
_KIB_PER_GIB: Final = 1024**2
_MAX_SNAPSHOT_DEPTH: Final = 64
_POWER_STATES: Final[dict[str, Literal["on", "off", "suspended"]]] = {
    "poweredOn": "on",
    "poweredOff": "off",
    "suspended": "suspended",
}
_GUEST_FAMILIES: Final[dict[str, Literal["windows", "linux", "other"]]] = {
    "windowsGuest": "windows",
    "linuxGuest": "linux",
}
#: Parts of a vSphere guest id that mean a Linux system.
_LINUX_GUEST_ID_PARTS: Final[tuple[str, ...]] = (
    "linux",
    "rhel",
    "centos",
    "ubuntu",
    "debian",
    "sles",
    "suse",
    "oracle",
    "rocky",
    "alma",
    "photon",
    "fedora",
    "coreos",
    "asianux",
    "mandr",
    "turbo",
)
_UTILIZATION_STATS: Final[tuple[str, ...]] = (
    "cpu_percent",
    "memory_percent",
    "network_in_mb_per_s",
    "network_out_mb_per_s",
)
_DISK_STAT_FIELDS: Final[tuple[str, ...]] = (
    "read_ops_per_s",
    "write_ops_per_s",
    "read_mb_per_s",
    "write_mb_per_s",
)


@dataclass
class VmProps:
    """The properties vCenter returned for one VM, and the ones it could not read."""

    moid: str
    props: dict[str, Any] = field(default_factory=dict)
    #: property path -> vim fault type name, or ``None`` when simply unset.
    faults: dict[str, str | None] = field(default_factory=dict)

    def get(self, path: str) -> Any:
        return self.props.get(path)

    def reason(self, paths: Iterable[str], default: MissingReason) -> MissingReason:
        """Why the value read from *paths* is unknown: a fault on a path wins."""
        for path in paths:
            if path not in self.faults:
                continue
            fault = self.faults[path]
            if fault == "NoPermission":
                return "no_permission"
            if fault is not None:
                return "collection_failed"
        return default


def extract_objects(retrieve_result: Any) -> dict[str, VmProps]:
    """``RetrievePropertiesEx`` answer -> ``{moid: VmProps}``, in answer order."""
    payload = unwrap_vim_value(retrieve_result)
    objects = payload.get("objects", []) if isinstance(payload, dict) else payload
    found: dict[str, VmProps] = {}
    if not isinstance(objects, list):
        return found
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        ref = obj.get("obj")
        moid = ref.get("value") if isinstance(ref, dict) else None
        if not isinstance(moid, str) or not moid:
            continue
        vm = found.setdefault(moid, VmProps(moid))
        for entry in obj.get("propSet") or []:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                vm.props[entry["name"]] = unwrap_vim_value(entry.get("val"))
        for entry in obj.get("missingSet") or []:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                vm.faults[entry["path"]] = fault_type_name(entry.get("fault"))
    return found


def as_dict(value: Any) -> dict[str, Any]:
    """*value* when it is a dict, else an empty dict."""
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _positive(value: int | None) -> int | None:
    return value if value is not None and value >= 1 else None


def _count_snapshots(nodes: Any, depth: int = 0) -> int:
    if not isinstance(nodes, list) or depth > _MAX_SNAPSHOT_DEPTH:
        return 0
    total = 0
    for node in nodes:
        if isinstance(node, dict):
            total += 1 + _count_snapshots(node.get("childSnapshotList"), depth + 1)
    return total


def _devices(vm: VmProps) -> list[dict[str, Any]] | None:
    devices = vm.get("config.hardware.device")
    if not isinstance(devices, list):
        return None
    return [device for device in devices if isinstance(device, dict)]


def _disk_size_gb(device: Mapping[str, Any]) -> float | None:
    size_bytes = _int(device.get("capacityInBytes"))
    if size_bytes is not None and size_bytes >= 0:
        return round(size_bytes / _GIB, 2)
    size_kb = _int(device.get("capacityInKB"))
    if size_kb is not None and size_kb >= 0:
        return round(size_kb / _KIB_PER_GIB, 2)
    return None


def _disk(device: Mapping[str, Any], index: int, missing: dict[str, MissingReason]) -> Disk:
    backing = as_dict(device.get("backing"))
    backing_type = str(backing.get("_typeName") or "")
    raw_mapping = backing_type.startswith("VirtualDiskRawDiskMapping")
    thin: bool | None = None
    if backing_type == "VirtualDiskFlatVer2BackingInfo":
        # vim leaves an unset boolean out of the answer: absent means "not thin".
        thin = backing.get("thinProvisioned") is True
    elif backing_type in ("VirtualDiskSeSparseBackingInfo", "VirtualDiskSparseVer2BackingInfo"):
        thin = True
    sharing = backing.get("sharing")
    shared = {"sharingMultiWriter": True, "sharingNone": False}.get(
        sharing if isinstance(sharing, str) else ""
    )
    disk_mode = backing.get("diskMode")
    independent = disk_mode.startswith("independent") if isinstance(disk_mode, str) else None
    info = as_dict(device.get("deviceInfo"))
    label = _str(info.get("label")) or f"disk {device.get('key', index)}"
    disk = Disk(
        label=label,
        size_gb=_disk_size_gb(device),
        thin=thin,
        shared=shared,
        raw_mapping=raw_mapping if backing_type else None,
        independent=independent,
    )
    prefix = f"disks.{index}"
    for name, value in (
        ("size_gb", disk.size_gb),
        ("thin", disk.thin),
        ("shared", disk.shared),
        ("raw_mapping", disk.raw_mapping),
        ("independent", disk.independent),
    ):
        if value is None:
            missing[f"{prefix}.{name}"] = "not_reported_by_source"
    missing[f"{prefix}.used_gb"] = "not_supported"
    for name in _DISK_STAT_FIELDS:
        missing[f"{prefix}.{name}"] = "not_measured"
    return disk


def _guest_ips_by_mac(vm: VmProps) -> dict[str, tuple[list[str], str | None]]:
    by_mac: dict[str, tuple[list[str], str | None]] = {}
    entries = vm.get("guest.net")
    if not isinstance(entries, list):
        return by_mac
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        mac = _str(entry.get("macAddress"))
        if mac is None:
            continue
        ips = entry.get("ipAddress")
        addresses = [ip for ip in ips if isinstance(ip, str)] if isinstance(ips, list) else []
        by_mac[mac.lower()] = (addresses, _str(entry.get("network")))
    return by_mac


def _nic(
    device: Mapping[str, Any],
    index: int,
    guest: Mapping[str, tuple[list[str], str | None]],
    missing: dict[str, MissingReason],
) -> Nic:
    mac = _str(device.get("macAddress"))
    addresses, guest_network = guest.get(mac.lower(), ([], None)) if mac else ([], None)
    backing = as_dict(device.get("backing"))
    network = _str(backing.get("deviceName")) or guest_network
    if mac is None:
        missing[f"nics.{index}.mac_address"] = "not_reported_by_source"
    if network is None:
        missing[f"nics.{index}.network"] = "not_reported_by_source"
    return Nic(mac_address=mac, ip_addresses=addresses, network=network)


def _os_family(vm: VmProps) -> Literal["windows", "linux", "other"] | None:
    family = vm.get("guest.guestFamily")
    if isinstance(family, str) and family:
        return _GUEST_FAMILIES.get(family, "other")
    guest_id = _str(vm.get("config.guestId"))
    if guest_id is None:
        return None
    lowered = guest_id.lower()
    if lowered.startswith("win"):
        return "windows"
    if any(part in lowered for part in _LINUX_GUEST_ID_PARTS):
        return "linux"
    return "other"


def _architecture(vm: VmProps) -> str | None:
    guest_id = (_str(vm.get("config.guestId")) or "").lower()
    if "arm64" in guest_id:
        return "arm64"
    if "64guest" in guest_id:
        return "x64"
    return None


def _operating_system(vm: VmProps, missing: dict[str, MissingReason]) -> OperatingSystem:
    name = _str(vm.get("guest.guestFullName")) or _str(vm.get("config.guestFullName"))
    os = OperatingSystem(
        name=name,
        version=None,
        family=_os_family(vm),
        architecture=_architecture(vm),
    )
    reason = vm.reason(("guest.guestFullName", "config.guestFullName"), "not_reported_by_source")
    if os.name is None:
        missing["os.name"] = reason
    missing["os.version"] = "not_reported_by_source"
    if os.family is None:
        missing["os.family"] = vm.reason(("config.guestId",), "not_reported_by_source")
    if os.architecture is None:
        missing["os.architecture"] = "not_reported_by_source"
    return os


def _boot(vm: VmProps) -> tuple[Literal["bios", "uefi"] | None, bool | None]:
    firmware = vm.get("config.firmware")
    boot_type: Literal["bios", "uefi"] | None = None
    if firmware == "efi":
        boot_type = "uefi"
    elif firmware == "bios":
        boot_type = "bios"
    secure = vm.get(_SECURE_BOOT_PROPERTY)
    if isinstance(secure, bool):
        return boot_type, secure
    if vm.faults.get(_SECURE_BOOT_PROPERTY):
        # vCenter refused or failed the read: the setting is unknown, not off.
        return boot_type, None
    # BIOS has no secure boot; on EFI vim leaves an unset (false) flag out.
    return boot_type, False if boot_type is not None else None


def _reverse_bytes(group: str) -> str:
    return "".join(group[i : i + 2] for i in range(len(group) - 2, -1, -2))


def guest_bios_uuid(config_uuid: str | None, hardware_version: str | None) -> str | None:
    """vCenter's ``config.uuid`` in the form the guest OS reports it.

    From hardware version 13 (``vmx-13``) on, the first three groups come
    back with their bytes reversed; below it, and when the version or the
    UUID cannot be read as such, the value stays as vCenter gives it.
    """
    uuid = _str(config_uuid)
    if uuid is None:
        return None
    uuid = uuid.lower()
    version = _HARDWARE_VERSION.match(hardware_version or "")
    parts = _UUID.match(uuid)
    if version is None or parts is None or int(version.group(1)) < _UUID_SWAP_HARDWARE_VERSION:
        return uuid
    return "-".join((*(_reverse_bytes(parts.group(i)) for i in (1, 2, 3)), parts.group(4)))


def _hardware(vm: VmProps, missing: dict[str, MissingReason]) -> dict[str, Any]:
    """Disks, NICs and MAC addresses from ``config.hardware.device``."""
    devices = _devices(vm)
    if devices is None:
        reason = vm.reason(("config.hardware.device",), "not_reported_by_source")
        missing["disks"] = reason
        missing["nics"] = reason
        return {"disks": None, "nics": None, "macs": []}
    guest = _guest_ips_by_mac(vm)
    disks = [
        _disk(device, index, missing)
        for index, device in enumerate(d for d in devices if d.get("_typeName") == "VirtualDisk")
    ]
    nic_devices = [d for d in devices if "macAddress" in d and "addressType" in d]
    nics = [_nic(device, index, guest, missing) for index, device in enumerate(nic_devices)]
    macs = [nic.mac_address for nic in nics if nic.mac_address]
    return {"disks": disks, "nics": nics, "macs": macs}


def _utilization_missing(utilization: Utilization) -> dict[str, MissingReason]:
    """The reason for each unknown part of a measured utilization block."""
    missing: dict[str, MissingReason] = {
        f"utilization.{name}": "not_measured"
        for name in _UTILIZATION_STATS
        if getattr(utilization, name) is None
    }
    if utilization.sample_interval_minutes is None:
        missing["utilization.sample_interval_minutes"] = "not_reported_by_source"
    return missing


def server_record(
    vm: VmProps,
    *,
    source_id: str,
    collected_at: datetime,
    esxi_host: str | None,
    cluster: str | None,
    utilization: Utilization | None,
    utilization_reason: MissingReason | None,
) -> ServerRecord:
    """Build one :class:`ServerRecord` from one VM's properties."""
    missing: dict[str, MissingReason] = {}
    power_state = _POWER_STATES.get(str(vm.get("runtime.powerState")))
    guest_default: MissingReason = (
        "powered_off" if power_state == "off" else "not_reported_by_source"
    )
    hardware = _hardware(vm, missing)
    boot_type, secure_boot = _boot(vm)
    cores = _positive(_int(vm.get("config.hardware.numCPU")))
    memory = _positive(_int(vm.get("config.hardware.memoryMB")))
    snapshot_root = vm.get("snapshot")
    snapshots: int | None = (
        _count_snapshots(snapshot_root.get("rootSnapshotList"))
        if isinstance(snapshot_root, dict)
        else (None if vm.faults.get("snapshot") else 0)
    )
    host_name = _str(vm.get("guest.hostName"))
    hardware_version = _str(vm.get("config.version"))
    attributes = {"vm_id": vm.moid}
    for key, value in (
        ("instance_uuid", _str(vm.get("config.instanceUuid"))),
        ("hardware_version", hardware_version),
        ("esxi_host", esxi_host),
        ("cluster", cluster),
    ):
        if value is not None:
            attributes[key] = value
    fields: dict[str, tuple[Any, MissingReason]] = {
        "name": (_str(vm.get("name")), vm.reason(("name",), "not_reported_by_source")),
        "cores": (cores, vm.reason(("config.hardware.numCPU",), "not_reported_by_source")),
        "memory_configured_mb": (
            memory,
            vm.reason(("config.hardware.memoryMB",), "not_reported_by_source"),
        ),
        # vSphere has no dynamic memory: a running VM has its configured memory.
        "memory_assigned_mb": (
            memory if power_state == "on" else None,
            guest_default if memory is not None else "not_reported_by_source",
        ),
        "snapshot_count": (snapshots, vm.reason(("snapshot",), "collection_failed")),
        "boot_type": (boot_type, vm.reason(("config.firmware",), "not_reported_by_source")),
        "secure_boot": (
            secure_boot,
            vm.reason((_SECURE_BOOT_PROPERTY,), "not_reported_by_source"),
        ),
        "power_state": (power_state, vm.reason(("runtime.powerState",), "not_reported_by_source")),
        "utilization": (utilization, utilization_reason or "not_measured"),
    }
    for name, (value, reason) in fields.items():
        if value is None:
            missing[name] = reason
    if utilization is not None:
        missing.update(_utilization_missing(utilization))
    hints = IdentityHints(
        bios_uuid=guest_bios_uuid(vm.get("config.uuid"), hardware_version),
        mac_addresses=hardware["macs"],
        host_name=host_name,
    )
    if hints.bios_uuid is None:
        missing["identity_hints.bios_uuid"] = vm.reason(("config.uuid",), "not_reported_by_source")
    if hints.host_name is None:
        missing["identity_hints.host_name"] = vm.reason(("guest.hostName",), guest_default)
    return ServerRecord(
        source_id=source_id,
        collected_at=collected_at,
        identity_hints=hints,
        name=fields["name"][0],
        server_type="virtual",
        hypervisor="VMware",
        cores=cores,
        memory_configured_mb=memory,
        memory_assigned_mb=fields["memory_assigned_mb"][0],
        disks=hardware["disks"],
        snapshot_count=snapshots,
        os=_operating_system(vm, missing),
        boot_type=boot_type,
        secure_boot=secure_boot,
        power_state=power_state,
        nics=hardware["nics"],
        utilization=utilization,
        source_attributes=attributes,
        missing=missing,
    )


def nearest_rank_stat(values: Iterable[float]) -> Stat | None:
    """Average, 95th percentile (nearest rank) and maximum; ``None`` without values."""
    ordered = sorted(value for value in values if value >= 0)
    if not ordered:
        return None
    rank = max(1, -(-95 * len(ordered) // 100))
    return Stat(
        average=round(sum(ordered) / len(ordered), 2),
        p95=round(ordered[rank - 1], 2),
        maximum=round(ordered[-1], 2),
    )
