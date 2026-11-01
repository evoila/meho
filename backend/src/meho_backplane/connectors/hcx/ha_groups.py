# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The HCX Network Extension high availability (HA) operations: list, build and dissolve groups.

An HA group joins four Network Extension appliances, two at each site, in
an active and standby pair. Each function is the body of a thin shim
method on :class:`~meho_backplane.connectors.hcx.connector.HcxConnector`;
the op metadata lives in :mod:`meho_backplane.connectors.hcx.ha_group_ops`.

``hcx.network_extension.ha.list`` reads
``GET /hybridity/api/interconnect/appliances/ha/groups``: one row per group
with its members, their roles and the group state.

``hcx.network_extension.ha.set`` builds a group
(``POST /hybridity/api/interconnect/appliances/ha/groups``) or dissolves one
(``DELETE /hybridity/api/interconnect/appliances/ha/groups/{groupId}``). Both
calls start vendor work: the Manager answers ``202`` with
``{"data": {"interconnectTaskId", "groupId"}, "warnings", "errors"}``. The
answer says ``accepted``, gives the interconnect task id and names
``hcx.task.get`` as the status read; it never says the work is done.

Before a start the operation checks:

* build -- every named appliance exists, is a Network Extension appliance
  and runs HCX 4.3 or newer (older appliances cannot join a group). An
  appliance that is already in a group starts nothing (``already_grouped``).
* dissolve -- the group exists; a missing group starts nothing
  (``not_found``).
* both -- the Manager's queued or running interconnect tasks
  (``POST /hybridity/api/interconnect/tasks/query``). A task that runs on
  the same appliance or group starts nothing (``already_running``). This
  check helps; it is not a lock.

The approval request of a dissolve lists the network extensions the
group serves (the ``blast_radius`` block). They keep running on the
appliance pair that was active; only the HA protection ends. The
``preview_operation`` tool never contacts the HCX Manager, so it shows no
plan for a dissolve; only the approval request does.

The paths come from Broadcom's "HCX Workload Migration APIs" reference
(Create, List and Delete HA Group) and the HCX user guide (activating,
monitoring and deactivating Network Extension HA).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.hcx.session import HcxTargetLike
from meho_backplane.connectors.hcx.typed_reads import (
    _envelope_notes,
    _nested,
    _redact_object,
    _segment,
    redact_secrets,
    rows_of,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.connectors.hcx.connector import HcxConnector

__all__ = [
    "HA_SET_OP_ID",
    "MIN_HA_APPLIANCE_VERSION",
    "STATUS_READ",
    "VENDOR_CALLS",
    "ha_group_row",
    "hcx_network_extension_ha_list_impl",
    "hcx_network_extension_ha_set_impl",
]

_HA_GROUPS_PATH: Final = "/hybridity/api/interconnect/appliances/ha/groups"
_HA_GROUP_PATH: Final = "/hybridity/api/interconnect/appliances/ha/groups/{groupId}"
_APPLIANCES_PATH: Final = "/hybridity/api/interconnect/appliances"
_TASK_QUERY_PATH: Final = "/hybridity/api/interconnect/tasks/query"
_L2_EXTENSIONS_PATH: Final = "/hybridity/api/l2Extensions"

#: Every vendor call of this module, as ``(method, path template)``.
#: The reconcile lane checks each one against the pinned HCX API files.
VENDOR_CALLS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("GET", _HA_GROUPS_PATH),
        ("POST", _HA_GROUPS_PATH),
        ("DELETE", _HA_GROUP_PATH),
        ("GET", _APPLIANCES_PATH),
        ("POST", _TASK_QUERY_PATH),
        ("GET", _L2_EXTENSIONS_PATH),
    }
)

HA_SET_OP_ID: Final = "hcx.network_extension.ha.set"

#: The read that follows the interconnect task a build or dissolve starts.
STATUS_READ: Final = "hcx.task.get"

#: The vendor type of a Network Extension appliance.
_NE_APPLIANCE_TYPE: Final = "HCX-NET-EXT"

#: Oldest appliance version that can join an HA group (``major``, ``minor``).
MIN_HA_APPLIANCE_VERSION: Final = (4, 3)

#: Vendor states of an interconnect task that has not ended.
_ACTIVE_TASK_STATES: Final[tuple[str, ...]] = ("QUEUED", "RUNNING")


def _member_row(member: dict[str, Any]) -> dict[str, Any]:
    return {
        "appliance_id": member.get("applianceId"),
        "appliance_name": member.get("applianceName"),
        "peer_appliance_id": member.get("peerApplianceId"),
        "peer_appliance_name": member.get("peerApplianceName"),
        "role": member.get("role"),
    }


def _members(group: dict[str, Any]) -> list[dict[str, Any]]:
    members = group.get("groupMembers")
    return [m for m in members if isinstance(m, dict)] if isinstance(members, list) else []


def ha_group_row(group: dict[str, Any]) -> dict[str, Any]:
    """A vendor HA group with the main fields added flat."""
    flat = dict(group)
    flat["group_id"] = group.get("groupId")
    flat["service_mesh_id"] = group.get("serviceMeshId")
    flat["group_state"] = _nested(group, "status", "groupState")
    flat["status_message"] = _nested(group, "status", "statusMessage")
    flat["availability_state"] = _nested(group, "status", "availabilityState")
    flat["policy"] = _nested(group, "policyInfo", "policy")
    flat["network_extension_count"] = group.get("networkExtensionCount")
    flat["members"] = [_member_row(m) for m in _members(group)]
    return flat


async def _groups(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    *,
    service_mesh_id: str | None = None,
    all_members: bool = False,
) -> list[dict[str, Any]]:
    query = {"allGroupMembers": "true" if all_members else "false"}
    if service_mesh_id:
        query["serviceMeshId"] = service_mesh_id
    payload = await connector._get_json(target, _HA_GROUPS_PATH, operator=operator, params=query)
    return rows_of(payload)


async def hcx_network_extension_ha_list_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.network_extension.ha.list`` -- ``GET .../appliances/ha/groups``, one row per group."""
    groups = await _groups(
        connector,
        operator,
        target,
        service_mesh_id=params.get("service_mesh_id"),
        all_members=bool(params.get("all_group_members", False)),
    )
    return {"items": [ha_group_row(group) for group in redact_secrets(groups)]}


def _group_of(groups: list[dict[str, Any]], appliance_id: str) -> dict[str, Any] | None:
    """The group that holds *appliance_id* as a member or as a member's peer."""
    for group in groups:
        for member in _members(group):
            if appliance_id in (member.get("applianceId"), member.get("peerApplianceId")):
                return group
    return None


def _version_of(appliance: dict[str, Any]) -> tuple[int, int] | None:
    version = appliance.get("applianceVersion")
    if not isinstance(version, dict):
        return None
    major, minor = version.get("major"), version.get("minor")
    if not isinstance(major, int) or not isinstance(minor, int):
        return None
    return (major, minor)


async def _check_appliances(
    connector: HcxConnector, operator: Operator, target: HcxTargetLike, appliance_ids: list[str]
) -> None:
    """Raise ``ValueError`` unless every id is a Network Extension appliance on HCX 4.3+."""
    payload = await connector._get_json(target, _APPLIANCES_PATH, operator=operator)
    by_id = {row.get("applianceId"): row for row in rows_of(payload)}
    for appliance_id in appliance_ids:
        row = by_id.get(appliance_id)
        if row is None:
            raise ValueError(
                f"no appliance with id {appliance_id!r}; call hcx.appliance.list for the ids"
            )
        if row.get("applianceType") != _NE_APPLIANCE_TYPE:
            raise ValueError(
                f"appliance {appliance_id!r} is a {row.get('applianceType')!r} appliance; "
                f"only Network Extension appliances ({_NE_APPLIANCE_TYPE}) join an HA group"
            )
        version = _version_of(row)
        if version is not None and version < MIN_HA_APPLIANCE_VERSION:
            wanted = ".".join(str(part) for part in MIN_HA_APPLIANCE_VERSION)
            raise ValueError(
                f"appliance {appliance_id!r} runs HCX {version[0]}.{version[1]}; only "
                f"appliances on HCX {wanted} or newer can join an HA group. Upgrade it first"
            )


async def _running_task(
    connector: HcxConnector, operator: Operator, target: HcxTargetLike, entity_ids: list[str]
) -> dict[str, Any] | None:
    """A queued or running interconnect task on one of *entity_ids*, else ``None``."""
    body = {"filter": {"status": list(_ACTIVE_TASK_STATES), "entityIds": entity_ids}}
    payload = await connector._post_json(target, _TASK_QUERY_PATH, operator=operator, json=body)
    for task in rows_of(payload):
        if str(task.get("status") or "").upper() not in _ACTIVE_TASK_STATES:
            continue
        if _nested(task, "entityDetails", "id") in entity_ids:
            return task
    return None


def _answer(
    status: str,
    *,
    action: str,
    group_id: Any = None,
    task_id: Any = None,
    group_state: Any = None,
    notes: dict[str, list[Any]] | None = None,
) -> dict[str, Any]:
    return _redact_object(
        {
            "status": status,
            "action": action,
            "group_id": group_id,
            "interconnect_task_id": task_id,
            "status_read": STATUS_READ,
            "group_state": group_state,
            **(notes or {"warnings": [], "errors": []}),
        }
    )


def _accepted(payload: Any, *, action: str, group_id: Any) -> dict[str, Any]:
    """The answer of a start the Manager accepted; raises when it named no task."""
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    notes = _envelope_notes(payload)
    task_id = data.get("interconnectTaskId")
    if not task_id:
        messages = "; ".join(
            str(error.get("message")) for error in notes["errors"] if isinstance(error, dict)
        )
        raise ValueError(
            "the HCX Manager answered without an interconnect task id"
            + (f": {messages}" if messages else "")
        )
    return _answer(
        "accepted",
        action=action,
        group_id=data.get("groupId") or group_id,
        task_id=task_id,
        notes=notes,
    )


async def _build(
    connector: HcxConnector, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    appliance_ids = list(params["appliance_ids"])
    await _check_appliances(connector, operator, target, appliance_ids)
    groups = await _groups(connector, operator, target)
    for appliance_id in appliance_ids:
        group = _group_of(groups, appliance_id)
        if group is not None:
            return _answer(
                "already_grouped",
                action="build",
                group_id=group.get("groupId"),
                group_state=_nested(group, "status", "groupState"),
            )
    task = await _running_task(connector, operator, target, appliance_ids)
    if task is not None:
        return _answer("already_running", action="build", task_id=task.get("interconnectTaskId"))
    payload = await connector._post_json(
        target, _HA_GROUPS_PATH, operator=operator, json={"appliances": appliance_ids}
    )
    return _accepted(payload, action="build", group_id=None)


async def _dissolve(
    connector: HcxConnector, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    group_id = params["group_id"]
    path = _HA_GROUP_PATH.format(groupId=_segment("HA group", group_id))
    group = next(
        (g for g in await _groups(connector, operator, target) if g.get("groupId") == group_id),
        None,
    )
    if group is None:
        return _answer("not_found", action="dissolve", group_id=group_id)
    group_state = _nested(group, "status", "groupState")
    task = await _running_task(connector, operator, target, [group_id])
    if task is not None:
        return _answer(
            "already_running",
            action="dissolve",
            group_id=group_id,
            task_id=task.get("interconnectTaskId"),
            group_state=group_state,
        )
    payload = await connector._post_json(
        target, path, operator=operator, verb="DELETE", params={"force": "false"}
    )
    answer = _accepted(payload, action="dissolve", group_id=group_id)
    answer["group_state"] = group_state
    return answer


async def hcx_network_extension_ha_set_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.network_extension.ha.set`` -- build (``POST``) or dissolve (``DELETE``) an HA group."""
    if params["action"] == "build":
        return await _build(connector, operator, target, params)
    return await _dissolve(connector, operator, target, params)


def _extension_child(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "network_extension",
        "id": row.get("stretchId"),
        "name": _nested(row, "sourceNetwork", "networkName"),
    }


async def _ha_set_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """The ``blast_radius`` of a dissolve: the group and the network extensions it serves.

    Read-only. Raises when the group does not exist. The shared preview
    code catches that: the request still waits for approval, the approval
    request says the preview is unavailable and why, and after approval the
    run answers ``not_found`` and sends no change. Declines (``None``) for a
    build, and without a live HCX connector or target (the
    ``preview_operation`` tool passes none).
    """
    from meho_backplane.connectors.hcx.connector import HcxConnector

    connector = ctx.connector_instance
    group_id = ctx.params.get("group_id")
    if ctx.params.get("action") != "dissolve" or not isinstance(group_id, str):
        return None
    if not isinstance(connector, HcxConnector) or ctx.target is None:
        return None
    groups = await _groups(connector, ctx.operator, ctx.target)
    group = next((g for g in groups if g.get("groupId") == group_id), None)
    if group is None:
        raise ValueError(f"the HA group {group_id!r} does not exist")
    member_ids = {m.get("applianceId") for m in _members(group)} | {group_id}
    extensions = await connector._get_json(ctx.target, _L2_EXTENSIONS_PATH, operator=ctx.operator)
    children = sorted(
        (
            _extension_child(row)
            for row in rows_of(extensions)
            if _nested(row, "sourceAppliance", "applianceId") in member_ids
        ),
        key=lambda child: str(child["id"]),
    )
    return {
        "blast_radius": {
            "object": {
                "kind": "ha_group",
                "id": group_id,
                "name": group.get("name"),
                "group_state": _nested(group, "status", "groupState"),
                "members": [_member_row(m) for m in _members(group)],
            },
            "children": children,
            "irreversibility": (
                "HA protection ends for the listed network extensions. They keep running "
                "on the appliance pair that was active; the standby pair becomes "
                "standalone. To protect them again, build a new group."
            ),
            "match_count": len(children),
        }
    }


register_preview_builder(HA_SET_OP_ID, _ha_set_preview)
