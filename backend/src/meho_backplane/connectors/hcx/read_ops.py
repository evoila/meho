# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the first eight HCX reads (the bodies live in ``typed_reads``).

``hcx.about``, ``hcx.site_pairing.list``, ``hcx.service_mesh.list``,
``hcx.appliance.list``, ``hcx.service_mesh.diagnose``, ``hcx.job.get``,
``hcx.alert.list`` and ``hcx.inventory.list``. All are ``safe`` and need
no approval. The diagnostics reads live in ``diagnostics_ops`` and the
other inventory reads in ``inventory_ops``.
:mod:`meho_backplane.connectors.hcx.typed_ops` adds them all to
``HCX_TYPED_OPS`` and registers them at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.hcx.op_types import (
    GROUP_INVENTORY,
    GROUP_JOBS_ALERTS,
    GROUP_MANAGER,
    GROUP_SERVICE_MESH,
    NO_PARAMS_SCHEMA,
    HcxTypedOp,
    list_response_schema,
    string_schema,
)

__all__ = ["HCX_READ_OPS", "HCX_READ_WHEN_TO_USE_BY_GROUP"]

#: Group texts: which reads each group holds, in plain words. The service
#: mesh and inventory groups also hold the reads of ``diagnostics_ops`` and
#: ``inventory_ops``.
HCX_READ_WHEN_TO_USE_BY_GROUP: Final[dict[str, str]] = {
    GROUP_MANAGER: (
        "Use to read the HCX Manager itself and its partner sites: the Manager "
        "version, build and role, connector or cloud (hcx.about), and the site "
        "pairings with the paired site's address (hcx.site_pairing.list)."
    ),
    GROUP_SERVICE_MESH: (
        "Use to check the HCX service meshes and their appliances: the service "
        "meshes with their state and last task (hcx.service_mesh.list), the "
        "interconnect, network extension and other appliances with version, "
        "state and tunnel state (hcx.appliance.list), and the diagnostics of one "
        "service mesh: start a run (hcx.service_mesh.diagnose), list the runs "
        "with their status (hcx.service_mesh.diagnostics.list) and read the "
        "findings of one run (hcx.service_mesh.diagnostics.get)."
    ),
    GROUP_JOBS_ALERTS: (
        "Use to follow HCX work and problems: one job with its state, progress "
        "and failure flag (hcx.job.get), and the HCX alerts, with an optional "
        "filter by severity, state, module, category or object (hcx.alert.list)."
    ),
    GROUP_INVENTORY: (
        "Use to plan a migration: the resource containers (vCenter instances) "
        "of the local and the paired sites (hcx.inventory.list), the vCenter "
        "instances with their datacenters and clusters (hcx.inventory.compute.list), "
        "the datastores of a cluster (hcx.inventory.datastore.list) and the "
        "networks of a site (hcx.inventory.network.list)."
    ),
}


_ABOUT = HcxTypedOp(
    op_id="hcx.about",
    handler_attr="about",
    summary="Read the HCX Manager version, build, role (connector or cloud) and local site.",
    description=(
        "Returns the HCX Manager version and build, its system type and role "
        "(connector or cloud), and the id, name, type and address of its local site."
    ),
    parameter_schema=NO_PARAMS_SCHEMA,
    response_schema={
        "type": "object",
        "properties": {
            "version": string_schema("HCX Manager version as the Manager reports it."),
            "build": string_schema("Build number, when the Manager reports one."),
            "system_type": string_schema(
                "Vendor system type of the Manager, for example 'HCX Cloud'."
            ),
            "system_name": string_schema("Name of the Manager as the Manager reports it."),
            "role": {
                "type": "string",
                "enum": ["connector", "cloud", "unknown"],
                "description": "connector (the source side), cloud (the target side) or unknown.",
            },
            "local_endpoint_id": string_schema("Id of the Manager's local site."),
            "local_endpoint_name": string_schema("Name of the local site."),
            "local_endpoint_type": string_schema("Vendor endpoint type of the local site."),
            "local_url": string_schema("Address of the local site."),
        },
    },
    group_key=GROUP_MANAGER,
    tags=("read-only", "hcx", "version", "manager"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call first on an HCX target: to learn its version and whether it is the "
            "connector (source) or the cloud (target) side before other HCX calls."
        ),
        "output_shape": (
            "{version, build, system_type, system_name, role, local_endpoint_id, "
            "local_endpoint_name, local_endpoint_type, local_url}. role is derived from "
            "system_type; unknown means the type named neither connector nor cloud."
        ),
        "next_step": (
            "Call hcx.site_pairing.list for the paired sites, or hcx.service_mesh.list "
            "to check the service meshes."
        ),
    },
)

_SITE_PAIRING_LIST = HcxTypedOp(
    op_id="hcx.site_pairing.list",
    handler_attr="site_pairing_list",
    summary="List the HCX site pairings: the remote sites this HCX Manager is paired with.",
    description=(
        "Returns one row per site pairing: the remote site's endpoint id, cloud type, "
        "address and the user the pairing logs in with. Passwords never come back."
    ),
    parameter_schema=NO_PARAMS_SCHEMA,
    response_schema=list_response_schema(
        "One row per site pairing.",
        {
            "endpointId": string_schema("Id of the paired site."),
            "cloudType": string_schema("Vendor cloud type of the paired site."),
            "url": string_schema("Address of the paired HCX Manager."),
            "username": string_schema("User the pairing logs in with."),
        },
    ),
    group_key=GROUP_MANAGER,
    tags=("read-only", "hcx", "site-pairing"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": "Call to see which remote sites this HCX Manager is paired with.",
        "output_shape": (
            "{items: [{endpointId, cloudType, url, username, ...}]}: one flat row per "
            "pairing. A long list is behind a result handle; read it with result_query."
        ),
        "next_step": (
            "Use endpointId to find the site in hcx.inventory.list or the service meshes "
            "of hcx.service_mesh.list. No row means the Manager has no pairing yet."
        ),
    },
)

_SERVICE_MESH_LIST = HcxTypedOp(
    op_id="hcx.service_mesh.list",
    handler_attr="service_mesh_list",
    summary="List the HCX service meshes with their state, services and last task.",
    description=(
        "Returns one row per service mesh: id, name, state, the services and their "
        "status, the compute profiles and the last task with its status."
    ),
    parameter_schema=NO_PARAMS_SCHEMA,
    response_schema=list_response_schema(
        "One row per service mesh.",
        {
            "serviceMeshId": string_schema("Id of the service mesh."),
            "name": string_schema("Name of the service mesh."),
            "state": string_schema("Vendor state of the service mesh."),
            "updateAvailable": {
                "type": ["boolean", "null"],
                "description": "True when a newer appliance version is available.",
            },
            "services": {
                "type": ["array", "null"],
                "description": "The HCX services the mesh runs.",
                "items": {
                    "type": "object",
                    "properties": {"name": string_schema("Service name.")},
                },
            },
            "status": {
                "type": ["object", "null"],
                "description": "Status per service: services[] with serviceName, status, reason.",
            },
            "last_task_id": string_schema("Id of the mesh's last task (interconnect task)."),
            "last_task_type": string_schema(
                "Type of the last task, for example a diagnostics run."
            ),
            "last_task_status": string_schema("Status of the last task."),
        },
    ),
    group_key=GROUP_SERVICE_MESH,
    tags=("read-only", "hcx", "service-mesh"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to check the service meshes: their state, the status of each service, "
            "and the result of the last task (for example a diagnostics run)."
        ),
        "output_shape": (
            "{items: [{serviceMeshId, name, state, services, status, computeProfiles, "
            "last_task_id, last_task_type, last_task_status, ...}]}: one flat row per "
            "mesh, vendor fields kept. A long list is behind a result handle."
        ),
        "next_step": (
            "For tunnel state, call hcx.appliance.list. To test a mesh, call "
            "hcx.service_mesh.diagnose with its serviceMeshId."
        ),
    },
)

_APPLIANCE_LIST = HcxTypedOp(
    op_id="hcx.appliance.list",
    handler_attr="appliance_list",
    summary="List the HCX appliances with their version, state and tunnel state.",
    description=(
        "Returns one row per appliance: id, type, name, service mesh, state, overall, "
        "transport and tunnel status, active and total tunnels, version and build."
    ),
    parameter_schema=NO_PARAMS_SCHEMA,
    response_schema=list_response_schema(
        "One row per appliance.",
        {
            "applianceId": string_schema("Id of the appliance."),
            "applianceType": string_schema("Vendor appliance type, for example HCX-NET-EXT."),
            "applianceName": string_schema("Name of the appliance."),
            "serviceMeshId": string_schema("Id of the service mesh the appliance belongs to."),
            "state": string_schema("Appliance state (applianceStatus.state)."),
            "overall_status": string_schema("Overall status of the appliance."),
            "transport_status": string_schema("Overall transport status."),
            "tunnel_status": string_schema("Overall status of the encrypted tunnels."),
            "tunnels_active": {
                "type": ["integer", "null"],
                "description": "Number of active encrypted tunnels.",
            },
            "tunnels_total": {
                "type": ["integer", "null"],
                "description": "Number of encrypted tunnels.",
            },
            "version": string_schema("Appliance version as major.minor.patch."),
            "build": {
                "type": ["integer", "string", "null"],
                "description": "Appliance build number.",
            },
            "update_available": {
                "type": ["boolean", "null"],
                "description": "True when a newer appliance version is available.",
            },
        },
    ),
    group_key=GROUP_SERVICE_MESH,
    tags=("read-only", "hcx", "appliance", "tunnel"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to check the health of the HCX appliances and their tunnels, or to "
            "see their versions before an upgrade."
        ),
        "output_shape": (
            "{items: [{applianceId, applianceType, applianceName, serviceMeshId, state, "
            "overall_status, transport_status, tunnel_status, tunnels_active, tunnels_total, "
            "version, build, update_available, ...}]}: one flat row per appliance; the "
            "vendor fields stay. A long list is behind a result handle."
        ),
        "next_step": (
            "If a tunnel is down, call hcx.service_mesh.diagnose for the appliance's "
            "serviceMeshId, then hcx.alert.list for related alerts."
        ),
    },
)

_SERVICE_MESH_DIAGNOSE = HcxTypedOp(
    op_id="hcx.service_mesh.diagnose",
    handler_attr="service_mesh_diagnose",
    summary="Start the diagnostics of one HCX service mesh and return the started task.",
    description=(
        "Starts the HCX diagnostics run of one service mesh. It changes no configuration. "
        "Returns the task id. Read the run and its findings with the diagnostics reads."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "service_mesh_id": {
                "type": "string",
                "minLength": 1,
                "description": "Id of the service mesh (serviceMeshId in hcx.service_mesh.list).",
            },
        },
        "required": ["service_mesh_id"],
        "additionalProperties": False,
        "examples": [{"service_mesh_id": "<service-mesh-id>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "service_mesh_id": {"type": "string", "description": "Id of the service mesh."},
            "interconnect_task_id": string_schema("Id of the started diagnostics task."),
            "warnings": {
                "type": "array",
                "description": "Warnings the Manager returned.",
                "items": {
                    "type": "object",
                    "properties": {"message": string_schema("Warning text.")},
                },
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
        },
    },
    group_key=GROUP_SERVICE_MESH,
    tags=("read-only", "hcx", "service-mesh", "diagnostics"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call when a service mesh or its tunnels look unhealthy, to let HCX test "
            "the mesh. The run changes nothing."
        ),
        "output_shape": "{service_mesh_id, interconnect_task_id, warnings, errors}.",
        "next_step": (
            "Wait a moment, then call hcx.service_mesh.diagnostics.list with the same "
            "service_mesh_id: the newest run (highest startTime) is this one; its status "
            "says whether it is done. Then call hcx.service_mesh.diagnostics.get with its "
            "diagId for the findings. If errors is not empty, report the messages."
        ),
    },
)

_JOB_GET = HcxTypedOp(
    op_id="hcx.job.get",
    handler_attr="job_get",
    summary="Read one HCX job: whether it is done or failed, its progress and its times.",
    description=(
        "Returns one HCX job by id: outcome (queued, running, succeeded, failed, "
        "cancelled, rolled_back, or not_reachable when HCX does not answer), "
        "percentComplete, the vendor's error, start and end time, and the vendor flags."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "minLength": 1,
                "description": "Id of the HCX job, as another HCX answer returned it.",
            },
        },
        "required": ["job_id"],
        "additionalProperties": False,
        "examples": [{"job_id": "<job-id>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "jobId": string_schema("Id of the job."),
            "username": string_schema("User who started the job."),
            "isQueued": {"type": ["boolean", "null"], "description": "True while the job waits."},
            "isDone": {"type": ["boolean", "null"], "description": "True when the job ended."},
            "didFail": {"type": ["boolean", "null"], "description": "True when the job failed."},
            "isCancelled": {
                "type": ["boolean", "null"],
                "description": "True when the job was cancelled.",
            },
            "isRolledBack": {
                "type": ["boolean", "null"],
                "description": "True when the job was rolled back.",
            },
            "percentComplete": {
                "type": ["number", "null"],
                "description": "Progress in percent.",
            },
            "startTime": {
                "type": ["integer", "null"],
                "description": "Start time, epoch milliseconds.",
            },
            "endTime": {
                "type": ["integer", "null"],
                "description": "End time, epoch milliseconds.",
            },
            "outcome": {
                "type": "string",
                "enum": [
                    "queued",
                    "running",
                    "succeeded",
                    "failed",
                    "cancelled",
                    "rolled_back",
                    "not_reachable",
                    "unknown",
                ],
                "description": (
                    "The job's state in plain words, from the vendor flags. not_reachable: "
                    "HCX did not answer; it does not mean the job failed."
                ),
            },
            "error": string_schema(
                "The vendor's error text of the job, when the job object has one."
            ),
            "checked_at": {
                "type": "string",
                "description": "When MEHO read the job, ISO 8601 in UTC.",
            },
        },
    },
    group_key=GROUP_JOBS_ALERTS,
    tags=("read-only", "hcx", "job"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": "Call to follow an HCX job that another HCX answer named by id.",
        "output_shape": (
            "The vendor job object {jobId, username, isQueued, isDone, didFail, "
            "isCancelled, isRolledBack, percentComplete, startTime, endTime, ...} plus "
            "outcome, error and checked_at. When HCX does not answer: {jobId, outcome: "
            "not_reachable, error: null, checked_at}."
        ),
        "next_step": (
            "If outcome is queued or running, call again later. If it is not_reachable, "
            "wait and call again with the same job_id; MEHO keeps no job state. If it is "
            "failed, read error and call hcx.alert.list for the cause. A 404 means the id "
            "is unknown."
        ),
    },
)

_ALERT_LIST = HcxTypedOp(
    op_id="hcx.alert.list",
    handler_attr="alert_list",
    summary="List the HCX alerts, with an optional filter by severity, state, module or object.",
    description=(
        "Returns one row per HCX alert: id, severity, state, module, category, message, "
        "the object it is about, creation time and whether it is acknowledged."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "severity": {
                "type": "array",
                "description": "Only alerts with one of these severities, as HCX names them.",
                "items": {"type": "string", "description": "One severity."},
                "minItems": 1,
            },
            "state": {"type": "string", "description": "Only alerts in this state."},
            "module": {"type": "string", "description": "Only alerts of this HCX module."},
            "category": {"type": "string", "description": "Only alerts of this category."},
            "entity_id": {
                "type": "string",
                "description": "Only alerts about the object with this id.",
            },
        },
        "additionalProperties": False,
        "examples": [{}, {"severity": ["<severity>"], "entity_id": "<object-id>"}],
    },
    response_schema=list_response_schema(
        "One row per alert.",
        {
            "alarm_id": string_schema("Id of the alert."),
            "severity": string_schema("Severity of the alert."),
            "state": string_schema("State of the alert."),
            "module": string_schema("HCX module that raised the alert."),
            "category": string_schema("Category of the alert."),
            "message": string_schema("Alert text."),
            "eventCode": string_schema("Vendor event code."),
            "creation_date": string_schema("When the alert was raised."),
            "isAlertAcknowledged": {
                "type": ["boolean", "null"],
                "description": "True when someone acknowledged the alert.",
            },
            "entity": {
                "type": ["object", "null"],
                "description": "The object the alert is about: id, name and type.",
            },
        },
    ),
    group_key=GROUP_JOBS_ALERTS,
    tags=("read-only", "hcx", "alert"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see what HCX complains about: after a failed job, a down tunnel or "
            "before a migration."
        ),
        "output_shape": (
            "{items: [{alarm_id, severity, state, module, category, message, eventCode, "
            "creation_date, isAlertAcknowledged, entity, ...}]}: one flat row per alert. "
            "A long list is behind a result handle; read it with result_query."
        ),
        "next_step": (
            "Read the message of each alert. To narrow the list, call again with a "
            "severity, state, module, category or entity_id filter."
        ),
    },
)

_INVENTORY_LIST = HcxTypedOp(
    op_id="hcx.inventory.list",
    handler_attr="inventory_list",
    summary="List the HCX resource containers (vCenter instances) of the local and paired sites.",
    description=(
        "Returns one row per resource container that HCX knows: name, id, type, version, "
        "the vCenter id and the site it belongs to. Use it to pick the source and target "
        "of a migration."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "site": {
                "type": "string",
                "enum": ["local", "remote"],
                "description": (
                    "local: only the local site; remote: only the paired sites. "
                    "Leave out for every site."
                ),
            },
        },
        "additionalProperties": False,
        "examples": [{}, {"site": "remote"}],
    },
    response_schema=list_response_schema(
        "One row per resource container.",
        {
            "resourceId": string_schema("Id of the resource container."),
            "resourceName": string_schema("Name of the resource container."),
            "resourceType": string_schema("Vendor type of the resource container."),
            "name": string_schema("Name as HCX shows it."),
            "version": string_schema("Version of the resource container, for example of vCenter."),
            "vcuuid": string_schema("Id of the vCenter instance."),
            "endpointId": string_schema("Id of the site the container belongs to."),
            "endpointName": string_schema("Name of that site."),
            "isLocal": {
                "type": ["boolean", "null"],
                "description": "True when the container belongs to the local site.",
            },
        },
    ),
    group_key=GROUP_INVENTORY,
    tags=("read-only", "hcx", "inventory", "migration"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call when planning a migration, to see which vCenter instances HCX can move "
            "workloads from and to, on the local and on the paired sites."
        ),
        "output_shape": (
            "{items: [{resourceId, resourceName, resourceType, name, version, vcuuid, "
            "endpointId, endpointName, isLocal, endpoint, ...}]}: one flat row per "
            "container. A long list is behind a result handle; read it with result_query."
        ),
        "next_step": (
            "Use endpointId and resourceId as the source and target of a migration; "
            "check the pairing with hcx.site_pairing.list."
        ),
    },
)

HCX_READ_OPS: Final[tuple[HcxTypedOp, ...]] = (
    _ABOUT,
    _SITE_PAIRING_LIST,
    _SERVICE_MESH_LIST,
    _APPLIANCE_LIST,
    _SERVICE_MESH_DIAGNOSE,
    _JOB_GET,
    _ALERT_LIST,
    _INVENTORY_LIST,
)
