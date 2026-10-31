# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Day-2 op specs for deployments: actions, lease and delete.

Shared by Aria Automation 8 and VCF Automation 9 (see :mod:`.day2_ops`).
Each function builds one :class:`Day2OpSpec` with every op id in its texts
under the prefix ``p``. The machine and disk specs are in
:mod:`._day2_specs_iaas`. Field names come from the pinned ``vra-sdk-go``
v0.6.5 files (``ResourceAction``, ``ResourceActionRequest``, ``Request``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

from meho_backplane.connectors.vcf_automation._day2 import (
    DEPLOYMENT_ACTION_IDS,
    LEASE_INPUT,
    RESOURCE_ACTION_IDS,
)
from meho_backplane.connectors.vcf_automation._day2_paths import (
    DEPLOYMENT_ACTIONS_PATH,
    DEPLOYMENT_REQUESTS_PATH,
    RESOURCE_REQUESTS_PATH,
)
from meho_backplane.connectors.vcf_automation._op_schemas import field, guidance, list_of, obj
from meho_backplane.connectors.vcf_automation._paths import (
    TENANT_DEPLOYMENT_API_DEPLOYMENT_PATH,
)

__all__ = [
    "CONSTRAINTS",
    "READ_TAGS",
    "TAGS",
    "WRITE_TAGS",
    "Day2OpSpec",
    "deployment_specs",
    "string_field",
    "tracker_schema",
]

_Safety = Literal["safe", "caution", "dangerous", "destructive"]


@dataclass(frozen=True)
class Day2OpSpec:
    """One day-2 op, without its connector. ``name`` follows the op id prefix."""

    name: str
    handler: str
    path: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any]
    tags: tuple[str, ...]
    safety_level: _Safety
    requires_approval: bool
    llm_instructions: dict[str, Any]


def string_field(description: str, **extra: Any) -> dict[str, Any]:
    """A required-style string field: at least one character."""
    return {"type": "string", "minLength": 1, "description": description, **extra}


_REASON: dict[str, Any] = {
    "type": "string",
    "maxLength": 1024,
    "description": "Optional reason, kept with the request.",
}
_INPUTS: dict[str, Any] = {
    "type": "object",
    "description": (
        "The action inputs, by input name. The 'schema' of the action in "
        "deployment.actions.list names them."
    ),
    "additionalProperties": True,
}
_REQUEST_STATES = (
    "CREATED, PENDING, INITIALIZATION, CHECKING_APPROVAL, APPROVAL_PENDING, INPROGRESS, "
    "COMPLETION, APPROVAL_REJECTED, ABORTED, SUCCESSFUL or FAILED"
)

_REQUEST: dict[str, Any] = {
    **obj(
        "The day-2 request the appliance started.",
        {
            "id": field(["string", "null"], "The request id."),
            "name": field(["string", "null"], "A short label of the request."),
            "actionId": field(["string", "null"], "The action that runs."),
            "status": field(["string", "null"], f"The request state: {_REQUEST_STATES}."),
            "deploymentId": field(["string", "null"], "The deployment id."),
            "resourceIds": {
                "type": ["array", "null"],
                "description": "The resources the request applies to.",
                "items": {"type": "string", "description": "One resource id."},
            },
            "requestedBy": field(["string", "null"], "The user who started it."),
            "createdAt": field(["string", "null"], "When it started (ISO 8601, UTC)."),
        },
    ),
    "type": ["object", "null"],
}

_ACTION_ANSWER: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": field("string", "What the op did: submitted.", enum=["submitted"]),
        "request": _REQUEST,
        "guidance": field("string", "A short note on what to do next."),
    },
    "required": ["status", "request", "guidance"],
    "additionalProperties": True,
}

_TRACKER_FIELDS: dict[str, Any] = {
    "id": field(["string", "null"], "The request id."),
    "name": field(["string", "null"], "The operation name."),
    "status": field(["string", "null"], "FINISHED, INPROGRESS or FAILED."),
    "progress": field(["integer", "null"], "Progress in percent."),
    "message": field(["string", "null"], "The status message, for example why it failed."),
    "resources": {
        "type": "array",
        "description": "Links to the objects the request made, for example /iaas/api/...",
        "items": {"type": "string", "description": "One link."},
    },
}


def tracker_schema(description: str) -> dict[str, Any]:
    """An IaaS request tracker, or null."""
    return {**obj(description, _TRACKER_FIELDS), "type": ["object", "null"]}


TAGS: dict[str, Any] = {
    "type": "array",
    "description": "Optional tags to set on the new object.",
    "items": {
        "type": "object",
        "description": "One tag.",
        "properties": {
            "key": string_field("The tag key."),
            "value": {"type": "string", "description": "The tag value; may be empty."},
        },
        "required": ["key"],
        "additionalProperties": False,
    },
}
CONSTRAINTS: dict[str, Any] = {
    "type": "array",
    "description": "Optional placement rules, matched against the tags of zones and hosts.",
    "items": {
        "type": "object",
        "description": "One placement rule.",
        "properties": {
            "expression": string_field("A tag rule, for example '<tag-key>:<tag-value>'."),
            "mandatory": {
                "type": "boolean",
                "description": "True (default) when the rule must hold; false for a wish.",
            },
        },
        "required": ["expression"],
        "additionalProperties": False,
    },
}


READ_TAGS: Final[tuple[str, ...]] = ("read-only", "vcfa", "day2")
WRITE_TAGS: Final[tuple[str, ...]] = ("write", "vcfa", "day2")


def _actions_list(p: str) -> Day2OpSpec:
    return Day2OpSpec(
        name="deployment.actions.list",
        handler="deployment_actions_list",
        path=DEPLOYMENT_ACTIONS_PATH,
        summary="List the day-2 actions of a deployment or of one of its resources.",
        description=(
            "Reads GET /deployment/api/deployments/{deploymentId}/actions, or "
            "/deployment/api/resources/{resourceId}/actions for one resource such as a "
            "machine. Each action has an id, a name, valid (usable now) and an input schema."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "deployment_id": string_field(f"The deployment id (from {p}.deployment.list)."),
                "resource_id": string_field(
                    f"The resource id of one machine (the machine id from {p}.machine.list)."
                ),
            },
            "oneOf": [{"required": ["deployment_id"]}, {"required": ["resource_id"]}],
            "additionalProperties": False,
            "examples": [{"deployment_id": "<deployment-id>"}],
        },
        response_schema=obj(
            "The actions, as a flat list.",
            {
                "items": list_of(
                    "One row per action.",
                    {
                        "id": field("string", "The action id, for example 'Deployment.PowerOff'."),
                        "name": field("string", "The action name."),
                        "displayName": field("string", "The name the UI shows."),
                        "description": field(["string", "null"], "What the action does."),
                        "actionType": field(
                            ["string", "null"], "RESOURCE_ACTION (built in) or RESOURCE_EXTENSION."
                        ),
                        "valid": field(
                            ["boolean", "null"], "True when the action can run in the state now."
                        ),
                        "schema": field(
                            ["object", "null"], "The input schema: the names of the inputs."
                        ),
                    },
                ),
            },
        ),
        tags=(*READ_TAGS, "deployment", "action"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=guidance(
            when_to_call=(
                "Call before a day-2 action, to see which actions a deployment or machine "
                "offers now and which inputs each needs."
            ),
            output_shape=(
                "{items: [{id, name, displayName, description, actionType, valid, schema}]}. "
                "A long list comes back as a link to the stored result."
            ),
            next_step=(
                f"Run a valid built-in action with {p}.deployment.action.run (deployment) or "
                f"{p}.resource.action.run (machine), with inputs named in its schema."
            ),
            parameter_hints={
                "deployment_id": "Give this or resource_id, not both.",
                "resource_id": f"A machine id from {p}.machine.list is its resource id.",
            },
        ),
    )


def _deployment_action_run(p: str) -> Day2OpSpec:
    return Day2OpSpec(
        name="deployment.action.run",
        handler="deployment_action_run",
        path=DEPLOYMENT_REQUESTS_PATH,
        summary="Run a built-in day-2 action on a deployment: owner, lease, tags, power.",
        description=(
            "Sends POST /deployment/api/deployments/{deploymentId}/requests with one action "
            "from a fixed list: change owner, change lease, edit tags, power on, power off. "
            "Delete and custom actions are refused. Runs in the background."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "deployment_id": string_field(f"The deployment id (from {p}.deployment.list)."),
                "action_id": {
                    "type": "string",
                    "enum": list(DEPLOYMENT_ACTION_IDS),
                    "description": "The built-in action to run.",
                },
                "inputs": _INPUTS,
                "reason": _REASON,
            },
            "required": ["deployment_id", "action_id"],
            "additionalProperties": False,
            "examples": [
                {
                    "deployment_id": "<deployment-id>",
                    "action_id": "Deployment.PowerOff",
                    "reason": "<reason>",
                }
            ],
        },
        response_schema=_ACTION_ANSWER,
        tags=(*WRITE_TAGS, "deployment", "action"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=guidance(
            when_to_call=(
                "Call to power a whole deployment on or off, change its owner, lease or tags."
            ),
            preconditions=(
                f"Check with {p}.deployment.actions.list that the action is valid now and which "
                "inputs it needs. Power off stops every machine of the deployment. A second "
                "person must approve the call. Delete and custom actions are refused."
            ),
            output_shape=(
                "{status: submitted, request: {id, name, actionId, status, deploymentId, "
                "resourceIds, requestedBy, createdAt}, guidance}."
            ),
            next_step=(
                f"Read {p}.deployment.get until the last request is SUCCESSFUL or FAILED. "
                f"To delete, use {p}.deployment.delete."
            ),
        ),
    )


def _resource_action_run(p: str) -> Day2OpSpec:
    return Day2OpSpec(
        name="resource.action.run",
        handler="resource_action_run",
        path=RESOURCE_REQUESTS_PATH,
        summary="Run a built-in day-2 action on a machine: power, reboot, resize, disk, snapshot.",
        description=(
            "Sends POST /deployment/api/resources/{resourceId}/requests with one vSphere machine "
            "action from a fixed list: power on, power off, reboot, resize, add disk, create "
            "snapshot. Delete and custom actions are refused."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "resource_id": string_field(f"The machine id (from {p}.machine.list)."),
                "action_id": {
                    "type": "string",
                    "enum": list(RESOURCE_ACTION_IDS),
                    "description": "The built-in machine action to run.",
                },
                "inputs": _INPUTS,
                "reason": _REASON,
            },
            "required": ["resource_id", "action_id"],
            "additionalProperties": False,
            "examples": [
                {
                    "resource_id": "<machine-id>",
                    "action_id": "Cloud.vSphere.Machine.Resize",
                    "inputs": {"cpuCount": "<cpu-count>", "totalMemoryMB": "<memory-mb>"},
                }
            ],
        },
        response_schema=_ACTION_ANSWER,
        tags=(*WRITE_TAGS, "machine", "action"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=guidance(
            when_to_call=(
                "Call to power one machine on or off, reboot it, resize it, add a disk or "
                "take a snapshot."
            ),
            preconditions=(
                f"Check with {p}.deployment.actions.list (resource_id) that the action is valid "
                "now and which inputs it needs; resize takes cpuCount and totalMemoryMB. A "
                "power off or reboot stops the machine. A second person must approve the call."
            ),
            output_shape=(
                "{status: submitted, request: {id, name, actionId, status, deploymentId, "
                "resourceIds, requestedBy, createdAt}, guidance}."
            ),
            next_step=(
                f"Read {p}.deployment.get until the last request ends, then {p}.machine.list "
                "to see the new state."
            ),
        ),
    )


def _lease_change(p: str) -> Day2OpSpec:
    return Day2OpSpec(
        name="deployment.lease.change",
        handler="deployment_lease_change",
        path=DEPLOYMENT_REQUESTS_PATH,
        summary="Change the lease end date of one deployment.",
        description=(
            "Runs the Deployment.ChangeLease action: POST "
            "/deployment/api/deployments/{deploymentId}/requests with the new lease end. A "
            "lease policy may refuse a date past its maximum. Runs in the background."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "deployment_id": string_field(f"The deployment id (from {p}.deployment.list)."),
                "lease_expiration_date": string_field(
                    "The new lease end, an ISO 8601 date and time in UTC.",
                    format="date-time",
                ),
                "reason": _REASON,
            },
            "required": ["deployment_id", "lease_expiration_date"],
            "additionalProperties": False,
            "examples": [
                {
                    "deployment_id": "<deployment-id>",
                    "lease_expiration_date": "2030-01-31T00:00:00Z",
                }
            ],
        },
        response_schema=_ACTION_ANSWER,
        tags=(*WRITE_TAGS, "deployment", "lease"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions=guidance(
            when_to_call="Call to make a deployment live longer or end sooner.",
            preconditions=(
                f"Read the lease now with {p}.deployment.get (leaseExpireAt). When the lease "
                "ends, the appliance may power off or delete the deployment. A person must "
                f"approve the call. The date goes to the input '{LEASE_INPUT}'."
            ),
            output_shape=(
                "{status: submitted, request: {id, name, actionId, status, deploymentId, "
                "resourceIds, requestedBy, createdAt}, guidance}."
            ),
            next_step=(
                f"Read {p}.deployment.get until the last request ends and leaseExpireAt shows "
                "the new date. A FAILED request often means a lease policy limit."
            ),
        ),
    )


def _delete(p: str) -> Day2OpSpec:
    return Day2OpSpec(
        name="deployment.delete",
        handler="deployment_delete",
        path=TENANT_DEPLOYMENT_API_DEPLOYMENT_PATH,
        summary="Delete one deployment and all its machines and disks.",
        description=(
            "Sends DELETE /deployment/api/deployments/{deploymentId}. This removes every "
            "resource of the deployment and cannot be undone. Runs in the background. An "
            "unknown id answers 'unchanged'."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "id": string_field(f"The deployment id (from {p}.deployment.list)."),
            },
            "required": ["id"],
            "additionalProperties": False,
            "examples": [{"id": "<deployment-id>"}],
        },
        response_schema={
            "type": "object",
            "properties": {
                "status": field(
                    "string",
                    "What the op did: submitted or unchanged.",
                    enum=["submitted", "unchanged"],
                ),
                "request": {
                    **obj(
                        "The delete request; null on 'unchanged'.",
                        {
                            "id": field(["string", "null"], "The request id."),
                            "name": field(["string", "null"], "The request label."),
                            "status": field(["string", "null"], "The request state."),
                            "deploymentId": field(["string", "null"], "The deployment id."),
                        },
                    ),
                    "type": ["object", "null"],
                },
                "guidance": field(["string", "null"], "A short note on what to do next."),
            },
            "required": ["status", "guidance"],
            "additionalProperties": True,
        },
        tags=("write", "vcfa", "deployment", "delete", "destructive"),
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=guidance(
            when_to_call="Call to remove a deployment and its VMs, for example a test build.",
            preconditions=(
                "Only a person can run it, never an agent. First call preview_operation with the "
                "same inputs, then call_operation with its preview_hash. The approval card lists "
                "every resource that is deleted. A second person must approve."
            ),
            output_shape=(
                "{status: submitted | unchanged, request: {id, name, status, deploymentId}, "
                "guidance}."
            ),
            next_step=f"Read {p}.deployment.get with the same id until it answers not found.",
        ),
    )


def deployment_specs(p: str) -> tuple[Day2OpSpec, ...]:
    """The deployment day-2 specs under the op id prefix *p*."""
    return (
        _actions_list(p),
        _deployment_action_run(p),
        _resource_action_run(p),
        _lease_change(p),
        _delete(p),
    )
