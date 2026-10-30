# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.composite.folder.teardown`` -- delete a whole disposable system (#4624).

A disposable system (for example a test vCenter with a few nested hosts) is a
set of VMs in one VM folder. This op removes it in one governed call: it
powers off every VM under the folder (at every depth), deletes every VM and
VM template with its disks, deletes the sub-folders bottom up and then the
folder. One approval covers the whole plan.

It follows the plan-then-act seam of :mod:`._teardown`: one read-only planning
function (:func:`._folder_teardown_plan.plan_folder_teardown`) feeds the
park-time blast radius and this handler, which plans again on the live state
after approval.

**The fence.** ``expected_vms`` (required) names every VM and template the
caller means to delete. Approval binds the params, so it binds this list. The
handler refuses before any write when the folder holds a VM that is not on the
list, so a VM moved into the folder after the approval request is never
deleted. An expected VM that is no longer under the folder is reported
``unchanged`` and not touched, so a second run is safe.

Refused before any write (``precondition_failed``, with ``blockers``): a root
or datacenter top folder, a folder that cannot hold VMs, a vApp or another
object type under the folder, a VM that another product manages
(``config.managedBy``, for example vSphere Cluster Services), a VM that is not
in ``expected_vms``, and an answer vCenter did not fully give.

The steps use the same vendor calls as ``vm.power`` (REST hard power off, or
a guest shutdown first), ``vm.destroy`` (vim ``Destroy_Task`` before vCenter
9.0, REST ``DELETE`` from 9.0; templates always use vim ``Destroy_Task``) and
``folder.delete`` (vim ``Folder.Destroy_Task``). Each step goes through the
same step check (:func:`._write._write_sub_op` / ``_write_vmomi_sub_op``) as
in those ops. A vendor fault raises after naming what was already deleted, so
the dispatcher records ``connector_error``.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._folder_teardown_plan import (
    POWERED_OFF,
    folder_teardown_preview,
    plan_folder_teardown,
)
from meho_backplane.connectors.vmware_rest.composites._teardown import (
    _OP_DESTROY_FOLDER_TASK,
    STATUS_DELETED,
    STATUS_STILL_PRESENT,
    STATUS_UNCHANGED,
    TeardownPlan,
    destroy_task,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moref_values,
    read_many,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _FOLDER_MO_TYPE,
    _OP_CONTINUE_RETRIEVE_PROPERTIES,
    _OP_DELETE_VM,
    _OP_DESTROY_VM_TASK,
    _OP_RETRIEVE_PROPERTIES,
    _VIRTUAL_MACHINE_MO_TYPE,
    _guest_power_vm_op_id,
    _power_vm_op_id,
    _vim_destroy_required,
    _write_sub_op,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "folder_teardown_composite",
    "folder_teardown_preview",
]

_OP_HARD_POWER_OFF: Final = _power_vm_op_id("stop")
_OP_GUEST_SHUTDOWN: Final = _guest_power_vm_op_id("shutdown")

#: REST steps (all served by the pinned vcenter.yaml; the same op ids as
#: ``vm.power`` and the 9.0+ arm of ``vm.destroy``).
_SUB_OPS_FOLDER_TEARDOWN: Final[tuple[str, ...]] = (
    _OP_HARD_POWER_OFF,
    _OP_GUEST_SHUTDOWN,
    _OP_DELETE_VM,
)
#: vim steps: the reads, the VM ``Destroy_Task`` and the folder ``Destroy_Task``.
_VIM_SUB_OPS_FOLDER_TEARDOWN: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_CONTINUE_RETRIEVE_PROPERTIES,
    _OP_DESTROY_VM_TASK,
    _OP_DESTROY_FOLDER_TASK,
)

_POWERED_ON: Final = "poweredOn"

#: Seconds between power-state reads while guests shut down; module-global so
#: tests can zero it.
_GUEST_POLL_INTERVAL_SECONDS = 5.0

_ROW_DELETED: Final = "deleted"
_ROW_UNCHANGED: Final = "unchanged"
_ROW_FAILED: Final = "failed"


async def _wait_powered_off(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    moids: list[str],
    timeout_s: float,
) -> list[str]:
    """Wait until *moids* read ``poweredOff``; return the ones still running at the deadline."""
    deadline = time.monotonic() + timeout_s
    pending = list(moids)
    while pending:
        found, _ok = await read_many(
            connector, target, operator, mo_type=_VIRTUAL_MACHINE_MO_TYPE, moids=pending,
            props=["runtime.powerState"],
        )  # fmt: skip
        pending = [m for m in pending if found.get(m, {}).get("runtime.powerState") != POWERED_OFF]
        if not pending or time.monotonic() >= deadline:
            break
        await asyncio.sleep(_GUEST_POLL_INTERVAL_SECONDS)
    return pending


async def _power_off(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    vms: list[dict[str, Any]],
    params: dict[str, Any],
) -> OperationResult | None:
    """Power off every VM that is not off; a parked / denied step is returned."""
    running = [vm for vm in vms if vm["power_state"] != POWERED_OFF]
    hard = [vm["moid"] for vm in running]
    if params.get("power_off", "hard") == "guest":
        asked: list[str] = []
        for vm in running:
            if vm["power_state"] != _POWERED_ON:
                continue
            try:
                gate, _ = await _write_sub_op(
                    connector, target, operator, _OP_GUEST_SHUTDOWN, {"vm": vm["moid"]}
                )
            except httpx.HTTPError:
                continue  # no VMware Tools (or the guest refused): hard power off below
            if gate is not None:
                return gate
            asked.append(vm["moid"])
        timeout_s = float(params.get("guest_timeout_s", 120))
        still = await _wait_powered_off(connector, target, operator, asked, timeout_s)
        hard = [m for m in hard if m not in asked or m in still]
    for moid in hard:
        gate, _ = await _write_sub_op(connector, target, operator, _OP_HARD_POWER_OFF, {"vm": moid})
        if gate is not None:
            return gate
    return None


async def _destroy_vm(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    vm: dict[str, Any],
    *,
    vim: bool,
) -> OperationResult | None:
    """Delete one VM or template like ``vm.destroy``; a parked / denied step is returned."""
    moid = vm["moid"]
    if vim or vm["kind"] == "template":
        outcome = await destroy_task(
            connector, target, operator, op_id=_OP_DESTROY_VM_TASK,
            mo_type=_VIRTUAL_MACHINE_MO_TYPE, moid=moid, params={"vm": moid},
            label="folder.teardown",
        )  # fmt: skip
        if isinstance(outcome, OperationResult):
            return outcome
        if outcome.timed_out:
            raise RuntimeError(f"Destroy_Task {outcome.task} on {moid!r} did not finish in time")
        return None
    gate, _ = await _write_sub_op(connector, target, operator, _OP_DELETE_VM, {"vm": moid})
    return gate


async def _destroy_folder(
    connector: VmwareRestConnector, target: Any, operator: Operator, moid: str
) -> OperationResult | None:
    """Delete one folder that must be empty now; anything left in it stops the run."""
    props = await read_object(
        connector, target, operator, mo_type=_FOLDER_MO_TYPE, moid=moid, props=["childEntity"]
    )
    if props is None:
        return None
    left = moref_values(props.get("childEntity"))
    if left:
        raise RuntimeError(
            f"folder {moid!r} is not empty after its VMs were deleted "
            f"({len(left)} object(s) left, for example {left[0][1]!r}); it was not deleted"
        )
    outcome = await destroy_task(
        connector, target, operator, op_id=_OP_DESTROY_FOLDER_TASK, mo_type=_FOLDER_MO_TYPE,
        moid=moid, params={"folder": moid}, label="folder.teardown",
    )  # fmt: skip
    if isinstance(outcome, OperationResult):
        return outcome
    if outcome.timed_out:
        raise RuntimeError(f"Destroy_Task {outcome.task} on {moid!r} did not finish in time")
    return None


def _row(obj: dict[str, Any], status: str, detail: str | None = None) -> dict[str, Any]:
    return {
        "kind": obj["kind"],
        "moid": obj["moid"],
        "name": obj.get("name"),
        "status": status,
        "powered_off": False,
        "detail": detail,
    }


async def _run_steps(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    plan: TeardownPlan,
    params: dict[str, Any],
    done: list[str],
) -> OperationResult | None:
    """Power off, delete the VMs, the sub-folders bottom up, then the folder."""
    vms = plan.context["vms"]
    gate = await _power_off(connector, target, operator, vms, params)
    if gate is not None:
        return gate
    vim = _vim_destroy_required(await connector._about_version(target, operator))
    for vm in vms:
        gate = await _destroy_vm(connector, target, operator, vm, vim=vim)
        if gate is not None:
            return gate
        done.append(vm["moid"])
    folders = [f["moid"] for f in plan.context["folders"]]
    if params.get("delete_folder", True) is not False:
        folders.append(plan.object["moid"])
    for moid in folders:
        gate = await _destroy_folder(connector, target, operator, moid)
        if gate is not None:
            return gate
        done.append(moid)
    return None


async def _read_back_rows(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    plan: TeardownPlan,
    delete_folder: bool,
) -> list[dict[str, Any]]:
    """One row per object of the plan, from a fresh read after the writes."""
    running = {vm["moid"] for vm in plan.context["vms"] if vm["power_state"] != POWERED_OFF}
    targets = [(_VIRTUAL_MACHINE_MO_TYPE, vm) for vm in plan.context["vms"]]
    targets += [(_FOLDER_MO_TYPE, f) for f in plan.context["folders"]]
    if delete_folder:
        targets.append((_FOLDER_MO_TYPE, plan.object))
    rows: list[dict[str, Any]] = []
    for mo_type, obj in targets:
        after = await read_object(
            connector, target, operator, mo_type=mo_type, moid=obj["moid"], props=["name"]
        )
        row = (
            _row(obj, _ROW_DELETED)
            if after is None
            else _row(obj, _ROW_FAILED, "the delete returned but the object still reads back")
        )
        row["powered_off"] = obj["moid"] in running
        rows.append(row)
    if not delete_folder:
        rows.append(_row(plan.object, _ROW_UNCHANGED, "kept: delete_folder is false"))
    return rows


def _missing_rows(plan: TeardownPlan) -> list[dict[str, Any]]:
    detail = "not under the folder (already deleted or moved away); not touched"
    return [
        _row({"kind": "vm", "moid": m}, _ROW_UNCHANGED, detail) for m in plan.context["missing"]
    ]


def _response(
    plan: TeardownPlan, status: str, rows: list[dict[str, Any]], guidance: str | None
) -> dict[str, Any]:
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in (_ROW_DELETED, _ROW_UNCHANGED)}
    return {
        "status": status,
        "object": plan.object,
        "rows": rows,
        "counts": {
            "vms": len(plan.context["vms"]),
            "folders": len(plan.context["folders"]),
            "powered_off": sum(1 for r in rows if r["powered_off"]),
            "deleted": counts[_ROW_DELETED],
            "unchanged": counts[_ROW_UNCHANGED],
            "failed": sum(1 for r in rows if r["status"] == _ROW_FAILED),
        },
        "blockers": plan.blockers,
        "guidance": guidance,
    }


async def folder_teardown_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Power off and delete every VM under one VM folder, then its folders (#4624).

    Op-id: ``vmware.composite.folder.teardown``. Plans again on the live
    state, refuses before any write on a blocker, then runs the steps and
    reads every object again. A vendor fault raises after naming what was
    already deleted (``connector_error``).
    """
    plan = await plan_folder_teardown(connector, target, operator, params)
    if plan.refusal is not None:
        return _response(plan, plan.refusal[0], [], plan.refusal[1])
    if not plan.present:
        rows = [_row(plan.object, _ROW_UNCHANGED, "the folder does not exist")]
        return _response(plan, STATUS_UNCHANGED, rows + _missing_rows(plan), None)
    done: list[str] = []
    try:
        gate = await _run_steps(connector, target, operator, plan, params, done)
    except Exception as exc:
        raise RuntimeError(
            f"folder.teardown of {plan.object['moid']!r} stopped: {exc}. "
            f"Already deleted: {', '.join(done) or 'nothing'}. Run it again to finish "
            "(objects that are gone are reported unchanged)."
        ) from exc
    if gate is not None:
        return gate
    delete_folder = params.get("delete_folder", True) is not False
    rows = await _read_back_rows(connector, target, operator, plan, delete_folder)
    rows += _missing_rows(plan)
    if any(r["status"] == _ROW_FAILED for r in rows):
        guidance = "some objects still read back after the delete; re-check them (see 'rows')"
        return _response(plan, STATUS_STILL_PRESENT, rows, guidance)
    status = STATUS_DELETED if any(r["status"] == _ROW_DELETED for r in rows) else STATUS_UNCHANGED
    return _response(plan, status, rows, None)
