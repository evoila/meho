# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``hyperv.migration.inventory`` -- the VMs of one Hyper-V host in the common shape.

Child reads, all on the Hyper-V connector (``hyperv-ssh-2022.x``):

1. ``hyperv.vms.list`` -- every VM: id, name, state, generation, processors,
   assigned memory. Its total is the source's own count.
2. ``hyperv.vms.guest.list`` -- BIOS GUID and the guest OS the integration
   services report, per VM id.
3. Per VM: ``hyperv.vms.config`` (startup memory, dynamic memory, Secure
   Boot), ``hyperv.disks.vm.list`` and ``hyperv.disks.vhd.get`` per disk,
   ``hyperv.checkpoints.list`` (counted as snapshots).

One record per VM, with ``source_id`` ``hyperv:<VM id>``. The VM id does not
change when the VM moves to another node of a cluster, so reading every
node of a cluster and merging the inventories finds each VM once. A failed
per-VM read leaves those fields unknown (``collection_failed``) and adds an
omission; the VM stays in the inventory.

Hyper-V lets two VMs share a name, and the per-VM reads find a VM by name.
For a name that is not unique the per-VM reads are skipped and the fields
are unknown (``not_supported``), so one VM never gets another VM's values.

A checkpoint always takes every virtual hard disk of the VM; Hyper-V has no
setting that leaves one disk out. So no disk is ``independent``. A
pass-through physical disk is a raw mapping; with one attached, Hyper-V
refuses to take a checkpoint at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from meho_backplane.connectors.migration.adapter_kit import (
    ChildReadError,
    InventoryBuilder,
    bytes_to_gb,
    bytes_to_mb,
    gather_limited,
    read_child,
    record_with_reasons,
    rows_of,
    store_inventory,
    target_name,
    usable_bios_uuid,
)
from meho_backplane.connectors.migration.contracts import (
    Disk,
    IdentityHints,
    MissingReason,
    OperatingSystem,
    ServerRecord,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.operations.composite import DispatchChild

__all__ = ["HYPERV_CONNECTOR_ID", "HYPERV_INVENTORY_OP_ID", "hyperv_migration_inventory"]

HYPERV_CONNECTOR_ID: Final[str] = "hyperv-ssh-2022.x"
HYPERV_INVENTORY_OP_ID: Final[str] = "hyperv.migration.inventory"

_POWER: Final[dict[str, str]] = {
    "running": "on",
    "off": "off",
    "saved": "suspended",
    "paused": "suspended",
}


def os_family(name: str | None) -> Literal["windows", "linux", "other"] | None:
    """``windows`` / ``linux`` / ``other`` from an OS name; ``None`` without a name."""
    if not name:
        return None
    lowered = name.lower()
    if "windows" in lowered:
        return "windows"
    linux_words = ("linux", "ubuntu", "debian", "red hat", "centos", "suse", "rocky", "alma")
    if any(word in lowered for word in linux_words):
        return "linux"
    return "other"


class _VmReads:
    """The per-VM child answers, or the reason each one is missing."""

    def __init__(self) -> None:
        self.config: dict[str, Any] | None = None
        self.disks: list[dict[str, Any]] | None = None
        self.vhds: dict[str, dict[str, Any] | MissingReason] = {}
        self.checkpoints: int | None = None
        self.failed: dict[str, MissingReason] = {}


async def _read_vm(dispatch_child: DispatchChild, vm_name: str) -> _VmReads:
    reads = _VmReads()

    async def child(op_id: str, params: dict[str, Any]) -> Any:
        return await read_child(
            dispatch_child, connector_id=HYPERV_CONNECTOR_ID, op_id=op_id, params=params
        )

    by_name = {"vm_name": vm_name}
    try:
        config = await child("hyperv.vms.config", by_name)
        reads.config = config if isinstance(config, dict) else None
    except ChildReadError as exc:
        reads.failed["config"] = exc.reason
    try:
        reads.disks = rows_of(await child("hyperv.disks.vm.list", by_name))
    except ChildReadError as exc:
        reads.failed["disks"] = exc.reason
    for drive in reads.disks or []:
        path = drive.get("Path")
        if not path:
            continue
        try:
            vhd = await child("hyperv.disks.vhd.get", {"path": path})
            reads.vhds[path] = vhd if isinstance(vhd, dict) else "collection_failed"
        except ChildReadError as exc:
            reads.vhds[path] = exc.reason
    try:
        reads.checkpoints = len(rows_of(await child("hyperv.checkpoints.list", by_name)))
    except ChildReadError as exc:
        reads.failed["snapshot_count"] = exc.reason
    return reads


def _disk(index: int, drive: dict[str, Any], vhd: Any) -> tuple[Disk, dict[str, MissingReason]]:
    path = drive.get("Path")
    reasons: dict[str, MissingReason] = {}
    prefix = f"disks.{index}"
    for field in ("read_ops_per_s", "write_ops_per_s", "read_mb_per_s", "write_mb_per_s"):
        reasons[f"{prefix}.{field}"] = "not_measured"
    shared = _bool(drive.get("SupportPersistentReservations"))
    if not path:
        # A pass-through physical disk: Hyper-V does not report its size here.
        reasons[prefix] = "not_reported_by_source"
        label = f"physical disk {drive.get('DiskNumber')}"
        return Disk(label=label, raw_mapping=True, shared=shared, independent=False), reasons
    label = str(path).replace("\\", "/").rsplit("/", 1)[-1] or str(path)
    if not isinstance(vhd, dict):
        reasons[prefix] = (
            cast("MissingReason", vhd) if isinstance(vhd, str) else "collection_failed"
        )
        return Disk(label=label, raw_mapping=False, shared=shared, independent=False), reasons
    vhd_type = str(vhd.get("VhdType") or "").lower()
    disk = Disk(
        label=label,
        size_gb=bytes_to_gb(vhd.get("Size")),
        used_gb=bytes_to_gb(vhd.get("FileSize")),
        thin=vhd_type in {"dynamic", "differencing"} if vhd_type else None,
        shared=shared,
        raw_mapping=False,
        independent=False,
    )
    return disk, reasons


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _secure_boot(generation: Any, config: dict[str, Any] | None) -> bool | None:
    if generation == 1:
        return False
    raw = config.get("SecureBoot") if config else None
    if isinstance(raw, str) and raw.lower() in {"on", "off"}:
        return raw.lower() == "on"
    return None


def build_vm_record(
    vm: dict[str, Any],
    guest: dict[str, Any] | None,
    reads: _VmReads | MissingReason,
    *,
    collected_at: Any,
) -> ServerRecord:
    """One Hyper-V VM as a server record. *reads* is the skip reason when no per-VM read ran."""
    reasons: dict[str, MissingReason] = {"utilization": "not_measured", "nics": "not_supported"}
    skipped = reads if isinstance(reads, str) else None
    vm_reads = reads if not isinstance(reads, str) else _VmReads()
    if skipped is not None:
        reasons.update({"disks": skipped, "snapshot_count": skipped})
        vm_reads.failed["config"] = skipped
    state = str(vm.get("State") or "").lower()
    power = _POWER.get(state)
    generation = vm.get("Generation")
    config = vm_reads.config
    memory_startup = bytes_to_mb(config.get("MemoryStartup")) if config else None
    if config is None:
        reasons["memory_configured_mb"] = vm_reads.failed.get("config", "collection_failed")
        if generation == 2:
            reasons["secure_boot"] = vm_reads.failed.get("config", "collection_failed")
    assigned = bytes_to_mb(vm.get("MemoryAssigned")) if power == "on" else None
    if power != "on":
        reasons["memory_assigned_mb"] = "powered_off"
    disks: list[Disk] | None = None
    if vm_reads.disks is not None:
        disks = []
        for index, drive in enumerate(vm_reads.disks):
            disk, disk_reasons = _disk(index, drive, vm_reads.vhds.get(drive.get("Path") or ""))
            disks.append(disk)
            reasons.update(disk_reasons)
    elif skipped is None:
        reasons["disks"] = vm_reads.failed.get("disks", "collection_failed")
    if vm_reads.checkpoints is None and skipped is None:
        reasons["snapshot_count"] = vm_reads.failed.get("snapshot_count", "collection_failed")
    os_name = guest.get("OSName") if guest else None
    if not os_name:
        reasons["os"] = "powered_off" if power != "on" else "not_reported_by_source"
    fields: dict[str, Any] = {
        "source_id": f"hyperv:{str(vm['Id']).lower()}",
        "collected_at": collected_at,
        "identity_hints": IdentityHints(
            bios_uuid=usable_bios_uuid(guest.get("BIOSGUID")) if guest else None
        ),
        "name": vm.get("Name") or None,
        "server_type": "virtual",
        "hypervisor": "Hyper-V",
        "cores": (config or {}).get("ProcessorCount") or vm.get("ProcessorCount") or None,
        "memory_configured_mb": memory_startup or None,
        "memory_assigned_mb": assigned,
        "disks": disks,
        "snapshot_count": vm_reads.checkpoints,
        "os": OperatingSystem(
            name=os_name,
            version=guest.get("OSVersion") if guest else None,
            family=os_family(os_name),
        )
        if os_name
        else None,
        "boot_type": {1: "bios", 2: "uefi"}.get(generation)
        if isinstance(generation, int)
        else None,
        "secure_boot": _secure_boot(generation, config),
        "power_state": power,
    }
    return record_with_reasons(ServerRecord, fields, reasons=reasons)


async def hyperv_migration_inventory(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Collect the VMs of one Hyper-V host and store them as one inventory."""
    del params
    builder = InventoryBuilder(connector_id=HYPERV_CONNECTOR_ID, target=target_name(target))
    vms = rows_of(
        await read_child(dispatch_child, connector_id=HYPERV_CONNECTOR_ID, op_id="hyperv.vms.list")
    )
    builder.source_totals["server"] = len(vms)
    guests: dict[str, dict[str, Any]] = {}
    try:
        guest_rows = await read_child(
            dispatch_child, connector_id=HYPERV_CONNECTOR_ID, op_id="hyperv.vms.guest.list"
        )
        guests = {str(row.get("Id")).lower(): row for row in rows_of(guest_rows)}
    except ChildReadError as exc:
        builder.omit("guest OS and BIOS UUID of every VM", exc.reason, detail=exc.op_id)
    names = [str(vm.get("Name") or "") for vm in vms]
    repeated = {name for name in names if names.count(name) > 1}
    usable = [vm for vm in vms if vm.get("Id")]

    async def per_vm(vm: dict[str, Any]) -> _VmReads | MissingReason:
        name = str(vm.get("Name") or "")
        if not name or name in repeated:
            return "not_supported"
        return await _read_vm(dispatch_child, name)

    all_reads = await gather_limited(usable, per_vm)
    collected_at = datetime.now(UTC)
    for vm, reads in zip(usable, all_reads, strict=True):
        name = str(vm.get("Name") or vm["Id"])
        if isinstance(reads, str):
            builder.omit(
                f"details of VM {name}",
                cast("MissingReason", reads),
                detail="VM name is not unique",
            )
        else:
            failures = [*reads.failed.values()]
            failures += [v for v in reads.vhds.values() if isinstance(v, str)]
            if failures:
                builder.omit(f"some details of VM {name}", failures[0])
        builder.add(
            build_vm_record(vm, guests.get(str(vm["Id"]).lower()), reads, collected_at=collected_at)
        )
    for vm in vms:
        if not vm.get("Id"):
            builder.omit(f"VM {vm.get('Name')}", "not_reported_by_source", item_kind="server")
    return await store_inventory(operator, builder, op_id=HYPERV_INVENTORY_OP_ID)
