# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.vm.boot.show`` -- the boot settings and boot order of one VM.

A typed read (``source_kind="typed"``). It needs no catalog ingest and works
on vCenter 8 and 9; every call below exists on both.

Calls:

* ``GET /vcenter/vm/{vm}/hardware/boot`` -- firmware (BIOS or EFI), boot
  delay, retry, enter-setup flag. Must work: a 404 means the VM does not
  exist, and the read answers ``not_found``.
* ``GET /vcenter/vm/{vm}/hardware/boot/device`` -- the boot order. Best
  effort.
* VIM ``RetrievePropertiesEx`` of ``name`` and ``config.bootOptions`` -- the
  VM name and the EFI secure boot flag, which REST does not have. Best
  effort.

When a best-effort part fails, it is ``null`` and ``read_notes`` says why.
The reader :func:`read_boot_state` is shared with the change
``vmware.composite.vm.boot.set`` (``composites/_vm_boot.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx
import structlog

from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.vmware_rest._vm_settings_schemas import (
    BOOT_ORDER_ENTRY_SCHEMA,
    VM_BOOT_RESPONSE_SCHEMA,
    VM_READ_PARAMETER_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value
from meho_backplane.connectors.vmware_rest.typed_ops_vm_hardware import (
    VM_HARDWARE_GROUP_KEY,
    check_vm_moid,
    resolve_vm,
)
from meho_backplane.connectors.vmware_rest.vim_vm_props import (
    RETRIEVE_PROPERTIES_CALL,
    read_vm_vim_props,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "BOOT_ORDER_ENTRY_SCHEMA",
    "BOOT_REST_CALLS",
    "BOOT_VIM_CALLS",
    "TYPED_OPS",
    "VMWARE_VM_BOOT_SHOW_OP",
    "read_boot_state",
    "vm_boot_show_impl",
]

_log = structlog.get_logger(__name__)

OP_ID: Final = "vmware.vm.boot.show"

#: Every REST call of the read, as ``METHOD:/path`` keys of ``vcenter.yaml``.
BOOT_REST_CALLS: Final[tuple[str, ...]] = (
    "GET:/vcenter/vm",
    "GET:/vcenter/vm/{vm}/hardware/boot",
    "GET:/vcenter/vm/{vm}/hardware/boot/device",
)
#: Every VIM call of the read, as ``METHOD:/path`` keys of ``vi-json.yaml``.
BOOT_VIM_CALLS: Final[tuple[str, ...]] = (RETRIEVE_PROPERTIES_CALL,)

_PROP_NAME: Final = "name"
_PROP_BOOT_OPTIONS: Final = "config.bootOptions"


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def boot_order_row(raw: Any) -> dict[str, Any]:
    """Flatten one ``Boot.Device.Entry`` into ``{type, nic, disks}``."""
    entry = raw if isinstance(raw, dict) else {}
    disks = entry.get("disks")
    return {
        "type": _str(entry.get("type")),
        "nic": _str(entry.get("nic")),
        "disks": [d for d in disks if isinstance(d, str)] if isinstance(disks, list) else None,
    }


async def _get(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    vm: str,
    suffix: str,
) -> Any:
    path = f"/vcenter/vm/{check_vm_moid(vm)}/{suffix}"
    mounted = await connector.mount_op_path(target, path, operator)
    return _unwrap_value(await connector._get_json(target, mounted, operator=operator))


async def _read_boot_info(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    vm: str,
) -> dict[str, Any]:
    try:
        raw = await _get(connector, operator, target, vm, "hardware/boot")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise ConnectorResourceNotFoundError([vm], f"{OP_ID}: no VM {vm!r}") from exc
        raise
    return raw if isinstance(raw, dict) else {}


async def read_boot_state(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    vm: str,
    notes: list[str],
) -> dict[str, Any]:
    """Read the boot settings of *vm*. See the module docstring.

    The ``hardware/boot`` read raises; the boot order and the VIM read add a
    line to *notes* instead.
    """
    info = await _read_boot_info(connector, operator, target, vm)
    boot_order: list[dict[str, Any]] | None
    try:
        devices = await _get(connector, operator, target, vm, "hardware/boot/device")
        boot_order = [boot_order_row(d) for d in devices] if isinstance(devices, list) else []
    except httpx.HTTPError as exc:
        boot_order = None
        notes.append(f"boot order skipped: {type(exc).__name__}: {exc}")
    vm_name: str | None = None
    secure_boot: bool | None = None
    try:
        props, missing = await read_vm_vim_props(
            connector, operator, target, vm, (_PROP_NAME, _PROP_BOOT_OPTIONS), op_id=OP_ID
        )
        vm_name = _str(props.get(_PROP_NAME))
        options = props.get(_PROP_BOOT_OPTIONS)
        if isinstance(options, dict):
            secure_boot = _bool(options.get("efiSecureBootEnabled"))
        if _PROP_BOOT_OPTIONS in missing:
            notes.append("EFI secure boot skipped: vCenter could not read config.bootOptions")
    except (httpx.HTTPError, ConnectorResourceNotFoundError) as exc:
        notes.append(f"EFI secure boot skipped: {type(exc).__name__}: {exc}")
    return {
        "vm": vm,
        "vm_name": vm_name,
        "firmware": _str(info.get("type")),
        "efi_secure_boot": secure_boot,
        "efi_legacy_boot": _bool(info.get("efi_legacy_boot")),
        "network_protocol": _str(info.get("network_protocol")),
        "delay_ms": _int(info.get("delay")),
        "retry": _bool(info.get("retry")),
        "retry_delay_ms": _int(info.get("retry_delay")),
        "enter_setup_mode": _bool(info.get("enter_setup_mode")),
        "boot_order": boot_order,
    }


async def vm_boot_show_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.vm.boot.show``. See the module docstring."""
    vm = await resolve_vm(connector, operator, target, params)
    notes: list[str] = []
    state = await read_boot_state(connector, operator, target, vm, notes)
    _log.info("vmware_vm_boot_read", target=target.name, vm=vm, notes=len(notes))
    return {**state, "read_notes": notes}


VMWARE_VM_BOOT_SHOW_OP = VmwareTypedOp(
    op_id=OP_ID,
    handler_attr="vm_boot_show",
    summary="One VM's firmware, EFI secure boot, boot delay, enter-setup flag and boot order.",
    description=(
        "Returns the boot settings of one VM, by id (vm) or name: firmware (BIOS or EFI), "
        "EFI secure boot, boot delay, boot retry, whether the VM enters the firmware setup "
        "on the next boot, and the boot order. Read-only."
    ),
    parameter_schema=VM_READ_PARAMETER_SCHEMA,
    response_schema=VM_BOOT_RESPONSE_SCHEMA,
    group_key=VM_HARDWARE_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "vm", "boot"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see how a VM boots: BIOS or EFI, whether EFI secure boot is on, the "
            "boot delay, whether it enters the firmware setup on the next boot, and the boot "
            "order (CD drive, disk, network, floppy). Call it before changing boot settings."
        ),
        "parameter_hints": {
            "vm": "VM id such as 'vm-42'; give exactly one of vm / name.",
            "name": "VM name; an unknown name returns status not_found.",
        },
        "output_shape": (
            "{vm, vm_name, firmware: BIOS|EFI, efi_secure_boot, efi_legacy_boot, "
            "network_protocol, delay_ms, retry, retry_delay_ms, enter_setup_mode, "
            "boot_order: [{type: CDROM|DISK|ETHERNET|FLOPPY, nic, disks}], read_notes}. "
            "An empty boot_order means vCenter's default order. A part that could not be "
            "read is null and read_notes says why."
        ),
        "next_step": (
            "To change boot settings or the boot order, call vmware.composite.vm.boot.set "
            "with the VM id. Disk ids for the boot order come from vmware.vm.hardware.show."
        ),
    },
)

TYPED_OPS: tuple[VmwareTypedOp, ...] = (VMWARE_VM_BOOT_SHOW_OP,)
