# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VM CPU and memory allocation: show and set.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites._vm_allocation import (
    vm_resource_allocation_set_composite,
    vm_resource_allocation_show_composite,
)
from meho_backplane.connectors.vmware_rest.composites._vm_llm_instructions import (
    VM_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._write_preview import (
    _vm_resource_allocation_set_preview,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    VM_RESOURCE_ALLOCATION_SET_PARAMETER_SCHEMA,
    VM_RESOURCE_ALLOCATION_SET_RESPONSE_SCHEMA,
    VM_RESOURCE_ALLOCATION_SHOW_PARAMETER_SCHEMA,
    VM_RESOURCE_ALLOCATION_SHOW_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.vm.resource_allocation.show",
        handler=vm_resource_allocation_show_composite,
        summary="Show a VM's CPU / memory limit, reservation and shares.",
        description=(
            "Shows a VM's CPU and memory limit, reservation and shares (CPU in MHz, "
            "memory in MB; limit -1 means unlimited). Read-only. vmware.vm.info "
            "does not show these fields."
        ),
        parameter_schema=VM_RESOURCE_ALLOCATION_SHOW_PARAMETER_SCHEMA,
        response_schema=VM_RESOURCE_ALLOCATION_SHOW_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "read-only", "vm", "resource-allocation", "vi-json"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.resource_allocation.show"],
        classifier_other=True,
    ),
    CompositeSpec(
        op_id="vmware.composite.vm.resource_allocation.set",
        handler=vm_resource_allocation_set_composite,
        summary="Set or clear a VM's CPU / memory limit and reservation.",
        description=(
            "Sets or clears a VM's CPU and memory limit and reservation. Only the "
            "fields you pass change; -1 removes a limit. Bad values are refused "
            "before any change. Takes effect at once and can be undone."
        ),
        parameter_schema=VM_RESOURCE_ALLOCATION_SET_PARAMETER_SCHEMA,
        response_schema=VM_RESOURCE_ALLOCATION_SET_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "resource-allocation", "vi-json"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.resource_allocation.set"],
        preview=_vm_resource_allocation_set_preview,
        classifier_other=True,
    ),
)
