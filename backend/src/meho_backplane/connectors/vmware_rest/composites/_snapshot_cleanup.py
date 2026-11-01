# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Delete all snapshots of a VM, and consolidate its disks.

Both writes use the VIM API through the same VI-JSON path as the other
snapshot writes (:mod:`._snapshot`): a read first, one governed ``*_Task``
through :func:`._write._write_vmomi_sub_op`, a poll with
:func:`~meho_backplane.connectors.vmware_rest.vim_task.poll_vim_task`, then a
new read of the VM.

* ``vmware.composite.vm.snapshot.delete_all`` (``destructive``) --
  ``VirtualMachine.RemoveAllSnapshots_Task`` with ``consolidate`` true, so
  vCenter merges the snapshot data into the base disks. It uses the
  plan-then-act seam of :mod:`._teardown`: one read-only plan feeds both the
  approval-card preview and the handler. A VM without snapshots changes
  nothing (``unchanged``); an unknown VM is refused.
* ``vmware.composite.vm.disks.consolidate`` (``caution``) --
  ``VirtualMachine.ConsolidateVMDisks_Task``. It runs only when vCenter says
  the VM needs it (``runtime.consolidationNeeded`` is ``true``). A ``false``
  flag changes nothing (``unchanged``); a flag vCenter did not report also
  changes nothing (``unknown``). After the task only an explicit ``false``
  confirms the result (``consolidated``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composite_issues import issue
from meho_backplane.connectors.vmware_rest.composites._snapshot import _snapshot_tree, _vm_name
from meho_backplane.connectors.vmware_rest.composites._teardown import (
    LIST_CAP,
    STATUS_DELETED,
    STATUS_INVALID_REQUEST,
    STATUS_STILL_PRESENT,
    STATUS_TIMEOUT,
    STATUS_UNCHANGED,
    TeardownPlan,
    blast_radius,
    envelope,
    pre_write_outcome,
    timeout_envelope,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moid_problem,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_RETRIEVE_PROPERTIES,
    _VIRTUAL_MACHINE_MO_TYPE,
    _task_timeout_issue,
    _unwrap_value,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import VM_MOID_PATTERN
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.vim_task import VimTaskResult
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "_VIM_SUB_OPS_VM_DISKS_CONSOLIDATE",
    "_VIM_SUB_OPS_VM_SNAPSHOT_DELETE_ALL",
    "plan_snapshot_delete_all",
    "vm_disks_consolidate_composite",
    "vm_disks_consolidate_preview",
    "vm_snapshot_delete_all_composite",
    "vm_snapshot_delete_all_preview",
]

_OP_REMOVE_ALL_SNAPSHOTS_TASK: Final = "POST:/VirtualMachine/{moId}/RemoveAllSnapshots_Task"
_OP_CONSOLIDATE_VM_DISKS_TASK: Final = "POST:/VirtualMachine/{moId}/ConsolidateVMDisks_Task"

#: vi-json sub-op manifests (``_VIM_SUB_OPS_*`` namespace, so the vcenter.yaml
#: sweep skips them; the vi-json reconcile lane checks every path exists).
_VIM_SUB_OPS_VM_SNAPSHOT_DELETE_ALL: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_REMOVE_ALL_SNAPSHOTS_TASK,
)
_VIM_SUB_OPS_VM_DISKS_CONSOLIDATE: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_CONSOLIDATE_VM_DISKS_TASK,
)

_PROP_CONSOLIDATION_NEEDED: Final = "runtime.consolidationNeeded"

#: Wall-clock bound for the task polls -- the 600 s convention; module-global so
#: tests can zero it.
_TASK_TIMEOUT_SECONDS = 600.0

STATUS_CONSOLIDATED: Final = "consolidated"
STATUS_STILL_NEEDED: Final = "still_needed"
STATUS_NOT_FOUND: Final = "not_found"
#: vCenter did not report ``runtime.consolidationNeeded`` before the write.
STATUS_UNKNOWN: Final = "unknown"
#: The task succeeded, but the new read did not report the flag.
STATUS_UNVERIFIED: Final = "unverified"


# ===========================================================================
# vm.snapshot.delete_all -- VirtualMachine.RemoveAllSnapshots_Task
# ===========================================================================


async def plan_snapshot_delete_all(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
) -> TeardownPlan:
    """Plan the delete of every snapshot of a VM (read-only).

    The moid is shape-checked before it reaches a vim path. An unknown VM is
    an ``invalid_request``. A VM without snapshots means absent
    (``unchanged``). Otherwise every snapshot is a ``child`` that goes.
    """
    vm = params["vm"]
    obj: dict[str, Any] = {"kind": "vm_snapshots", "vm": vm}
    problem = moid_problem(vm, VM_MOID_PATTERN, "vm")
    if problem is not None:
        return TeardownPlan(obj, False, refusal=(STATUS_INVALID_REQUEST, problem))
    tree = await _snapshot_tree(connector, target, operator, vm)
    if tree is None:
        refusal = (STATUS_INVALID_REQUEST, f"VM {vm!r} not found; pass a VM moid")
        return TeardownPlan(obj, False, refusal=refusal)
    props, rows = tree
    obj.update(vm_name=_vm_name(props), snapshot_count=len(rows))
    plan = TeardownPlan(object=obj, present=bool(rows))
    plan.children = [
        {
            "kind": "vm_snapshot",
            "snapshot_id": r["snapshot_id"],
            "name": r["name"],
            "created": r["created"],
        }
        for r in rows[:LIST_CAP]
    ]
    return plan


async def _delete_all_result(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    plan: TeardownPlan,
    outcome: VimTaskResult,
) -> dict[str, Any]:
    """Map the finished (or timed-out) task to the envelope; read the tree again."""
    vm = plan.object["vm"]
    if outcome.state == TASK_STATE_ERROR:
        raise RuntimeError(
            f"vm.snapshot.delete_all: RemoveAllSnapshots_Task on vm {vm!r} faulted: "
            f"{outcome.error_message or '<no fault reported>'}"
        )
    if outcome.timed_out:
        return timeout_envelope(
            plan,
            outcome,
            method="RemoveAllSnapshots_Task",
            reread="list the VM's snapshots to confirm (deleted snapshots are gone)",
        )
    tree = await _snapshot_tree(connector, target, operator, vm)
    left = len(tree[1]) if tree is not None else 0
    plan.object["snapshots_left"] = left
    if left:
        return envelope(
            plan,
            STATUS_STILL_PRESENT,
            task=outcome.task,
            task_state=outcome.state,
            guidance=(
                f"RemoveAllSnapshots_Task succeeded but {left} snapshot(s) still read back; "
                "list the VM's snapshots again"
            ),
        )
    return envelope(plan, STATUS_DELETED, task=outcome.task, task_state=outcome.state)


async def vm_snapshot_delete_all_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Delete every snapshot of one VM.

    Op-id: ``vmware.composite.vm.snapshot.delete_all``. ``unchanged`` when the
    VM has no snapshots; ``invalid_request`` for an unknown VM. Otherwise the
    gated ``RemoveAllSnapshots_Task`` (a parked / denied gate is returned as
    is, nothing reaches vCenter), polled to the end. A task fault raises
    (``connector_error``, audited as failed). ``deleted`` only when a new read
    of the tree shows no snapshots.
    """
    plan = await plan_snapshot_delete_all(connector, target, operator, params)
    early = pre_write_outcome(plan, absent_guidance="the VM has no snapshots; nothing was deleted")
    if early is not None:
        return early
    vm = params["vm"]
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_REMOVE_ALL_SNAPSHOTS_TASK,
        vmomi_path=f"/{_VIRTUAL_MACHINE_MO_TYPE}/{vm}/RemoveAllSnapshots_Task",
        body={"consolidate": True},
        params={"vm": vm, "snapshot_count": plan.object["snapshot_count"]},
    )
    if gate is not None:
        return gate
    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(task_payload),
        timeout_seconds=_TASK_TIMEOUT_SECONDS,
    )
    return await _delete_all_result(connector, target, operator, plan=plan, outcome=outcome)


async def vm_snapshot_delete_all_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius for ``vm.snapshot.delete_all`` (the handler's own plan)."""
    vm = ctx.params.get("vm")
    if not isinstance(vm, str) or ctx.connector_instance is None:
        return None
    plan = await plan_snapshot_delete_all(
        ctx.connector_instance,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        {"vm": vm},
    )
    return blast_radius(plan)


# ===========================================================================
# vm.disks.consolidate -- VirtualMachine.ConsolidateVMDisks_Task
# ===========================================================================


def _consolidate_envelope(
    status: str,
    vm: str,
    *,
    vm_name: str | None = None,
    consolidation_needed: bool | None = None,
    task: str | None = None,
    task_state: str | None = None,
    guidance: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "vm": vm,
        "vm_name": vm_name,
        "consolidation_needed": consolidation_needed,
        "task": task,
        "task_state": task_state,
        "guidance": guidance,
    }


async def _consolidation_state(
    connector: VmwareRestConnector, target: Any, operator: Operator, vm: str
) -> tuple[str | None, bool | None] | None:
    """``(VM name, consolidation needed)``; ``None`` when the VM does not exist."""
    props = await read_object(
        connector,
        target,
        operator,
        mo_type=_VIRTUAL_MACHINE_MO_TYPE,
        moid=vm,
        props=["name", _PROP_CONSOLIDATION_NEEDED],
    )
    if props is None:
        return None
    needed = props.get(_PROP_CONSOLIDATION_NEEDED)
    return _vm_name(props), needed if isinstance(needed, bool) else None


async def _consolidate_result(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    vm: str,
    vm_name: str | None,
    outcome: VimTaskResult,
) -> dict[str, Any]:
    """Map the finished (or timed-out) task to the envelope; read the VM again."""
    if outcome.state == TASK_STATE_ERROR:
        raise RuntimeError(
            f"vm.disks.consolidate: ConsolidateVMDisks_Task on vm {vm!r} faulted: "
            f"{outcome.error_message or '<no fault reported>'}"
        )
    if outcome.timed_out:
        timed_out = _consolidate_envelope(
            STATUS_TIMEOUT,
            vm,
            vm_name=vm_name,
            consolidation_needed=True,
            task=outcome.task,
            task_state=outcome.state,
            guidance=(
                f"ConsolidateVMDisks_Task {outcome.task} did not finish within "
                f"{int(_TASK_TIMEOUT_SECONDS)}s; it may still finish. Check the task, then "
                "call vmware.vm.info"
            ),
        )
        timed_out["issues"] = [_task_timeout_issue("ConsolidateVMDisks_Task", outcome.task)]
        return timed_out
    after = await _consolidation_state(connector, target, operator, vm)
    needed = after[1] if after is not None else None
    return _consolidate_readback(vm, vm_name=vm_name, needed=needed, outcome=outcome)


def _consolidate_readback(
    vm: str, *, vm_name: str | None, needed: bool | None, outcome: VimTaskResult
) -> dict[str, Any]:
    """The envelope for a finished task, from the flag read back afterwards.

    Only an explicit ``False`` confirms the result. A flag vCenter did not
    report (or a VM that is gone) is ``unverified`` with a warning issue.
    """
    if needed is True:
        return _consolidate_envelope(
            STATUS_STILL_NEEDED,
            vm,
            vm_name=vm_name,
            consolidation_needed=True,
            task=outcome.task,
            task_state=outcome.state,
            guidance=(
                "ConsolidateVMDisks_Task succeeded but vCenter still says the disks need "
                "consolidation; check the datastore space and the VM's tasks"
            ),
        )
    if needed is False:
        return _consolidate_envelope(
            STATUS_CONSOLIDATED,
            vm,
            vm_name=vm_name,
            consolidation_needed=False,
            task=outcome.task,
            task_state=outcome.state,
        )
    unverified = _consolidate_envelope(
        STATUS_UNVERIFIED,
        vm,
        vm_name=vm_name,
        consolidation_needed=None,
        task=outcome.task,
        task_state=outcome.state,
        guidance=(
            "ConsolidateVMDisks_Task succeeded but the new read of the VM did not report "
            "runtime.consolidationNeeded; check the VM and its tasks in vCenter"
        ),
    )
    unverified["issues"] = [
        issue(
            "connector",
            "warning",
            f"ConsolidateVMDisks_Task {outcome.task} succeeded but vCenter did not report "
            "runtime.consolidationNeeded afterwards; the result is not confirmed",
        )
    ]
    return unverified


async def _consolidate_precheck(
    connector: VmwareRestConnector, target: Any, operator: Operator, vm: str
) -> tuple[dict[str, Any] | None, str | None]:
    """``(early envelope or None, VM name)``: the checks before any write."""
    problem = moid_problem(vm, VM_MOID_PATTERN, "vm")
    if problem is not None:
        return _consolidate_envelope(STATUS_INVALID_REQUEST, vm, guidance=problem), None
    state = await _consolidation_state(connector, target, operator, vm)
    if state is None:
        guidance = f"no VM {vm!r}; pass a VM moid (vmware.vm.info finds it from the name)"
        return _consolidate_envelope(STATUS_NOT_FOUND, vm, guidance=guidance), None
    vm_name, needed = state
    if needed is True:
        return None, vm_name
    if needed is False:
        unchanged = _consolidate_envelope(
            STATUS_UNCHANGED,
            vm,
            vm_name=vm_name,
            consolidation_needed=False,
            guidance="vCenter says the VM's disks do not need consolidation; nothing was done",
        )
        return unchanged, vm_name
    unknown = _consolidate_envelope(
        STATUS_UNKNOWN,
        vm,
        vm_name=vm_name,
        consolidation_needed=None,
        guidance=(
            "vCenter did not report runtime.consolidationNeeded for the VM; nothing was "
            "written. Check the VM in vCenter, then call again"
        ),
    )
    return unknown, vm_name


async def vm_disks_consolidate_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Consolidate the disks of one VM that needs it.

    Op-id: ``vmware.composite.vm.disks.consolidate``. Reads
    ``runtime.consolidationNeeded`` first: an unknown VM is ``not_found``, a
    VM that does not need it is ``unchanged``, and a VM whose flag vCenter
    did not report is ``unknown`` (nothing is written in both cases). Only an
    explicit ``true`` runs the gated ``ConsolidateVMDisks_Task`` (a parked /
    denied gate is returned as is), polled to the end. A task fault raises
    (``connector_error``, audited as failed); a poll timeout returns
    ``timeout`` with an error issue. On success the VM is read again:
    ``consolidated`` only when the flag reads back ``false``, ``still_needed``
    when it is ``true``, ``unverified`` (warning issue) when it is missing.
    """
    vm = params["vm"]
    early, vm_name = await _consolidate_precheck(connector, target, operator, vm)
    if early is not None:
        return early
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_CONSOLIDATE_VM_DISKS_TASK,
        vmomi_path=f"/{_VIRTUAL_MACHINE_MO_TYPE}/{vm}/ConsolidateVMDisks_Task",
        body={},
        params={"vm": vm},
    )
    if gate is not None:
        return gate
    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(task_payload),
        timeout_seconds=_TASK_TIMEOUT_SECONDS,
    )
    return await _consolidate_result(
        connector, target, operator, vm=vm, vm_name=vm_name, outcome=outcome
    )


async def vm_disks_consolidate_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``vm.disks.consolidate`` -- param echo (no I/O; not destructive)."""
    vm = ctx.params.get("vm")
    if not isinstance(vm, str):
        return None
    return {"action": "consolidate_vm_disks", "vm": vm}
