# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``cluster.drs_rule.delete`` -- remove one DRS rule from a cluster, by name.

A DRS rule tells vCenter where to run VMs: keep them together, keep them
apart, or run them on (or away from) a group of hosts. This op is the governed
opposite of ``cluster.drs_rule.create`` and ``cluster.drs_vm_host_rule.create``.

There is no REST path for cluster rules, so the delete uses the same vim call
as the creates: ``ClusterComputeResource.ReconfigureComputeResource_Task``
with one ``rulesSpec`` entry, ``operation="remove"`` and ``removeKey`` set to
the rule's key. ``modify=true``, so nothing else on the cluster changes. The
call is the same for every rule type. VM groups and host groups are not
removed: a VM-Host rule's groups stay on the cluster.

It follows the plan-then-act seam of :mod:`._teardown`. One read-only plan is
used by both the park-time preview and the handler, so the approver sees the
same decision the handler makes:

* no rule with that name -> ``unchanged`` (nothing is written);
* two or more rules with that name -> ``precondition_failed`` (the name does
  not pick one rule);
* otherwise the gated reconfigure, a task poll, then a new read of the
  cluster's rules: ``deleted`` only when the rule's key is gone.

``safety_level="destructive"`` + ``requires_approval=True``: a second person
always approves, after reading the rule on the approval card.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._teardown import (
    LIST_CAP,
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
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moid_problem,
    moref_values,
    read_names,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _CLUSTER_AFFINITY_RULE_TYPE,
    _CLUSTER_ANTI_AFFINITY_RULE_TYPE,
    _CLUSTER_COMPUTE_RESOURCE_MO_TYPE,
    _CLUSTER_CONFIG_SPEC_EX_TYPE,
    _CLUSTER_RULE_SPEC_TYPE,
    _CLUSTER_VM_HOST_RULE_INFO_TYPE,
    _OP_CONTINUE_RETRIEVE_PROPERTIES,
    _OP_RECONFIGURE_COMPUTE_RESOURCE_TASK,
    _OP_RETRIEVE_PROPERTIES,
    _PROP_CONFIGURATION_EX,
    _VIRTUAL_MACHINE_MO_TYPE,
    _cluster_config_items,
    _unwrap_value,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import CLUSTER_MOID_PATTERN
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY, VIM_VALUE_KEY
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "cluster_drs_rule_delete_composite",
    "cluster_drs_rule_delete_preview",
    "plan_drs_rule_delete",
]

#: vi-json sub-op manifest (``_VIM_SUB_OPS_*`` namespace, so the vcenter.yaml
#: sweep skips it; the vi-json reconcile lane checks every path exists). The
#: same pair as ``cluster.drs_rule.create``: read the rules, then reconfigure.
#: The VM name read for the approval card follows continuation pages (#4862).
_VIM_SUB_OPS_CLUSTER_DRS_RULE_DELETE: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_CONTINUE_RETRIEVE_PROPERTIES,
    _OP_RECONFIGURE_COMPUTE_RESOURCE_TASK,
)

#: Wall-clock bound for the reconfigure task poll -- the 600 s convention;
#: module-global so tests can zero it.
_TASK_TIMEOUT_SECONDS = 600.0

#: ``ArrayUpdateSpec.removeKey`` is typed ``Any`` in the pinned vi-json.yaml,
#: so the int key travels boxed: ``{"_typeName": "int", "_value": <key>}``
#: (the ``Any`` discriminator maps ``int`` to ``PrimitiveInt``).
_VIM_INT_TYPE: Final = "int"

#: The whole ``configurationEx``, not ``configurationEx.rule``: vCenter 8.0.x
#: rejects a path into it with ``InvalidProperty`` (see ``_PROP_CONFIGURATION_EX``).
_CLUSTER_PROPS: Final = ["name", _PROP_CONFIGURATION_EX]

#: vim rule-info ``_typeName`` -> the rule type shown on the approval card.
#: Any other rule type is shown by its vim type name.
_RULE_TYPES: Final[dict[str, str]] = {
    _CLUSTER_AFFINITY_RULE_TYPE: "affinity",
    _CLUSTER_ANTI_AFFINITY_RULE_TYPE: "anti_affinity",
    _CLUSTER_VM_HOST_RULE_INFO_TYPE: "vm_host",
}


def _rules(props: dict[str, Any]) -> list[dict[str, Any]]:
    """The cluster's rule-info dicts (an absent ``rule`` array means no rules)."""
    return _cluster_config_items(props.get(_PROP_CONFIGURATION_EX), "rule")


def _rule_type(rule: dict[str, Any]) -> str | None:
    type_name = rule.get(VIM_TYPE_NAME_KEY)
    if not isinstance(type_name, str):
        return None
    return _RULE_TYPES.get(type_name, type_name)


def _rule_key(rule: dict[str, Any]) -> int | None:
    key = rule.get("key")
    return key if isinstance(key, int) and not isinstance(key, bool) else None


async def _describe_rule(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    rule: dict[str, Any],
) -> dict[str, Any]:
    """The rule's fields for the approval card (VM names are best effort)."""
    out: dict[str, Any] = {
        "key": _rule_key(rule),
        "rule_type": _rule_type(rule),
        "enabled": rule.get("enabled"),
        "mandatory": rule.get("mandatory"),
    }
    vm_moids = [moid for _type, moid in moref_values(rule.get("vm"))]
    if vm_moids:
        shown = vm_moids[:LIST_CAP]
        names = await read_names(
            connector, target, operator, mo_type=_VIRTUAL_MACHINE_MO_TYPE, moids=shown
        )
        out["vms"] = [{"vm": moid, "name": names.get(moid)} for moid in shown]
        out["vm_count"] = len(vm_moids)
    if rule.get(VIM_TYPE_NAME_KEY) == _CLUSTER_VM_HOST_RULE_INFO_TYPE:
        out["vm_group"] = rule.get("vmGroupName")
        out["affine_host_group"] = rule.get("affineHostGroupName")
        out["anti_affine_host_group"] = rule.get("antiAffineHostGroupName")
    return out


async def plan_drs_rule_delete(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
) -> TeardownPlan:
    """Plan a DRS rule delete (read-only).

    One ``RetrievePropertiesEx`` of the cluster's ``name`` and whole
    ``configurationEx``; the rules are its ``rule`` array. A
    ``configurationEx`` vCenter could not read fails the call before any
    write (:func:`read_object`). An unknown cluster is an
    ``invalid_request``; no matching rule means absent; two or more matching
    rules are refused, because the name does not pick one.
    """
    cluster = params["cluster"]
    rule_name = params["rule_name"]
    obj: dict[str, Any] = {"kind": "drs_rule", "cluster": cluster, "name": rule_name}
    problem = moid_problem(cluster, CLUSTER_MOID_PATTERN, "cluster")
    if problem is not None:
        return TeardownPlan(obj, False, refusal=(STATUS_INVALID_REQUEST, problem))
    props = await read_object(
        connector,
        target,
        operator,
        mo_type=_CLUSTER_COMPUTE_RESOURCE_MO_TYPE,
        moid=cluster,
        props=_CLUSTER_PROPS,
    )
    if props is None:
        refusal = (STATUS_INVALID_REQUEST, f"cluster {cluster!r} not found; pass a cluster moid")
        return TeardownPlan(obj, False, refusal=refusal)
    obj["cluster_name"] = props.get("name")
    matches = [rule for rule in _rules(props) if rule.get("name") == rule_name]
    if not matches:
        return TeardownPlan(object=obj, present=False)
    if len(matches) > 1:
        plan = TeardownPlan(object=obj, present=True)
        plan.blockers = [
            {"kind": "drs_rule", "key": _rule_key(rule), "rule_type": _rule_type(rule)}
            for rule in matches[:LIST_CAP]
        ]
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"{len(matches)} DRS rules on cluster {cluster!r} are named {rule_name!r}; the "
            "name does not pick one rule, so nothing was deleted. Give the rules "
            "different names first (see 'blockers')",
        )
        return plan
    rule = matches[0]
    obj.update(await _describe_rule(connector, target, operator, rule))
    plan = TeardownPlan(object=obj, present=True)
    key = _rule_key(rule)
    if key is None:
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"vCenter returned DRS rule {rule_name!r} without a key; it cannot be removed",
        )
        return plan
    plan.context["key"] = key
    return plan


async def _rule_key_present(
    connector: VmwareRestConnector, target: Any, operator: Operator, *, cluster: str, key: int
) -> bool:
    """A new read of the cluster's rules: is a rule with *key* still there?"""
    props = await read_object(
        connector,
        target,
        operator,
        mo_type=_CLUSTER_COMPUTE_RESOURCE_MO_TYPE,
        moid=cluster,
        props=[_PROP_CONFIGURATION_EX],
    )
    if props is None:
        return False
    return any(_rule_key(rule) == key for rule in _rules(props))


def _remove_rule_body(key: int) -> dict[str, Any]:
    """The ``ReconfigureComputeResource_Task`` body that removes rule *key* only."""
    return {
        "spec": {
            VIM_TYPE_NAME_KEY: _CLUSTER_CONFIG_SPEC_EX_TYPE,
            "rulesSpec": [
                {
                    VIM_TYPE_NAME_KEY: _CLUSTER_RULE_SPEC_TYPE,
                    "operation": "remove",
                    "removeKey": {VIM_TYPE_NAME_KEY: _VIM_INT_TYPE, VIM_VALUE_KEY: key},
                }
            ],
        },
        "modify": True,
    }


async def cluster_drs_rule_delete_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Remove one DRS rule from a cluster, by name.

    Op-id: ``vmware.composite.cluster.drs_rule.delete``. ``unchanged`` when no
    rule has the name; refused when two or more do. Otherwise the gated
    ``ReconfigureComputeResource_Task`` (a parked / denied gate is returned
    as is, nothing reaches vCenter), polled to the end. A task fault raises
    (``connector_error``, audited as failed). ``deleted`` only when a new
    read of the cluster's rules no longer shows the rule's key.
    """
    plan = await plan_drs_rule_delete(connector, target, operator, params)
    early = pre_write_outcome(
        plan, absent_guidance="the cluster has no DRS rule with that name; nothing was deleted"
    )
    if early is not None:
        return early
    cluster = params["cluster"]
    key: int = plan.context["key"]
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_RECONFIGURE_COMPUTE_RESOURCE_TASK,
        vmomi_path=f"/ClusterComputeResource/{cluster}/ReconfigureComputeResource_Task",
        body=_remove_rule_body(key),
        params={"cluster": cluster, "rule_name": params["rule_name"], "rule_key": key},
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
    if outcome.state == TASK_STATE_ERROR:
        raise RuntimeError(
            f"cluster.drs_rule.delete: ReconfigureComputeResource_Task on cluster "
            f"{cluster!r} faulted: {outcome.error_message or '<no fault reported>'}"
        )
    if outcome.timed_out:
        return timeout_envelope(
            plan,
            outcome,
            method="ReconfigureComputeResource_Task",
            reread="re-run to confirm (no rule with that name = unchanged)",
        )
    if await _rule_key_present(connector, target, operator, cluster=cluster, key=key):
        return envelope(
            plan,
            STATUS_STILL_PRESENT,
            task=outcome.task,
            task_state=outcome.state,
            guidance=(
                "ReconfigureComputeResource_Task succeeded but the rule still reads back "
                "on the cluster; re-check the cluster's DRS rules"
            ),
        )
    return envelope(plan, STATUS_DELETED, task=outcome.task, task_state=outcome.state)


async def cluster_drs_rule_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius for ``cluster.drs_rule.delete`` (the handler's own plan)."""
    cluster = ctx.params.get("cluster")
    rule_name = ctx.params.get("rule_name")
    if (
        not isinstance(cluster, str)
        or not isinstance(rule_name, str)
        or ctx.connector_instance is None
    ):
        return None
    plan = await plan_drs_rule_delete(
        ctx.connector_instance,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        {"cluster": cluster, "rule_name": rule_name},
    )
    return blast_radius(plan)
