# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``linux.migration.inventory`` and ``winsrv.migration.inventory`` -- one host, one record.

For a server MEHO reaches as an operating system: a physical server, or a VM
on a platform MEHO does not read. Each adapter returns one server record for
the host itself. Every child read is a read-only operation of the host's
own connector.

Linux (``linux-ssh-1.x``): ``linux.about`` (host name, distribution,
version), then ``linux.file.read`` on files under ``/proc``, ``/sys`` and
``/etc``:

* ``/sys/devices/system/cpu/online`` -- the online CPUs (``0-7``) give the cores.
* ``/proc/meminfo`` -- ``MemTotal`` gives the memory the kernel sees.
* ``/proc/partitions`` -- the whole disks and their sizes.
* ``/sys/firmware/efi`` -- exists only when the host booted with UEFI.
* ``/sys/class/dmi/id/product_uuid`` -- the BIOS UUID; only root may read
  it, so without root it is unknown (``no_permission``).
* ``/sys/class/dmi/id/sys_vendor`` and ``product_name`` -- tell a VM from
  a physical server when they name a known hypervisor.
* ``/etc/machine-id`` -- the stable id of the installed system.

Windows Server (``winsrv-ssh-2022.x``): ``winsrv.system.os-info``,
``winsrv.system.hardware`` and ``winsrv.storage.disk.list``.

Both adapters see the host from inside its operating system. Snapshots and
the hypervisor's disk settings (shared, raw mapping, independent, thin) are
not visible there, so they are ``not_supported``; for a VM, read it through
its hypervisor's adapter to get them. A placeholder BIOS UUID (all zero or
all ``f``) is dropped, so two hosts with the same placeholder stay separate.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from meho_backplane.connectors.migration.adapter_kit import (
    ChildReadError,
    InventoryBuilder,
    bytes_to_gb,
    bytes_to_mb,
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
from meho_backplane.connectors.migration.source_hyperv import os_family

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.operations.composite import DispatchChild

__all__ = [
    "LINUX_CONNECTOR_ID",
    "LINUX_INVENTORY_OP_ID",
    "WINSRV_CONNECTOR_ID",
    "WINSRV_INVENTORY_OP_ID",
    "build_linux_record",
    "build_windows_record",
    "linux_migration_inventory",
    "winsrv_migration_inventory",
]

LINUX_CONNECTOR_ID: Final[str] = "linux-ssh-1.x"
LINUX_INVENTORY_OP_ID: Final[str] = "linux.migration.inventory"
WINSRV_CONNECTOR_ID: Final[str] = "winsrv-ssh-2022.x"
WINSRV_INVENTORY_OP_ID: Final[str] = "winsrv.migration.inventory"

#: Files the Linux adapter reads, by short key.
LINUX_FILES: Final[dict[str, str]] = {
    "cpus": "/sys/devices/system/cpu/online",
    "meminfo": "/proc/meminfo",
    "partitions": "/proc/partitions",
    "efi": "/sys/firmware/efi",
    "product_uuid": "/sys/class/dmi/id/product_uuid",
    "sys_vendor": "/sys/class/dmi/id/sys_vendor",
    "product_name": "/sys/class/dmi/id/product_name",
    "machine_id": "/etc/machine-id",
}

#: Maker or model text that names a hypervisor, and the hypervisor's name.
_HYPERVISORS: Final[tuple[tuple[str, str], ...]] = (
    ("vmware", "VMware"),
    ("microsoft corporation virtual machine", "Hyper-V"),
    ("virtual machine", "Hyper-V"),
    ("qemu", "KVM"),
    ("kvm", "KVM"),
    ("proxmox", "Proxmox VE"),
    ("xen", "Xen"),
    ("virtualbox", "VirtualBox"),
    ("amazon ec2", "AWS"),
    ("google compute engine", "Google Cloud"),
)

#: Block devices that are not disks of their own.
_NOT_A_DISK: Final[re.Pattern[str]] = re.compile(r"^(loop|ram|sr|fd|dm-|md|zram|nbd)")

_HOST_REASONS: Final[dict[str, MissingReason]] = {
    "utilization": "not_measured",
    "nics": "not_supported",
    "snapshot_count": "not_supported",
    "memory_assigned_mb": "not_supported",
}


def hypervisor_of(*texts: Any) -> str | None:
    """The hypervisor a maker or model text names, or ``None``."""
    joined = " ".join(str(t) for t in texts if t).lower()
    return next((name for word, name in _HYPERVISORS if word in joined), None)


def count_cpus(online: str) -> int | None:
    """Count the CPUs in a list like ``0-3,6,8-9``."""
    total = 0
    for part in online.strip().split(","):
        if not part:
            continue
        low, _, high = part.partition("-")
        try:
            total += (int(high) - int(low) + 1) if high else 1
        except ValueError:
            return None
    return total or None


def meminfo_mb(text: str) -> int | None:
    """``MemTotal`` of ``/proc/meminfo`` in MB."""
    match = re.search(r"^MemTotal:\s+(\d+)\s*kB", text, re.MULTILINE)
    return int(match.group(1)) // 1024 if match else None


def whole_disks(text: str) -> list[tuple[str, float]]:
    """Whole disks of ``/proc/partitions`` with their size in GB; partitions left out."""
    devices: list[tuple[str, int]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[2].isdigit():
            devices.append((parts[3], int(parts[2])))
    names = {name for name, _ in devices}
    disks = []
    for name, blocks in devices:
        if _NOT_A_DISK.match(name):
            continue
        parent = re.sub(r"p?\d+$", "", name)
        if parent != name and parent in names:
            continue
        disks.append((name, round(blocks * 1024 / 1024**3, 2)))
    return disks


def _host_disks(
    disks: list[tuple[str, float | None]],
) -> tuple[list[Disk], dict[str, MissingReason]]:
    reasons: dict[str, MissingReason] = {}
    for index in range(len(disks)):
        prefix = f"disks.{index}"
        for field in ("read_ops_per_s", "write_ops_per_s", "read_mb_per_s", "write_mb_per_s"):
            reasons[f"{prefix}.{field}"] = "not_measured"
        for field in ("used_gb", "thin", "shared", "raw_mapping", "independent"):
            reasons[f"{prefix}.{field}"] = "not_supported"
    return [Disk(label=label, size_gb=size) for label, size in disks], reasons


def _server_type(hypervisor: str | None) -> Literal["virtual"] | None:
    return "virtual" if hypervisor else None


def build_linux_record(
    about: dict[str, Any] | MissingReason,
    files: dict[str, dict[str, Any] | MissingReason],
    *,
    target: str,
    collected_at: datetime,
) -> ServerRecord:
    """The Linux host as one server record. A ``str`` value is the reason that read is missing."""
    reasons: dict[str, MissingReason] = dict(_HOST_REASONS)

    def content(key: str, field: str | None = None) -> str | None:
        answer = files.get(key, "collection_failed")
        reason: MissingReason | None = None
        text = ""
        if isinstance(answer, str):
            reason = answer
        elif not answer.get("exists", True):
            reason = "not_reported_by_source"
        else:
            text = str(answer.get("content") or "").strip()
            # The file is there but empty: the account may not read it.
            reason = None if text else "no_permission"
        if reason is not None and field is not None:
            reasons[field] = reason
        return text or None

    info = about if isinstance(about, dict) else {}
    if isinstance(about, str):
        reasons["os"] = about
    cpus = content("cpus", "cores")
    memory = content("meminfo", "memory_configured_mb")
    partitions = content("partitions", "disks")
    uuid_text = content("product_uuid", "identity_hints.bios_uuid")
    uuid = usable_bios_uuid(uuid_text) or ""
    if uuid_text and not uuid:
        reasons["identity_hints.bios_uuid"] = "not_reported_by_source"
    machine_id = content("machine_id")
    hypervisor = hypervisor_of(content("sys_vendor"), content("product_name"))
    reasons.update(
        {"server_type": "not_reported_by_source", "hypervisor": "not_reported_by_source"}
    )
    efi = files.get("efi", "collection_failed")
    boot_type: str | None = None
    if isinstance(efi, str):
        reasons["boot_type"] = efi
    else:
        boot_type = "uefi" if efi.get("exists") else "bios"
    # BIOS firmware cannot have Secure Boot. On UEFI, MEHO does not read it.
    if boot_type != "bios":
        reasons["secure_boot"] = "not_supported"
    disks, disk_reasons = _host_disks(list(whole_disks(partitions))) if partitions else (None, {})
    reasons.update(disk_reasons)
    host_name = info.get("hostname") or None
    stable = machine_id or uuid or host_name or target
    os_name = info.get("os_pretty") or None
    fields: dict[str, Any] = {
        "source_id": f"linux:{stable}",
        "collected_at": collected_at,
        "identity_hints": IdentityHints(bios_uuid=uuid or None, host_name=host_name),
        "name": host_name or target,
        "server_type": _server_type(hypervisor),
        "hypervisor": hypervisor,
        "cores": count_cpus(cpus) if cpus else None,
        "memory_configured_mb": meminfo_mb(memory) if memory else None,
        "disks": disks,
        "os": OperatingSystem(name=os_name, version=info.get("version") or None, family="linux")
        if info
        else None,
        "boot_type": boot_type,
        "secure_boot": False if boot_type == "bios" else None,
        "power_state": "on",
    }
    return record_with_reasons(ServerRecord, fields, reasons=reasons)


async def linux_migration_inventory(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Read one Linux host and store it as an inventory of one server record."""
    del params
    name = target_name(target)
    builder = InventoryBuilder(connector_id=LINUX_CONNECTOR_ID, target=name)
    builder.source_totals["server"] = 1
    about: dict[str, Any] | MissingReason
    try:
        answer = await read_child(
            dispatch_child, connector_id=LINUX_CONNECTOR_ID, op_id="linux.about"
        )
        about = answer if isinstance(answer, dict) else "collection_failed"
    except ChildReadError as exc:
        about = exc.reason
        builder.omit("host identity and OS", exc.reason, detail=exc.op_id)
    files: dict[str, dict[str, Any] | MissingReason] = {}
    for key, path in LINUX_FILES.items():
        try:
            answer = await read_child(
                dispatch_child,
                connector_id=LINUX_CONNECTOR_ID,
                op_id="linux.file.read",
                params={"path": path},
            )
            files[key] = answer if isinstance(answer, dict) else "collection_failed"
        except ChildReadError as exc:
            files[key] = exc.reason
            builder.omit(f"file {path}", exc.reason)
    builder.add(build_linux_record(about, files, target=name, collected_at=datetime.now(UTC)))
    return await store_inventory(operator, builder, op_id=LINUX_INVENTORY_OP_ID)


def _secure_boot(value: Any, firmware: str) -> bool | None:
    """The Secure Boot state; BIOS firmware cannot have it, so there it is ``False``.

    The hardware read gives ``None`` when ``Confirm-SecureBootUEFI`` fails,
    on BIOS and on UEFI without the rights to ask. On UEFI the state is then
    unknown.
    """
    if isinstance(value, bool):
        return value
    return False if firmware == "bios" else None


def build_windows_record(
    os_info: dict[str, Any] | str,
    hardware: dict[str, Any] | str,
    disks: list[dict[str, Any]] | str,
    *,
    target: str,
    collected_at: datetime,
) -> ServerRecord:
    """The Windows host as one server record.

    A ``str`` value is the :data:`~.contracts.MissingReason` that read is missing.
    """
    reasons: dict[str, MissingReason] = dict(_HOST_REASONS)
    reasons["server_type"] = "not_reported_by_source"
    reasons["hypervisor"] = "not_reported_by_source"
    osd = os_info if isinstance(os_info, dict) else {}
    hw = hardware if isinstance(hardware, dict) else {}
    if isinstance(os_info, str):
        reasons["os"] = cast("MissingReason", os_info)
    if isinstance(hardware, str):
        for field in (
            "cores",
            "memory_configured_mb",
            "boot_type",
            "secure_boot",
            "identity_hints.bios_uuid",
            "server_type",
            "hypervisor",
        ):
            reasons[field] = cast("MissingReason", hardware)
    uuid = usable_bios_uuid(hw.get("ProductUuid")) or ""
    firmware = str(hw.get("FirmwareType") or "").lower()
    hypervisor = hypervisor_of(hw.get("Manufacturer"), hw.get("Model"))
    disk_list: list[Disk] | None = None
    if isinstance(disks, str):
        reasons["disks"] = cast("MissingReason", disks)
    else:
        rows = sorted(disks, key=lambda row: str(row.get("Number")))
        disk_list, disk_reasons = _host_disks(
            [
                (
                    str(row.get("FriendlyName") or f"Disk {row.get('Number')}"),
                    bytes_to_gb(row.get("Size")),
                )
                for row in rows
            ]
        )
        reasons.update(disk_reasons)
    host_name = (osd.get("Hostname") or hw.get("Hostname") or "").lower() or None
    os_name = osd.get("Caption") or None
    fields: dict[str, Any] = {
        "source_id": f"winsrv:{uuid or host_name or target}",
        "collected_at": collected_at,
        "identity_hints": IdentityHints(bios_uuid=uuid or None, host_name=host_name),
        "name": osd.get("Hostname") or hw.get("Hostname") or target,
        "server_type": _server_type(hypervisor),
        "hypervisor": hypervisor,
        "cores": hw.get("LogicalProcessors") or None,
        "memory_configured_mb": bytes_to_mb(hw.get("TotalPhysicalMemory")) or None,
        "disks": disk_list,
        "os": OperatingSystem(
            name=os_name,
            version=osd.get("Version") or None,
            family=os_family(os_name) or "windows",
            architecture=osd.get("OSArchitecture") or None,
        )
        if osd
        else None,
        "boot_type": {"uefi": "uefi", "bios": "bios"}.get(firmware),
        "secure_boot": _secure_boot(hw.get("SecureBoot"), firmware),
        "power_state": "on",
    }
    return record_with_reasons(ServerRecord, fields, reasons=reasons)


async def winsrv_migration_inventory(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Read one Windows Server host and store it as an inventory of one server record."""
    del params
    name = target_name(target)
    builder = InventoryBuilder(connector_id=WINSRV_CONNECTOR_ID, target=name)
    builder.source_totals["server"] = 1
    answers: dict[str, Any] = {}
    for op_id in ("winsrv.system.os-info", "winsrv.system.hardware", "winsrv.storage.disk.list"):
        try:
            answers[op_id] = await read_child(
                dispatch_child, connector_id=WINSRV_CONNECTOR_ID, op_id=op_id
            )
        except ChildReadError as exc:
            answers[op_id] = exc.reason
            builder.omit(f"answer of {op_id}", exc.reason)
    os_info = answers["winsrv.system.os-info"]
    hardware = answers["winsrv.system.hardware"]
    disk_answer = answers["winsrv.storage.disk.list"]
    record = build_windows_record(
        os_info if isinstance(os_info, dict | str) else "collection_failed",
        hardware if isinstance(hardware, dict | str) else "collection_failed",
        disk_answer if isinstance(disk_answer, str) else rows_of(disk_answer),
        target=name,
        collected_at=datetime.now(UTC),
    )
    builder.add(record)
    return await store_inventory(operator, builder, op_id=WINSRV_INVENTORY_OP_ID)
