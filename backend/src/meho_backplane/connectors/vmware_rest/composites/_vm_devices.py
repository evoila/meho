# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Three VM device changes: remove a disk, remove a network adapter, add a controller.

* ``vmware.composite.vm.disk.remove`` (``destructive``) -- in
  :mod:`._vm_disk_remove` (VIM ``ReconfigVM_Task``).
* ``vmware.composite.vm.nic.remove`` (``dangerous``) -- in
  :mod:`._vm_nic_remove` (REST ``DELETE .../hardware/ethernet/{nic}``).
* ``vmware.composite.vm.controller.add`` (``dangerous``) -- here: a SCSI (LSI Logic
  SAS or ParaVirtual), SATA or NVMe controller through
  ``POST /vcenter/vm/{vm}/hardware/adapter/{scsi|sata|nvme}``, read back with
  the matching ``GET``; a USB 3 (xHCI) controller through VIM
  ``ReconfigVM_Task`` (vSphere REST has no USB controller), polled to its end
  and read back from ``config.hardware.device``. A VM has one USB 3
  controller at most, so a second call sends nothing (``unchanged``).

All wait for approval. The VM is named only by its id: after approval the
change must run on the VM the approver saw. Every call exists on vCenter 8.0
U1 and newer and on 9.x, so the three are on both catalogs. vCenter refuses
some requests with HTTP 400 (for example a hot add it does not allow); the
answer is then ``refused`` with vCenter's reason, and nothing changed.

The registration rows live here, so the shared registry in ``_register.py``
only collects :data:`COMPOSITES`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vmware_rest.composites._spec import CompositeSpec
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import read_object
from meho_backplane.connectors.vmware_rest.composites._vm_devices_schemas import (
    SCSI_CONTROLLER_TYPES,
    VM_CONTROLLER_ADD_LLM_INSTRUCTIONS,
    VM_CONTROLLER_ADD_PARAMETER_SCHEMA,
    VM_CONTROLLER_ADD_RESPONSE_SCHEMA,
    VM_DISK_REMOVE_LLM_INSTRUCTIONS,
    VM_DISK_REMOVE_PARAMETER_SCHEMA,
    VM_DISK_REMOVE_RESPONSE_SCHEMA,
    VM_NIC_REMOVE_LLM_INSTRUCTIONS,
    VM_NIC_REMOVE_PARAMETER_SCHEMA,
    VM_NIC_REMOVE_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._vm_disk_remove import (
    OP_ID_VM_DISK_REMOVE,
    vm_disk_remove_composite,
    vm_disk_remove_preview,
)
from meho_backplane.connectors.vmware_rest.composites._vm_nic_remove import (
    OP_ID_VM_NIC_REMOVE,
    vendor_refusal,
    vm_nic_remove_composite,
    vm_nic_remove_preview,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_RECONFIG_VM_TASK,
    _OP_RETRIEVE_PROPERTIES,
    _PROP_CONFIG_HARDWARE_DEVICE,
    _VIRTUAL_DEVICE_CONFIG_SPEC_TYPE,
    _VIRTUAL_MACHINE_MO_TYPE,
    _VM_CONFIG_SPEC_TYPE,
    _coerce_int,
    _read_sub_op,
    _task_timeout_issue,
    _unwrap_value,
    _write_sub_op,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vm_hardware import check_vm_moid
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task
from meho_backplane.operations._preview import (
    PreviewContext,
    preview_failure_reason,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "COMPOSITES",
    "SUB_OPS_VM_CONTROLLER_ADD",
    "vm_controller_add_composite",
]

OP_ID_VM_CONTROLLER_ADD: Final = "vmware.composite.vm.controller.add"

#: The REST controller kinds, with their list, read and create calls.
_REST_KINDS: Final[tuple[str, ...]] = ("scsi", "sata", "nvme")
_OP_LIST_ADAPTERS: Final = {k: f"GET:/vcenter/vm/{{vm}}/hardware/adapter/{k}" for k in _REST_KINDS}
_OP_GET_ADAPTER: Final = {
    k: f"GET:/vcenter/vm/{{vm}}/hardware/adapter/{k}/{{adapter}}" for k in _REST_KINDS
}
_OP_CREATE_ADAPTER: Final = {
    k: f"POST:/vcenter/vm/{{vm}}/hardware/adapter/{k}" for k in _REST_KINDS
}

#: Every REST call of each change. The reconcile test checks them against the
#: pinned vCenter 8.0 and 9.0 API files.
SUB_OPS_VM_CONTROLLER_ADD: Final[tuple[str, ...]] = tuple(
    op
    for k in _REST_KINDS
    for op in (_OP_LIST_ADAPTERS[k], _OP_GET_ADAPTER[k], _OP_CREATE_ADAPTER[k])
)
#: The VIM calls of the USB 3 controller add (checked against the vi-json files).
_VIM_SUB_OPS_VM_CONTROLLER_ADD: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_RECONFIG_VM_TASK,
)

#: The VIM type of a USB 3 controller.
_VIRTUAL_USB_XHCI_CONTROLLER_TYPE: Final = "VirtualUSBXHCIController"
#: New-device key (negative, the VIM convention for a device to add).
_NEW_DEVICE_KEY: Final = -1
_USB_XHCI: Final = "USB_XHCI"

#: Wall-clock bound for the USB task poll; module-global so tests can zero it.
_TASK_TIMEOUT_SECONDS = 600.0


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


# ---------------------------------------------------------------------------
# vm.controller.add
# ---------------------------------------------------------------------------


def _kind(controller_type: str) -> str:
    if controller_type in SCSI_CONTROLLER_TYPES:
        return "scsi"
    return {"SATA": "sata", "NVME": "nvme"}.get(controller_type, "usb")


def _create_spec(params: dict[str, Any]) -> dict[str, Any]:
    """The REST ``CreateSpec`` (top-level body on ``/api``) for a disk controller."""
    controller_type = params["type"]
    spec: dict[str, Any] = {}
    if controller_type in SCSI_CONTROLLER_TYPES:
        spec["type"] = controller_type
        if params.get("sharing") is not None:
            spec["sharing"] = params["sharing"]
    elif controller_type == "SATA":
        spec["type"] = "AHCI"
    # The create needs a body. An NVMe spec has only ``bus``, and an empty body
    # is sent as none, so ``bus`` is always there: null lets vCenter pick one.
    spec["bus"] = params.get("bus")
    return spec


def _param_problem(params: dict[str, Any]) -> str | None:
    controller_type = params["type"]
    if params.get("sharing") is not None and controller_type not in SCSI_CONTROLLER_TYPES:
        return "sharing is only for a SCSI controller (LSILOGICSAS or PVSCSI)"
    if params.get("bus") is not None and controller_type == _USB_XHCI:
        return "bus is not used for a USB_XHCI controller"
    return None


def _rest_controller_row(kind: str, adapter: str, raw: Any) -> dict[str, Any]:
    info = raw if isinstance(raw, dict) else {}
    scsi = info.get("scsi")
    bus = scsi.get("bus") if kind == "scsi" and isinstance(scsi, dict) else info.get("bus")
    return {
        "kind": kind,
        "adapter": adapter,
        "label": _str(info.get("label")),
        "type": _str(info.get("type")) or ("NVME" if kind == "nvme" else None),
        "bus": _coerce_int(bus),
        "sharing": _str(info.get("sharing")),
        "pci_slot_number": _coerce_int(info.get("pci_slot_number")),
    }


def _usb_controller_row(device: dict[str, Any]) -> dict[str, Any]:
    info = device.get("deviceInfo")
    slot = device.get("slotInfo")
    return {
        "kind": "usb",
        "adapter": str(device.get("key")),
        "label": _str(info.get("label")) if isinstance(info, dict) else None,
        "type": _USB_XHCI,
        "bus": _coerce_int(device.get("busNumber")),
        "sharing": None,
        "pci_slot_number": _coerce_int(slot.get("pciSlotNumber"))
        if isinstance(slot, dict)
        else None,
    }


async def _usb_controller(
    connector: VmwareRestConnector, target: Any, operator: Operator, vm: str
) -> tuple[bool, dict[str, Any] | None]:
    """``(VM exists, its USB 3 controller row or None)`` from ``config.hardware.device``."""
    props = await read_object(
        connector,
        target,
        operator,
        mo_type=_VIRTUAL_MACHINE_MO_TYPE,
        moid=vm,
        props=[_PROP_CONFIG_HARDWARE_DEVICE],
    )
    if props is None:
        return False, None
    devices = props.get(_PROP_CONFIG_HARDWARE_DEVICE)
    for device in devices if isinstance(devices, list) else []:
        if isinstance(device, dict) and device.get(VIM_TYPE_NAME_KEY) == (
            _VIRTUAL_USB_XHCI_CONTROLLER_TYPE
        ):
            return True, _usb_controller_row(device)
    return True, None


def _usb_add_body() -> dict[str, Any]:
    """The ``ReconfigVM_Task`` body that adds one USB 3 controller (as ``govc device.usb.add``)."""
    device = {
        VIM_TYPE_NAME_KEY: _VIRTUAL_USB_XHCI_CONTROLLER_TYPE,
        "key": _NEW_DEVICE_KEY,
        "busNumber": 0,
        "autoConnectDevices": True,
    }
    change = {
        VIM_TYPE_NAME_KEY: _VIRTUAL_DEVICE_CONFIG_SPEC_TYPE,
        "operation": "add",
        "device": device,
    }
    return {"spec": {VIM_TYPE_NAME_KEY: _VM_CONFIG_SPEC_TYPE, "deviceChange": [change]}}


async def _add_usb_controller(
    connector: VmwareRestConnector, target: Any, operator: Operator, envelope: dict[str, Any]
) -> dict[str, Any] | OperationResult:
    vm = envelope["vm"]
    exists, current = await _usb_controller(connector, target, operator, vm)
    if not exists:
        return {**envelope, "status": "not_found", "guidance": f"no VM {vm!r}; nothing sent"}
    if current is not None:
        return {
            **envelope,
            "status": "unchanged",
            "controller": current,
            "guidance": "The VM already has a USB 3 controller (one at most); nothing sent.",
        }
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_RECONFIG_VM_TASK,
        vmomi_path=f"/{_VIRTUAL_MACHINE_MO_TYPE}/{vm}/ReconfigVM_Task",
        body=_usb_add_body(),
        params={"vm": vm, "type": _USB_XHCI},
    )
    if gate is not None:
        return gate
    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(task_payload),
        timeout_seconds=_TASK_TIMEOUT_SECONDS,
    )
    if outcome.state == TASK_STATE_ERROR:
        raise RuntimeError(
            f"vm.controller.add: ReconfigVM_Task on vm {vm!r} faulted "
            f"({outcome.fault_type or 'unknown fault'}): "
            f"{outcome.error_message or '<no fault reported>'}"
        )
    if outcome.timed_out:
        return {
            **envelope,
            "status": "timeout",
            "task": outcome.task,
            "guidance": f"ReconfigVM_Task {outcome.task} did not finish in time; it may still "
            "finish. Check with vmware.vm.hardware.show.",
            "issues": [_task_timeout_issue("ReconfigVM_Task", outcome.task)],
        }
    _, added = await _usb_controller(connector, target, operator, vm)
    guidance = (
        None if added else "vCenter finished the task, but the controller does not read back yet."
    )
    return {
        **envelope,
        "status": "added",
        "task": outcome.task,
        "controller": added,
        "guidance": guidance,
    }


async def _add_rest_controller(
    connector: VmwareRestConnector, target: Any, operator: Operator, envelope: dict[str, Any]
) -> dict[str, Any] | OperationResult:
    vm, kind, spec = envelope["vm"], _kind(envelope["type"]), envelope["requested"]
    try:
        gate, payload = await _write_sub_op(
            connector, target, operator, _OP_CREATE_ADAPTER[kind], {"vm": vm, **spec}
        )
    except httpx.HTTPError as exc:
        refusal = vendor_refusal(envelope, exc)
        if refusal is None:
            raise
        return refusal
    if gate is not None:
        return gate
    adapter = _unwrap_value(payload)
    if not isinstance(adapter, str):
        return {
            **envelope,
            "status": "added",
            "guidance": "vCenter added the controller but sent no id. Read it with "
            "vmware.vm.hardware.show.",
        }
    try:
        raw = await _read_sub_op(
            connector, target, operator, _OP_GET_ADAPTER[kind], {"vm": vm, "adapter": adapter}
        )
        row = _rest_controller_row(kind, adapter, _unwrap_value(raw))
        guidance = None
    except httpx.HTTPError:
        row = _rest_controller_row(kind, adapter, None)
        guidance = "vCenter added the controller, but MEHO could not read it back."
    return {**envelope, "status": "added", "controller": row, "guidance": guidance}


async def vm_controller_add_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Add one disk or USB 3 controller to a VM. Op-id: ``vmware.composite.vm.controller.add``."""
    vm = check_vm_moid(params["vm"])
    controller_type = params["type"]
    envelope: dict[str, Any] = {
        "vm": vm,
        "type": controller_type,
        "requested": {"type": controller_type}
        if controller_type == _USB_XHCI
        else _create_spec(params),
        "controller": None,
        "task": None,
        "error_type": None,
        "guidance": None,
    }
    problem = _param_problem(params)
    if problem is not None:
        return {**envelope, "status": "invalid_request", "guidance": f"{problem}; nothing sent"}
    if controller_type == _USB_XHCI:
        return await _add_usb_controller(connector, target, operator, envelope)
    return await _add_rest_controller(connector, target, operator, envelope)


async def _vm_controller_add_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the controller to add and the ones of that kind the VM has now."""
    if ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    controller_type = ctx.params.get("type")
    if not isinstance(controller_type, str):
        return None
    kind = _kind(controller_type)
    try:
        vm = check_vm_moid(ctx.params["vm"])
        if kind == "usb":
            _, usb = await _usb_controller(connector, ctx.target, ctx.operator, vm)
            current = [usb] if usb else []
        else:
            raw = await _read_sub_op(
                connector, ctx.target, ctx.operator, _OP_LIST_ADAPTERS[kind], {"vm": vm}
            )
            listing = _unwrap_value(raw)
            current = (
                [e for e in listing if isinstance(e, dict)] if isinstance(listing, list) else []
            )
    except Exception as exc:
        return {
            "summary": "MEHO could not read the VM, so it cannot show its controllers.",
            "requested": {"type": controller_type},
            "read_error": preview_failure_reason(exc),
        }
    return {
        "summary": f"Add a {controller_type} controller to {vm} ({len(current)} {kind} "
        "controller(s) now).",
        "vm": vm,
        "requested": {"type": controller_type} if kind == "usb" else _create_spec(ctx.params),
        "current_controllers": current,
    }


#: The three registration rows.
COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id=OP_ID_VM_DISK_REMOVE,
        handler=vm_disk_remove_composite,
        summary="Remove a virtual disk from a VM; optionally delete its VMDK file too.",
        description=(
            "Removes one virtual disk from a VM, named by VM id and disk id. By default the "
            "VMDK file stays on the datastore; with delete_file the data is gone for good. "
            "Returns the disks left. A second person always approves, after a preview."
        ),
        parameter_schema=VM_DISK_REMOVE_PARAMETER_SCHEMA,
        response_schema=VM_DISK_REMOVE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "hardware", "disk", "vi-json"],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=VM_DISK_REMOVE_LLM_INSTRUCTIONS,
        preview=vm_disk_remove_preview,
    ),
    CompositeSpec(
        op_id=OP_ID_VM_NIC_REMOVE,
        handler=vm_nic_remove_composite,
        summary="Remove a network adapter from a VM.",
        description=(
            "Removes one network adapter from a VM, named by VM id and adapter id. Reads the "
            "adapter first and sends nothing when it does not exist. Returns the adapters "
            "left. Waits for approval."
        ),
        parameter_schema=VM_NIC_REMOVE_PARAMETER_SCHEMA,
        response_schema=VM_NIC_REMOVE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "hardware", "nic"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_NIC_REMOVE_LLM_INSTRUCTIONS,
        preview=vm_nic_remove_preview,
    ),
    CompositeSpec(
        op_id=OP_ID_VM_CONTROLLER_ADD,
        handler=vm_controller_add_composite,
        summary="Add a SCSI, NVMe, SATA or USB 3 controller to a VM.",
        description=(
            "Adds one controller to a VM, named by its VM id: SCSI (LSI Logic SAS or "
            "ParaVirtual), NVMe, SATA, or USB 3. Reads the new controller back. vCenter may "
            "refuse a hot add on a running VM. Waits for approval."
        ),
        parameter_schema=VM_CONTROLLER_ADD_PARAMETER_SCHEMA,
        response_schema=VM_CONTROLLER_ADD_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "hardware", "controller"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_CONTROLLER_ADD_LLM_INSTRUCTIONS,
        preview=_vm_controller_add_preview,
    ),
)
