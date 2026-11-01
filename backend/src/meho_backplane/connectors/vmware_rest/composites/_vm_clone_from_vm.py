# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vm.clone_from_vm`` -- copy a VM inside one vCenter (``VirtualMachine.CloneVM_Task``).

Copies a VM, on or off: a full copy, or a linked copy whose new child disks
build on a snapshot of the source (``diskMoveType=createNewChildDiskBacking``,
the way ``govc vm.clone -link`` does it). A vendor job: the answer says
``accepted`` with the vCenter task id, and ``vmware.task.info`` follows it.
A VM with the name in the target folder is returned (``already_exists``), and a
copy of the same source that still runs is returned (``already_running``);
nothing new starts then.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import structlog

from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moid_problem,
    moref_values,
    read_names,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._vm_job_common import (
    FOLDER_MO_TYPE,
    PLACEMENT_FIELDS,
    VM_MO_TYPE,
    as_str,
    moref_id,
    placement_problem,
    running_task,
    send_vm_task,
    status_read,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_CLONE_VM_TASK,
    _OP_RETRIEVE_PROPERTIES,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    VM_MOID_PATTERN,
    VM_SNAPSHOT_MOID_PATTERN,
)
from meho_backplane.connectors.vmware_rest.snapshot_tree import snapshot_rows
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY, vim_moref
from meho_backplane.operations._preview import register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "VM_CLONE_FROM_VM_OP_ID",
    "build_clone_body",
    "vm_clone_from_vm_composite",
    "vm_clone_from_vm_preview",
]

_log = structlog.get_logger(__name__)

VM_CLONE_FROM_VM_OP_ID: Final = "vmware.composite.vm.clone_from_vm"
#: vi-json sub-op manifest: the reads, then the one write.
_VIM_SUB_OPS_VM_CLONE_FROM_VM: Final[tuple[str, ...]] = (_OP_RETRIEVE_PROPERTIES, _OP_CLONE_VM_TASK)
#: ``TaskInfo.descriptionId`` of a copy (``<Type>.<method>``).
_CLONE_DESCRIPTION_ID: Final = "VirtualMachine.clone"
_RELOCATE_SPEC_TYPE: Final = "VirtualMachineRelocateSpec"
_CLONE_SPEC_TYPE: Final = "VirtualMachineCloneSpec"
_LINKED_DISK_MOVE_TYPE: Final = "createNewChildDiskBacking"
_CLONE_VM_PROPS: Final = ["name", "parent", "config.template", "snapshot", "recentTask"]


def build_clone_body(params: dict[str, Any], folder: str) -> dict[str, Any]:
    """The ``CloneVM_Task`` body: ``{folder, name, spec: VirtualMachineCloneSpec}``."""
    location: dict[str, Any] = {VIM_TYPE_NAME_KEY: _RELOCATE_SPEC_TYPE}
    for name, field, mo_type, _pattern in PLACEMENT_FIELDS:
        if name != "folder" and isinstance(params.get(name), str):
            location[field] = vim_moref(mo_type, params[name])
    linked = params.get("clone_type") == "linked"
    if linked:
        location["diskMoveType"] = _LINKED_DISK_MOVE_TYPE
    spec: dict[str, Any] = {
        VIM_TYPE_NAME_KEY: _CLONE_SPEC_TYPE,
        "location": location,
        "template": False,
        "powerOn": bool(params.get("power_on", False)),
    }
    if isinstance(params.get("snapshot"), str):
        spec["snapshot"] = vim_moref("VirtualMachineSnapshot", params["snapshot"])
    return {"folder": vim_moref(FOLDER_MO_TYPE, folder), "name": params["name"], "spec": spec}


def _clone_answer(
    status: str,
    params: dict[str, Any],
    guidance: str,
    *,
    source_vm_name: str | None = None,
    folder: str | None = None,
    existing_vm: str | None = None,
    task: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "source_vm": str(params.get("source_vm")),
        "source_vm_name": source_vm_name,
        "name": str(params.get("name")),
        "clone_type": "linked" if params.get("clone_type") == "linked" else "full",
        "snapshot": as_str(params.get("snapshot")),
        "folder": folder,
        "existing_vm": existing_vm,
        "task": task,
        "status_read": status_read(task),
        "guidance": guidance,
    }


def _clone_shape_problem(params: dict[str, Any]) -> str | None:
    problem = moid_problem(params.get("source_vm"), VM_MOID_PATTERN, "source_vm")
    problem = problem or placement_problem(params)
    snapshot = params.get("snapshot")
    if problem is None and snapshot is not None:
        problem = moid_problem(snapshot, VM_SNAPSHOT_MOID_PATTERN, "snapshot")
    if problem is None and params.get("clone_type") == "linked" and snapshot is None:
        problem = "a linked copy needs snapshot (vmware.snapshot.list lists them)"
    return problem


def _source_problem(params: dict[str, Any], props: dict[str, Any]) -> str | None:
    """Why the source VM cannot be copied as asked, else ``None``."""
    source = params["source_vm"]
    if props.get("config.template") is True:
        return f"{source!r} is a template; copy it with vmware.composite.vm.clone_from_template"
    snapshot = params.get("snapshot")
    if isinstance(snapshot, str):
        _current, rows = snapshot_rows(props.get("snapshot"))
        if snapshot not in {row["snapshot_id"] for row in rows}:
            return f"VM {source!r} has no snapshot {snapshot!r}; vmware.snapshot.list lists them"
    return None


async def _named_vm_in_folder(
    connector: VmwareRestConnector, target: Any, operator: Operator, folder: str, name: str
) -> tuple[bool, str | None]:
    """``(folder exists, moid of a VM named *name* in it)``."""
    props = await read_object(
        connector, target, operator, mo_type=FOLDER_MO_TYPE, moid=folder, props=["childEntity"]
    )
    if props is None:
        return False, None
    vms = [moid for kind, moid in moref_values(props.get("childEntity")) if kind == VM_MO_TYPE]
    names = await read_names(connector, target, operator, mo_type=VM_MO_TYPE, moids=vms)
    return True, next((moid for moid, vm_name in names.items() if vm_name == name), None)


async def _clone_precheck(
    connector: VmwareRestConnector, target: Any, operator: Operator, params: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """``(early answer or None, source name, target folder)``: the checks before a write."""
    problem = _clone_shape_problem(params)
    if problem is not None:
        return _clone_answer("invalid_request", params, problem), None, None
    source = params["source_vm"]
    props = await read_object(
        connector, target, operator, mo_type=VM_MO_TYPE, moid=source, props=_CLONE_VM_PROPS
    )
    if props is None:
        guidance = f"no VM {source!r}; pass a VM moid (vmware.vm.info finds it from the name)"
        return _clone_answer("not_found", params, guidance), None, None
    name = as_str(props.get("name"))
    folder = as_str(params.get("folder")) or moref_id(props.get("parent"))
    problem = _source_problem(params, props)
    if problem is None and (folder is None or not folder.startswith("group-v")):
        problem = f"VM {source!r} is not in a VM folder (for example in a vApp); pass folder"
    if problem is not None:
        return _clone_answer("invalid_request", params, problem, source_vm_name=name), name, None
    assert folder is not None
    found, existing = await _named_vm_in_folder(connector, target, operator, folder, params["name"])
    if not found:
        guidance = f"no folder {folder!r}; vmware.folder.list lists the VM folders"
        return _clone_answer("invalid_request", params, guidance, source_vm_name=name), name, None
    if existing is not None:
        guidance = (
            f"folder {folder!r} already has a VM named {params['name']!r} ({existing}). "
            "Nothing was sent. If it is the copy of an earlier call, use it; else pick "
            "another name."
        )
        early = _clone_answer(
            "already_exists",
            params,
            guidance,
            source_vm_name=name,
            folder=folder,
            existing_vm=existing,
        )
        return early, name, folder
    running = await running_task(
        connector, target, operator, props.get("recentTask"), _CLONE_DESCRIPTION_ID
    )
    if running is not None:
        guidance = (
            f"a copy of VM {source!r} ({running}) still runs. Nothing new was started. Follow "
            "it with vmware.task.info, then start the next copy."
        )
        early = _clone_answer(
            "already_running", params, guidance, source_vm_name=name, folder=folder, task=running
        )
        return early, name, folder
    return None, name, folder


async def vm_clone_from_vm_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Copy one VM (on or off) to a new VM: a full or a linked copy.

    Op-id: ``vmware.composite.vm.clone_from_vm``. Steps:

    1. Check every moid, then read the source VM, the target folder and the
       source's recent tasks. An unknown VM is ``not_found``; a template, an
       unknown snapshot or an unknown folder is ``invalid_request``; a VM with
       the name in the folder is ``already_exists``; a copy of the source that
       still runs is ``already_running``. Nothing is sent in these cases.
    2. Send ``CloneVM_Task`` through the governed vim seam (a parked or denied
       gate is returned as is). Answer ``accepted`` with the task id and the
       status read; no waiting.
    """
    early, source_name, folder = await _clone_precheck(connector, target, operator, params)
    if early is not None:
        return early
    assert folder is not None
    source = params["source_vm"]
    body = build_clone_body(params, folder)
    gate_params: dict[str, Any] = {
        "source_vm": source,
        "name": params["name"],
        "clone_type": "linked" if params.get("clone_type") == "linked" else "full",
        "snapshot": as_str(params.get("snapshot")),
        "folder": folder,
        **{n: params[n] for n in ("resource_pool", "datastore", "host") if n in params},
        "power_on": bool(params.get("power_on", False)),
    }
    gate, task = await send_vm_task(
        connector,
        target,
        operator,
        op_id=_OP_CLONE_VM_TASK,
        vmomi_path=f"/VirtualMachine/{source}/CloneVM_Task",
        body=body,
        gate_params=gate_params,
    )
    if gate is not None:
        return gate
    _log.info("vm_clone_from_vm_accepted", source_vm=source, task=task)
    guidance = (
        f"vCenter accepted the copy task {task}; the new VM is not ready yet. Read "
        "vmware.task.info with this task id until it ends, then find the new VM by name. "
        "Do not start again."
    )
    return _clone_answer(
        "accepted", params, guidance, source_vm_name=source_name, folder=folder, task=task
    )


async def vm_clone_from_vm_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Approval card of ``vm.clone_from_vm``: what is copied, how, and where (no I/O)."""
    source = ctx.params.get("source_vm")
    name = ctx.params.get("name")
    if not isinstance(source, str) or not isinstance(name, str):
        return None
    return {
        "action": "clone_vm",
        "source_vm": source,
        "name": name,
        "clone_type": "linked" if ctx.params.get("clone_type") == "linked" else "full",
        "snapshot": as_str(ctx.params.get("snapshot")),
        **{n: as_str(ctx.params.get(n)) for n in ("folder", "resource_pool", "datastore", "host")},
        "power_on": bool(ctx.params.get("power_on", False)),
    }


register_preview_builder(VM_CLONE_FROM_VM_OP_ID, vm_clone_from_vm_preview)
