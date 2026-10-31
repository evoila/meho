# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add or remove a PCI passthrough device on a VM.

vCenter has these changes only in the VIM API, so both use the VI-JSON seam
the other VM device changes use:

* ``vmware.composite.vm.pci_device.add`` (``dangerous``) -- reads the VM, asks
  the VM's ``EnvironmentBrowser.QueryConfigTarget`` which passthrough devices
  its host offers, then runs one governed ``VirtualMachine.ReconfigVM_Task``
  with a ``deviceChange`` add of a ``VirtualPCIPassthrough``. The backing is
  ``VirtualPCIPassthroughDeviceBackingInfo`` for DirectPath I/O (one host
  device by its PCI address) or ``VirtualPCIPassthroughDynamicBackingInfo``
  for dynamic DirectPath (any free device of a vendor and device id). A
  passthrough VM needs all of its memory reserved, so the same spec sets
  ``memoryReservationLockedToMax`` unless the caller says no.
* ``vmware.composite.vm.pci_device.remove`` (``dangerous``) -- reads the VM's
  devices and runs ``ReconfigVM_Task`` with a ``deviceChange`` remove of the
  passthrough device with the given key.

Both need the VM powered off, and both read the devices again after the task.
A task fault raises (``connector_error``, audited as failed) with the vim
fault type and text. The schemas and AI guidance live in
:mod:`._vm_console_pci_schemas`; the shared registry only splats
:data:`VM_PCI_DEVICE_COMPOSITES`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moid_problem,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._vm_console_pci_schemas import (
    VM_PCI_DEVICE_ADD_LLM_INSTRUCTIONS,
    VM_PCI_DEVICE_ADD_PARAMETER_SCHEMA,
    VM_PCI_DEVICE_ADD_RESPONSE_SCHEMA,
    VM_PCI_DEVICE_REMOVE_LLM_INSTRUCTIONS,
    VM_PCI_DEVICE_REMOVE_PARAMETER_SCHEMA,
    VM_PCI_DEVICE_REMOVE_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._vm_pci_backing import (
    add_spec,
    as_str,
    direct_backing,
    dynamic_backing,
    passthrough_rows,
    raw_passthrough,
    remove_spec,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_RECONFIG_VM_TASK,
    _OP_RETRIEVE_PROPERTIES,
    _VIRTUAL_MACHINE_MO_TYPE,
    _moref_value,
    _task_timeout_issue,
    _unwrap_value,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import VM_MOID_PATTERN
from meho_backplane.connectors.vmware_rest.vim_body import unwrap_vim_value
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task
from meho_backplane.operations._preview import preview_failure_reason, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.vim_task import VimTaskResult
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "VM_PCI_DEVICE_COMPOSITES",
    "VM_PCI_DEVICE_GOVERNED_SUBOPS",
    "vm_pci_device_add_composite",
    "vm_pci_device_remove_composite",
]

OP_ID_VM_PCI_DEVICE_ADD: Final = "vmware.composite.vm.pci_device.add"
OP_ID_VM_PCI_DEVICE_REMOVE: Final = "vmware.composite.vm.pci_device.remove"

_OP_QUERY_CONFIG_TARGET: Final = "POST:/EnvironmentBrowser/{moId}/QueryConfigTarget"

#: Every VI-JSON method of each change. The reconcile tests check them
#: against the pinned 8.0 and 9.0 VI-JSON files; ``_governed_subops`` lists
#: them for the grant set.
_VIM_SUB_OPS_VM_PCI_DEVICE_ADD: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_QUERY_CONFIG_TARGET,
    _OP_RECONFIG_VM_TASK,
)
_VIM_SUB_OPS_VM_PCI_DEVICE_REMOVE: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_RECONFIG_VM_TASK,
)

VM_PCI_DEVICE_GOVERNED_SUBOPS: Final[dict[str, tuple[str, ...]]] = {
    OP_ID_VM_PCI_DEVICE_ADD: _VIM_SUB_OPS_VM_PCI_DEVICE_ADD,
    OP_ID_VM_PCI_DEVICE_REMOVE: _VIM_SUB_OPS_VM_PCI_DEVICE_REMOVE,
}

_PROP_DEVICES: Final = "config.hardware.device"
_PROP_POWER: Final = "runtime.powerState"
_PROP_ENV_BROWSER: Final = "environmentBrowser"
_VM_PROPS: Final = ["name", _PROP_POWER, _PROP_DEVICES]

#: Wall-clock bound for the task poll (module-global so tests can lower it).
_TASK_TIMEOUT_SECONDS = 600.0


# ---------------------------------------------------------------------------
# Shared reads
# ---------------------------------------------------------------------------


async def _read_vm(
    connector: VmwareRestConnector, target: Any, operator: Operator, vm: str, props: list[str]
) -> dict[str, Any] | None:
    return await read_object(
        connector, target, operator, mo_type=_VIRTUAL_MACHINE_MO_TYPE, moid=vm, props=props
    )


def _envelope(status: str, vm: str, **fields: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": status,
        "vm": vm,
        "vm_name": None,
        "device": None,
        "pci_devices": None,
        "task": None,
        "task_state": None,
        "guidance": None,
    }
    out.update(fields)
    return out


def _task_fault(op_id: str, vm: str, outcome: VimTaskResult) -> RuntimeError:
    fault = outcome.fault_type or "fault"
    return RuntimeError(
        f"{op_id}: ReconfigVM_Task on vm {vm!r} failed ({fault}): "
        f"{outcome.error_message or '<no fault text>'}"
    )


async def _precheck(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    vm: str,
    props: list[str],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """``(early envelope or None, VM props)``: the checks before any write."""
    problem = moid_problem(vm, VM_MOID_PATTERN, "vm")
    if problem is not None:
        return _envelope("invalid_request", vm, guidance=problem), {}
    props_read = await _read_vm(connector, target, operator, vm, props)
    if props_read is None:
        guidance = f"no VM {vm!r}; pass a VM id such as 'vm-42' (vmware.vm.list finds it)"
        return _envelope("not_found", vm, guidance=guidance), {}
    name = as_str(props_read.get("name"))
    rows = passthrough_rows(props_read.get(_PROP_DEVICES))
    if props_read.get(_PROP_POWER) != "poweredOff":
        early = _envelope(
            "refused",
            vm,
            vm_name=name,
            pci_devices=rows,
            guidance=(
                f"VM {vm!r} is {props_read.get(_PROP_POWER) or 'not powered off'}; a PCI "
                "passthrough device can only change while the VM is powered off. "
                "Nothing was sent"
            ),
        )
        return early, props_read
    return None, props_read


async def _reconfigure(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    vm: str,
    spec: dict[str, Any],
    gate_params: dict[str, Any],
) -> tuple[OperationResult | None, VimTaskResult | None]:
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_RECONFIG_VM_TASK,
        vmomi_path=f"/{_VIRTUAL_MACHINE_MO_TYPE}/{vm}/ReconfigVM_Task",
        body={"spec": spec},
        params=gate_params,
    )
    if gate is not None:
        return gate, None
    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(task_payload),
        timeout_seconds=_TASK_TIMEOUT_SECONDS,
    )
    return None, outcome


def _timeout(envelope: dict[str, Any], outcome: VimTaskResult) -> dict[str, Any]:
    return {
        **envelope,
        "status": "timeout",
        "task": outcome.task,
        "task_state": outcome.state,
        "guidance": (
            f"ReconfigVM_Task {outcome.task} did not finish within "
            f"{int(_TASK_TIMEOUT_SECONDS)}s; it may still finish. Read the VM's devices "
            "with vmware.vm.hardware.show before you try again"
        ),
        "issues": [_task_timeout_issue("ReconfigVM_Task", outcome.task)],
    }


# ---------------------------------------------------------------------------
# vm.pci_device.add
# ---------------------------------------------------------------------------


async def _config_target(
    connector: VmwareRestConnector, target: Any, operator: Operator, vm: str
) -> dict[str, Any]:
    """The VM host's ``ConfigTarget`` (passthrough devices it offers)."""
    props = await _read_vm(connector, target, operator, vm, [_PROP_ENV_BROWSER])
    browser = _moref_value(unwrap_vim_value((props or {}).get(_PROP_ENV_BROWSER)))
    if browser is None:
        raise RuntimeError(f"{OP_ID_VM_PCI_DEVICE_ADD}: VM {vm!r} has no environment browser")
    payload = await connector._post_vmomi_json(
        target,
        f"/EnvironmentBrowser/{browser}/QueryConfigTarget",
        operator=operator,
        json={},
    )
    config = unwrap_vim_value(_unwrap_value(payload))
    return config if isinstance(config, dict) else {}


def _requested(params: dict[str, Any]) -> dict[str, Any]:
    if isinstance(params.get("pci_id"), str):
        return {"kind": "directpath", "pci_id": params["pci_id"]}
    dynamic = params.get("dynamic_device") or {}
    return {
        "kind": "dynamic",
        "vendor_id": dynamic.get("vendor_id"),
        "device_id": dynamic.get("device_id"),
        "custom_label": dynamic.get("custom_label"),
    }


async def vm_pci_device_add_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Add one PCI passthrough device to a powered-off VM.

    Op-id: ``vmware.composite.vm.pci_device.add``. Refuses before any write
    when the VM is unknown (``not_found``), not powered off (``refused``), the
    host does not offer the device (``invalid_request``) or the VM already has
    that DirectPath I/O device (``unchanged``). Otherwise the gated
    ``ReconfigVM_Task`` (a parked or denied gate is returned as is), polled to
    the end, then the devices are read again.
    """
    vm = params["vm"]
    requested = _requested(params)
    early, props = await _precheck(connector, target, operator, vm, _VM_PROPS)
    if early is not None:
        return {**early, "requested": requested}
    name = as_str(props.get("name"))
    before = passthrough_rows(props.get(_PROP_DEVICES))
    envelope = _envelope("added", vm, vm_name=name, requested=requested, pci_devices=before)
    if requested["kind"] == "directpath":
        wanted = params["pci_id"].lower()
        same = next((r for r in before if (r["pci_id"] or "").lower() == wanted), None)
        if same is not None:
            return {
                **envelope,
                "status": "unchanged",
                "device": same,
                "guidance": "the VM already has this passthrough device; nothing was sent",
            }
    config = await _config_target(connector, target, operator, vm)
    backing = (
        direct_backing(params["pci_id"], config)
        if requested["kind"] == "directpath"
        else dynamic_backing(params["dynamic_device"], config)
    )
    if isinstance(backing, str):
        return {**envelope, "status": "invalid_request", "guidance": backing}
    reserve = bool(params.get("reserve_all_memory", True))
    gate, outcome = await _reconfigure(
        connector,
        target,
        operator,
        vm=vm,
        spec=add_spec(backing, reserve),
        gate_params={"vm": vm, **requested, "reserve_all_memory": reserve},
    )
    if gate is not None:
        return gate
    assert outcome is not None
    if outcome.state == TASK_STATE_ERROR:
        raise _task_fault(OP_ID_VM_PCI_DEVICE_ADD, vm, outcome)
    if outcome.timed_out:
        return _timeout(envelope, outcome)
    after_props = await _read_vm(connector, target, operator, vm, [_PROP_DEVICES])
    after = passthrough_rows((after_props or {}).get(_PROP_DEVICES))
    old_keys = {r["device_key"] for r in before}
    new = next((r for r in after if r["device_key"] not in old_keys), None)
    return {
        **envelope,
        "device": new,
        "pci_devices": after,
        "task": outcome.task,
        "task_state": outcome.state,
        "guidance": (
            None
            if new is not None
            else "ReconfigVM_Task succeeded, but the new device does not read back yet; "
            "read the VM again with vmware.vm.hardware.show"
        ),
    }


async def _vm_pci_device_add_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the VM, its power state, the device to add and the ones it has."""
    if ctx.connector_instance is None or not isinstance(ctx.params.get("vm"), str):
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    vm = ctx.params["vm"]
    requested = _requested(ctx.params)
    try:
        if moid_problem(vm, VM_MOID_PATTERN, "vm") is not None:
            raise ValueError(f"vm {vm!r} is not a VM id")
        props = await _read_vm(connector, ctx.target, ctx.operator, vm, _VM_PROPS)
    except Exception as exc:
        return {
            "summary": "MEHO could not read the VM, so it cannot show its devices.",
            "requested": requested,
            "read_error": preview_failure_reason(exc),
        }
    if props is None:
        return {"summary": f"There is no VM {vm}; the call will change nothing.", "vm": vm}
    return {
        "summary": f"Add a PCI passthrough device to {props.get('name') or vm} ({vm}).",
        "vm": vm,
        "power_state": props.get(_PROP_POWER),
        "requested": requested,
        "reserve_all_memory": bool(ctx.params.get("reserve_all_memory", True)),
        "current_pci_devices": passthrough_rows(props.get(_PROP_DEVICES)),
    }


# ---------------------------------------------------------------------------
# vm.pci_device.remove
# ---------------------------------------------------------------------------


async def vm_pci_device_remove_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Remove one PCI passthrough device from a powered-off VM, by device key.

    Op-id: ``vmware.composite.vm.pci_device.remove``. ``unchanged`` when the
    VM has no passthrough device with the key (nothing sent); ``not_found``
    for an unknown VM; ``refused`` when the VM is not powered off. Otherwise
    the gated ``ReconfigVM_Task`` remove, polled to the end; ``removed`` only
    when a new read no longer shows the device.
    """
    vm = params["vm"]
    device_key = params["device_key"]
    early, props = await _precheck(connector, target, operator, vm, _VM_PROPS)
    if early is not None:
        return {**early, "device_key": device_key}
    name = as_str(props.get("name"))
    before = passthrough_rows(props.get(_PROP_DEVICES))
    envelope = _envelope("removed", vm, vm_name=name, device_key=device_key, pci_devices=before)
    raw = raw_passthrough(props.get(_PROP_DEVICES), device_key)
    if raw is None:
        return {
            **envelope,
            "status": "unchanged",
            "guidance": (
                f"VM {vm!r} has no PCI passthrough device with key {device_key}; nothing was "
                "sent. The device keys are in pci_devices"
            ),
        }
    device = next(r for r in before if r["device_key"] == device_key)
    envelope["device"] = device
    gate, outcome = await _reconfigure(
        connector,
        target,
        operator,
        vm=vm,
        spec=remove_spec(raw),
        gate_params={"vm": vm, "device_key": device_key, "label": device["label"]},
    )
    if gate is not None:
        return gate
    assert outcome is not None
    if outcome.state == TASK_STATE_ERROR:
        raise _task_fault(OP_ID_VM_PCI_DEVICE_REMOVE, vm, outcome)
    if outcome.timed_out:
        return _timeout(envelope, outcome)
    after_props = await _read_vm(connector, target, operator, vm, [_PROP_DEVICES])
    after = passthrough_rows((after_props or {}).get(_PROP_DEVICES))
    still = any(r["device_key"] == device_key for r in after)
    return {
        **envelope,
        "status": "still_present" if still else "removed",
        "pci_devices": after,
        "task": outcome.task,
        "task_state": outcome.state,
        "guidance": (
            "ReconfigVM_Task succeeded, but the device still reads back; read the VM again"
            if still
            else None
        ),
    }


async def _vm_pci_device_remove_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the device that goes away."""
    if ctx.connector_instance is None or not isinstance(ctx.params.get("vm"), str):
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    vm = ctx.params["vm"]
    device_key = ctx.params.get("device_key")
    try:
        if moid_problem(vm, VM_MOID_PATTERN, "vm") is not None:
            raise ValueError(f"vm {vm!r} is not a VM id")
        props = await _read_vm(connector, ctx.target, ctx.operator, vm, _VM_PROPS)
    except Exception as exc:
        return {
            "summary": "MEHO could not read the VM, so it cannot show the device.",
            "device_key": device_key,
            "read_error": preview_failure_reason(exc),
        }
    if props is None:
        return {"summary": f"There is no VM {vm}; the call will change nothing.", "vm": vm}
    rows = passthrough_rows(props.get(_PROP_DEVICES))
    device = next((r for r in rows if r["device_key"] == device_key), None)
    summary = (
        f"Remove PCI passthrough device {device['label'] or device_key} from "
        f"{props.get('name') or vm} ({vm})."
        if device is not None
        else f"{vm} has no PCI passthrough device with key {device_key}; nothing will change."
    )
    return {
        "summary": summary,
        "vm": vm,
        "power_state": props.get(_PROP_POWER),
        "device": device,
    }


register_preview_builder(OP_ID_VM_PCI_DEVICE_ADD, _vm_pci_device_add_preview)
register_preview_builder(OP_ID_VM_PCI_DEVICE_REMOVE, _vm_pci_device_remove_preview)


#: The two registration rows, as ``_CompositeSpec`` keyword arguments.
VM_PCI_DEVICE_COMPOSITES: Final[tuple[dict[str, Any], ...]] = (
    {
        "op_id": OP_ID_VM_PCI_DEVICE_ADD,
        "handler": vm_pci_device_add_composite,
        "summary": "Add a PCI passthrough device (DirectPath I/O or dynamic) to a powered-off VM.",
        "description": (
            "Adds one PCI passthrough device to a powered-off VM: a host device by its PCI "
            "address (DirectPath I/O), or any free device of a vendor and device id (dynamic "
            "DirectPath). Reserves all VM memory by default. Reads the devices back. Waits "
            "for approval."
        ),
        "parameter_schema": VM_PCI_DEVICE_ADD_PARAMETER_SCHEMA,
        "response_schema": VM_PCI_DEVICE_ADD_RESPONSE_SCHEMA,
        "group_key": "vm",
        "tags": ["composite", "write", "vm", "hardware", "pci-passthrough", "vi-json"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": VM_PCI_DEVICE_ADD_LLM_INSTRUCTIONS,
    },
    {
        "op_id": OP_ID_VM_PCI_DEVICE_REMOVE,
        "handler": vm_pci_device_remove_composite,
        "summary": "Remove a PCI passthrough device from a powered-off VM.",
        "description": (
            "Removes one PCI passthrough device from a powered-off VM, named by its device "
            "key. Sends nothing when the VM has no such device. Reads the devices back. "
            "Waits for approval."
        ),
        "parameter_schema": VM_PCI_DEVICE_REMOVE_PARAMETER_SCHEMA,
        "response_schema": VM_PCI_DEVICE_REMOVE_RESPONSE_SCHEMA,
        "group_key": "vm",
        "tags": ["composite", "write", "vm", "hardware", "pci-passthrough", "vi-json"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": VM_PCI_DEVICE_REMOVE_LLM_INSTRUCTIONS,
    },
)
