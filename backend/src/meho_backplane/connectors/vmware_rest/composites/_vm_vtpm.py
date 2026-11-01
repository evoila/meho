# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add or remove a virtual TPM on a VM -- ``vm.vtpm.add`` and ``vm.vtpm.remove``.

Both use one ``VirtualMachine.ReconfigVM_Task`` (see :mod:`._vm_security`):

* ``vmware.composite.vm.vtpm.add`` (``dangerous``) -- a ``deviceChange`` add of
  a ``VirtualTPM``, like the Ansible ``vmware_guest_tpm`` module sends it.
  vCenter makes the keys from the default key provider and encrypts the VM
  files. A VM that has a vTPM already is ``unchanged``. Before any write it
  checks what the vendor needs: the VM is off, uses EFI firmware and virtual
  hardware version 14 or newer.
* ``vmware.composite.vm.vtpm.remove`` (``destructive``) -- a ``deviceChange``
  remove of the VM's ``VirtualTPM`` device as read. The secrets the guest kept
  in it are lost. It uses the plan-then-act seam of :mod:`._teardown`: one
  read-only plan feeds the approval-card ``blast_radius`` and the handler.

The schemas and AI guidance live in :mod:`._vm_security_schemas`; the shared
registry in ``_register.py`` only collects :data:`COMPOSITES`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._spec import CompositeSpec
from meho_backplane.connectors.vmware_rest.composites._teardown import (
    STATUS_INVALID_REQUEST,
    STATUS_PRECONDITION_FAILED,
    STATUS_STILL_PRESENT,
    STATUS_TIMEOUT,
    STATUS_UNCHANGED,
    TeardownPlan,
    blast_radius,
    envelope,
    pre_write_outcome,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import moid_problem
from meho_backplane.connectors.vmware_rest.composites._vm_security import (
    PROP_FIRMWARE,
    PROP_VERSION,
    STATUS_NOT_CONFIRMED,
    STATUS_NOT_FOUND,
    VIRTUAL_TPM_TYPE,
    hw_version,
    power_state,
    powered_on_problem,
    read_vm,
    reconfigure,
    timeout_fields,
    vm_name,
    vtpm_device,
    vtpm_view,
)
from meho_backplane.connectors.vmware_rest.composites._vm_security_schemas import (
    VM_VTPM_ADD_LLM_INSTRUCTIONS,
    VM_VTPM_ADD_PARAMETER_SCHEMA,
    VM_VTPM_ADD_RESPONSE_SCHEMA,
    VM_VTPM_REMOVE_LLM_INSTRUCTIONS,
    VM_VTPM_REMOVE_PARAMETER_SCHEMA,
    VM_VTPM_REMOVE_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _VIRTUAL_DEVICE_CONFIG_SPEC_TYPE,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import VM_MOID_PATTERN
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY
from meho_backplane.operations._preview import (
    PreviewContext,
    preview_failure_reason,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "COMPOSITES",
    "plan_vtpm_remove",
    "vm_vtpm_add_composite",
    "vm_vtpm_remove_composite",
    "vtpm_add_spec",
    "vtpm_remove_spec",
]

OP_ID_VM_VTPM_ADD: Final = "vmware.composite.vm.vtpm.add"
OP_ID_VM_VTPM_REMOVE: Final = "vmware.composite.vm.vtpm.remove"

#: A vTPM needs virtual hardware version 14 or newer.
_VTPM_MIN_HW_VERSION: Final = 14
#: New devices get a negative temporary key; vCenter assigns the real one.
_NEW_DEVICE_KEY: Final = -1

STATUS_ADDED: Final = "added"
STATUS_REMOVED: Final = "removed"

_VTPM_LOSS: Final = (
    "the keys and secrets the guest kept in the vTPM (for example BitLocker keys or "
    "Windows Hello credentials); a new vTPM does not bring them back"
)


# ===========================================================================
# vm.vtpm.add
# ===========================================================================


def _vtpm_add_envelope(vm: str, status: str | None, **fields: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": status,
        "vm": vm,
        "vm_name": None,
        "power_state": None,
        "vtpm": None,
        "task": None,
        "task_state": None,
        "guidance": None,
    }
    out.update(fields)
    return out


async def _vtpm_add_precheck(
    connector: VmwareRestConnector, target: Any, operator: Operator, vm: str
) -> dict[str, Any]:
    """The envelope the handler would stop with, or one with ``status=None`` to go on."""
    problem = moid_problem(vm, VM_MOID_PATTERN, "vm")
    if problem is not None:
        return _vtpm_add_envelope(vm, STATUS_INVALID_REQUEST, guidance=problem)
    props = await read_vm(connector, target, operator, vm)
    if props is None:
        return _vtpm_add_envelope(
            vm, STATUS_NOT_FOUND, guidance=f"no VM {vm!r}; pass a VM id such as vm-42"
        )
    base = {"vm_name": vm_name(props), "power_state": power_state(props)}
    existing = vtpm_device(props)
    if existing is not None:
        return _vtpm_add_envelope(
            vm,
            STATUS_UNCHANGED,
            vtpm=vtpm_view(existing),
            guidance="the VM has a vTPM already; nothing was sent",
            **base,
        )
    hw = hw_version(props)
    firmware = props.get(PROP_FIRMWARE)
    problem = powered_on_problem(props)
    if problem is None and firmware != "efi":
        problem = f"a vTPM needs EFI firmware; the VM uses {firmware or 'unknown firmware'}"
    if problem is None and hw is not None and hw < _VTPM_MIN_HW_VERSION:
        problem = (
            f"a vTPM needs virtual hardware version {_VTPM_MIN_HW_VERSION} or newer; the VM "
            f"has {props.get(PROP_VERSION)}. Upgrade the hardware version first"
        )
    if problem is not None:
        return _vtpm_add_envelope(vm, STATUS_PRECONDITION_FAILED, guidance=problem, **base)
    return _vtpm_add_envelope(vm, None, **base)


def vtpm_add_spec() -> dict[str, Any]:
    """The ``VirtualMachineConfigSpec`` fields that add one ``VirtualTPM``."""
    return {
        "deviceChange": [
            {
                VIM_TYPE_NAME_KEY: _VIRTUAL_DEVICE_CONFIG_SPEC_TYPE,
                "operation": "add",
                "device": {VIM_TYPE_NAME_KEY: VIRTUAL_TPM_TYPE, "key": _NEW_DEVICE_KEY},
            }
        ]
    }


async def vm_vtpm_add_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Add a virtual TPM to one powered-off VM. Op-id: ``vmware.composite.vm.vtpm.add``."""
    vm = params["vm"]
    checked = await _vtpm_add_precheck(connector, target, operator, vm)
    if checked["status"] is not None:
        return checked
    gate, outcome = await reconfigure(
        connector,
        target,
        operator,
        spec=vtpm_add_spec(),
        params={"op": OP_ID_VM_VTPM_ADD, "vm": vm, "change": "add VirtualTPM"},
    )
    if gate is not None:
        return gate
    assert outcome is not None
    if outcome.timed_out:
        return {
            **checked,
            **timeout_fields(
                outcome, "Run this call again: it answers unchanged once the vTPM is there."
            ),
        }
    after = await read_vm(connector, target, operator, vm)
    vtpm = vtpm_device(after) if after is not None else None
    done = {**checked, "task": outcome.task, "task_state": outcome.state}
    if vtpm is None:
        return {
            **done,
            "status": STATUS_NOT_CONFIRMED,
            "guidance": "ReconfigVM_Task succeeded, but no vTPM reads back yet; read the VM again",
        }
    return {**done, "status": STATUS_ADDED, "vtpm": vtpm_view(vtpm)}


async def _vtpm_add_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the VM and whether the handler would stop before the change."""
    vm = ctx.params.get("vm")
    if not isinstance(vm, str) or ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    try:
        checked = await _vtpm_add_precheck(connector, ctx.target, ctx.operator, vm)
    except Exception as exc:
        return {
            "summary": f"MEHO could not read VM {vm}, so it cannot show its state.",
            "vm": vm,
            "read_error": preview_failure_reason(exc),
        }
    preview: dict[str, Any] = {
        "summary": f"Add a virtual TPM to {vm}. vCenter encrypts the VM files with a new key.",
        "vm": vm,
        "vm_name": checked["vm_name"],
        "power_state": checked["power_state"],
    }
    if checked["status"] is not None:
        preview["summary"] = f"The call will change nothing on {vm}: {checked['guidance']}."
        preview["stops_with"] = checked["status"]
    return preview


# ===========================================================================
# vm.vtpm.remove -- destructive, plan-then-act
# ===========================================================================


async def plan_vtpm_remove(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
) -> TeardownPlan:
    """Plan a vTPM remove (read-only); the preview and the handler share it.

    A bad or unknown VM id is ``invalid_request``. A VM without a vTPM is
    absent (``unchanged``). A running VM is ``precondition_failed``.
    """
    vm = params["vm"]
    obj: dict[str, Any] = {"kind": "vm_vtpm", "vm": vm}
    problem = moid_problem(vm, VM_MOID_PATTERN, "vm")
    if problem is not None:
        return TeardownPlan(obj, False, refusal=(STATUS_INVALID_REQUEST, problem))
    props = await read_vm(connector, target, operator, vm)
    if props is None:
        refusal = (STATUS_INVALID_REQUEST, f"VM {vm!r} not found; pass a VM id such as vm-42")
        return TeardownPlan(obj, False, refusal=refusal)
    device = vtpm_device(props)
    obj.update(vm_name=vm_name(props), power_state=power_state(props), vtpm=vtpm_view(device))
    if device is None:
        return TeardownPlan(object=obj, present=False)
    obj["lost_with_it"] = _VTPM_LOSS
    plan = TeardownPlan(object=obj, present=True, context={"device": device})
    problem = powered_on_problem(props)
    if problem is not None:
        plan.refusal = (STATUS_PRECONDITION_FAILED, problem)
    return plan


def vtpm_remove_spec(device: dict[str, Any]) -> dict[str, Any]:
    """The ``VirtualMachineConfigSpec`` fields that remove *device* (the VM's vTPM)."""
    return {
        "deviceChange": [
            {
                VIM_TYPE_NAME_KEY: _VIRTUAL_DEVICE_CONFIG_SPEC_TYPE,
                "operation": "remove",
                "device": device,
            }
        ]
    }


async def vm_vtpm_remove_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Remove the virtual TPM of one powered-off VM. Op-id: ``vm.vtpm.remove``.

    ``removed`` only when a new read no longer shows a vTPM.
    """
    plan = await plan_vtpm_remove(connector, target, operator, params)
    early = pre_write_outcome(plan, absent_guidance="the VM has no vTPM; nothing was removed")
    if early is not None:
        return early
    vm = params["vm"]
    gate, outcome = await reconfigure(
        connector,
        target,
        operator,
        spec=vtpm_remove_spec(plan.context["device"]),
        params={"op": OP_ID_VM_VTPM_REMOVE, "vm": vm, "change": "remove VirtualTPM"},
    )
    if gate is not None:
        return gate
    assert outcome is not None
    if outcome.timed_out:
        return {
            **envelope(plan, STATUS_TIMEOUT),
            **timeout_fields(outcome, "Read the VM to see whether the vTPM is gone."),
        }
    after = await read_vm(connector, target, operator, vm)
    if after is not None and vtpm_device(after) is not None:
        return envelope(
            plan,
            STATUS_STILL_PRESENT,
            task=outcome.task,
            task_state=outcome.state,
            guidance="ReconfigVM_Task succeeded, but the vTPM still reads back; read the VM again",
        )
    return envelope(plan, STATUS_REMOVED, task=outcome.task, task_state=outcome.state)


async def _vtpm_remove_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius for ``vm.vtpm.remove`` (the handler's own plan)."""
    vm = ctx.params.get("vm")
    if not isinstance(vm, str) or ctx.connector_instance is None:
        return None
    plan = await plan_vtpm_remove(
        ctx.connector_instance,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        {"vm": vm},
    )
    return blast_radius(plan)


#: The two registration rows.
COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id=OP_ID_VM_VTPM_ADD,
        handler=vm_vtpm_add_composite,
        summary="Add a virtual TPM to a powered-off VM.",
        description=(
            "Adds a virtual TPM 2.0 device to a VM, named by its VM id. The VM must be off, "
            "use EFI firmware and hardware version 14 or newer. vCenter encrypts the VM files "
            "with a key from the default key provider. Waits for approval."
        ),
        parameter_schema=VM_VTPM_ADD_PARAMETER_SCHEMA,
        response_schema=VM_VTPM_ADD_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "security", "vtpm", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_VTPM_ADD_LLM_INSTRUCTIONS,
        preview=_vtpm_add_preview,
    ),
    CompositeSpec(
        op_id=OP_ID_VM_VTPM_REMOVE,
        handler=vm_vtpm_remove_composite,
        summary="Remove the virtual TPM of a powered-off VM; its secrets are lost.",
        description=(
            "Removes the virtual TPM of a VM, named by its VM id. The VM must be off. The keys "
            "and secrets the guest kept in the vTPM are lost for good. A second person always "
            "approves, after a preview."
        ),
        parameter_schema=VM_VTPM_REMOVE_PARAMETER_SCHEMA,
        response_schema=VM_VTPM_REMOVE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "security", "vtpm", "vi-json"],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=VM_VTPM_REMOVE_LLM_INSTRUCTIONS,
        preview=_vtpm_remove_preview,
    ),
)
