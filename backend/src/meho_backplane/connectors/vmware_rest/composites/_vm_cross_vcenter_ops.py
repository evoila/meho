# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registry row, approval card and child-op list of ``vm.migrate_cross_vcenter``.

``_register.py`` splats :data:`VM_CROSS_VCENTER_COMPOSITES` into its
``_COMPOSITES`` table (so both catalogs, ``vmware-rest-8.0`` and
``vmware-rest-9.0``, get it), and ``_governed_subops.py`` reads
:data:`VM_CROSS_VCENTER_GOVERNED_SUBOPS`. vCenter 8.0 U1 to 9.1.1 all have
the VIM methods, so no version check is needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites import _vm_cross_vcenter
from meho_backplane.connectors.vmware_rest.composites._vm_cross_vcenter_schemas import (
    VM_MIGRATE_CROSS_VCENTER_LLM_INSTRUCTIONS,
    VM_MIGRATE_CROSS_VCENTER_PARAMETER_SCHEMA,
    VM_MIGRATE_CROSS_VCENTER_RESPONSE_SCHEMA,
)
from meho_backplane.operations._preview import register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "VM_CROSS_VCENTER_COMPOSITES",
    "VM_CROSS_VCENTER_GOVERNED_SUBOPS",
    "vm_migrate_cross_vcenter_preview",
]

_OP_ID: Final = _vm_cross_vcenter.VM_MIGRATE_CROSS_VCENTER_OP_ID


async def vm_migrate_cross_vcenter_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Approval card: which VM moves, to which vCenter target, and where there.

    Only the call's own inputs; never the other vCenter's login.
    """
    vm, destination = ctx.params.get("vm"), ctx.params.get("destination_target")
    if not isinstance(vm, str) or not isinstance(destination, str):
        return None
    networks = {
        str(nic.get("nic_key")): nic.get("network")
        for nic in ctx.params.get("nics") or []
        if isinstance(nic, dict)
    }
    return {
        "action": "migrate_vm_cross_vcenter",
        "vm": vm,
        "destination_target": destination,
        "requested": _vm_cross_vcenter.placement(ctx.params),
        "network": ctx.params.get("network"),
        "nics": networks,
        "note": (
            "The VM leaves this vCenter. MEHO checks the other vCenter's certificate, login "
            "and target objects before the start."
        ),
    }


register_preview_builder(_OP_ID, vm_migrate_cross_vcenter_preview)


VM_CROSS_VCENTER_COMPOSITES: Final[tuple[dict[str, Any], ...]] = (
    {
        "op_id": _OP_ID,
        "handler": _vm_cross_vcenter.vm_migrate_cross_vcenter_composite,
        "summary": "Move a VM, also while it runs, to another vCenter (cross vCenter vMotion).",
        "description": (
            "Starts a move of one VM to another vCenter (RelocateVM_Task with the other "
            "vCenter's service locator). The other vCenter is its own MEHO target; its login "
            "comes from Vault. Answers accepted with the task id; vmware.task.info follows it."
        ),
        "parameter_schema": VM_MIGRATE_CROSS_VCENTER_PARAMETER_SCHEMA,
        "response_schema": VM_MIGRATE_CROSS_VCENTER_RESPONSE_SCHEMA,
        "group_key": "vm",
        "tags": ["composite", "write", "vm", "migration", "vi-json"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": VM_MIGRATE_CROSS_VCENTER_LLM_INSTRUCTIONS,
    },
)

#: composite op_id -> its governed child ops (the vim manifest).
VM_CROSS_VCENTER_GOVERNED_SUBOPS: Final[dict[str, tuple[str, ...]]] = {
    _OP_ID: _vm_cross_vcenter._VIM_SUB_OPS_VM_MIGRATE_CROSS_VCENTER,
}
