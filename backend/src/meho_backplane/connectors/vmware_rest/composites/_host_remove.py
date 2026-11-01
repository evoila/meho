# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``host.remove`` -- remove a host from the vCenter inventory (#4349).

A host in a cluster is removed with ``HostSystem.Destroy_Task``. A host that
is alone sits in its own single-host compute resource, so that compute
resource is removed with ``ComputeResource.Destroy_Task`` (the host goes with
it). vCenter then forgets the host and every VM registered on it; the VM files
stay on the datastores.

It follows the plan-then-act seam of :mod:`._teardown`: one read-only plan
feeds both the approval-card preview (the mandatory ``blast_radius``) and the
handler, so the approver reads the same decision the handler makes.

* a host that is connected and not in maintenance mode is refused before any
  write (``precondition_failed``); a host that is disconnected or not
  responding may be removed (it cannot enter maintenance mode);
* the preview refuses a host that does not exist, so nothing waits for
  approval; the handler answers ``unchanged`` when the host went away between
  the approval and the run;
* otherwise the gated ``Destroy_Task``, a task poll, then a new read: ``removed``
  only when the host is gone.

``safety_level="destructive"`` + ``requires_approval=True``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._host_membership import (
    needs_vcenter,
    read_host_state,
)
from meho_backplane.connectors.vmware_rest.composites._teardown import (
    LIST_CAP,
    STATUS_PRECONDITION_FAILED,
    TeardownPlan,
    blast_radius,
    destroy_outcome,
    destroy_task,
    envelope,
    pre_write_outcome,
    timeout_envelope,
    vm_rows,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _CLUSTER_COMPUTE_RESOURCE_MO_TYPE,
    _HOST_SYSTEM_MO_TYPE,
)
from meho_backplane.operations._preview import register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = ["OP_ID_HOST_REMOVE", "host_remove_composite", "host_remove_preview", "plan_host_remove"]

OP_ID_HOST_REMOVE: Final = "vmware.composite.host.remove"

_OP_DESTROY_HOST: Final = "POST:/HostSystem/{moId}/Destroy_Task"
_OP_DESTROY_COMPUTE_RESOURCE: Final = "POST:/ComputeResource/{moId}/Destroy_Task"

_COMPUTE_RESOURCE_MO_TYPE: Final = "ComputeResource"
_CONNECTED: Final = "connected"


async def plan_host_remove(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
) -> TeardownPlan:
    """Plan a host remove (read-only): the host, the VMs that go with it, the refusal."""
    host = params["host"]
    obj: dict[str, Any] = {"kind": "host", "moid": host}
    state = await read_host_state(connector, target, operator, host)
    if state is None:
        return TeardownPlan(object=obj, present=False)
    fields = state.as_fields()
    obj.update(
        name=fields["name"],
        parent=fields["parent"],
        parent_type=fields["parent_type"],
        connection_state=fields["connection_state"],
        in_maintenance_mode=fields["in_maintenance_mode"],
        vm_count=fields["vm_count"],
    )
    plan = TeardownPlan(object=obj, present=True)
    plan.children = await vm_rows(connector, target, operator, list(state.vms[:LIST_CAP]))
    if state.parent_type == _COMPUTE_RESOURCE_MO_TYPE and state.parent is not None:
        plan.children.append({"kind": "compute_resource", "moid": state.parent, "name": None})
        plan.context["destroy"] = (
            _OP_DESTROY_COMPUTE_RESOURCE,
            _COMPUTE_RESOURCE_MO_TYPE,
            state.parent,
        )
    elif state.parent_type == _CLUSTER_COMPUTE_RESOURCE_MO_TYPE:
        plan.context["destroy"] = (_OP_DESTROY_HOST, _HOST_SYSTEM_MO_TYPE, host)
    else:
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"vCenter gave no cluster or compute resource as the parent of host {host!r}; "
            "nothing was removed",
        )
        return plan
    if state.connection_state == _CONNECTED and state.in_maintenance_mode is not True:
        plan.blockers = [{"kind": "host", "moid": host, "reason": "not in maintenance mode"}]
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"host {host!r} is connected and not in maintenance mode; nothing was removed. "
            "Move its VMs away and put it into maintenance mode first (host.evacuate)",
        )
    return plan


async def host_remove_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Remove a host from vCenter (``Destroy_Task`` on the host or its compute resource)."""
    host = params["host"]
    refusal = needs_vcenter(target)
    if refusal is not None:
        plan = TeardownPlan(object={"kind": "host", "moid": host}, present=False)
        return envelope(plan, "needs_vcenter", guidance=refusal)
    plan = await plan_host_remove(connector, target, operator, params)
    early = pre_write_outcome(
        plan, absent_guidance="vCenter has no host with this id; nothing was removed"
    )
    if early is not None:
        return early
    op_id, mo_type, moid = plan.context["destroy"]
    outcome = await destroy_task(
        connector,
        target,
        operator,
        op_id=op_id,
        mo_type=mo_type,
        moid=moid,
        params={"host": host, "destroy": f"{mo_type}:{moid}"},
        label="host.remove",
    )
    if isinstance(outcome, OperationResult):
        return outcome
    if outcome.timed_out:
        return timeout_envelope(
            plan,
            outcome,
            method="Destroy_Task",
            reread="re-run to confirm (a host that is gone answers unchanged)",
        )
    after = await read_host_state(connector, target, operator, host)
    return {
        **destroy_outcome(plan, outcome, gone=after is None, what="host"),
        **({"status": "removed"} if after is None else {}),
    }


async def host_remove_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius of ``host.remove`` (the handler's own plan).

    Raises when the host does not exist: the call is then refused before it
    waits for approval (``blast_radius_missing_reason`` names this reason).
    """
    host = ctx.params.get("host")
    if not isinstance(host, str) or ctx.connector_instance is None:
        return None
    plan = await plan_host_remove(
        ctx.connector_instance,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        {"host": host},
    )
    if not plan.present:
        raise LookupError(f"vCenter has no host {host!r}; there is nothing to remove")
    preview = blast_radius(plan)
    preview["blast_radius"]["what_happens_next"] = (
        "vCenter forgets the host and every VM registered on it. The VM files stay on the "
        "datastores. To use the host again, add it again with host.add."
    )
    return preview


register_preview_builder(OP_ID_HOST_REMOVE, host_remove_preview)
