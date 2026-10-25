# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VM snapshot writes -- ``vm.snapshot.create`` and ``vm.snapshot.delete``.

vCenter keeps snapshots only in the VIM API, so both writes use the VI-JSON seam
the other vim writes use: read the VM's snapshot tree (``RetrievePropertiesEx``
of ``VirtualMachine.snapshot``, flattened by
:func:`~meho_backplane.connectors.vmware_rest.snapshot_tree.snapshot_rows`), run
one governed ``*_Task`` through :func:`._write._write_vmomi_sub_op`, poll it with
:func:`~meho_backplane.connectors.vmware_rest.vim_task.poll_vim_task`, then read
the tree again.

* ``vmware.composite.vm.snapshot.create`` (``caution``, waits for approval) --
  ``VirtualMachine.CreateSnapshotEx_Task``. The plain ``CreateSnapshot_Task`` is
  deprecated since vSphere 8.0. Quiescing is an empty
  ``VirtualMachineGuestQuiesceSpec``, as ``govc snapshot.create -q`` sends it. A
  VM that already has a snapshot with the name gets no second one
  (``status="unchanged"``), so a retry after a timeout is safe.
* ``vmware.composite.vm.snapshot.delete`` (``destructive``) --
  ``VirtualMachineSnapshot.RemoveSnapshot_Task``. It uses the plan-then-act seam
  of :mod:`._teardown`: one read-only plan feeds both the approval-card preview
  and the handler, so the approver sees the decision the handler makes.
  ``removeChildren`` is false unless asked; ``consolidate`` keeps its vendor
  default (true).

The revert stays in :mod:`._write` (``vm_snapshot_revert_composite``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors import OperationResult
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
    _PROP_SNAPSHOT,
    _VIRTUAL_MACHINE_MO_TYPE,
    _VM_SNAPSHOT_MO_TYPE,
    _moref_value,
    _task_timeout_issue,
    _unwrap_value,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    VM_MOID_PATTERN,
    VM_SNAPSHOT_MOID_PATTERN,
)
from meho_backplane.connectors.vmware_rest.snapshot_tree import descendants, snapshot_rows
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.vim_task import VimTaskResult
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "plan_snapshot_delete",
    "vm_snapshot_create_composite",
    "vm_snapshot_create_preview",
    "vm_snapshot_delete_composite",
    "vm_snapshot_delete_preview",
]

_OP_CREATE_SNAPSHOT_EX_TASK: Final = "POST:/VirtualMachine/{moId}/CreateSnapshotEx_Task"
_OP_REMOVE_SNAPSHOT_TASK: Final = "POST:/VirtualMachineSnapshot/{moId}/RemoveSnapshot_Task"

#: vi-json sub-op manifests (``_VIM_SUB_OPS_*`` namespace, so the vcenter.yaml
#: sweep skips them; the vi-json reconcile lane checks every path exists).
_VIM_SUB_OPS_VM_SNAPSHOT_CREATE: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_CREATE_SNAPSHOT_EX_TASK,
)
_VIM_SUB_OPS_VM_SNAPSHOT_DELETE: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_REMOVE_SNAPSHOT_TASK,
)

#: The quiesce request: an empty spec asks for the default quiescing.
_GUEST_QUIESCE_SPEC_TYPE: Final = "VirtualMachineGuestQuiesceSpec"

_VM_PROPS: Final = ["name", _PROP_SNAPSHOT]

#: Wall-clock bound for the task polls -- the 600 s convention; module-global so
#: tests can zero it.
_TASK_TIMEOUT_SECONDS = 600.0

STATUS_CREATED: Final = "created"
STATUS_NOT_FOUND: Final = "not_found"


async def _snapshot_tree(
    connector: VmwareRestConnector, target: Any, operator: Operator, vm: str
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """``(VM properties, snapshot rows)``; ``None`` when the VM does not exist.

    :func:`read_object` fails the call when vCenter could not read the snapshot
    property, so a missing permission never reads as "no snapshots".
    """
    props = await read_object(
        connector, target, operator, mo_type=_VIRTUAL_MACHINE_MO_TYPE, moid=vm, props=_VM_PROPS
    )
    if props is None:
        return None
    _current, rows = snapshot_rows(props.get(_PROP_SNAPSHOT))
    return props, rows


def _vm_name(props: dict[str, Any]) -> str | None:
    name = props.get("name")
    return name if isinstance(name, str) else None


# ===========================================================================
# vm.snapshot.create -- VirtualMachine.CreateSnapshotEx_Task
# ===========================================================================


def _create_envelope(
    status: str,
    vm: str,
    *,
    vm_name: str | None = None,
    snapshot_id: str | None = None,
    snapshot: dict[str, Any] | None = None,
    task: str | None = None,
    task_state: str | None = None,
    guidance: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "vm": vm,
        "vm_name": vm_name,
        "snapshot_id": snapshot_id,
        "snapshot": snapshot,
        "task": task,
        "task_state": task_state,
        "guidance": guidance,
    }


def _create_body(params: dict[str, Any]) -> dict[str, Any]:
    """The ``CreateSnapshotEx_Task`` request body (``name`` and ``memory`` are required)."""
    body: dict[str, Any] = {
        "name": params["snapshot_name"],
        "memory": bool(params.get("memory", False)),
    }
    description = params.get("description")
    if isinstance(description, str):
        body["description"] = description
    if params.get("quiesce", False):
        body["quiesceSpec"] = {VIM_TYPE_NAME_KEY: _GUEST_QUIESCE_SPEC_TYPE}
    return body


async def _create_precheck(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    vm: str,
    snapshot_name: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """``(early envelope or None, VM name)``: the checks before any write."""
    problem = moid_problem(vm, VM_MOID_PATTERN, "vm")
    if problem is not None:
        return _create_envelope(STATUS_INVALID_REQUEST, vm, guidance=problem), None
    tree = await _snapshot_tree(connector, target, operator, vm)
    if tree is None:
        guidance = f"no VM {vm!r}; pass a VM moid (vmware.vm.info finds it from the name)"
        return _create_envelope(STATUS_NOT_FOUND, vm, guidance=guidance), None
    props, rows = tree
    vm_name = _vm_name(props)
    existing = next((row for row in rows if row["name"] == snapshot_name), None)
    if existing is None:
        return None, vm_name
    unchanged = _create_envelope(
        STATUS_UNCHANGED,
        vm,
        vm_name=vm_name,
        snapshot_id=existing["snapshot_id"],
        snapshot=existing,
        guidance=(
            f"VM {vm!r} already has a snapshot named {snapshot_name!r}; nothing was created. "
            "Use that snapshot, or pick another name"
        ),
    )
    return unchanged, vm_name


async def _create_result(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    vm: str,
    vm_name: str | None,
    outcome: VimTaskResult,
) -> dict[str, Any]:
    """Map the finished (or timed-out) task to the envelope; read the tree again."""
    if outcome.state == TASK_STATE_ERROR:
        raise RuntimeError(
            f"vm.snapshot.create: CreateSnapshotEx_Task on vm {vm!r} faulted: "
            f"{outcome.error_message or '<no fault reported>'}"
        )
    if outcome.timed_out:
        timed_out = _create_envelope(
            STATUS_TIMEOUT,
            vm,
            vm_name=vm_name,
            task=outcome.task,
            task_state=outcome.state,
            guidance=(
                f"CreateSnapshotEx_Task {outcome.task} did not finish within "
                f"{int(_TASK_TIMEOUT_SECONDS)}s; it may still finish. Check the task, then "
                "list the snapshots"
            ),
        )
        timed_out["issues"] = [_task_timeout_issue("CreateSnapshotEx_Task", outcome.task)]
        return timed_out
    snapshot_id = _moref_value(outcome.result)
    after = await _snapshot_tree(connector, target, operator, vm)
    row = next((r for r in (after[1] if after else []) if r["snapshot_id"] == snapshot_id), None)
    guidance = None
    if row is None:
        guidance = (
            "CreateSnapshotEx_Task succeeded, but the new snapshot does not read back yet; "
            "list the snapshots again"
        )
    return _create_envelope(
        STATUS_CREATED,
        vm,
        vm_name=vm_name,
        snapshot_id=snapshot_id,
        snapshot=row,
        task=outcome.task,
        task_state=outcome.state,
        guidance=guidance,
    )


async def vm_snapshot_create_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Take a snapshot of one VM.

    Op-id: ``vmware.composite.vm.snapshot.create``. Reads the VM's snapshot tree
    first: an unknown VM is ``not_found``, and a snapshot that already has the
    name is ``unchanged`` (nothing is written). Otherwise the gated
    ``CreateSnapshotEx_Task`` (a parked / denied gate is returned as is),
    polled to the end. A task fault raises (``connector_error``, audited as
    failed); a poll timeout returns ``timeout`` with an error issue. On success
    the tree is read again and the new snapshot's row is returned.
    """
    vm = params["vm"]
    snapshot_name = params["snapshot_name"]
    early, vm_name = await _create_precheck(
        connector, target, operator, vm=vm, snapshot_name=snapshot_name
    )
    if early is not None:
        return early
    body = _create_body(params)
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_CREATE_SNAPSHOT_EX_TASK,
        vmomi_path=f"/{_VIRTUAL_MACHINE_MO_TYPE}/{vm}/CreateSnapshotEx_Task",
        body=body,
        params={
            "vm": vm,
            "snapshot_name": snapshot_name,
            "memory": body["memory"],
            "quiesce": "quiesceSpec" in body,
        },
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
    return await _create_result(
        connector, target, operator, vm=vm, vm_name=vm_name, outcome=outcome
    )


async def vm_snapshot_create_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``vm.snapshot.create`` -- param echo (no I/O; a create, not destructive)."""
    vm = ctx.params.get("vm")
    snapshot_name = ctx.params.get("snapshot_name")
    if not isinstance(vm, str) or not isinstance(snapshot_name, str):
        return None
    return {
        "action": "create_vm_snapshot",
        "vm": vm,
        "snapshot_name": snapshot_name,
        "memory": bool(ctx.params.get("memory", False)),
        "quiesce": bool(ctx.params.get("quiesce", False)),
    }


# ===========================================================================
# vm.snapshot.delete -- VirtualMachineSnapshot.RemoveSnapshot_Task
# ===========================================================================


async def plan_snapshot_delete(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
) -> TeardownPlan:
    """Plan a snapshot delete (read-only).

    Both moids are shape-checked before they reach a vim path. An unknown VM is
    an ``invalid_request``. A snapshot id that is not in the VM's tree means
    absent (``unchanged``). With ``remove_children`` the snapshots below it are
    the ``children`` that go with it; without it nothing else is deleted.
    """
    vm = params["vm"]
    snapshot_id = params["snapshot_id"]
    remove_children = bool(params.get("remove_children", False))
    obj: dict[str, Any] = {
        "kind": "vm_snapshot",
        "vm": vm,
        "snapshot_id": snapshot_id,
        "remove_children": remove_children,
    }
    problem = moid_problem(vm, VM_MOID_PATTERN, "vm") or moid_problem(
        snapshot_id, VM_SNAPSHOT_MOID_PATTERN, "snapshot_id"
    )
    if problem is not None:
        return TeardownPlan(obj, False, refusal=(STATUS_INVALID_REQUEST, problem))
    tree = await _snapshot_tree(connector, target, operator, vm)
    if tree is None:
        refusal = (STATUS_INVALID_REQUEST, f"VM {vm!r} not found; pass a VM moid")
        return TeardownPlan(obj, False, refusal=refusal)
    props, rows = tree
    obj["vm_name"] = _vm_name(props)
    row = next((r for r in rows if r["snapshot_id"] == snapshot_id), None)
    if row is None:
        return TeardownPlan(object=obj, present=False)
    below = descendants(rows, snapshot_id)
    obj.update(
        name=row["name"],
        description=row["description"],
        created=row["created"],
        power_state=row["power_state"],
        current=row["current"],
        child_count=len(below),
    )
    plan = TeardownPlan(object=obj, present=True)
    if remove_children:
        plan.children = [
            {"kind": "vm_snapshot", "snapshot_id": r["snapshot_id"], "name": r["name"]}
            for r in below[:LIST_CAP]
        ]
    return plan


async def _snapshot_present(
    connector: VmwareRestConnector, target: Any, operator: Operator, *, vm: str, snapshot_id: str
) -> bool:
    """A new read of the VM's snapshot tree: is *snapshot_id* still there?"""
    tree = await _snapshot_tree(connector, target, operator, vm)
    return tree is not None and any(r["snapshot_id"] == snapshot_id for r in tree[1])


async def _delete_result(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    plan: TeardownPlan,
    outcome: VimTaskResult,
) -> dict[str, Any]:
    """Map the finished (or timed-out) task to the envelope; read the tree again."""
    vm = plan.object["vm"]
    snapshot_id = plan.object["snapshot_id"]
    if outcome.state == TASK_STATE_ERROR:
        raise RuntimeError(
            f"vm.snapshot.delete: RemoveSnapshot_Task on snapshot {snapshot_id!r} of vm "
            f"{vm!r} faulted: {outcome.error_message or '<no fault reported>'}"
        )
    if outcome.timed_out:
        return timeout_envelope(
            plan,
            outcome,
            method="RemoveSnapshot_Task",
            reread="list the VM's snapshots to confirm (a deleted snapshot is gone)",
        )
    if await _snapshot_present(connector, target, operator, vm=vm, snapshot_id=snapshot_id):
        return envelope(
            plan,
            STATUS_STILL_PRESENT,
            task=outcome.task,
            task_state=outcome.state,
            guidance=(
                "RemoveSnapshot_Task succeeded but the snapshot still reads back; list the "
                "VM's snapshots again"
            ),
        )
    return envelope(plan, STATUS_DELETED, task=outcome.task, task_state=outcome.state)


async def vm_snapshot_delete_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Delete one snapshot of a VM, by id.

    Op-id: ``vmware.composite.vm.snapshot.delete``. ``unchanged`` when the VM
    has no snapshot with the id; ``invalid_request`` for an unknown VM.
    Otherwise the gated ``RemoveSnapshot_Task`` (a parked / denied gate is
    returned as is, nothing reaches vCenter), polled to the end. A task fault
    raises (``connector_error``, audited as failed). ``deleted`` only when a
    new read of the tree no longer shows the snapshot.
    """
    plan = await plan_snapshot_delete(connector, target, operator, params)
    early = pre_write_outcome(
        plan, absent_guidance="the VM has no snapshot with that id; nothing was deleted"
    )
    if early is not None:
        return early
    vm = params["vm"]
    snapshot_id = params["snapshot_id"]
    remove_children = bool(plan.object["remove_children"])
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_REMOVE_SNAPSHOT_TASK,
        vmomi_path=f"/{_VM_SNAPSHOT_MO_TYPE}/{snapshot_id}/RemoveSnapshot_Task",
        body={"removeChildren": remove_children},
        params={"vm": vm, "snapshot_id": snapshot_id, "remove_children": remove_children},
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
    return await _delete_result(connector, target, operator, plan=plan, outcome=outcome)


async def vm_snapshot_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius for ``vm.snapshot.delete`` (the handler's own plan)."""
    vm = ctx.params.get("vm")
    snapshot_id = ctx.params.get("snapshot_id")
    if (
        not isinstance(vm, str)
        or not isinstance(snapshot_id, str)
        or ctx.connector_instance is None
    ):
        return None
    plan = await plan_snapshot_delete(
        ctx.connector_instance,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        {
            "vm": vm,
            "snapshot_id": snapshot_id,
            "remove_children": bool(ctx.params.get("remove_children", False)),
        },
    )
    return blast_radius(plan)
