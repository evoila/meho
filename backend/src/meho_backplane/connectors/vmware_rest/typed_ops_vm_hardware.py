# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.vm.hardware.show`` -- one VM's CPU, memory, CD drives and disk controllers.

A typed read (``source_kind="typed"``). It calls the vSphere Automation REST
API on the connector session, so it needs no catalog ingest. It works on
vCenter 8 and 9; every path below exists on both.

Calls, all ``GET`` under ``/vcenter/vm/{vm}``:

* ``hardware/cpu`` and ``hardware/memory`` -- must work. A 404 means the VM
  does not exist, and the read returns ``not_found``.
* ``hardware/cdrom`` (the list) and ``hardware/cdrom/{cdrom}`` (each drive).
* ``hardware/adapter/scsi``, ``.../sata``, ``.../nvme`` (the lists) and
  ``.../{adapter}`` (each controller, for its bus).
* ``tools/installer`` -- whether the VMware Tools installer is mounted.

The CD drive, controller and installer reads are best effort. When one
fails, its part of the answer is ``null`` and ``read_notes`` says why.

The CD drive and installer readers are shared with the two changes
``vmware.composite.vm.cdrom.add`` and ``vmware.composite.vm.tools.installer.unmount``
(``composites/_vm_cdrom_tools.py``).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value
from meho_backplane.connectors.vmware_rest.typed_ops_vm_info import _resolve_vm_moid_by_name

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "HARDWARE_REST_CALLS",
    "VMWARE_VM_HARDWARE_SHOW_OP",
    "VM_HARDWARE_GROUP_KEY",
    "VM_HARDWARE_WHEN_TO_USE",
    "VM_MOID_PATTERN",
    "VM_SELECTOR_PROPERTIES",
    "check_vm_moid",
    "read_cdrom_rows",
    "read_tools_installer_mounted",
    "resolve_vm",
    "vm_hardware_show_impl",
]

_log = structlog.get_logger(__name__)

VM_HARDWARE_GROUP_KEY: Final = "vmware-vm-hardware"

#: A vCenter VM id such as ``vm-42``. It goes into the URL path, so anything
#: else is refused. ``(?!\n)`` stops ``$`` from accepting a trailing newline.
VM_MOID_PATTERN: Final = r"^vm-[0-9]+$(?!\n)"
_VM_MOID_RE: Final = re.compile(r"vm-[0-9]+")
#: A device or controller id vCenter returns (``3000``, ``16000``).
_DEVICE_ID_RE: Final = re.compile(r"[A-Za-z0-9-]+")

#: The controller kinds, in answer order, with the REST list path segment.
_CONTROLLER_KINDS: Final[tuple[str, ...]] = ("scsi", "sata", "nvme")

#: Every REST call this read makes, as ``METHOD:/path`` keys of the pinned
#: ``vcenter.yaml``. The reconcile test checks them against the vCenter
#: 8.0 and 9.0 API files.
HARDWARE_REST_CALLS: Final[tuple[str, ...]] = (
    "GET:/vcenter/vm",
    "GET:/vcenter/vm/{vm}/hardware/cpu",
    "GET:/vcenter/vm/{vm}/hardware/memory",
    "GET:/vcenter/vm/{vm}/hardware/cdrom",
    "GET:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}",
    "GET:/vcenter/vm/{vm}/hardware/adapter/scsi",
    "GET:/vcenter/vm/{vm}/hardware/adapter/scsi/{adapter}",
    "GET:/vcenter/vm/{vm}/hardware/adapter/sata",
    "GET:/vcenter/vm/{vm}/hardware/adapter/sata/{adapter}",
    "GET:/vcenter/vm/{vm}/hardware/adapter/nvme",
    "GET:/vcenter/vm/{vm}/hardware/adapter/nvme/{adapter}",
    "GET:/vcenter/vm/{vm}/tools/installer",
)


def check_vm_moid(vm: str) -> str:
    """Return *vm* when it is a VM id such as ``vm-42``; raise ``ValueError`` otherwise."""
    if not _VM_MOID_RE.fullmatch(vm):
        raise ValueError(f"vm {vm!r} is not a vCenter VM id such as 'vm-42'")
    return vm


def _vm_path(vm: str, suffix: str) -> str:
    return f"/vcenter/vm/{quote(check_vm_moid(vm), safe='')}/{suffix}"


def _device_segment(device_id: Any) -> str | None:
    if isinstance(device_id, str) and _DEVICE_ID_RE.fullmatch(device_id):
        return quote(device_id, safe="")
    return None


async def resolve_vm(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> str:
    """Return the VM id from ``vm``, or look up ``name`` (unknown -> ``not_found``)."""
    vm = params.get("vm")
    if not isinstance(vm, str):
        vm = await _resolve_vm_moid_by_name(connector, operator, target, params["name"])
    return check_vm_moid(vm)


async def _get(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    path: str,
) -> Any:
    mounted = await connector.mount_op_path(target, path, operator)
    return _unwrap_value(await connector._get_json(target, mounted, operator=operator))


def _note(what: str, exc: Exception) -> str:
    return f"{what} skipped: {type(exc).__name__}: {exc}"


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _ids(listing: Any, key: str) -> list[str]:
    if not isinstance(listing, list):
        return []
    return [e[key] for e in listing if isinstance(e, dict) and isinstance(e.get(key), str)]


def _cpu_view(raw: Any) -> dict[str, Any]:
    cpu = raw if isinstance(raw, dict) else {}
    return {
        "count": _int(cpu.get("count")),
        "cores_per_socket": _int(cpu.get("cores_per_socket")),
        "hot_add_enabled": _bool(cpu.get("hot_add_enabled")),
        "hot_remove_enabled": _bool(cpu.get("hot_remove_enabled")),
    }


def _memory_view(raw: Any) -> dict[str, Any]:
    mem = raw if isinstance(raw, dict) else {}
    return {
        "size_mib": _int(mem.get("size_MiB")),
        "hot_add_enabled": _bool(mem.get("hot_add_enabled")),
        "hot_add_increment_size_mib": _int(mem.get("hot_add_increment_size_MiB")),
        "hot_add_limit_mib": _int(mem.get("hot_add_limit_MiB")),
    }


def _address(raw: Any, keys: tuple[str, ...]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return {key: raw.get(key) for key in keys}


def cdrom_view(cdrom_id: str, raw: Any) -> dict[str, Any]:
    """Flatten one ``Cdrom.Info`` into the row this read and the cdrom changes return."""
    info = raw if isinstance(raw, dict) else {}
    raw_backing = info.get("backing")
    backing: dict[str, Any] = raw_backing if isinstance(raw_backing, dict) else {}
    state = _str(info.get("state"))
    return {
        "cdrom": cdrom_id,
        "label": _str(info.get("label")),
        "type": _str(info.get("type")),
        "ide": _address(info.get("ide"), ("primary", "master")),
        "sata": _address(info.get("sata"), ("bus", "unit")),
        "backing_type": _str(backing.get("type")),
        "iso_file": _str(backing.get("iso_file")),
        "host_device": _str(backing.get("host_device")),
        "device_access_type": _str(backing.get("device_access_type")),
        "state": state,
        "connected": None if state is None else state == "CONNECTED",
        "start_connected": _bool(info.get("start_connected")),
        "allow_guest_control": _bool(info.get("allow_guest_control")),
    }


async def read_cdrom_rows(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    vm: str,
) -> list[dict[str, Any]]:
    """Read every CD drive of *vm*. The list read raises on failure; a drive read does not."""
    listing = await _get(connector, operator, target, _vm_path(vm, "hardware/cdrom"))
    rows: list[dict[str, Any]] = []
    for cdrom_id in _ids(listing, "cdrom"):
        segment = _device_segment(cdrom_id)
        info: Any = None
        if segment is not None:
            try:
                info = await _get(
                    connector, operator, target, _vm_path(vm, f"hardware/cdrom/{segment}")
                )
            except httpx.HTTPError:
                info = None
        rows.append(cdrom_view(cdrom_id, info))
    return rows


async def read_tools_installer_mounted(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    vm: str,
) -> bool | None:
    """Return ``Installer.Info.is_connected`` of *vm*; raises on a failed read."""
    info = await _get(connector, operator, target, _vm_path(vm, "tools/installer"))
    return _bool(info.get("is_connected")) if isinstance(info, dict) else None


def _controller_view(kind: str, adapter: str, raw: Any) -> dict[str, Any]:
    info = raw if isinstance(raw, dict) else {}
    raw_scsi = info.get("scsi")
    scsi: dict[str, Any] = raw_scsi if isinstance(raw_scsi, dict) else {}
    return {
        "kind": kind,
        "adapter": adapter,
        "label": _str(info.get("label")),
        "type": _str(info.get("type")),
        "bus": _int(scsi.get("bus")) if kind == "scsi" else _int(info.get("bus")),
        "unit": _int(scsi.get("unit")) if kind == "scsi" else None,
        "sharing": _str(info.get("sharing")),
        "pci_slot_number": _int(info.get("pci_slot_number")),
    }


async def _read_controllers(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    vm: str,
    notes: list[str],
) -> list[dict[str, Any]] | None:
    rows: list[dict[str, Any]] = []
    failed = 0
    for kind in _CONTROLLER_KINDS:
        try:
            listing = await _get(
                connector, operator, target, _vm_path(vm, f"hardware/adapter/{kind}")
            )
        except httpx.HTTPError as exc:
            failed += 1
            notes.append(_note(f"{kind} controller list", exc))
            continue
        for adapter in _ids(listing, "adapter"):
            segment = _device_segment(adapter)
            info: Any = None
            if segment is not None:
                try:
                    info = await _get(
                        connector,
                        operator,
                        target,
                        _vm_path(vm, f"hardware/adapter/{kind}/{segment}"),
                    )
                except httpx.HTTPError as exc:
                    notes.append(_note(f"{kind} controller {adapter}", exc))
            rows.append(_controller_view(kind, adapter, info))
    return None if failed == len(_CONTROLLER_KINDS) else rows


async def _read_required(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    vm: str,
    suffix: str,
) -> Any:
    try:
        return await _get(connector, operator, target, _vm_path(vm, suffix))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise ConnectorResourceNotFoundError(
                [vm], f"vmware.vm.hardware.show: no VM {vm!r}"
            ) from exc
        raise


async def vm_hardware_show_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.vm.hardware.show``. See the module docstring."""
    vm = await resolve_vm(connector, operator, target, params)
    cpu = await _read_required(connector, operator, target, vm, "hardware/cpu")
    memory = await _read_required(connector, operator, target, vm, "hardware/memory")
    notes: list[str] = []
    cdroms: list[dict[str, Any]] | None
    try:
        cdroms = await read_cdrom_rows(connector, operator, target, vm)
    except httpx.HTTPError as exc:
        cdroms = None
        notes.append(_note("CD drive list", exc))
    controllers = await _read_controllers(connector, operator, target, vm, notes)
    installer: bool | None
    try:
        installer = await read_tools_installer_mounted(connector, operator, target, vm)
    except httpx.HTTPError as exc:
        installer = None
        notes.append(_note("VMware Tools installer read", exc))
    _log.info(
        "vmware_vm_hardware_read",
        target=target.name,
        vm=vm,
        cdroms=None if cdroms is None else len(cdroms),
        notes=len(notes),
    )
    return {
        "vm": vm,
        "name": params.get("name"),
        "cpu": _cpu_view(cpu),
        "memory": _memory_view(memory),
        "cdroms": cdroms,
        "disk_controllers": controllers,
        "tools_installer_mounted": installer,
        "read_notes": notes,
    }


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

#: The ``vm`` / ``name`` selector of the read. The two changes take ``vm`` only.
VM_SELECTOR_PROPERTIES: Final[dict[str, Any]] = {
    "vm": {
        "type": "string",
        "pattern": VM_MOID_PATTERN,
        "description": "VM id, such as 'vm-42'. Give exactly one of 'vm' or 'name'.",
    },
    "name": {
        "type": "string",
        "minLength": 1,
        "description": (
            "VM name. MEHO looks up the VM id. An unknown name gives not_found; a name "
            "that fits two VMs is an error. Give exactly one of 'vm' or 'name'."
        ),
    },
}

_NULLABLE_BOOL: Final = {"type": ["boolean", "null"]}
_NULLABLE_INT: Final = {"type": ["integer", "null"]}
_NULLABLE_STR: Final = {"type": ["string", "null"]}


def _field(base: dict[str, Any], description: str) -> dict[str, Any]:
    return {**base, "description": description}


#: One CD drive row. Shared with the cdrom change's answer.
CDROM_ROW_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "cdrom": {"type": "string", "description": "CD drive id, such as '16000'."},
        "label": _field(_NULLABLE_STR, "Device label, such as 'CD/DVD drive 1'."),
        "type": _field(_NULLABLE_STR, "Controller kind the drive sits on: IDE or SATA."),
        "ide": {
            "type": ["object", "null"],
            "description": "IDE address ({primary, master}) when type is IDE.",
        },
        "sata": {
            "type": ["object", "null"],
            "description": "SATA address ({bus, unit}) when type is SATA.",
        },
        "backing_type": _field(_NULLABLE_STR, "ISO_FILE, HOST_DEVICE or CLIENT_DEVICE."),
        "iso_file": _field(_NULLABLE_STR, "Datastore path of the ISO file, for ISO_FILE."),
        "host_device": _field(_NULLABLE_STR, "Host device name, for HOST_DEVICE."),
        "device_access_type": _field(
            _NULLABLE_STR, "EMULATION, PASSTHRU or PASSTHRU_EXCLUSIVE (device backings)."
        ),
        "state": _field(
            _NULLABLE_STR,
            "CONNECTED, NOT_CONNECTED, RECOVERABLE_ERROR, UNRECOVERABLE_ERROR or UNKNOWN.",
        ),
        "connected": _field(_NULLABLE_BOOL, "True when state is CONNECTED."),
        "start_connected": _field(_NULLABLE_BOOL, "Connects when the VM powers on."),
        "allow_guest_control": _field(
            _NULLABLE_BOOL, "The guest can connect and disconnect the drive."
        ),
    },
    "required": ["cdrom"],
}

_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": VM_SELECTOR_PROPERTIES,
    "oneOf": [{"required": ["vm"]}, {"required": ["name"]}],
    "additionalProperties": False,
    "examples": [{"name": "<vm-name>"}, {"vm": "vm-42"}],
}

_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": {"type": "string", "description": "VM id that was read."},
        "name": _field(_NULLABLE_STR, "VM name when the call named the VM, else null."),
        "cpu": {
            "type": "object",
            "description": "CPU settings.",
            "properties": {
                "count": _field(_NULLABLE_INT, "Number of virtual CPU cores."),
                "cores_per_socket": _field(_NULLABLE_INT, "Cores per socket."),
                "hot_add_enabled": _field(_NULLABLE_BOOL, "CPUs can be added while on."),
                "hot_remove_enabled": _field(_NULLABLE_BOOL, "CPUs can be removed while on."),
            },
        },
        "memory": {
            "type": "object",
            "description": "Memory settings.",
            "properties": {
                "size_mib": _field(_NULLABLE_INT, "Memory size in MiB."),
                "hot_add_enabled": _field(_NULLABLE_BOOL, "Memory can be added while on."),
                "hot_add_increment_size_mib": _field(
                    _NULLABLE_INT, "Step for adding memory while on, in MiB."
                ),
                "hot_add_limit_mib": _field(
                    _NULLABLE_INT, "Most memory the VM can have after a hot add, in MiB."
                ),
            },
        },
        "cdroms": {
            "type": ["array", "null"],
            "description": "Every CD drive; null when the list read failed.",
            "items": CDROM_ROW_SCHEMA,
        },
        "disk_controllers": {
            "type": ["array", "null"],
            "description": "Every SCSI, SATA and NVMe controller; null when all reads failed.",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "description": "scsi, sata or nvme."},
                    "adapter": {"type": "string", "description": "Controller id."},
                    "label": _field(_NULLABLE_STR, "Device label."),
                    "type": _field(
                        _NULLABLE_STR, "Model: PVSCSI, LSILOGIC, LSILOGICSAS, BUSLOGIC, AHCI."
                    ),
                    "bus": _field(_NULLABLE_INT, "Bus number."),
                    "unit": _field(_NULLABLE_INT, "SCSI unit number of the controller."),
                    "sharing": _field(_NULLABLE_STR, "SCSI bus sharing: NONE, VIRTUAL, PHYSICAL."),
                    "pci_slot_number": _field(_NULLABLE_INT, "PCI slot; null before power on."),
                },
                "required": ["kind", "adapter"],
            },
        },
        "tools_installer_mounted": _field(
            _NULLABLE_BOOL, "True when the VMware Tools installer is in a CD drive."
        ),
        "read_notes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "One line per part that could not be read.",
        },
    },
    "required": ["vm", "cpu", "memory", "read_notes"],
}

VM_HARDWARE_WHEN_TO_USE: Final = (
    "Use to read the virtual hardware of one VM: CPU count and cores per socket, "
    "memory size, whether CPU and memory hot add is on, each CD drive (ISO file, host "
    "device or client device; connected or not), each SCSI, SATA and NVMe disk "
    "controller, and whether the VMware Tools installer is mounted. Read-only. For "
    "power state, guest IP and Tools status use vmware.vm.info instead."
)

VMWARE_VM_HARDWARE_SHOW_OP = VmwareTypedOp(
    op_id="vmware.vm.hardware.show",
    handler_attr="vm_hardware_show",
    summary="One VM's CPU, memory, CD drives, disk controllers and Tools installer state.",
    description=(
        "Returns one VM's hardware, by id (vm) or name: CPU count, cores per socket and "
        "hot add, memory size and hot add, each CD drive with its backing and state, each "
        "SCSI, SATA and NVMe controller with its bus, and whether the VMware Tools "
        "installer is mounted. Read-only."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=_RESPONSE_SCHEMA,
    group_key=VM_HARDWARE_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "vm", "hardware", "cdrom"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see how many CPUs and how much memory a VM has, whether hot add is "
            "on, which ISO is in a CD drive and whether the drive is connected, which disk "
            "controllers the VM has, or whether the VMware Tools installer is mounted. Call "
            "it before adding a CD drive or unmounting the Tools installer."
        ),
        "parameter_hints": {
            "vm": "VM id such as 'vm-42'; give exactly one of vm / name.",
            "name": "VM name; an unknown name returns status not_found.",
        },
        "output_shape": (
            "{vm, name, cpu: {count, cores_per_socket, hot_add_enabled, hot_remove_enabled}, "
            "memory: {size_mib, hot_add_enabled, hot_add_increment_size_mib, "
            "hot_add_limit_mib}, cdroms: [{cdrom, label, type, ide, sata, backing_type, "
            "iso_file, host_device, device_access_type, state, connected, start_connected, "
            "allow_guest_control}], disk_controllers: [{kind, adapter, label, type, bus, "
            "unit, sharing, pci_slot_number}], tools_installer_mounted, read_notes}. A part "
            "that could not be read is null and read_notes says why."
        ),
        "next_step": (
            "To add a CD drive, call vmware.composite.vm.cdrom.add. To change, disconnect or "
            "remove a CD drive, call vmware.composite.vm.device.cdrom with its cdrom id. To "
            "take out the Tools installer, call vmware.composite.vm.tools.installer.unmount. "
            "To change CPU or memory, call vmware.composite.vm.resize."
        ),
    },
)
