# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registry rows of the VM move and copy jobs (``vm.relocate``, ``vm.clone_from_vm``).

``_register.py`` splats :data:`VM_MOVE_COPY_COMPOSITES` into its
``_COMPOSITES`` table (so both catalogs, ``vmware-rest-8.0`` and
``vmware-rest-9.0``, get them), and ``_governed_subops.py`` reads
:data:`VM_MOVE_COPY_GOVERNED_SUBOPS`. Both are ``dangerous`` with
``requires_approval=True``. vCenter 8.0 U1 to 9.1.1 all have both VIM
methods, so no version check is needed.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites import (
    _vm_clone_from_vm,
    _vm_relocate,
)
from meho_backplane.connectors.vmware_rest.composites._vm_move_copy_schemas import (
    VM_CLONE_FROM_VM_LLM_INSTRUCTIONS,
    VM_CLONE_FROM_VM_PARAMETER_SCHEMA,
    VM_CLONE_FROM_VM_RESPONSE_SCHEMA,
    VM_RELOCATE_LLM_INSTRUCTIONS,
    VM_RELOCATE_PARAMETER_SCHEMA,
    VM_RELOCATE_RESPONSE_SCHEMA,
)

__all__ = ["VM_MOVE_COPY_COMPOSITES", "VM_MOVE_COPY_GOVERNED_SUBOPS"]

VM_MOVE_COPY_COMPOSITES: Final[tuple[dict[str, Any], ...]] = (
    {
        "op_id": _vm_relocate.VM_RELOCATE_OP_ID,
        "handler": _vm_relocate.vm_relocate_composite,
        "summary": "Move a VM, also while it runs, to another datastore, pool, folder or host.",
        "description": (
            "Starts a move of one VM inside this vCenter (RelocateVM_Task): Storage vMotion "
            "to another datastore (each disk can go to its own), or a move to another "
            "resource pool, folder or host. Answers accepted with the task id; "
            "vmware.task.info follows it."
        ),
        "parameter_schema": VM_RELOCATE_PARAMETER_SCHEMA,
        "response_schema": VM_RELOCATE_RESPONSE_SCHEMA,
        "group_key": "vm",
        "tags": ["composite", "write", "vm", "migration", "vi-json"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": VM_RELOCATE_LLM_INSTRUCTIONS,
    },
    {
        "op_id": _vm_clone_from_vm.VM_CLONE_FROM_VM_OP_ID,
        "handler": _vm_clone_from_vm.vm_clone_from_vm_composite,
        "summary": "Copy a VM, on or off, to a new VM: a full copy or a linked copy.",
        "description": (
            "Starts a copy of one VM inside this vCenter (CloneVM_Task): a full copy of "
            "every disk, or a linked copy that shares the disks of a snapshot. Answers "
            "accepted with the task id; vmware.task.info follows it."
        ),
        "parameter_schema": VM_CLONE_FROM_VM_PARAMETER_SCHEMA,
        "response_schema": VM_CLONE_FROM_VM_RESPONSE_SCHEMA,
        "group_key": "vm",
        "tags": ["composite", "write", "vm", "clone", "vi-json"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": VM_CLONE_FROM_VM_LLM_INSTRUCTIONS,
    },
)

#: composite op_id -> its governed child ops (the vim manifests).
VM_MOVE_COPY_GOVERNED_SUBOPS: Final[dict[str, tuple[str, ...]]] = {
    _vm_relocate.VM_RELOCATE_OP_ID: _vm_relocate._VIM_SUB_OPS_VM_RELOCATE,
    _vm_clone_from_vm.VM_CLONE_FROM_VM_OP_ID: _vm_clone_from_vm._VIM_SUB_OPS_VM_CLONE_FROM_VM,
}
