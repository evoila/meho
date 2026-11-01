# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schemas and agent guidance of ``vm.migrate_cross_vcenter``.

A vendor job: the answer says ``accepted`` with the vCenter task id, and the
caller follows the task with ``vmware.task.info``. The handler lives in
:mod:`._vm_cross_vcenter`.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites.schemas import (
    DATASTORE_MOID_PATTERN,
    HOST_MOID_PATTERN,
    VM_FOLDER_MOID_PATTERN,
    VM_MOID_PATTERN,
)

__all__ = [
    "NETWORK_MOID_PATTERN",
    "RESOURCE_POOL_MOID_PATTERN",
    "STATUSES",
    "VM_MIGRATE_CROSS_VCENTER_LLM_INSTRUCTIONS",
    "VM_MIGRATE_CROSS_VCENTER_PARAMETER_SCHEMA",
    "VM_MIGRATE_CROSS_VCENTER_RESPONSE_SCHEMA",
]

#: Resource pool moids: ``resgroup-8``, or ``resgroup-v12`` for a vApp.
RESOURCE_POOL_MOID_PATTERN: Final = r"^resgroup-v?[0-9]+$"
#: A standard network (``network-12``) or a distributed port group (``dvportgroup-34``).
NETWORK_MOID_PATTERN: Final = r"^(network|dvportgroup)-[0-9]+$"

#: Every ``status`` the answer can carry.
STATUSES: Final = [
    "accepted",
    "already_running",
    "not_found",
    "invalid_request",
    "certificate_mismatch",
    "destination_unreachable",
    "destination_login_failed",
]


def _moid(pattern: str, text: str) -> dict[str, Any]:
    return {"type": "string", "pattern": pattern, "description": text}


def _nullable(text: str) -> dict[str, Any]:
    return {"type": ["string", "null"], "description": text}


_NIC: Final[dict[str, Any]] = {
    "type": "object",
    "description": "One network card of the VM and the network it uses in the other vCenter.",
    "properties": {
        "nic_key": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "The device key of the network card, for example 4000 for the first one. "
                "vmware.vm.hardware.show lists the cards with their keys."
            ),
        },
        "network": _moid(
            NETWORK_MOID_PATTERN,
            "Moid of the network in the other vCenter: 'network-<n>' or 'dvportgroup-<n>'.",
        ),
    },
    "required": ["nic_key", "network"],
    "additionalProperties": False,
}

VM_MIGRATE_CROSS_VCENTER_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": _moid(
            VM_MOID_PATTERN,
            "Moid of the VM to move, in the vCenter of this call's target, e.g. 'vm-42'.",
        ),
        "destination_target": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Name of the MEHO target of the other vCenter (list_targets). Its address, "
                "its ssl_thumbprint and its Vault login are used; never pass a login here."
            ),
        },
        "host": _moid(HOST_MOID_PATTERN, "Moid of the host in the other vCenter, e.g. 'host-21'."),
        "resource_pool": _moid(
            RESOURCE_POOL_MOID_PATTERN,
            "Moid of the resource pool in the other vCenter, e.g. 'resgroup-8'.",
        ),
        "datastore": _moid(
            DATASTORE_MOID_PATTERN,
            "Moid of the datastore in the other vCenter for the VM files and disks.",
        ),
        "folder": _moid(
            VM_FOLDER_MOID_PATTERN, "Moid of the VM folder in the other vCenter, e.g. 'group-v3'."
        ),
        "network": _moid(
            NETWORK_MOID_PATTERN,
            "Moid of the network in the other vCenter for every network card not named in "
            "nics. Needed when the VM has a network card.",
        ),
        "nics": {
            "type": "array",
            "minItems": 1,
            "items": _NIC,
            "description": "Network cards that use their own network. Others use network.",
        },
        "priority": {
            "type": "string",
            "enum": ["defaultPriority", "highPriority", "lowPriority"],
            "default": "defaultPriority",
            "description": "The vCenter priority of the move task. Default defaultPriority.",
        },
    },
    "required": ["vm", "destination_target", "host", "resource_pool", "datastore", "folder"],
    "additionalProperties": False,
    "examples": [
        {
            "vm": "vm-42",
            "destination_target": "<other-vcenter-target>",
            "host": "host-21",
            "resource_pool": "resgroup-8",
            "datastore": "datastore-12",
            "folder": "group-v3",
            "network": "dvportgroup-34",
        }
    ],
}

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

VM_MIGRATE_CROSS_VCENTER_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": STATUSES,
            "description": (
                "accepted: vCenter took the move task; the move is not done yet. "
                "already_running: a move of this VM still runs; nothing new started. "
                "not_found / invalid_request: nothing sent; guidance says why. "
                "certificate_mismatch: the other vCenter shows another certificate than "
                "the ssl_thumbprint of its target; nothing sent. "
                "destination_unreachable / destination_login_failed: MEHO could not reach "
                "or log in to the other vCenter; nothing sent."
            ),
        },
        "vm": {"type": "string", "description": "The VM moid of the call."},
        "vm_name": _nullable("The VM name."),
        "destination_target": {"type": "string", "description": "The other vCenter's target."},
        "task": _nullable("The vCenter task id of the move (accepted, already_running)."),
        "status_read": _STATUS_READ,
        "requested": {
            "type": "object",
            "description": "Where the call moves the VM: host, pool, datastore, folder, networks.",
        },
        "missing": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The objects the other vCenter does not have (not_found).",
        },
        "guidance": {"type": "string", "description": "What happened and what to do next."},
    },
    "required": ["status", "vm", "destination_target", "task", "status_read", "guidance"],
}

VM_MIGRATE_CROSS_VCENTER_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to move a VM, also while it runs, to another vCenter (advanced cross vCenter "
        "vMotion), for example before a vCenter is retired. For a move inside one vCenter, "
        "use vm.migrate."
    ),
    "preconditions": (
        "The other vCenter is its own MEHO target with a Vault login and an ssl_thumbprint "
        "(the SHA-1 hash of its certificate) in its extras. host, resource_pool, "
        "datastore, folder and network are moids in the other vCenter: read them there "
        "(vmware.cluster.list, vmware.resource_pool.list, vmware.folder.list, "
        "vmware.network.list with the other target). The call waits for approval. Use async "
        "mode (async: true) for the start, so MEHO keeps the answer with the task id under a "
        "run handle even if the reply is lost."
    ),
    "parameter_hints": {
        "destination_target": "The target name, not its address. Never pass a password.",
        "network": "Needed when the VM has a network card; nics overrides it per card.",
    },
    "output_shape": (
        "{status: accepted|already_running|not_found|invalid_request|certificate_mismatch|"
        "destination_unreachable|destination_login_failed, vm, vm_name, destination_target, "
        "task, status_read: {op_id: vmware.task.info, params: {task}}, requested, missing, "
        "guidance}."
    ),
    "next_step": (
        "accepted or already_running: call vmware.task.info on this call's target with the "
        "task id until done is true, then find the VM in the other vCenter (vmware.vm.info "
        "there). After a lost reply, do not start again: read vmware.tasks.recent or the VM "
        "first; a move that still runs is returned as already_running."
    ),
}
