# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``cluster.drs_group.delete`` -- remove one DRS VM group or host group, by name.

``cluster.drs_rule.delete`` leaves a VM-Host rule's groups on the cluster on
purpose. This op removes such a group. The VMs and hosts in it are not
touched: only the group goes.

Same vim call as the other DRS ops:
``ClusterComputeResource.ReconfigureComputeResource_Task`` with one
``groupSpec`` entry, ``operation="remove"`` and ``removeKey`` = the group
name (vim keys groups by name, as ``govc cluster.group.remove`` does),
``modify=true``. ``removeKey`` is typed ``Any`` in the pinned vi-json.yaml,
so the name travels boxed: ``{"_typeName": "string", "_value": <name>}``.

It follows the plan-then-act seam of :mod:`._teardown`, so the approval card
and the handler use the same decision:

* no group with that name -> ``unchanged`` (nothing is written);
* a rule still uses the group -> ``precondition_failed`` before any write;
  the rules are listed in ``blockers`` and named in ``guidance``;
* otherwise the gated reconfigure, a task poll, then a new read:
  ``deleted`` only when the group name is gone.

``safety_level="destructive"`` + ``requires_approval=True``: a second person
always approves, after reading the group and its members on the card.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._drs_group import (
    find_group,
    group_kind,
    member_moids,
    member_rows,
    read_cluster,
    reconfigure_cluster,
    rules_using_group,
)
from meho_backplane.connectors.vmware_rest.composites._teardown import (
    STATUS_DELETED,
    STATUS_INVALID_REQUEST,
    STATUS_PRECONDITION_FAILED,
    STATUS_STILL_PRESENT,
    TeardownPlan,
    blast_radius,
    envelope,
    pre_write_outcome,
    timeout_envelope,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import moid_problem
from meho_backplane.connectors.vmware_rest.composites._write import (
    _CLUSTER_GROUP_SPEC_TYPE,
    _OP_CONTINUE_RETRIEVE_PROPERTIES,
    _OP_RECONFIGURE_COMPUTE_RESOURCE_TASK,
    _OP_RETRIEVE_PROPERTIES,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import CLUSTER_MOID_PATTERN
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY, VIM_VALUE_KEY

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "cluster_drs_group_delete_composite",
    "cluster_drs_group_delete_preview",
    "plan_drs_group_delete",
]

#: vi-json sub-op manifest: the cluster read, the member name read (paged),
#: then the same reconfigure as the DRS rule ops.
_VIM_SUB_OPS_CLUSTER_DRS_GROUP_DELETE: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_CONTINUE_RETRIEVE_PROPERTIES,
    _OP_RECONFIGURE_COMPUTE_RESOURCE_TASK,
)

#: ``removeKey`` is ``Any``-typed; a group is keyed by its name, a vim string.
_VIM_STRING_TYPE: Final = "string"


async def plan_drs_group_delete(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
) -> TeardownPlan:
    """Plan a DRS group delete (read-only): the group, its members, the rules using it."""
    cluster = params["cluster"]
    group_name = params["group_name"]
    obj: dict[str, Any] = {"kind": "drs_group", "cluster": cluster, "name": group_name}
    problem = moid_problem(cluster, CLUSTER_MOID_PATTERN, "cluster")
    if problem is not None:
        return TeardownPlan(obj, False, refusal=(STATUS_INVALID_REQUEST, problem))
    props = await read_cluster(connector, target, operator, cluster)
    if props is None:
        refusal = (STATUS_INVALID_REQUEST, f"cluster {cluster!r} not found; pass a cluster moid")
        return TeardownPlan(obj, False, refusal=refusal)
    obj["cluster_name"] = props.get("name")
    group = find_group(props, group_name)
    if group is None:
        return TeardownPlan(object=obj, present=False)
    kind = group_kind(group)
    moids = member_moids(group)
    obj["group_kind"] = kind[0] if kind is not None else group.get(VIM_TYPE_NAME_KEY)
    if kind is not None:
        obj["members"] = await member_rows(
            connector, target, operator, mo_type=kind[2], moids=moids
        )
    obj["member_count"] = len(moids)
    plan = TeardownPlan(object=obj, present=True)
    plan.blockers = rules_using_group(props, group_name)
    if plan.blockers:
        names = [rule["name"] for rule in plan.blockers]
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"DRS rule(s) {names!r} on cluster {cluster!r} still use group {group_name!r}, "
            "so nothing was deleted. Delete those rules first with cluster.drs_rule.delete",
        )
    return plan


def _remove_group_body(group_name: str) -> dict[str, Any]:
    """The ``groupSpec`` that removes group *group_name* only."""
    return {
        "groupSpec": [
            {
                VIM_TYPE_NAME_KEY: _CLUSTER_GROUP_SPEC_TYPE,
                "operation": "remove",
                "removeKey": {VIM_TYPE_NAME_KEY: _VIM_STRING_TYPE, VIM_VALUE_KEY: group_name},
            }
        ]
    }


async def cluster_drs_group_delete_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Remove one DRS group from a cluster, by name.

    Op-id: ``vmware.composite.cluster.drs_group.delete``. ``unchanged`` when
    no group has the name; refused while a rule uses it. Otherwise the gated
    reconfigure (a parked / denied gate is returned as is), polled to the end.
    A task fault raises. ``deleted`` only when a new read no longer shows the
    group.
    """
    plan = await plan_drs_group_delete(connector, target, operator, params)
    early = pre_write_outcome(
        plan, absent_guidance="the cluster has no DRS group with that name; nothing was deleted"
    )
    if early is not None:
        return early
    cluster, group_name = params["cluster"], params["group_name"]
    outcome = await reconfigure_cluster(
        connector,
        target,
        operator,
        cluster=cluster,
        spec=_remove_group_body(group_name),
        params={"cluster": cluster, "group_name": group_name},
        label="cluster.drs_group.delete",
    )
    if isinstance(outcome, OperationResult):
        return outcome
    if outcome.timed_out:
        return timeout_envelope(
            plan,
            outcome,
            method="ReconfigureComputeResource_Task",
            reread="re-run to confirm (no group with that name = unchanged)",
        )
    props = await read_cluster(connector, target, operator, cluster)
    if props is not None and find_group(props, group_name) is not None:
        return envelope(
            plan,
            STATUS_STILL_PRESENT,
            task=outcome.task,
            task_state=outcome.state,
            guidance=(
                "ReconfigureComputeResource_Task succeeded but the group still reads back "
                "on the cluster; re-check the cluster's DRS groups"
            ),
        )
    return envelope(plan, STATUS_DELETED, task=outcome.task, task_state=outcome.state)


async def cluster_drs_group_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius for ``cluster.drs_group.delete`` (the handler's own plan)."""
    cluster = ctx.params.get("cluster")
    group_name = ctx.params.get("group_name")
    if (
        not isinstance(cluster, str)
        or not isinstance(group_name, str)
        or ctx.connector_instance is None
    ):
        return None
    plan = await plan_drs_group_delete(
        ctx.connector_instance,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        {"cluster": cluster, "group_name": group_name},
    )
    return blast_radius(plan)
