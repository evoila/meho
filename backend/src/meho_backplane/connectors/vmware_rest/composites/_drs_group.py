# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``cluster.drs_group.members.set`` -- change the members of one DRS group.

A DRS group is a named list of VMs (a VM group) or of hosts (a host group) on
one cluster. A VM-Host rule runs the VMs of a VM group on (or away from) the
hosts of a host group.

* ``cluster.drs_group.members.set`` adds VMs to, or removes VMs from, an
  existing VM group (or hosts from a host group). ``caution`` +
  ``requires_approval=True``.

The group delete is in :mod:`._drs_group_delete`, and the
``use_existing_groups=true`` arm of ``cluster.drs_vm_host_rule.create`` in
:mod:`._drs_vm_host_rule_existing`. Both use the group reads below. All of
them use the same vim
call as the DRS rule ops: ``ClusterComputeResource.ReconfigureComputeResource_Task``
with ``modify=true``, so nothing else on the cluster changes. A group ``edit``
replaces the whole member list (that is how vim works, and how
``govc cluster.group.change`` does it), so the handler reads the group first
and sends the full new list. Every read takes the whole ``configurationEx``
(vCenter 8.0.x rejects a path into it).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._drs_rule_delete import (
    _CLUSTER_PROPS,
    _rule_key,
    _rule_type,
    _rules,
)
from meho_backplane.connectors.vmware_rest.composites._teardown import (
    STATUS_INVALID_REQUEST,
    STATUS_TIMEOUT,
    STATUS_UNCHANGED,
    capped,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moid_problem,
    moref_values,
    read_names,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _CLUSTER_COMPUTE_RESOURCE_MO_TYPE,
    _CLUSTER_CONFIG_SPEC_EX_TYPE,
    _CLUSTER_GROUP_SPEC_TYPE,
    _CLUSTER_HOST_GROUP_TYPE,
    _CLUSTER_VM_GROUP_TYPE,
    _HOST_SYSTEM_MO_TYPE,
    _OP_CONTINUE_RETRIEVE_PROPERTIES,
    _OP_RECONFIGURE_COMPUTE_RESOURCE_TASK,
    _OP_RETRIEVE_PROPERTIES,
    _PROP_CONFIGURATION_EX,
    _VIRTUAL_MACHINE_MO_TYPE,
    _cluster_config_items,
    _resolve_drs_rule_vms,
    _resolve_named_hosts_in_cluster,
    _unwrap_value,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import CLUSTER_MOID_PATTERN
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY, vim_moref
from meho_backplane.connectors.vmware_rest.vim_task import (
    TASK_STATE_ERROR,
    VimTaskResult,
    poll_vim_task,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "GROUP_TYPES",
    "approval_lists",
    "cluster_drs_group_members_set_composite",
    "cluster_drs_group_members_set_preview",
    "find_group",
    "group_kind",
    "member_moids",
    "member_rows",
    "read_cluster",
    "reconfigure_cluster",
    "rules_using_group",
]

#: vi-json sub-op manifest (``_VIM_SUB_OPS_*`` namespace, so the vcenter.yaml
#: sweep skips it): the cluster read, the member name read (paged), then the
#: reconfigure. The VM / host name lookups are REST reads on the session,
#: not listed here (the same as ``cluster.drs_vm_host_rule.create``).
_VIM_SUB_OPS_CLUSTER_DRS_GROUP_MEMBERS_SET: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_CONTINUE_RETRIEVE_PROPERTIES,
    _OP_RECONFIGURE_COMPUTE_RESOURCE_TASK,
)

#: Wall-clock bound for the reconfigure task poll (the 600 s convention);
#: module-global so tests can zero it.
_TASK_TIMEOUT_SECONDS = 600.0

STATUS_UPDATED: Final = "updated"
STATUS_GROUP_NOT_FOUND: Final = "group_not_found"
STATUS_NAMES_NOT_FOUND: Final = "names_not_found"
STATUS_MISMATCH: Final = "mismatch"

#: vim group type -> (group kind, member field, member MO type).
GROUP_TYPES: Final[dict[str, tuple[str, str, str]]] = {
    _CLUSTER_VM_GROUP_TYPE: ("vm", "vm", _VIRTUAL_MACHINE_MO_TYPE),
    _CLUSTER_HOST_GROUP_TYPE: ("host", "host", _HOST_SYSTEM_MO_TYPE),
}

#: Rule fields that name a group: VM-Host rules and VM-VM dependency rules.
_RULE_GROUP_FIELDS: Final = (
    "vmGroupName",
    "affineHostGroupName",
    "antiAffineHostGroupName",
    "vmGroup",
    "dependsOnVmGroup",
)


# ---------------------------------------------------------------------------
# Shared group reads (also used by _drs_group_delete + _drs_vm_host_rule_existing)
# ---------------------------------------------------------------------------


def find_group(props: dict[str, Any], name: str) -> dict[str, Any] | None:
    """The group named *name* in the cluster's ``configurationEx``, or ``None``."""
    for group in _cluster_config_items(props.get(_PROP_CONFIGURATION_EX), "group"):
        if group.get("name") == name:
            return group
    return None


def group_kind(group: dict[str, Any]) -> tuple[str, str, str] | None:
    """``(kind, member field, member MO type)`` of a VM or host group, else ``None``."""
    type_name = group.get(VIM_TYPE_NAME_KEY)
    return GROUP_TYPES.get(type_name) if isinstance(type_name, str) else None


def member_moids(group: dict[str, Any]) -> list[str]:
    """The member moids of a VM or host group, in vCenter's order."""
    kind = group_kind(group)
    if kind is None:
        return []
    return [moid for _type, moid in moref_values(group.get(kind[1]))]


def rules_using_group(props: dict[str, Any], name: str) -> list[dict[str, Any]]:
    """``[{kind: drs_rule, name, key, rule_type}]`` for every rule that names group *name*."""
    return [
        {
            "kind": "drs_rule",
            "name": rule.get("name"),
            "key": _rule_key(rule),
            "rule_type": _rule_type(rule),
        }
        for rule in _rules(props)
        if any(rule.get(f) == name for f in _RULE_GROUP_FIELDS)
    ]


async def member_rows(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    mo_type: str,
    moids: list[str],
) -> list[dict[str, Any]]:
    """``[{moid, name}]`` for every moid, in vCenter's order (names best effort).

    The whole list, however long: an operation result must carry the full
    membership (JSONFlux stores a large list and ``result_query`` reads it
    back). Only the approval cards cut it, see :func:`approval_lists`.
    """
    names = await read_names(connector, target, operator, mo_type=mo_type, moids=moids)
    return [{"moid": moid, "name": names.get(moid)} for moid in moids]


def approval_lists(card: dict[str, Any], *keys: tuple[str, str]) -> dict[str, Any]:
    """A copy of *card* for the approval display: each list cut to :data:`LIST_CAP`.

    *keys* are ``(list key, count key)`` pairs; the count key carries the
    full length, so the approver sees the true size next to the first rows.
    """
    out = dict(card)
    for key, count_key in keys:
        rows = card.get(key)
        if isinstance(rows, list):
            out[key] = capped(rows)
            out[count_key] = len(rows)
    return out


async def reconfigure_cluster(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    cluster: str,
    spec: dict[str, Any],
    params: dict[str, Any],
    label: str,
) -> OperationResult | VimTaskResult:
    """Gate + issue one ``ReconfigureComputeResource_Task`` (``modify=true``) and poll it.

    A parked / denied gate comes back as is (nothing reaches vCenter). A task
    fault raises (``connector_error``, audited as failed).
    """
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_RECONFIGURE_COMPUTE_RESOURCE_TASK,
        vmomi_path=f"/ClusterComputeResource/{cluster}/ReconfigureComputeResource_Task",
        body={"spec": {VIM_TYPE_NAME_KEY: _CLUSTER_CONFIG_SPEC_EX_TYPE, **spec}, "modify": True},
        params=params,
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
            f"{label}: ReconfigureComputeResource_Task on cluster {cluster!r} faulted: "
            f"{outcome.error_message or '<no fault reported>'}"
        )
    return outcome


async def read_cluster(
    connector: VmwareRestConnector, target: Any, operator: Operator, cluster: str
) -> dict[str, Any] | None:
    """The cluster's ``name`` and whole ``configurationEx``; ``None`` for an unknown cluster."""
    return await read_object(
        connector,
        target,
        operator,
        mo_type=_CLUSTER_COMPUTE_RESOURCE_MO_TYPE,
        moid=cluster,
        props=_CLUSTER_PROPS,
    )


# ---------------------------------------------------------------------------
# cluster.drs_group.members.set
# ---------------------------------------------------------------------------


@dataclass
class _MembersPlan:
    """The read-only decision ``members.set`` is taken on (shared with the preview)."""

    card: dict[str, Any]
    refusal: tuple[str, str] | None = None
    group_type: str = ""
    new_members: list[str] = field(default_factory=list)
    changed: bool = False


def _names(params: dict[str, Any], key: str) -> list[str]:
    """The distinct string names under *key*, in the order given."""
    raw = params.get(key)
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(n for n in raw if isinstance(n, str)))


async def _resolve_in_cluster(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    kind: str,
    cluster: str,
    names: list[str],
) -> list[dict[str, Any]]:
    """``[{moid, name}]`` of the VMs or hosts in *cluster* with one of *names*."""
    if not names:
        return []
    if kind == "vm":
        rows = await _resolve_drs_rule_vms(
            connector=connector,
            target=target,
            operator=operator,
            cluster_moid=cluster,
            vm_names=names,
        )
        return [{"moid": r["vm"], "name": r["name"]} for r in rows]
    rows = await _resolve_named_hosts_in_cluster(
        connector=connector,
        target=target,
        operator=operator,
        cluster_moid=cluster,
        host_names=names,
    )
    return [{"moid": r["host"], "name": r["name"]} for r in rows]


async def plan_members_set(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
) -> _MembersPlan:
    """Plan a member change (read-only): what changes, and whether it must be refused.

    Names to add must all be found in the cluster, or nothing is written
    (``names_not_found``). Names to remove that are not in the group are
    listed under ``not_members`` and skipped, so the op is safe to run twice.
    """
    cluster, group_name = params["cluster"], params["group_name"]
    add, remove = _names(params, "add"), _names(params, "remove")
    card: dict[str, Any] = {"cluster": cluster, "group_name": group_name}
    problem = moid_problem(cluster, CLUSTER_MOID_PATTERN, "cluster")
    if problem is None and not (add or remove):
        problem = "give at least one name in 'add' or 'remove'"
    both = sorted(set(add) & set(remove))
    if problem is None and both:
        problem = f"name(s) {both!r} are in both 'add' and 'remove'; pick one"
    if problem is not None:
        return _MembersPlan(card, refusal=(STATUS_INVALID_REQUEST, problem))
    props = await read_cluster(connector, target, operator, cluster)
    if props is None:
        detail = f"cluster {cluster!r} not found; pass a cluster moid"
        return _MembersPlan(card, refusal=(STATUS_INVALID_REQUEST, detail))
    card["cluster_name"] = props.get("name")
    group = find_group(props, group_name)
    kind = group_kind(group) if group is not None else None
    if group is None or kind is None:
        detail = f"cluster {cluster!r} has no VM group or host group named {group_name!r}"
        return _MembersPlan(card, refusal=(STATUS_GROUP_NOT_FOUND, detail))
    card["group_kind"] = kind[0]
    card["rules"] = rules_using_group(props, group_name)
    before = member_moids(group)
    card["members_before"] = await member_rows(
        connector, target, operator, mo_type=kind[2], moids=before
    )
    card["member_count_before"] = len(before)
    added = await _resolve_in_cluster(
        connector, target, operator, kind=kind[0], cluster=cluster, names=add
    )
    found = {row["name"] for row in added}
    card["not_found"] = [n for n in add if n not in found]
    removed_rows = await _resolve_in_cluster(
        connector, target, operator, kind=kind[0], cluster=cluster, names=remove
    )
    card["removed"] = [row for row in removed_rows if row["moid"] in before]
    gone = {row["name"] for row in card["removed"]}
    card["not_members"] = [n for n in remove if n not in gone]
    present = {row["name"] for row in added if row["moid"] in before}
    card["already_members"] = [n for n in add if n in present]
    card["added"] = [row for row in added if row["moid"] not in before]
    drop = {row["moid"] for row in card["removed"]}
    new = [m for m in before if m not in drop] + [r["moid"] for r in card["added"]]
    card["member_count_after"] = len(new)
    plan = _MembersPlan(card, group_type=str(group[VIM_TYPE_NAME_KEY]), new_members=new)
    plan.changed = new != before
    if card["not_found"]:
        plan.refusal = (
            STATUS_NAMES_NOT_FOUND,
            f"no {kind[0]} in cluster {cluster!r} has the name(s) {card['not_found']!r}; "
            "nothing was changed. Check the names",
        )
    return plan


def _members_envelope(
    plan: _MembersPlan,
    status: str,
    *,
    task: str | None = None,
    task_state: str | None = None,
    guidance: str | None = None,
    members: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {**plan.card, "status": status}
    out.update(task=task, task_state=task_state, guidance=guidance)
    if members is not None:
        out["members"] = members
    return out


async def cluster_drs_group_members_set_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Add members to, or remove members from, one existing DRS group.

    Op-id: ``vmware.composite.cluster.drs_group.members.set``. Refusals
    (``invalid_request`` / ``group_not_found`` / ``names_not_found``) and
    ``unchanged`` write nothing. Otherwise one gated reconfigure with a
    ``groupSpec`` ``edit`` that carries the full new member list, a task
    poll, and a new read of the group: ``updated`` only when the members read
    back as planned, else ``mismatch``.
    """
    plan = await plan_members_set(connector, target, operator, params)
    if plan.refusal is not None:
        return _members_envelope(plan, plan.refusal[0], guidance=plan.refusal[1])
    if not plan.changed:
        guidance = "the group already has these members; nothing was changed"
        return _members_envelope(plan, STATUS_UNCHANGED, guidance=guidance)
    cluster, group_name = params["cluster"], params["group_name"]
    kind = GROUP_TYPES[plan.group_type]
    info = {
        VIM_TYPE_NAME_KEY: plan.group_type,
        "name": group_name,
        kind[1]: [vim_moref(kind[2], moid) for moid in plan.new_members],
    }
    outcome = await reconfigure_cluster(
        connector,
        target,
        operator,
        cluster=cluster,
        spec={
            "groupSpec": [
                {VIM_TYPE_NAME_KEY: _CLUSTER_GROUP_SPEC_TYPE, "operation": "edit", "info": info}
            ]
        },
        params={
            "cluster": cluster,
            "group_name": group_name,
            "add": [row["moid"] for row in plan.card["added"]],
            "remove": [row["moid"] for row in plan.card["removed"]],
        },
        label="cluster.drs_group.members.set",
    )
    if isinstance(outcome, OperationResult):
        return outcome
    if outcome.timed_out:
        return _members_envelope(
            plan,
            STATUS_TIMEOUT,
            task=outcome.task,
            task_state=outcome.state,
            guidance=(
                f"ReconfigureComputeResource_Task {outcome.task} did not finish within the poll "
                "bound; it may still complete -- run the op again to check the members"
            ),
        )
    props = await read_cluster(connector, target, operator, cluster)
    group = find_group(props, group_name) if props is not None else None
    after = member_moids(group) if group is not None else []
    members = await member_rows(connector, target, operator, mo_type=kind[2], moids=after)
    if set(after) == set(plan.new_members):
        return _members_envelope(
            plan, STATUS_UPDATED, task=outcome.task, task_state=outcome.state, members=members
        )
    return _members_envelope(
        plan,
        STATUS_MISMATCH,
        task=outcome.task,
        task_state=outcome.state,
        members=members,
        guidance=(
            "ReconfigureComputeResource_Task succeeded but the group members read back "
            "differently; read the group again before you retry"
        ),
    )


async def cluster_drs_group_members_set_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Approval card for ``members.set``: the handler's own plan (no write).

    The member lists are cut to :data:`LIST_CAP` rows; ``member_count_before``,
    ``added_count`` and ``removed_count`` carry the full numbers.
    """
    p = ctx.params
    if (
        not isinstance(p.get("cluster"), str)
        or not isinstance(p.get("group_name"), str)
        or ctx.connector_instance is None
    ):
        return None
    plan = await plan_members_set(
        ctx.connector_instance,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        {k: p.get(k) for k in ("cluster", "group_name", "add", "remove")},
    )
    effect = approval_lists(
        plan.card,
        ("members_before", "member_count_before"),
        ("added", "added_count"),
        ("removed", "removed_count"),
    )
    if plan.refusal is not None:
        effect["refusal"] = {"status": plan.refusal[0], "guidance": plan.refusal[1]}
    elif not plan.changed:
        effect["refusal"] = {"status": STATUS_UNCHANGED, "guidance": "nothing would change"}
    return effect
