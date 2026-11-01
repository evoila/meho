# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vm.relocate`` -- move a VM inside one vCenter (``VirtualMachine.RelocateVM_Task``).

Moves a VM, also while it runs, to another datastore (each disk can go to its
own datastore), resource pool, folder or host. A vendor job: the answer says
``accepted`` with the vCenter task id as soon as vCenter took the task, and
``vmware.task.info`` follows it. One vendor call, so there is no partial
result. A move of the same VM that still runs is returned (``already_running``)
and nothing new starts -- a help, not a lock.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, Final

import structlog

from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moid_problem,
    moref_values,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._vm_job_common import (
    PLACEMENT_FIELDS,
    VM_MO_TYPE,
    as_str,
    moref_id,
    placement_problem,
    running_task,
    send_vm_task,
    status_read,
)
from meho_backplane.connectors.vmware_rest.composites._write import _OP_RETRIEVE_PROPERTIES
from meho_backplane.connectors.vmware_rest.composites.schemas import VM_MOID_PATTERN
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY, vim_moref
from meho_backplane.connectors.vmware_rest.vm_relocate_preview import vm_relocate_preview
from meho_backplane.operations._preview import register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "VM_RELOCATE_OP_ID",
    "build_relocate_spec",
    "vm_relocate_composite",
    "vm_relocate_preview_builder",
]

_log = structlog.get_logger(__name__)

VM_RELOCATE_OP_ID: Final = "vmware.composite.vm.relocate"
_OP_RELOCATE_VM_TASK: Final = "POST:/VirtualMachine/{moId}/RelocateVM_Task"
#: vi-json sub-op manifest: the reads, then the one write.
_VIM_SUB_OPS_VM_RELOCATE: Final[tuple[str, ...]] = (_OP_RETRIEVE_PROPERTIES, _OP_RELOCATE_VM_TASK)
#: ``TaskInfo.descriptionId`` of a move (``<Type>.<method>``).
_RELOCATE_DESCRIPTION_ID: Final = "VirtualMachine.relocate"
_RELOCATE_SPEC_TYPE: Final = "VirtualMachineRelocateSpec"
_DISK_LOCATOR_TYPE: Final = "VirtualMachineRelocateSpecDiskLocator"
_RELOCATE_VM_PROPS: Final = [
    "name",
    "runtime.powerState",
    "runtime.host",
    "resourcePool",
    "parent",
    "datastore",
    "config.hardware.device",
    "recentTask",
]


def build_relocate_spec(params: dict[str, Any]) -> dict[str, Any]:
    """The ``VirtualMachineRelocateSpec`` of the call: only the fields it names."""
    spec: dict[str, Any] = {VIM_TYPE_NAME_KEY: _RELOCATE_SPEC_TYPE}
    for name, field, mo_type, _pattern in PLACEMENT_FIELDS:
        if isinstance(params.get(name), str):
            spec[field] = vim_moref(mo_type, params[name])
    disks = params.get("disks") or []
    if disks:
        spec["disk"] = [
            {
                VIM_TYPE_NAME_KEY: _DISK_LOCATOR_TYPE,
                "diskId": int(disk["disk_key"]),
                "datastore": vim_moref("Datastore", disk["datastore"]),
            }
            for disk in disks
        ]
    return spec


def _requested(params: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        name: params[name] for name, *_rest in PLACEMENT_FIELDS if isinstance(params.get(name), str)
    }
    if params.get("disks"):
        out["disks"] = [dict(disk) for disk in params["disks"]]
    return out


def _disk_datastores(devices: Any) -> dict[int, str | None]:
    """``{disk key: datastore moid}`` of the VM's virtual disks."""
    out: dict[int, str | None] = {}
    for device in devices if isinstance(devices, list) else []:
        if not isinstance(device, dict) or device.get(VIM_TYPE_NAME_KEY) != "VirtualDisk":
            continue
        key = device.get("key")
        backing = device.get("backing")
        if isinstance(key, int):
            out[key] = moref_id(backing.get("datastore")) if isinstance(backing, dict) else None
    return out


def _before(props: dict[str, Any]) -> dict[str, Any]:
    return {
        "power_state": as_str(props.get("runtime.powerState")),
        "host": moref_id(props.get("runtime.host")),
        "resource_pool": moref_id(props.get("resourcePool")),
        "folder": moref_id(props.get("parent")),
        "datastores": [moid for _kind, moid in moref_values(props.get("datastore"))],
    }


def _is_unchanged(params: dict[str, Any], before: dict[str, Any], disks: dict[int, Any]) -> bool:
    """Whether the VM already is where every named field asks it to be."""
    for name in ("resource_pool", "folder", "host"):
        if isinstance(params.get(name), str) and params[name] != before[name]:
            return False
    datastore = params.get("datastore")
    if isinstance(datastore, str) and before["datastores"] != [datastore]:
        return False
    return all(disks.get(d["disk_key"]) == d["datastore"] for d in params.get("disks") or [])


def _relocate_answer(
    status: str,
    params: dict[str, Any],
    guidance: str,
    *,
    vm_name: str | None = None,
    before: dict[str, Any] | None = None,
    task: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "vm": str(params.get("vm")),
        "vm_name": vm_name,
        "task": task,
        "status_read": status_read(task),
        "before": before,
        "requested": _requested(params),
        "guidance": guidance,
    }


async def _relocate_precheck(
    connector: VmwareRestConnector, target: Any, operator: Operator, params: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None]:
    """``(early answer or None, VM name, placement before)``: the checks before a write."""
    vm = params.get("vm")
    problem = moid_problem(vm, VM_MOID_PATTERN, "vm") or placement_problem(params)
    if problem is not None:
        return _relocate_answer("invalid_request", params, problem), None, None
    assert isinstance(vm, str)
    props = await read_object(
        connector, target, operator, mo_type=VM_MO_TYPE, moid=vm, props=_RELOCATE_VM_PROPS
    )
    if props is None:
        guidance = f"no VM {vm!r}; pass a VM moid (vmware.vm.info finds it from the name)"
        return _relocate_answer("not_found", params, guidance), None, None
    name, before = as_str(props.get("name")), _before(props)
    disks = _disk_datastores(props.get("config.hardware.device"))
    unknown = sorted({d["disk_key"] for d in params.get("disks") or []} - set(disks))
    if unknown:
        guidance = (
            f"VM {vm!r} has no disk with key {unknown}; vmware.vm.hardware.show lists the "
            "disk keys. Nothing was sent."
        )
        return _relocate_answer("invalid_request", params, guidance, vm_name=name), name, before
    if _is_unchanged(params, before, disks):
        guidance = f"VM {vm!r} is already where the call asks. Nothing was sent."
        early = _relocate_answer("unchanged", params, guidance, vm_name=name, before=before)
        return early, name, before
    running = await running_task(
        connector, target, operator, props.get("recentTask"), _RELOCATE_DESCRIPTION_ID
    )
    if running is not None:
        guidance = (
            f"a move of VM {vm!r} ({running}) still runs. Nothing new was started. Follow it "
            "with vmware.task.info."
        )
        early = _relocate_answer(
            "already_running", params, guidance, vm_name=name, before=before, task=running
        )
        return early, name, before
    return None, name, before


async def vm_relocate_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Move one VM to another datastore, resource pool, folder or host.

    Op-id: ``vmware.composite.vm.relocate``. Steps:

    1. Check every moid, then read the VM and its recent tasks. An unknown VM
       is ``not_found``; an unknown disk key is ``invalid_request``; a VM that
       already is where the call asks is ``unchanged``; a move that still runs
       is ``already_running``. Nothing is sent in these cases.
    2. Send ``RelocateVM_Task`` through the governed vim seam (a parked or
       denied gate is returned as is). Answer ``accepted`` with the task id
       and the status read; no waiting.
    """
    early, vm_name, before = await _relocate_precheck(connector, target, operator, params)
    if early is not None:
        return early
    vm = params["vm"]
    body: dict[str, Any] = {"spec": build_relocate_spec(params)}
    priority = params.get("priority")
    if isinstance(priority, str):
        body["priority"] = priority
    gate, task = await send_vm_task(
        connector,
        target,
        operator,
        op_id=_OP_RELOCATE_VM_TASK,
        vmomi_path=f"/VirtualMachine/{vm}/RelocateVM_Task",
        body=body,
        gate_params={"vm": vm, **_requested(params)},
    )
    if gate is not None:
        return gate
    _log.info("vm_relocate_accepted", vm=vm, task=task)
    guidance = (
        f"vCenter accepted the move task {task}; the move is not done yet. Read "
        "vmware.task.info with this task id until it ends, then read the VM. Do not start again."
    )
    return _relocate_answer("accepted", params, guidance, vm_name=vm_name, before=before, task=task)


async def vm_relocate_preview_builder(ctx: PreviewContext) -> dict[str, Any] | None:
    """Approval card of ``vm.relocate``: the copy size of a datastore move, and the targets.

    Reuses the generic relocate preview with the spec the handler sends, so
    the approver sees how much disk data the move copies and where it goes.
    """
    vm = ctx.params.get("vm")
    if not isinstance(vm, str):
        return None
    inner = dataclasses.replace(
        ctx, params={"vm": vm, "body": {"spec": build_relocate_spec(ctx.params)}}
    )
    copy_preview = await vm_relocate_preview(inner)
    return {"action": "relocate_vm", "requested": _requested(ctx.params), **(copy_preview or {})}


register_preview_builder(VM_RELOCATE_OP_ID, vm_relocate_preview_builder)
