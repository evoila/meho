# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schemas and agent guidance of ``vm.relocate`` and ``vm.clone_from_vm``.

Both are vendor jobs: the answer says ``accepted`` with the vCenter task id,
and the caller follows the task with ``vmware.task.info``. The handlers live
in :mod:`._vm_move_copy`.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites.schemas import (
    DATASTORE_MOID_PATTERN,
    HOST_MOID_PATTERN,
    VM_FOLDER_MOID_PATTERN,
    VM_MOID_PATTERN,
    VM_SNAPSHOT_MOID_PATTERN,
)

__all__ = [
    "RESOURCE_POOL_MOID_PATTERN",
    "VM_CLONE_FROM_VM_LLM_INSTRUCTIONS",
    "VM_CLONE_FROM_VM_PARAMETER_SCHEMA",
    "VM_CLONE_FROM_VM_RESPONSE_SCHEMA",
    "VM_RELOCATE_LLM_INSTRUCTIONS",
    "VM_RELOCATE_PARAMETER_SCHEMA",
    "VM_RELOCATE_RESPONSE_SCHEMA",
]

#: Resource pool moids: ``resgroup-8``, or ``resgroup-v12`` for a vApp.
RESOURCE_POOL_MOID_PATTERN: Final = r"^resgroup-v?[0-9]+$"

#: The task states of a job that still runs, and the ones the answer can carry.
_STATUSES_RELOCATE: Final = [
    "accepted",
    "already_running",
    "unchanged",
    "not_found",
    "invalid_request",
]
_STATUSES_CLONE: Final = [
    "accepted",
    "already_running",
    "already_exists",
    "not_found",
    "invalid_request",
]


def _moid(pattern: str, text: str) -> dict[str, Any]:
    return {"type": "string", "pattern": pattern, "description": text}


def _nullable(text: str) -> dict[str, Any]:
    return {"type": ["string", "null"], "description": text}


_STATUS_READ: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "description": (
        "The read that follows the job: call op_id with params until the task ends. "
        "Null when nothing was started and no job runs."
    ),
    "properties": {
        "op_id": {"type": "string", "description": "Always vmware.task.info."},
        "params": {
            "type": "object",
            "description": "The parameters of the status read.",
            "properties": {"task": {"type": "string", "description": "The vCenter task id."}},
        },
    },
}

_DISK_LOCATOR: Final[dict[str, Any]] = {
    "type": "object",
    "description": "One disk of the VM and the datastore it moves to.",
    "properties": {
        "disk_key": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "The device key of the disk, for example 2000 for the first disk. "
                "vmware.vm.hardware.show lists the disks with their keys."
            ),
        },
        "datastore": _moid(
            DATASTORE_MOID_PATTERN, "Moid of the datastore the disk moves to, e.g. 'datastore-12'."
        ),
    },
    "required": ["disk_key", "datastore"],
    "additionalProperties": False,
}

VM_RELOCATE_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": _moid(
            VM_MOID_PATTERN,
            "Moid of the VM to move, e.g. 'vm-42'. Not the name: vmware.vm.info finds the moid.",
        ),
        "datastore": _moid(
            DATASTORE_MOID_PATTERN,
            "Moid of the datastore for the VM files and every disk not named in disks "
            "(Storage vMotion when the VM runs).",
        ),
        "disks": {
            "type": "array",
            "minItems": 1,
            "items": _DISK_LOCATOR,
            "description": "Disks that go to their own datastore. Other disks follow datastore.",
        },
        "resource_pool": _moid(
            RESOURCE_POOL_MOID_PATTERN,
            "Moid of the resource pool the VM moves to, e.g. 'resgroup-8'.",
        ),
        "folder": _moid(
            VM_FOLDER_MOID_PATTERN, "Moid of the VM folder the VM moves to, e.g. 'group-v3'."
        ),
        "host": _moid(
            HOST_MOID_PATTERN,
            "Moid of the host the VM runs on after the move, e.g. 'host-21'. Optional: "
            "without it, vCenter keeps the host or DRS picks one.",
        ),
        "priority": {
            "type": "string",
            "enum": ["defaultPriority", "highPriority", "lowPriority"],
            "default": "defaultPriority",
            "description": "The vCenter priority of the move task. Default defaultPriority.",
        },
    },
    "required": ["vm"],
    "anyOf": [
        {"required": ["datastore"]},
        {"required": ["disks"]},
        {"required": ["resource_pool"]},
        {"required": ["folder"]},
        {"required": ["host"]},
    ],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42", "datastore": "datastore-12", "resource_pool": "resgroup-8"}],
}

_PLACEMENT: Final[dict[str, Any]] = {
    "type": "object",
    "description": "Where the VM is: before the call, or where the call moves it.",
    "properties": {
        "power_state": _nullable("poweredOn, poweredOff or suspended."),
        "host": _nullable("Host moid."),
        "resource_pool": _nullable("Resource pool moid."),
        "folder": _nullable("VM folder moid."),
        "datastores": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Datastore moids the VM files and disks are on.",
        },
    },
}

VM_RELOCATE_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": _STATUSES_RELOCATE,
            "description": (
                "accepted: vCenter took the move task; the move is not done yet. "
                "already_running: a move of this VM still runs; nothing new started. "
                "unchanged: the VM is already where the call asks; nothing sent. "
                "not_found / invalid_request: nothing sent; guidance says why."
            ),
        },
        "vm": {"type": "string", "description": "The VM moid of the call."},
        "vm_name": _nullable("The VM name."),
        "task": _nullable("The vCenter task id of the move (accepted, already_running)."),
        "status_read": _STATUS_READ,
        "before": {**_PLACEMENT, "type": ["object", "null"]},
        "requested": {
            "type": "object",
            "description": "The placement the call asked for (only the fields it named).",
        },
        "guidance": {"type": "string", "description": "What happened and what to do next."},
    },
    "required": ["status", "vm", "task", "status_read", "guidance"],
}

VM_CLONE_FROM_VM_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "source_vm": _moid(
            VM_MOID_PATTERN,
            "Moid of the VM to copy, on or off, e.g. 'vm-42'. Not a template: use "
            "vm.clone_from_template for that.",
        ),
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80,
            "description": "Name of the new VM. It must be free in the target folder.",
        },
        "clone_type": {
            "type": "string",
            "enum": ["full", "linked"],
            "default": "full",
            "description": (
                "full: copy every disk. linked: the new VM shares the disks of the snapshot "
                "and writes its changes to new child disks (fast, small, needs snapshot)."
            ),
        },
        "snapshot": _moid(
            VM_SNAPSHOT_MOID_PATTERN,
            "Moid of a snapshot of source_vm, e.g. 'snapshot-7'. Needed for linked; for "
            "full, the copy is of that snapshot state instead of the current state.",
        ),
        "folder": _moid(
            VM_FOLDER_MOID_PATTERN,
            "Moid of the VM folder for the new VM. Default: the folder of source_vm.",
        ),
        "resource_pool": _moid(
            RESOURCE_POOL_MOID_PATTERN,
            "Moid of the resource pool for the new VM. Default: the pool of source_vm.",
        ),
        "datastore": _moid(
            DATASTORE_MOID_PATTERN,
            "Moid of the datastore for the new VM. Default: the datastores of source_vm.",
        ),
        "host": _moid(HOST_MOID_PATTERN, "Moid of the host for the new VM. Optional."),
        "power_on": {
            "type": "boolean",
            "default": False,
            "description": "Power on the new VM when the copy is done. Default false.",
        },
    },
    "required": ["source_vm", "name"],
    "anyOf": [
        {
            "description": "A full copy: snapshot is optional.",
            "properties": {
                "clone_type": {"const": "full", "description": "A full copy of every disk."}
            },
        },
        {
            "description": "A linked copy: snapshot is required.",
            "properties": {
                "clone_type": {"const": "linked", "description": "A linked copy on a snapshot."}
            },
            "required": ["snapshot"],
        },
    ],
    "additionalProperties": False,
    "examples": [{"source_vm": "vm-42", "name": "<new-vm-name>", "folder": "group-v3"}],
}

VM_CLONE_FROM_VM_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": _STATUSES_CLONE,
            "description": (
                "accepted: vCenter took the copy task; the new VM is not ready yet. "
                "already_running: a copy of this VM still runs; nothing new started. "
                "already_exists: the folder has a VM with this name; nothing sent. "
                "not_found / invalid_request: nothing sent; guidance says why."
            ),
        },
        "source_vm": {"type": "string", "description": "The source VM moid of the call."},
        "source_vm_name": _nullable("The source VM name."),
        "name": {"type": "string", "description": "The name of the new VM."},
        "clone_type": {"type": "string", "enum": ["full", "linked"], "description": "The type."},
        "snapshot": _nullable("The snapshot the copy is made from, if any."),
        "folder": _nullable("The folder of the new VM."),
        "existing_vm": _nullable("The moid of the VM that has the name (already_exists)."),
        "task": _nullable("The vCenter task id of the copy (accepted, already_running)."),
        "status_read": _STATUS_READ,
        "guidance": {"type": "string", "description": "What happened and what to do next."},
    },
    "required": ["status", "source_vm", "name", "task", "status_read", "guidance"],
}

_ASYNC_HINT: Final = (
    "Use async mode (async: true) for the start, so MEHO keeps the answer with the task id "
    "under a run handle even if the reply is lost."
)

VM_RELOCATE_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to move a VM, also while it runs, to another datastore (Storage vMotion; each "
        "disk can go to its own datastore), resource pool, folder or host of the same "
        "vCenter. For a host move inside one cluster with DRS, vm.migrate also works."
    ),
    "preconditions": (
        "vm and every target are moids (vmware.vm.info, vmware.resource_pool.list, "
        "vmware.folder.list, vmware.cluster.list find them). The call waits for approval; "
        "the approval card shows how much disk data a datastore move copies. " + _ASYNC_HINT
    ),
    "parameter_hints": {
        "disks": "Only for disks that go to another datastore than the VM files.",
        "host": "Leave out to keep the host, or let DRS pick one.",
    },
    "output_shape": (
        "{status: accepted|already_running|unchanged|not_found|invalid_request, vm, vm_name, "
        "task, status_read: {op_id: vmware.task.info, params: {task}}, before: {power_state, "
        "host, resource_pool, folder, datastores}, requested, guidance}."
    ),
    "next_step": (
        "accepted or already_running: call vmware.task.info with the task id until done is "
        "true, then read the VM (vmware.vm.info). After a lost reply, do not start again: "
        "read vmware.tasks.recent or the VM first; a move that still runs is returned as "
        "already_running."
    ),
}

VM_CLONE_FROM_VM_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to copy a VM (on or off) to a new VM in the same vCenter: a full copy, or a "
        "linked copy that shares the disks of a snapshot. For a template, use "
        "vm.clone_from_template; for a content library item, vm.clone."
    ),
    "preconditions": (
        "source_vm and every target are moids. A linked copy needs a snapshot of source_vm "
        "(vmware.snapshot.list; vm.snapshot.create makes one). The name must be free in the "
        "folder. The call waits for approval. " + _ASYNC_HINT
    ),
    "parameter_hints": {
        "clone_type": "linked is fast and small, but the new VM needs the source disks.",
        "snapshot": "Required for linked; optional for full.",
    },
    "output_shape": (
        "{status: accepted|already_running|already_exists|not_found|invalid_request, "
        "source_vm, source_vm_name, name, clone_type, snapshot, folder, existing_vm, task, "
        "status_read: {op_id: vmware.task.info, params: {task}}, guidance}."
    ),
    "next_step": (
        "accepted or already_running: call vmware.task.info with the task id until done is "
        "true, then find the new VM by name (vmware.vm.info). After a lost reply, do not "
        "start again: read vmware.task.info or look for the name first; a VM with the name "
        "is returned as already_exists."
    ),
}
