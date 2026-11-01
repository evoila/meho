# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Delete a vApp and the VMs in it -- ``vmware.composite.vapp.delete`` (``destructive``).

One ``VirtualApp.Destroy_Task``: vCenter deletes the vApp and every VM in it,
with their disks. It uses the plan-then-act seam of :mod:`._teardown`: one
read-only plan feeds the approval-card ``blast_radius`` and the handler, and
the handler plans again after approval, so it acts on the vApp as it is then.

The plan refuses before any write when the vApp is not stopped, when a VM in
it is not powered off, or when it holds other vApps or resource pools (their
VMs would go too, and the plan does not list them). A vApp that does not
exist is ``unchanged``. After the task MEHO reads the vApp again: ``deleted``
only when it is gone.

``children`` and ``blockers`` carry every VM, however many: the approval card
shows all that goes, and the stored result keeps the whole list (JSONFlux
stores a long list under a result handle; ``result_query`` with ``path``
``/children`` pages it). Nothing is cut before approval or storage.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._spec import CompositeSpec
from meho_backplane.connectors.vmware_rest.composites._teardown import (
    STATUS_INVALID_REQUEST,
    STATUS_PRECONDITION_FAILED,
    TeardownPlan,
    blast_radius,
    destroy_outcome,
    destroy_task,
    pre_write_outcome,
    timeout_envelope,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moid_problem,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._vapp import (
    VAPP_MO_TYPE,
    child_pools,
    read_vapp,
    vapp_state,
    vm_rows,
)
from meho_backplane.connectors.vmware_rest.composites._vapp_schemas import (
    VAPP_DELETE_LLM_INSTRUCTIONS,
    VAPP_DELETE_PARAMETER_SCHEMA,
    VAPP_DELETE_RESPONSE_SCHEMA,
    VAPP_MOID_PATTERN,
)
from meho_backplane.connectors.vmware_rest.composites._write import _OP_DESTROY_VAPP_TASK

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "COMPOSITES",
    "plan_vapp_delete",
    "vapp_delete_composite",
]

OP_ID_VAPP_DELETE: Final = "vmware.composite.vapp.delete"

_POWERED_OFF: Final = "poweredOff"
_STOPPED: Final = "stopped"
_LOSS: Final = "the vApp, its settings, and every VM in it with their disks"


async def plan_vapp_delete(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
) -> TeardownPlan:
    """Plan a vApp delete (read-only); the preview and the handler share it."""
    vapp = params["vapp"]
    obj: dict[str, Any] = {"kind": "vapp", "vapp": vapp}
    problem = moid_problem(vapp, VAPP_MOID_PATTERN, "vapp")
    if problem is not None:
        return TeardownPlan(obj, False, refusal=(STATUS_INVALID_REQUEST, problem))
    props = await read_vapp(connector, target, operator, vapp)
    if props is None:
        return TeardownPlan(object=obj, present=False)
    rows, complete = await vm_rows(connector, target, operator, props)
    state = vapp_state(props)
    obj.update(name=props.get("name"), state=state, vm_count=len(rows), lost_with_it=_LOSS)
    children = [
        {"kind": "vm", "moid": r["vm"], "name": r["name"], "power_state": r["power_state"]}
        for r in rows
    ]
    plan = TeardownPlan(object=obj, present=True, children=children)
    plan.blockers = [c for c in children if c["power_state"] != _POWERED_OFF] + [
        {"kind": kind, "moid": moid} for kind, moid in child_pools(props)
    ]
    if not complete:
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            "vCenter did not fully answer the read of the VMs in the vApp; refusing to delete "
            "on a partial answer",
        )
    elif state != _STOPPED or any(b["kind"] == "vm" for b in plan.blockers):
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"the vApp is {state or 'in an unknown state'} or a VM in it is not powered off; "
            "power it off first with vmware.composite.vapp.power (action power_off)",
        )
    elif plan.blockers:
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            "the vApp holds other vApps or resource pools (see 'blockers'); delete or move "
            "them first",
        )
    return plan


async def vapp_delete_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Delete one stopped vApp and its VMs. Op-id: ``vmware.composite.vapp.delete``."""
    plan = await plan_vapp_delete(connector, target, operator, params)
    early = pre_write_outcome(plan, absent_guidance="the vApp does not exist; nothing was deleted")
    if early is not None:
        return {**early, "children": plan.children}
    vapp = params["vapp"]
    outcome = await destroy_task(
        connector,
        target,
        operator,
        op_id=_OP_DESTROY_VAPP_TASK,
        mo_type=VAPP_MO_TYPE,
        moid=vapp,
        params={"op": OP_ID_VAPP_DELETE, "vapp": vapp},
        label="vapp.delete",
    )
    if isinstance(outcome, OperationResult):
        return outcome
    if outcome.timed_out:
        envelope = timeout_envelope(
            plan,
            outcome,
            method="Destroy_Task",
            reread="call again: a vApp that is gone is unchanged",
        )
        return {**envelope, "children": plan.children}
    after = await read_object(
        connector, target, operator, mo_type=VAPP_MO_TYPE, moid=vapp, props=["name"]
    )
    envelope = destroy_outcome(plan, outcome, gone=after is None, what="vApp")
    return {**envelope, "children": plan.children}


async def _vapp_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius for ``vapp.delete`` (the handler's own plan)."""
    vapp = ctx.params.get("vapp")
    if not isinstance(vapp, str) or ctx.connector_instance is None:
        return None
    plan = await plan_vapp_delete(
        ctx.connector_instance,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        {"vapp": vapp},
    )
    return blast_radius(plan)


#: The registration rows; ``_register.py`` collects ``COMPOSITES`` from every module.
COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id=OP_ID_VAPP_DELETE,
        handler=vapp_delete_composite,
        summary="Delete a powered-off vApp and every VM in it, with their disks.",
        description=(
            "Deletes a vApp, named by its id, and every VM in it with their disks. The "
            "vApp and its VMs must be powered off. This cannot be undone. A second person "
            "always approves, after a preview."
        ),
        parameter_schema=VAPP_DELETE_PARAMETER_SCHEMA,
        response_schema=VAPP_DELETE_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "write", "vapp", "delete", "vi-json"],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=VAPP_DELETE_LLM_INSTRUCTIONS,
        preview=_vapp_delete_preview,
    ),
)
