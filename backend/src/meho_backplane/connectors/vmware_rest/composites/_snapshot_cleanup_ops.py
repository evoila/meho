# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VM snapshot delete_all and disks consolidate.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites._snapshot_cleanup import (
    vm_disks_consolidate_composite,
    vm_disks_consolidate_preview,
    vm_snapshot_delete_all_composite,
    vm_snapshot_delete_all_preview,
)
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites._vm_llm_instructions import (
    VM_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    VM_DISKS_CONSOLIDATE_PARAMETER_SCHEMA,
    VM_DISKS_CONSOLIDATE_RESPONSE_SCHEMA,
    VM_SNAPSHOT_DELETE_ALL_PARAMETER_SCHEMA,
    VM_SNAPSHOT_DELETE_ALL_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.vm.snapshot.delete_all",
        handler=vm_snapshot_delete_all_composite,
        summary="Delete all snapshots of one VM; their data is merged into the disks.",
        description=(
            "Deletes every snapshot of one VM. The VM keeps its present state; all restore "
            "points are gone for good. A second person always approves, after a preview. "
            "Like 'govc snapshot.remove '*''."
        ),
        parameter_schema=VM_SNAPSHOT_DELETE_ALL_PARAMETER_SCHEMA,
        response_schema=VM_SNAPSHOT_DELETE_ALL_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "snapshot", "vi-json"],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.snapshot.delete_all"],
        # Destructive: the blast radius is the handler's plan (every snapshot of the VM).
        preview=vm_snapshot_delete_all_preview,
    ),
    CompositeSpec(
        op_id="vmware.composite.vm.disks.consolidate",
        handler=vm_disks_consolidate_composite,
        summary="Consolidate the disks of a VM that vCenter says need consolidation.",
        description=(
            "Merges left-over snapshot delta files of one VM into its disks and waits for the "
            "vCenter task. Runs only when vCenter reports that the VM needs it; otherwise "
            "nothing changes. Like 'Consolidate' in the vSphere Client."
        ),
        parameter_schema=VM_DISKS_CONSOLIDATE_PARAMETER_SCHEMA,
        response_schema=VM_DISKS_CONSOLIDATE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "snapshot", "disk", "vi-json"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.disks.consolidate"],
        preview=vm_disks_consolidate_preview,
    ),
)
