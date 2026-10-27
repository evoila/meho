# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``proxmox.migration.inventory`` -- the QEMU VMs of one Proxmox VE target in the common shape.

Every read goes through ``proxmox.api.get`` on the Proxmox connector
(``proxmox-api-8.x``), the connector's allow-listed read:

1. ``nodes`` -- the nodes. A node that is not online is an omission, and
   the source's own VM count is then unknown, so the counts cannot match.
2. ``nodes/{node}/qemu`` per node -- the VMs. Their sum is the source's own
   count. Templates are counted and left out (they are not running servers).
3. Per VM: ``nodes/{node}/qemu/{vmid}/config`` (cores, sockets, memory,
   disks, firmware, network cards, SMBIOS UUID, OS type) and
   ``nodes/{node}/qemu/{vmid}/snapshot`` (the list has one extra entry,
   ``current``, which is not a snapshot).
4. For a running VM with the guest agent turned on:
   ``nodes/{node}/qemu/{vmid}/agent/get-osinfo`` for the OS name and
   version. Without it only the OS family from the VM's OS type is known.

A Proxmox snapshot always takes every disk of the VM; there is no setting
that leaves one disk out. So no disk is ``independent``.

``source_id`` is ``proxmox:<target>:<vmid>``; a VM id is unique in a
Proxmox cluster and does not change when the VM moves to another node.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from meho_backplane.connectors.migration.adapter_kit import (
    ChildReadError,
    InventoryBuilder,
    gather_limited,
    read_child,
    record_with_reasons,
    store_inventory,
    target_name,
    usable_bios_uuid,
)
from meho_backplane.connectors.migration.contracts import (
    Disk,
    IdentityHints,
    MissingReason,
    Nic,
    OperatingSystem,
    ServerRecord,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.operations.composite import DispatchChild

__all__ = ["PROXMOX_CONNECTOR_ID", "PROXMOX_INVENTORY_OP_ID", "proxmox_migration_inventory"]

PROXMOX_CONNECTOR_ID: Final[str] = "proxmox-api-8.x"
PROXMOX_INVENTORY_OP_ID: Final[str] = "proxmox.migration.inventory"

_DISK_KEY: Final[re.Pattern[str]] = re.compile(r"^(scsi|virtio|sata|ide)\d+$")
_NIC_KEY: Final[re.Pattern[str]] = re.compile(r"^net\d+$")
_SIZE: Final[re.Pattern[str]] = re.compile(r"^(\d+(?:\.\d+)?)([KMGT]?)$")
_SIZE_TO_GB: Final[dict[str, float]] = {
    "": 1 / 1024**3,
    "K": 1 / 1024**2,
    "M": 1 / 1024,
    "G": 1,
    "T": 1024,
}
_MAC: Final[re.Pattern[str]] = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")
_POWER: Final[dict[str, Literal["on", "off", "suspended"]]] = {
    "running": "on",
    "stopped": "off",
    "paused": "suspended",
    "suspended": "suspended",
}


def property_string(value: Any) -> tuple[str | None, dict[str, str]]:
    """Split a Proxmox property string ``main,key=value,...`` into its first part and keys.

    The first part is the one without ``=`` (a volume id), or ``None``.
    """
    main: str | None = None
    keys: dict[str, str] = {}
    for part in str(value or "").split(","):
        if "=" in part:
            key, _, val = part.partition("=")
            keys[key.strip()] = val.strip()
        elif part.strip() and main is None:
            main = part.strip()
    return main, keys


def size_gb(text: str | None) -> float | None:
    """A Proxmox size (``32G``, ``512M``, ``1T``) in GB; ``None`` when it does not parse."""
    match = _SIZE.match(text or "")
    if not match:
        return None
    return round(float(match.group(1)) * _SIZE_TO_GB[match.group(2)], 2)


def _int(value: Any) -> int | None:
    try:
        number = int(str(value))
    except ValueError:
        return None
    return number if number > 0 else None


def memory_mb(value: Any) -> int | None:
    """The VM's memory in MB: ``4096`` or ``current=4096,...`` (newer Proxmox)."""
    main, keys = property_string(value)
    return _int(keys.get("current", main))


def os_family_from_type(ostype: Any) -> Literal["windows", "linux", "other"] | None:
    """OS family from the Proxmox ``ostype`` (``l26``, ``win11``, ``other``)."""
    text = str(ostype or "")
    if not text:
        return None
    if text.startswith(("win", "w2k", "wxp", "wvista")):
        return "windows"
    if text.startswith("l2"):
        return "linux"
    return "other"


def _disks(config: dict[str, Any]) -> tuple[list[Disk], dict[str, MissingReason]]:
    disks: list[Disk] = []
    reasons: dict[str, MissingReason] = {}
    for key in sorted(k for k in config if _DISK_KEY.match(k)):
        volume, keys = property_string(config[key])
        if keys.get("media") == "cdrom":
            continue
        prefix = f"disks.{len(disks)}"
        for field in ("read_ops_per_s", "write_ops_per_s", "read_mb_per_s", "write_mb_per_s"):
            reasons[f"{prefix}.{field}"] = "not_measured"
        disks.append(
            Disk(
                label=key,
                size_gb=size_gb(keys.get("size")),
                raw_mapping=(volume or "").startswith("/dev/"),
                independent=False,
            )
        )
    return disks, reasons


def _nics(config: dict[str, Any]) -> list[Nic]:
    nics: list[Nic] = []
    for key in sorted(k for k in config if _NIC_KEY.match(k)):
        _, keys = property_string(config[key])
        mac = next((v for v in keys.values() if _MAC.match(v)), None)
        nics.append(Nic(mac_address=mac.lower() if mac else None, network=keys.get("bridge")))
    return nics


def _secure_boot(config: dict[str, Any]) -> bool | None:
    if config.get("bios", "seabios") != "ovmf":
        return False
    efidisk = config.get("efidisk0")
    if efidisk is None:
        return None
    _, keys = property_string(efidisk)
    return keys.get("pre-enrolled-keys") == "1"


def build_vm_record(
    *,
    source: str,
    vm: dict[str, Any],
    config: dict[str, Any] | MissingReason,
    snapshots: int | MissingReason,
    osinfo: dict[str, Any] | MissingReason,
    collected_at: datetime,
) -> ServerRecord:
    """One Proxmox VM as a server record. A ``str`` input is the reason that read is missing."""
    reasons: dict[str, MissingReason] = {
        "utilization": "not_measured",
        "memory_assigned_mb": "not_supported",
    }
    power = _POWER.get(str(vm.get("status") or ""))
    fields: dict[str, Any] = {
        "source_id": f"proxmox:{source}:{vm['vmid']}",
        "collected_at": collected_at,
        "name": vm.get("name") or None,
        "server_type": "virtual",
        "hypervisor": "Proxmox VE",
        "power_state": power,
        "snapshot_count": snapshots if isinstance(snapshots, int) else None,
    }
    if isinstance(snapshots, str):
        reasons["snapshot_count"] = snapshots
    if isinstance(config, str):
        for field in ("cores", "memory_configured_mb", "disks", "nics", "boot_type", "secure_boot"):
            reasons[field] = config
        reasons["identity_hints"] = config
        fields["identity_hints"] = IdentityHints()
        reasons["os"] = config
    else:
        cores = (_int(config.get("cores")) or 1) * (_int(config.get("sockets")) or 1)
        disks, disk_reasons = _disks(config)
        reasons.update(disk_reasons)
        _, smbios = property_string(config.get("smbios1"))
        nics = _nics(config)
        fields.update(
            {
                "cores": cores,
                "memory_configured_mb": memory_mb(config.get("memory", 512)),
                "disks": disks,
                "nics": nics,
                "boot_type": "uefi" if config.get("bios") == "ovmf" else "bios",
                "secure_boot": _secure_boot(config),
                "identity_hints": IdentityHints(
                    bios_uuid=usable_bios_uuid(smbios.get("uuid")),
                    mac_addresses=[n.mac_address for n in nics if n.mac_address],
                ),
            }
        )
        info = osinfo if isinstance(osinfo, dict) else {}
        if isinstance(osinfo, str):
            reasons["os.name"] = osinfo
            reasons["os.version"] = osinfo
            reasons["os.architecture"] = osinfo
        fields["os"] = OperatingSystem(
            name=info.get("pretty-name") or info.get("name") or None,
            version=info.get("version") or info.get("version-id") or None,
            family=os_family_from_type(config.get("ostype")),
            architecture=info.get("machine") or None,
        )
    return record_with_reasons(ServerRecord, fields, reasons=reasons)


async def _get(dispatch_child: DispatchChild, path: str) -> Any:
    answer = await read_child(
        dispatch_child,
        connector_id=PROXMOX_CONNECTOR_ID,
        op_id="proxmox.api.get",
        params={"path": path},
    )
    return answer.get("data") if isinstance(answer, dict) else None


def _agent_on(config: dict[str, Any]) -> bool:
    main, keys = property_string(config.get("agent"))
    return (keys.get("enabled") or main) == "1"


async def _read_vm(
    dispatch_child: DispatchChild, node: str, vm: dict[str, Any]
) -> tuple[Any, Any, Any]:
    base = f"nodes/{node}/qemu/{vm['vmid']}"
    config: dict[str, Any] | MissingReason
    snapshots: int | MissingReason
    osinfo: dict[str, Any] | MissingReason = "not_reported_by_source"
    try:
        data = await _get(dispatch_child, f"{base}/config")
        config = data if isinstance(data, dict) else "collection_failed"
    except ChildReadError as exc:
        config = exc.reason
    try:
        data = await _get(dispatch_child, f"{base}/snapshot")
        items = data if isinstance(data, list) else []
        snapshots = len([s for s in items if isinstance(s, dict) and s.get("name") != "current"])
    except ChildReadError as exc:
        snapshots = exc.reason
    if vm.get("status") != "running":
        osinfo = "powered_off"
    elif isinstance(config, dict) and _agent_on(config):
        try:
            data = await _get(dispatch_child, f"{base}/agent/get-osinfo")
            result = data.get("result") if isinstance(data, dict) else None
            osinfo = result if isinstance(result, dict) else "not_reported_by_source"
        except ChildReadError as exc:
            osinfo = exc.reason
    return config, snapshots, osinfo


async def proxmox_migration_inventory(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Collect the QEMU VMs of one Proxmox VE target and store them as one inventory."""
    del params
    source = target_name(target)
    builder = InventoryBuilder(connector_id=PROXMOX_CONNECTOR_ID, target=source)
    nodes = await _get(dispatch_child, "nodes")
    found: list[tuple[str, dict[str, Any]]] = []
    total = 0
    for node in nodes if isinstance(nodes, list) else []:
        name = str(node.get("node") or "") if isinstance(node, dict) else ""
        if not name or node.get("status") not in (None, "online"):
            builder.omit(
                f"VMs on node {name or '(no name)'}",
                "collection_failed",
                detail="node is not online",
            )
            builder.total_incomplete("server")
            continue
        try:
            vms = await _get(dispatch_child, f"nodes/{name}/qemu")
        except ChildReadError as exc:
            builder.omit(f"VMs on node {name}", exc.reason)
            builder.total_incomplete("server")
            continue
        for vm in vms if isinstance(vms, list) else []:
            if not isinstance(vm, dict) or vm.get("vmid") is None:
                continue
            total += 1
            if vm.get("template"):
                builder.omit(f"VM template {vm['vmid']}", "not_supported", item_kind="server")
                continue
            found.append((name, vm))
    builder.source_totals["server"] = total
    reads = await gather_limited(found, lambda item: _read_vm(dispatch_child, *item))
    collected_at = datetime.now(UTC)
    for (_node, vm), (config, snapshots, osinfo) in zip(found, reads, strict=True):
        failed = [cast("MissingReason", r) for r in (config, snapshots) if isinstance(r, str)]
        if failed:
            builder.omit(f"some details of VM {vm['vmid']}", failed[0])
        builder.add(
            build_vm_record(
                source=source,
                vm=vm,
                config=config,
                snapshots=snapshots,
                osinfo=osinfo,
                collected_at=collected_at,
            )
        )
    return await store_inventory(operator, builder, op_id=PROXMOX_INVENTORY_OP_ID)
