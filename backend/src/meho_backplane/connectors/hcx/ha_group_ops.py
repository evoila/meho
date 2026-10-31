# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the HCX Network Extension HA operations (the bodies live in ``ha_groups``).

``hcx.network_extension.ha.list`` is a ``safe`` read.
``hcx.network_extension.ha.set`` builds or dissolves a group: ``dangerous``
and ``requires_approval``, so it waits until a person approves it.
:mod:`meho_backplane.connectors.hcx.typed_ops` adds both to
``HCX_TYPED_OPS`` and registers them at start-up. Importing
:mod:`~meho_backplane.connectors.hcx.ha_groups` here registers the
dissolve preview.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.hcx import ha_groups as _ha_groups
from meho_backplane.connectors.hcx.op_types import HcxTypedOp, list_response_schema, string_schema

__all__ = [
    "GROUP_NETWORK_EXTENSION_HA",
    "HCX_HA_GROUP_OPS",
    "HCX_HA_GROUP_WHEN_TO_USE_BY_GROUP",
]

GROUP_NETWORK_EXTENSION_HA: Final = "hcx-network-extension-ha"

HCX_HA_GROUP_WHEN_TO_USE_BY_GROUP: Final[dict[str, str]] = {
    GROUP_NETWORK_EXTENSION_HA: (
        "Use for Network Extension high availability (HA): groups of four network "
        "extension appliances, an active and a standby pair, that keep extended "
        "networks up when one appliance fails. One read: the HA groups with their "
        "appliances, roles and state (hcx.network_extension.ha.list). One change, "
        "which waits for a person's approval: build a group from free appliances, or "
        "dissolve a group so its appliances are standalone again "
        "(hcx.network_extension.ha.set). Follow the started task with hcx.task.get."
    ),
}

_ID_SCHEMA: dict[str, Any] = {"type": "string", "minLength": 1}

_MEMBER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "appliance_id": string_schema("Id of the appliance."),
        "appliance_name": string_schema("Name of the appliance."),
        "peer_appliance_id": string_schema("Id of its partner appliance at the other site."),
        "peer_appliance_name": string_schema("Name of the partner appliance."),
        "role": string_schema("HA role: ACTIVE, STANDBY, UNDECIDED or UNINIT."),
    },
}

_LIST = HcxTypedOp(
    op_id="hcx.network_extension.ha.list",
    handler_attr="network_extension_ha_list",
    summary="List the Network Extension HA groups: appliances, their roles and the group state.",
    description=(
        "Returns one row per Network Extension HA group: group id and name, service mesh, "
        "group state (HEALTHY, DEGRADED, FAILINGOVER, FAILED, MAINTENANCE), the member "
        "appliances with their partner and role (ACTIVE or STANDBY), and how many networks "
        "it extends."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "service_mesh_id": {
                **_ID_SCHEMA,
                "description": "Only the groups of this service mesh. Leave out for all groups.",
            },
            "all_group_members": {
                "type": "boolean",
                "default": False,
                "description": (
                    "True: list the members at both sites. False (default): only the members "
                    "at this site; each one still names its partner at the other site."
                ),
            },
        },
        "additionalProperties": False,
        "examples": [{"service_mesh_id": "<service-mesh-id>", "all_group_members": True}],
    },
    response_schema=list_response_schema(
        "One row per HA group. The vendor fields stay; the main ones are flat.",
        {
            "group_id": string_schema("Id of the HA group."),
            "name": string_schema("Name of the HA group."),
            "service_mesh_id": string_schema("Id of the service mesh of the group."),
            "group_state": string_schema(
                "Group state: HEALTHY, DEGRADED, FAILINGOVER, FAILED or MAINTENANCE."
            ),
            "status_message": string_schema("The vendor's status text."),
            "availability_state": string_schema("The vendor's availability state."),
            "policy": string_schema("The HA policy of the group."),
            "network_extension_count": {
                "type": ["integer", "null"],
                "description": "How many networks the group extends.",
            },
            "members": {
                "type": "array",
                "description": "The member appliances with their partner and role.",
                "items": _MEMBER_SCHEMA,
            },
        },
    ),
    group_key=GROUP_NETWORK_EXTENSION_HA,
    tags=("read-only", "hcx", "network-extension", "high-availability"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see the HA groups and their health, before a build or dissolve, and to "
            "check the result after one."
        ),
        "output_shape": (
            "{items: [{group_id, name, service_mesh_id, group_state, status_message, "
            "availability_state, policy, network_extension_count, members: [{appliance_id, "
            "appliance_name, peer_appliance_id, peer_appliance_name, role}], ...vendor "
            "fields}]}. A long list is behind a result handle."
        ),
        "next_step": (
            "DEGRADED or FAILED: one or more appliances cannot take over; report it. Use "
            "group_id with hcx.network_extension.ha.set to dissolve a group. Use "
            "hcx.appliance.list to find free appliances for a new group."
        ),
    },
)

_ASYNC_HINT = (
    "Start it in MEHO's async mode (async: true), so MEHO keeps the answer with the task id "
    "under a run handle even if the reply is lost."
)

_SET = HcxTypedOp(
    op_id=_ha_groups.HA_SET_OP_ID,
    handler_attr="network_extension_ha_set",
    summary="Build or dissolve a Network Extension HA group. Starts an HCX task; needs approval.",
    description=(
        "build: make an HA group from a free Network Extension appliance at this site; HCX "
        "picks the standby pair. dissolve: end a group, its appliances become standalone. "
        "Returns 'accepted' with the task id; the work is not done then."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["build", "dissolve"],
                "description": "build: make a new HA group. dissolve: end an HA group.",
            },
            "appliance_ids": {
                "type": "array",
                "minItems": 1,
                "maxItems": 2,
                "uniqueItems": True,
                "items": {**_ID_SCHEMA, "description": "Id of a Network Extension appliance."},
                "description": (
                    "For build: one free Network Extension appliance at this site (HCX picks "
                    "the standby), or two to name both. Each must run HCX 4.3 or newer and "
                    "extend no network. Ids come from hcx.appliance.list."
                ),
            },
            "group_id": {
                **_ID_SCHEMA,
                "description": "For dissolve: id of the group (group_id in the HA group list).",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
        "oneOf": [
            {
                "properties": {
                    "action": {"const": "build", "description": "Build a group."},
                },
                "required": ["appliance_ids"],
                "not": {"required": ["group_id"]},
            },
            {
                "properties": {
                    "action": {"const": "dissolve", "description": "Dissolve a group."},
                },
                "required": ["group_id"],
                "not": {"required": ["appliance_ids"]},
            },
        ],
        "examples": [
            {"action": "build", "appliance_ids": ["<appliance-id>"]},
            {"action": "dissolve", "group_id": "<ha-group-id>"},
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["accepted", "already_grouped", "already_running", "not_found"],
                "description": (
                    "accepted: HCX took the task, the work is not done yet. already_grouped: "
                    "an appliance is already in a group; nothing started. already_running: a "
                    "task runs on the appliance or group; nothing started. not_found: no group "
                    "has this id; nothing started."
                ),
            },
            "action": {"type": "string", "description": "build or dissolve."},
            "group_id": string_schema("Id of the HA group, when known."),
            "interconnect_task_id": string_schema(
                "Id of the vendor task to follow with hcx.task.get; null when nothing started."
            ),
            "status_read": {
                "type": "string",
                "description": "The read that follows the task: hcx.task.get with the task id.",
            },
            "group_state": string_schema("State of the group before the call, when known."),
            "warnings": {
                "type": "array",
                "description": "The Manager's warnings.",
                "items": {"type": "object"},
            },
            "errors": {
                "type": "array",
                "description": "The Manager's errors.",
                "items": {"type": "object"},
            },
        },
    },
    group_key=GROUP_NETWORK_EXTENSION_HA,
    tags=("hcx", "network-extension", "high-availability", "change", "vendor-task"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call with action build to protect network extensions against an appliance "
            "failure, or with action dissolve to end a group, for example before the "
            "service mesh is changed."
        ),
        "preconditions": (
            "Build needs a Network Extension appliance at the source site that extends no "
            "network, a second free pair for HCX to pick, appliances on HCX 4.3 or newer "
            "and the HCX Enterprise licence; HCX refuses otherwise and the error is in the "
            "answer. Dissolve ends the HA protection of every network the group extends: "
            "they keep running on the active pair, but one more appliance failure takes "
            "them down. Call preview_operation first to see those networks. A second "
            "person must approve the call. " + _ASYNC_HINT
        ),
        "output_shape": (
            "{status: accepted|already_grouped|already_running|not_found, action, group_id, "
            "interconnect_task_id, status_read: 'hcx.task.get', group_state, warnings, "
            "errors}. accepted never means done."
        ),
        "next_step": (
            "Call hcx.task.get with interconnect_task_id until done is true, then "
            "hcx.network_extension.ha.list: after a build the group shows ACTIVE and "
            "STANDBY roles; after a dissolve the group is gone. A failed task step names a "
            "job_id; hcx.job.get shows its error. If the reply was lost, do not start "
            "again: call hcx.network_extension.ha.list first; a group with the appliance, "
            "or a missing group after a dissolve, means the task was started."
        ),
    },
)

HCX_HA_GROUP_OPS: Final[tuple[HcxTypedOp, ...]] = (_LIST, _SET)
