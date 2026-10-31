# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``cluster.drs_vm_host_rule.create`` with ``use_existing_groups=true``.

By default ``cluster.drs_vm_host_rule.create`` makes a new VM group and a new
host group together with the rule (code in ``_write.py``). With
``use_existing_groups=true`` it makes no group: it adds only the rule, on a VM
group and a host group that are already on the cluster. Same vim call
(``ReconfigureComputeResource_Task`` with one ``rulesSpec`` ``add``,
``modify=true``), same response shape, same risk level.

* a taken rule name -> ``rule_exists``;
* a missing VM group or host group (or one of the other kind) ->
  ``group_not_found``;

both before any write. ``resolved_vms`` / ``resolved_hosts`` list the members
of the two groups, by name, on the approval card and in the result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._drs_group import (
    GROUP_TYPES,
    find_group,
    member_moids,
    member_rows,
    read_cluster,
    reconfigure_cluster,
)
from meho_backplane.connectors.vmware_rest.composites._drs_rule_delete import _rules
from meho_backplane.connectors.vmware_rest.composites._teardown import capped
from meho_backplane.connectors.vmware_rest.composites._write import (
    _CLUSTER_HOST_GROUP_TYPE,
    _CLUSTER_RULE_SPEC_TYPE,
    _CLUSTER_VM_GROUP_TYPE,
    _CLUSTER_VM_HOST_RULE_INFO_TYPE,
)
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = ["create_rule_on_existing_groups", "existing_groups_preview"]


async def _group_rows(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    props: dict[str, Any],
    name: Any,
    group_type: str,
) -> list[dict[str, Any]] | None:
    """``[{vm|host, name}]`` members of group *name* of *group_type*; ``None`` if no such group."""
    group = find_group(props, name) if isinstance(name, str) else None
    if group is None or group.get(VIM_TYPE_NAME_KEY) != group_type:
        return None
    _kind, member_field, mo_type = GROUP_TYPES[group_type]
    rows = await member_rows(
        connector, target, operator, mo_type=mo_type, moids=member_moids(group)
    )
    return [{member_field: row["moid"], "name": row["name"]} for row in rows]


async def _read_groups(
    connector: VmwareRestConnector, target: Any, operator: Operator, params: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]] | None, list[dict[str, Any]] | None]:
    """The cluster props and the members of the named VM group and host group."""
    props = await read_cluster(connector, target, operator, params["cluster"]) or {}
    vms = await _group_rows(
        connector, target, operator, props, params.get("vm_group_name"), _CLUSTER_VM_GROUP_TYPE
    )
    hosts = await _group_rows(
        connector, target, operator, props, params.get("host_group_name"), _CLUSTER_HOST_GROUP_TYPE
    )
    return props, vms, hosts


def _refusal(
    props: dict[str, Any],
    params: dict[str, Any],
    vms: list[dict[str, Any]] | None,
    hosts: list[dict[str, Any]] | None,
) -> tuple[str, str] | None:
    """``(status, guidance)`` when the rule must not be added, else ``None``."""
    cluster, rule_name = params["cluster"], params["rule_name"]
    if any(rule.get("name") == rule_name for rule in _rules(props)):
        return (
            "rule_exists",
            f"a DRS rule named {rule_name!r} already exists on cluster {cluster!r}; "
            "pick a new name or remove the existing rule",
        )
    missing = []
    if vms is None:
        missing.append(f"VM group {params.get('vm_group_name')!r}")
    if hosts is None:
        missing.append(f"host group {params.get('host_group_name')!r}")
    if missing:
        return (
            "group_not_found",
            f"cluster {cluster!r} has no {' and no '.join(missing)}; check the names, or "
            "leave out use_existing_groups to create new groups",
        )
    return None


async def create_rule_on_existing_groups(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any] | OperationResult:
    """Add a VM-Host rule on a VM group and a host group that already exist."""
    affine = bool(params.get("affine", True))
    mandatory = bool(params.get("mandatory", False))
    enabled = bool(params.get("enabled", True))
    props, vms, hosts = await _read_groups(connector, target, operator, params)
    base = {
        "cluster": params["cluster"],
        "rule_name": params["rule_name"],
        "vm_group_name": params["vm_group_name"],
        "host_group_name": params["host_group_name"],
        "affine": affine,
        "mandatory": mandatory,
        "enabled": enabled,
        "task": None,
        "resolved_vms": vms or [],
        "resolved_hosts": hosts or [],
        "guidance": None,
    }
    refusal = _refusal(props, params, vms, hosts)
    if refusal is not None:
        return {**base, "status": refusal[0], "guidance": refusal[1]}
    host_group_key = "affineHostGroupName" if affine else "antiAffineHostGroupName"
    rule_info = {
        VIM_TYPE_NAME_KEY: _CLUSTER_VM_HOST_RULE_INFO_TYPE,
        "name": params["rule_name"],
        "enabled": enabled,
        "mandatory": mandatory,
        "vmGroupName": params["vm_group_name"],
        host_group_key: params["host_group_name"],
    }
    rule_spec = {VIM_TYPE_NAME_KEY: _CLUSTER_RULE_SPEC_TYPE, "operation": "add", "info": rule_info}
    outcome = await reconfigure_cluster(
        connector,
        target,
        operator,
        cluster=params["cluster"],
        spec={"rulesSpec": [rule_spec]},
        params={k: params[k] for k in ("cluster", "rule_name", "vm_group_name", "host_group_name")},
        label="cluster.drs_vm_host_rule.create",
    )
    if isinstance(outcome, OperationResult):
        return outcome
    if outcome.timed_out:
        return {
            **base,
            "status": "timeout",
            "task": outcome.task,
            "guidance": (
                f"ReconfigureComputeResource_Task {outcome.task} did not finish within the poll "
                "bound; read the cluster's DRS rules -- the rule add may still complete"
            ),
        }
    return {**base, "status": "created", "task": outcome.task}


async def existing_groups_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Approval card for the existing-groups arm: both groups and their members, by name.

    Each member list is cut to :data:`~._teardown.LIST_CAP` rows; ``total_vms``
    and ``total_hosts`` carry the full numbers (the same keys as the new-groups
    arm). A missing group shows ``null`` members and the ``group_not_found``
    refusal the handler will apply.
    """
    params = ctx.params
    if ctx.connector_instance is None or not isinstance(params.get("cluster"), str):
        return None
    connector: Any = ctx.connector_instance
    props, vms, hosts = await _read_groups(connector, ctx.target, ctx.operator, params)
    effect: dict[str, Any] = {
        "cluster": params["cluster"],
        "cluster_name": props.get("name"),
        "rule_name": params.get("rule_name"),
        "use_existing_groups": True,
        "vm_group_name": params.get("vm_group_name"),
        "host_group_name": params.get("host_group_name"),
        "affine": bool(params.get("affine", True)),
        "mandatory": bool(params.get("mandatory", False)),
        "enabled": bool(params.get("enabled", True)),
        "resolved_vms": capped(vms) if vms is not None else None,
        "total_vms": len(vms) if vms is not None else None,
        "resolved_hosts": capped(hosts) if hosts is not None else None,
        "total_hosts": len(hosts) if hosts is not None else None,
    }
    if isinstance(params.get("rule_name"), str):
        refusal = _refusal(props, params, vms, hosts)
        if refusal is not None:
            effect["refusal"] = {"status": refusal[0], "guidance": refusal[1]}
    return effect
