# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VM snapshot create and delete.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites._snapshot import (
    vm_snapshot_create_composite,
    vm_snapshot_create_preview,
    vm_snapshot_delete_composite,
    vm_snapshot_delete_preview,
)
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites._vm_llm_instructions import (
    VM_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    VM_SNAPSHOT_CREATE_PARAMETER_SCHEMA,
    VM_SNAPSHOT_CREATE_RESPONSE_SCHEMA,
    VM_SNAPSHOT_DELETE_PARAMETER_SCHEMA,
    VM_SNAPSHOT_DELETE_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.vm.snapshot.create",
        handler=vm_snapshot_create_composite,
        summary="Take a snapshot of one VM: name, description, with memory or not, quiesce or not.",
        description=(
            "Takes a snapshot of one VM and waits for the vCenter task. Returns the new "
            "snapshot id. Disks only unless memory is set. When the VM already has a "
            "snapshot with that name, nothing is created. Like 'govc snapshot.create'."
        ),
        parameter_schema=VM_SNAPSHOT_CREATE_PARAMETER_SCHEMA,
        response_schema=VM_SNAPSHOT_CREATE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "snapshot", "vi-json"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.snapshot.create"],
        preview=vm_snapshot_create_preview,
    ),
    CompositeSpec(
        op_id="vmware.composite.vm.snapshot.delete",
        handler=vm_snapshot_delete_composite,
        summary="Delete one VM snapshot by id; its child snapshots stay unless asked.",
        description=(
            "Deletes one snapshot of a VM, by id. The VM keeps its present state; the "
            "restore point is gone for good. Child snapshots stay unless remove_children "
            "is set. A second person always approves, after a preview. Like 'govc "
            "snapshot.remove'."
        ),
        parameter_schema=VM_SNAPSHOT_DELETE_PARAMETER_SCHEMA,
        response_schema=VM_SNAPSHOT_DELETE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "snapshot", "vi-json"],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.snapshot.delete"],
        preview=vm_snapshot_delete_preview,
    ),
)
