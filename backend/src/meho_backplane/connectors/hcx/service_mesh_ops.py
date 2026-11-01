# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the HCX service mesh changes and the interconnect task read.

``hcx.service_mesh.create``, ``hcx.service_mesh.update`` and
``hcx.service_mesh.resync`` are ``dangerous``; ``hcx.service_mesh.delete``
is ``destructive`` and has a preview builder. Every change waits for a
person's approval. ``hcx.task.get`` is the ``safe`` status read that
follows the task each change starts. The bodies live in
:mod:`meho_backplane.connectors.hcx.service_mesh_writes`.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.hcx.op_types import (
    GROUP_JOBS_ALERTS,
    GROUP_SERVICE_MESH,
    HcxTypedOp,
    string_schema,
)

__all__ = ["HCX_SERVICE_MESH_OPS"]

#: The HCX services a mesh can run. HCX 9.0 removed Disaster Recovery and
#: WAN optimisation, so they are left out.
_SERVICE_NAMES: Final[list[str]] = [
    "INTERCONNECT",
    "VMOTION",
    "BULK_MIGRATION",
    "RAV",
    "NETWORK_EXTENSION",
]

_MESH_ID_FIELD: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "description": "Id of the service mesh (serviceMeshId in hcx.service_mesh.list).",
}

_SERVICES_FIELD: Final[dict[str, Any]] = {
    "type": "array",
    "minItems": 1,
    "uniqueItems": True,
    "description": (
        "The HCX services the mesh runs. Each must be on in both compute profiles. "
        "INTERCONNECT is always needed."
    ),
    "items": {"type": "string", "enum": _SERVICE_NAMES, "description": "One HCX service."},
}

_NETWORKS_FIELD: Final[dict[str, Any]] = {
    "type": "array",
    "minItems": 1,
    "description": "Uplink networks the mesh uses instead of the profile's own uplinks.",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1, "description": "Id of the network profile."},
            "name": {"type": "string", "description": "Name of the network profile."},
        },
        "required": ["id"],
        "additionalProperties": False,
    },
}

_COMPUTE_PROFILES_FIELD: Final[dict[str, Any]] = {
    "type": "array",
    "minItems": 2,
    "maxItems": 2,
    "description": "The two compute profiles: first the local site's, then the paired site's.",
    "items": {
        "type": "object",
        "properties": {
            "endpoint_id": {
                "type": "string",
                "minLength": 1,
                "description": "Id of the site (endpointId in hcx.site_pairing.list or hcx.about).",
            },
            "endpoint_name": {"type": "string", "description": "Name of the site."},
            "compute_profile_id": {
                "type": "string",
                "minLength": 1,
                "description": "Id of the compute profile on that site.",
            },
            "compute_profile_name": {
                "type": "string",
                "description": "Name of the compute profile.",
            },
            "uplink_networks": _NETWORKS_FIELD,
        },
        "required": ["endpoint_id", "compute_profile_id"],
        "additionalProperties": False,
    },
}

_SWITCH_PAIRS_FIELD: Final[dict[str, Any]] = {
    "type": "array",
    "minItems": 1,
    "description": (
        "Switch pairs for the network extension service: one local and one remote "
        "switch from the compute profiles, and how many extension appliances to deploy."
    ),
    "items": {
        "type": "object",
        "properties": {
            "switches": {
                "type": "array",
                "minItems": 2,
                "maxItems": 2,
                "description": "The local switch, then the remote switch.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Id of the switch.",
                        },
                        "cmp_id": {
                            "type": "string",
                            "description": "Id of the vCenter or NSX instance the switch is in.",
                        },
                        "name": {"type": "string", "description": "Name of the switch."},
                        "type": {
                            "type": "string",
                            "description": "Vendor switch type, as the compute profile names it.",
                        },
                    },
                    "required": ["id"],
                    "additionalProperties": False,
                },
            },
            "network_extension_appliances": {
                "type": "integer",
                "minimum": 1,
                "description": "How many network extension appliances to deploy (default 1).",
            },
        },
        "required": ["switches"],
        "additionalProperties": False,
    },
}

_OPTION_FIELDS: Final[dict[str, Any]] = {
    "switch_pairs": _SWITCH_PAIRS_FIELD,
    "uplink_max_bandwidth": {
        "type": "integer",
        "minimum": 1,
        "description": "Most bandwidth the mesh may use on its uplinks, in Mbit/s.",
    },
    "app_path_resiliency": {
        "type": "boolean",
        "description": "Turn Application Path Resiliency on or off.",
    },
    "tcp_flow_conditioning": {
        "type": "boolean",
        "description": "Turn TCP flow conditioning on or off.",
    },
}

_NOTES_SCHEMA: Final[dict[str, Any]] = {
    "warnings": {
        "type": "array",
        "description": "Warnings the Manager returned.",
        "items": {"type": "object", "properties": {"message": string_schema("Warning text.")}},
    },
    "errors": {
        "type": "array",
        "description": "Errors the Manager returned.",
        "items": {
            "type": "object",
            "properties": {
                "code": string_schema("Vendor error code."),
                "message": string_schema("Error text."),
            },
        },
    },
}

_START_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["accepted", "already_running"],
            "description": (
                "accepted: the Manager took the job; it is not done yet. already_running: "
                "a task already runs on this mesh, so nothing was started."
            ),
        },
        "service_mesh_id": string_schema("Id of the service mesh."),
        "interconnect_task_id": string_schema("Id of the vendor task to follow with hcx.task.get."),
        "task_type": string_schema("Type of the running task (already_running only)."),
        "task_status": string_schema("Vendor state of the running task (already_running only)."),
        "status_read": {"type": "string", "description": "The read that follows the task."},
        **_NOTES_SCHEMA,
    },
}

_START_OUTPUT: Final = (
    "{status: accepted|already_running, service_mesh_id, interconnect_task_id, task_type, "
    "task_status, status_read: hcx.task.get, warnings, errors}. accepted means the Manager "
    "took the job, never that it is done."
)
_START_NEXT: Final = (
    "Call hcx.task.get with interconnect_task_id until done is true; status FAILED gives the "
    "vendor's error. On already_running, follow that task first, then decide whether to call "
    "again. After a lost reply, do not start again: call hcx.service_mesh.list and look at the "
    "mesh's last_task_id and last_task_status. Use async: true on call_operation so MEHO keeps "
    "the answer with the task id under a run handle."
)
_RUNNING_CHECK: Final = (
    "Before the start, it looks for a queued or running task on the same mesh and returns "
    "that task instead of starting a second one (a help, not a lock)."
)

_CREATE = HcxTypedOp(
    op_id="hcx.service_mesh.create",
    handler_attr="service_mesh_create",
    summary="Create an HCX service mesh from two compute profiles and a list of services.",
    description=(
        "Starts the creation of a service mesh between the local and a paired site, from "
        "one compute profile per site. HCX then deploys the appliances. Returns the "
        "accepted task; follow it with hcx.task.get."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1, "description": "Name of the new mesh."},
            "compute_profiles": _COMPUTE_PROFILES_FIELD,
            "services": _SERVICES_FIELD,
            **_OPTION_FIELDS,
        },
        "required": ["name", "compute_profiles", "services"],
        "additionalProperties": False,
        "examples": [
            {
                "name": "<service-mesh-name>",
                "compute_profiles": [
                    {"endpoint_id": "<local-site-id>", "compute_profile_id": "<local-profile-id>"},
                    {
                        "endpoint_id": "<remote-site-id>",
                        "compute_profile_id": "<remote-profile-id>",
                    },
                ],
                "services": ["INTERCONNECT", "VMOTION", "BULK_MIGRATION"],
            }
        ],
    },
    response_schema=_START_RESPONSE,
    group_key=GROUP_SERVICE_MESH,
    tags=("write", "hcx", "service-mesh"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call on the source site's HCX Manager to connect it to a paired site, so "
            "workloads can move and networks can be extended. " + _RUNNING_CHECK
        ),
        "preconditions": (
            "The sites are paired (hcx.site_pairing.list) and both have a compute profile "
            "with the chosen services. Run it on the source site; the destination cannot "
            "create the mesh. It waits for a person's approval."
        ),
        "output_shape": _START_OUTPUT,
        "next_step": _START_NEXT,
    },
)

_UPDATE = HcxTypedOp(
    op_id="hcx.service_mesh.update",
    handler_attr="service_mesh_update",
    summary="Change an HCX service mesh: its services, uplinks, compute profiles or settings.",
    description=(
        "Reads the service mesh, replaces the fields you set (services, compute profiles, "
        "uplinks, switch pairs, bandwidth, traffic settings) and sends it back. HCX can "
        "redeploy appliances. Returns the accepted task."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "service_mesh_id": _MESH_ID_FIELD,
            "services": _SERVICES_FIELD,
            "compute_profiles": _COMPUTE_PROFILES_FIELD,
            "uplinks": {
                "type": "array",
                "minItems": 1,
                "description": "Uplink changes: the new uplink networks of a compute profile.",
                "items": {
                    "type": "object",
                    "properties": {
                        "compute_profile_id": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Id of a compute profile the mesh uses.",
                        },
                        "networks": _NETWORKS_FIELD,
                    },
                    "required": ["compute_profile_id", "networks"],
                    "additionalProperties": False,
                },
            },
            **_OPTION_FIELDS,
        },
        "required": ["service_mesh_id"],
        "anyOf": [
            {"required": [field]}
            for field in ("services", "compute_profiles", "uplinks", *_OPTION_FIELDS)
        ],
        "additionalProperties": False,
        "examples": [
            {
                "service_mesh_id": "<service-mesh-id>",
                "services": ["INTERCONNECT", "VMOTION", "NETWORK_EXTENSION"],
            }
        ],
    },
    response_schema=_START_RESPONSE,
    group_key=GROUP_SERVICE_MESH,
    tags=("write", "hcx", "service-mesh"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to add or remove services of a mesh, change its uplinks, switch it to "
            "other compute profiles or change its bandwidth or traffic settings. Set only "
            "the fields to change; the others stay. " + _RUNNING_CHECK
        ),
        "preconditions": (
            "The mesh exists and is healthy (hcx.service_mesh.list). Run it on the source "
            "site. A new service must be on in both compute profiles. It waits for a "
            "person's approval."
        ),
        "output_shape": _START_OUTPUT,
        "next_step": _START_NEXT,
    },
)

_RESYNC = HcxTypedOp(
    op_id="hcx.service_mesh.resync",
    handler_attr="service_mesh_resync",
    summary="Resync an HCX service mesh with the current state of its compute profiles.",
    description=(
        "Lets HCX compare the service mesh with its compute profiles and apply the "
        "difference. It can redeploy appliances when a profile changed. Returns the "
        "accepted task."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"service_mesh_id": _MESH_ID_FIELD},
        "required": ["service_mesh_id"],
        "additionalProperties": False,
        "examples": [{"service_mesh_id": "<service-mesh-id>"}],
    },
    response_schema=_START_RESPONSE,
    group_key=GROUP_SERVICE_MESH,
    tags=("write", "hcx", "service-mesh"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call after a compute profile of the mesh changed, so the mesh takes the "
            "change. " + _RUNNING_CHECK
        ),
        "preconditions": (
            "The mesh exists and is healthy; a resync does not repair a mesh in an error "
            "state (use hcx.service_mesh.diagnose for that). Run it on the source site. "
            "It waits for a person's approval."
        ),
        "output_shape": _START_OUTPUT,
        "next_step": _START_NEXT,
    },
)

_DELETE = HcxTypedOp(
    op_id="hcx.service_mesh.delete",
    handler_attr="service_mesh_delete",
    summary="Delete an HCX service mesh and remove its appliances.",
    description=(
        "Starts the deletion of a service mesh. HCX removes its appliances; migrations and "
        "network extensions through the mesh stop. Needs a preview first. Returns the "
        "accepted task."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "service_mesh_id": _MESH_ID_FIELD,
            "force": {
                "type": "boolean",
                "description": ("Delete even when the paired site does not answer. Default false."),
            },
        },
        "required": ["service_mesh_id"],
        "additionalProperties": False,
        "examples": [{"service_mesh_id": "<service-mesh-id>"}],
    },
    response_schema=_START_RESPONSE,
    group_key=GROUP_SERVICE_MESH,
    tags=("write", "hcx", "service-mesh", "destructive"),
    safety_level="destructive",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to remove a service mesh that is no longer needed. " + _RUNNING_CHECK
        ),
        "preconditions": (
            "No network is extended and no migration runs through the mesh. A person runs "
            "it: first preview_operation with the same inputs, then call_operation with "
            "the preview_hash; it then waits for a second person's approval. An agent is "
            "always refused."
        ),
        "output_shape": _START_OUTPUT,
        "next_step": _START_NEXT,
    },
)

_TASK_GET = HcxTypedOp(
    op_id="hcx.task.get",
    handler_attr="task_get",
    summary="Read one HCX interconnect task: its state, progress, error and steps.",
    description=(
        "Returns one interconnect task, the kind a service mesh change starts: state "
        "(QUEUED, RUNNING, SUCCESS, FAILED), progress, message, the vendor error when it "
        "failed, and its steps. Says not_reachable when the Manager does not answer."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "minLength": 1,
                "description": "Id of the task (interconnect_task_id of a service mesh change).",
            },
        },
        "required": ["task_id"],
        "additionalProperties": False,
        "examples": [{"task_id": "<task-id>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "interconnect_task_id": {"type": "string", "description": "Id of the task."},
            "status": string_schema(
                "Vendor state: QUEUED, RUNNING, SUCCESS or FAILED; not_reachable when the "
                "Manager did not answer."
            ),
            "done": {"type": "boolean", "description": "True when the task ended."},
            "progress": {"type": ["integer", "null"], "description": "Progress in percent."},
            "message": string_schema("The vendor's status text."),
            "error": string_schema("The vendor's error text when the task failed."),
            "type": string_schema("Vendor task type."),
            "entity_id": string_schema("Id of the object the task works on."),
            "entity_type": string_schema("Type of that object."),
            "entity_name": string_schema("Name of that object."),
            "start_time": {"type": ["integer", "null"], "description": "Start, epoch ms."},
            "end_time": {"type": ["integer", "null"], "description": "End, epoch ms."},
            "steps": {
                "type": "array",
                "description": "The task's steps in order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": string_schema("Step title."),
                        "status": string_schema("Vendor state of the step."),
                        "message": string_schema("Step text."),
                        "job_id": string_schema("Id of the vendor job of the step."),
                    },
                },
            },
            "checked_at": {"type": "string", "description": "When MEHO read the task, ISO 8601."},
        },
    },
    group_key=GROUP_JOBS_ALERTS,
    tags=("read-only", "hcx", "task", "service-mesh"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to follow a task that hcx.service_mesh.create, update, resync or delete "
            "started, until it ends."
        ),
        "output_shape": (
            "{interconnect_task_id, status, done, progress, message, error, type, entity_id, "
            "entity_type, entity_name, start_time, end_time, steps: [{title, type, status, "
            "progress, message, job_id, errors}], checked_at}."
        ),
        "next_step": (
            "If done is false, call again later. If status is FAILED, report error. If "
            "status is not_reachable, the Manager did not answer (for example during its "
            "upgrade); it does not mean the task failed: call again later with the same id."
        ),
    },
)

HCX_SERVICE_MESH_OPS: Final[tuple[HcxTypedOp, ...]] = (
    _CREATE,
    _UPDATE,
    _RESYNC,
    _DELETE,
    _TASK_GET,
)
