# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the pairing, replication, VM-check and task reads (bodies in ``reads``).

``vr.pairing.list``, ``vr.replication.list``, ``vr.replication.issues``,
``vr.vm.check`` and ``vr.task.get``. All are ``safe`` and need no
approval. All exist on vSphere Replication 8.8 and 9.0 and VCF 9.1
Protection and Recovery.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vr.op_types import (
    GROUP_PAIRINGS,
    GROUP_REPLICATIONS,
    GROUP_TASKS,
    VrTypedOp,
)
from meho_backplane.connectors.vr.reads import MAX_DISK_READS

__all__ = ["VR_READ_OPS", "VR_READ_WHEN_TO_USE_BY_GROUP"]

#: Group texts: which reads each group holds, in plain words.
VR_READ_WHEN_TO_USE_BY_GROUP: Final[dict[str, str]] = {
    GROUP_PAIRINGS: (
        "Use to see the site pairings of a vSphere Replication server: the local and "
        "remote vCenter and vSphere Replication servers of each pairing, and the "
        "replication servers that carry the data (vr.pairing.list). One read."
    ),
    GROUP_REPLICATIONS: (
        "Use to look at replications of virtual machines: list the replications of a "
        "pairing with state, RPO, last sync and disks (vr.replication.list), read the "
        "current replication issues such as RPO not met (vr.replication.issues), and "
        "check whether a VM can be replicated and which target datastores fit "
        "(vr.vm.check). Three reads."
    ),
    GROUP_TASKS: (
        "Use to read the state, progress and error of one vSphere Replication task, "
        "for example a task a replication change started (vr.task.get). One read."
    ),
}

_TAGS: Final = ("read-only", "vr", "vsphere-replication", "live-recovery")
_NULL_STR: Final[dict[str, Any]] = {"type": ["string", "null"]}
_NULL_INT: Final[dict[str, Any]] = {"type": ["integer", "null"]}
_NULL_BOOL: Final[dict[str, Any]] = {"type": ["boolean", "null"]}


def _field(kind: dict[str, Any], description: str) -> dict[str, Any]:
    return {**kind, "description": description}


def _id_param(description: str) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "description": description}


_PAIRING_ID = _id_param(
    "Id of the pairing, as vr.pairing.list returns it (the pairing_id field of a row)."
)
_SOURCE_VC_GUID = _id_param(
    "Optional. Only replications whose source is this vCenter (its GUID, for example "
    "local_vc_id or remote_vc_id from vr.pairing.list)."
)

_ERRORS_SCHEMA: Final[dict[str, Any]] = {
    "type": "array",
    "description": (
        "Reasons the server gave for missing data (for example the remote site is not "
        "logged in), and reads that failed. Empty when the data is complete."
    ),
    "items": {
        "type": "object",
        "description": "One reason.",
        "properties": {
            "code": _field(_NULL_STR, "Error code."),
            "message": _field(_NULL_STR, "Error text."),
            "field": _field(_NULL_STR, "The field or server the reason is about."),
        },
    },
}


def _list_schema(description: str, row: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "properties": {
            "items": {
                "type": "array",
                "description": "One flat row per entry.",
                "items": {"type": "object", "description": "One row.", "properties": row},
            },
            "count": {"type": "integer", "description": "Number of rows in items."},
            "more_rows": {
                "type": "boolean",
                "description": "True when the server has more rows than the read fetched.",
            },
            "errors": _ERRORS_SCHEMA,
            **extra,
        },
    }


def _server_row(prefix: str, what: str) -> dict[str, Any]:
    return {
        f"{prefix}_id": _field(_NULL_STR, f"Id (GUID) of the {what}."),
        f"{prefix}_name": _field(_NULL_STR, f"Name of the {what}."),
        f"{prefix}_url": _field(_NULL_STR, f"URL of the {what}."),
        f"{prefix}_status": _field(_NULL_STR, f"Status of the {what}: OK, WARNING or ERROR."),
        f"{prefix}_version": _field(_NULL_STR, f"Version of the {what}."),
        f"{prefix}_build": _field(_NULL_STR, f"Build number of the {what}."),
    }


_PAIRING_ROW: Final[dict[str, Any]] = {
    "pairing_id": _field(_NULL_STR, "Id of the pairing."),
    **_server_row("local_vc", "local vCenter"),
    **_server_row("remote_vc", "remote vCenter"),
    **_server_row("local_vrms", "local vSphere Replication server"),
    **_server_row("remote_vrms", "remote vSphere Replication server"),
}

_REPLICATION_SERVER_ROW: Final[dict[str, Any]] = {
    "pairing_id": _field(_NULL_STR, "Id of the pairing."),
    "vrms_id": _field(_NULL_STR, "Id of the vSphere Replication server the row belongs to."),
    "vrms_name": _field(_NULL_STR, "Name of that vSphere Replication server."),
    "id": _field(_NULL_STR, "Id of the replication server."),
    "name": _field(_NULL_STR, "Name of the replication server."),
    "hostname": _field(_NULL_STR, "Host name set in the replication server appliance."),
    "uuid": _field(_NULL_STR, "UUID of the replication server."),
    "connected": _field(_NULL_BOOL, "True when the replication server is connected."),
    "decommissioned": _field(_NULL_BOOL, "True when the replication server is retired."),
    "internally_managed": _field(_NULL_BOOL, "True for the server built into the appliance."),
    "replication_count": _field(_NULL_INT, "Number of replications this server carries."),
    "fault": _field(_NULL_STR, "Error the server gave while reading this row, if any."),
}

_REPLICATION_ROW: Final[dict[str, Any]] = {
    "pairing_id": _field(_NULL_STR, "Id of the pairing."),
    "id": _field(_NULL_STR, "Id of the replication."),
    "name": _field(_NULL_STR, "Name of the replication (the VM name)."),
    "vm_id": _field(_NULL_STR, "Id of the replicated VM."),
    "source_site_name": _field(_NULL_STR, "Name of the source site."),
    "source_vc_id": _field(_NULL_STR, "Id of the source vCenter."),
    "target_site_name": _field(_NULL_STR, "Name of the target site."),
    "target_vc_id": _field(_NULL_STR, "Id of the target vCenter."),
    "status": _field(_NULL_STR, "Replication status, for example OK, SYNC, PAUSED or ERROR."),
    "rpo_minutes": _field(_NULL_INT, "Configured RPO (most data loss allowed), in minutes."),
    "rpo_violation": _field(_NULL_BOOL, "True when the RPO is not met."),
    "rpo_met": _field(_NULL_BOOL, "True when the RPO is met; null when unknown."),
    "current_rpo_violation": _field(_NULL_INT, "How far past the RPO the replication is."),
    "last_sync_time": _field(_NULL_INT, "End of the last sync, milliseconds since 1970."),
    "last_sync_duration": _field(_NULL_INT, "Duration of the last sync, in seconds."),
    "last_sync_size": _field(_NULL_INT, "Data sent in the last sync, in bytes."),
    "sync_progress": _field(_NULL_INT, "Progress of the running sync, in percent."),
    "configuration_state": _field(_NULL_STR, "IN_PROGRESS, ERROR, OK or UNKNOWN."),
    "configuration_error": _field(_NULL_STR, "Configuration error text, if any."),
    "configuration_error_details": _field(_NULL_STR, "Details of the configuration error."),
    "last_group_error": _field(_NULL_STR, "Last error of the replication, if any."),
    "last_group_error_details": _field(_NULL_STR, "Details of the last error."),
    "recovery_state": _field(_NULL_STR, "UNKNOWN, NOT_STARTED, IN_PROGRESS, ERROR or OK."),
    "recovery_error": _field(_NULL_STR, "Recovery error text, if any."),
    "type": _field(_NULL_STR, "Replication type: VC_TO_VC, VSAN or UNKNOWN."),
    "replication_server_name": _field(_NULL_STR, "Target replication server (incoming only)."),
    "mpit_enabled": _field(_NULL_BOOL, "True when point-in-time copies are kept."),
    "mpit_instances": _field(_NULL_INT, "Point-in-time copies kept per day."),
    "mpit_days": _field(_NULL_INT, "Days point-in-time copies are kept."),
    "quiescing_enabled": _field(_NULL_BOOL, "True when the guest is quiesced before a copy."),
    "network_compression_enabled": _field(_NULL_BOOL, "True when traffic is compressed."),
    "encryption_enabled": _field(_NULL_BOOL, "True when replication traffic is encrypted."),
}

_DISK_ROW: Final[dict[str, Any]] = {
    "replication_id": _field(_NULL_STR, "Id of the replication the disk belongs to."),
    "device_key": _field(_NULL_INT, "Device key of the source disk."),
    "label": _field(_NULL_STR, "Label of the source disk."),
    "is_vm_home": _field(_NULL_BOOL, "True for the VM home row (the VM's config files)."),
    "replicated": _field(_NULL_BOOL, "True when this disk is replicated."),
    "capacity_kb": _field(_NULL_INT, "Size of the source disk, in KB."),
    "source_datastore_name": _field(_NULL_STR, "Datastore of the source disk."),
    "destination_datastore_name": _field(_NULL_STR, "Datastore of the replica."),
    "destination_path": _field(_NULL_STR, "Folder of the replica on that datastore."),
    "destination_disk_format": _field(_NULL_STR, "Disk format of the replica."),
    "replica_disk_usage": _field(_NULL_INT, "Space the replica uses."),
}

_ISSUE_ROW: Final[dict[str, Any]] = {
    "entity": _field(_NULL_STR, "Id of the object the issue is about."),
    "entity_name": _field(_NULL_STR, "Name of that object, for example the VM."),
    "description": _field(_NULL_STR, "The issue in the server's words."),
    "status": _field(_NULL_STR, "WARNING or ERROR."),
    "issue_type": _field(_NULL_STR, "Where the issue is: VC, VRS, VRMS or UNKNOWN."),
    "timestamp": _field(_NULL_INT, "When the issue was raised, milliseconds since 1970."),
}

_DATASTORE_ROW: Final[dict[str, Any]] = {
    "id": _field(_NULL_STR, "Id of the datastore."),
    "name": _field(_NULL_STR, "Name of the datastore."),
    "type": _field(_NULL_STR, "DATASTORE or STORAGE_POD (a datastore cluster)."),
    "file_system_type": _field(_NULL_STR, "VMFS, NFS, VSAN, VVOL and so on."),
    "capacity": _field(_NULL_INT, "Total size, in bytes."),
    "free_space": _field(_NULL_INT, "Free space, in bytes."),
    "overall_status": _field(_NULL_STR, "GREEN, YELLOW, RED or GRAY."),
    "accessible": _field(_NULL_BOOL, "True when the datastore can be reached."),
    "maintenance_mode": _field(_NULL_STR, "Maintenance mode of the datastore."),
}

_PAIRING_LIST = VrTypedOp(
    op_id="vr.pairing.list",
    handler_attr="pairing_list",
    summary="List the vSphere Replication pairings and their replication servers.",
    description=(
        "Reads the pairings of this vSphere Replication server, the local and remote "
        "vCenter and vSphere Replication servers of each, and the replication servers "
        "registered with them. One row per pairing; the servers are in "
        "replication_servers."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
        "examples": [{}],
    },
    response_schema=_list_schema(
        "Pairings, one flat row each, and their replication servers.",
        _PAIRING_ROW,
        replication_servers={
            "type": "array",
            "description": "One flat row per replication server, of every pairing.",
            "items": {
                "type": "object",
                "description": "One replication server.",
                "properties": _REPLICATION_SERVER_ROW,
            },
        },
    ),
    group_key=GROUP_PAIRINGS,
    tags=(*_TAGS, "pairing", "replication-server"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call first on a vSphere Replication target, after vr.about, to get the "
            "pairing_id and the vCenter ids that the replication reads need, and to see "
            "whether the remote site and the replication servers are connected."
        ),
        "output_shape": (
            "{items: [{pairing_id, local_vc_id, local_vc_name, local_vc_status, "
            "remote_vc_id, remote_vc_name, remote_vc_status, local_vrms_*, remote_vrms_* "
            "(id, name, url, status, version, build)}], count, more_rows, errors, "
            "replication_servers: [{pairing_id, vrms_id, vrms_name, id, name, hostname, "
            "connected, decommissioned, replication_count, fault}]}. The data of the "
            "remote site can be missing until that site is logged in; errors says why."
        ),
        "next_step": (
            "Pass a pairing_id to vr.replication.list or vr.replication.issues. Report a "
            "status that is not OK, a replication server with connected false, and every "
            "entry in errors."
        ),
        "result_scalars": {"keys": ["count", "more_rows"]},
    },
)

_REPLICATION_LIST = VrTypedOp(
    op_id="vr.replication.list",
    handler_attr="replication_list",
    summary="List the replications of a pairing with status, RPO, last sync and disks.",
    description=(
        "Reads the incoming and outgoing replications of one pairing with full details: "
        "VM, status, RPO and whether it is met, last sync, errors and point-in-time "
        "settings. With include_disks, also the disks of each replication."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "pairing_id": _PAIRING_ID,
            "source_vc_guid": _SOURCE_VC_GUID,
            "name": _id_param(
                "Optional. Only the replication with this name (the name of the replicated VM)."
            ),
            "include_disks": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Also read the disks of each replication (one call each, at most "
                    f"{MAX_DISK_READS} replications). Default false."
                ),
            },
        },
        "required": ["pairing_id"],
        "additionalProperties": False,
        "examples": [{"pairing_id": "<pairing-id>", "name": "<vm-name>", "include_disks": True}],
    },
    response_schema=_list_schema(
        "Replications, one flat row each.",
        _REPLICATION_ROW,
        disks={
            "type": "array",
            "description": "One row per disk; only with include_disks.",
            "items": {"type": "object", "description": "One disk.", "properties": _DISK_ROW},
        },
    ),
    group_key=GROUP_REPLICATIONS,
    tags=(*_TAGS, "replication", "rpo", "health"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which VMs are replicated in a pairing and whether each "
            "replication is healthy: status, RPO met or not, last sync time and size. "
            "Set name to look at one VM; set include_disks to see its disks and target "
            "datastores."
        ),
        "output_shape": (
            "{items: [{pairing_id, id, name, vm_id, status, rpo_minutes, rpo_violation, "
            "rpo_met, current_rpo_violation, last_sync_time, last_sync_duration, "
            "last_sync_size, configuration_state, configuration_error, last_group_error, "
            "recovery_state, source_site_name, target_site_name, ...}], count, more_rows, "
            "errors, disks: [{replication_id, label, replicated, capacity_kb, "
            "destination_datastore_name, ...}] (only with include_disks)}. Times are "
            "milliseconds since 1970."
        ),
        "next_step": (
            "For rows with rpo_met false, status ERROR or a configuration_error, call "
            "vr.replication.issues with the same pairing_id to read the reasons. Use "
            "result_query to filter a long list, for example by status."
        ),
        "result_scalars": {"keys": ["count", "more_rows"]},
    },
)

_REPLICATION_ISSUES = VrTypedOp(
    op_id="vr.replication.issues",
    handler_attr="replication_issues",
    summary="List the current issues of the replications of a pairing, such as RPO not met.",
    description=(
        "Reads the current issues of all incoming and outgoing replications of one "
        "pairing: the object, its name, the issue text, WARNING or ERROR, where the "
        "issue is (vCenter, replication server or management server) and when it was "
        "raised."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"pairing_id": _PAIRING_ID, "source_vc_guid": _SOURCE_VC_GUID},
        "required": ["pairing_id"],
        "additionalProperties": False,
        "examples": [{"pairing_id": "<pairing-id>"}],
    },
    response_schema=_list_schema("Replication issues, one flat row each.", _ISSUE_ROW),
    group_key=GROUP_REPLICATIONS,
    tags=(*_TAGS, "replication", "rpo", "health", "issues"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to find what is wrong with replications in a pairing, for example "
            "which VMs do not meet their RPO, or after vr.replication.list showed a "
            "replication that is not OK."
        ),
        "output_shape": (
            "{items: [{entity, entity_name, description, status, issue_type, "
            "timestamp}], count, more_rows, errors}. timestamp is milliseconds since "
            "1970. An empty items list means no current issue."
        ),
        "next_step": (
            "Report each issue with its entity_name and description. For details of one "
            "replication, call vr.replication.list with name set to the entity_name."
        ),
        "result_scalars": {"keys": ["count", "more_rows"]},
    },
)

_VM_CHECK = VrTypedOp(
    op_id="vr.vm.check",
    handler_attr="vm_check",
    summary="Check whether a VM can be replicated and list the target datastores.",
    description=(
        "Reads the replication capability of one VM (validation status, reasons it "
        "cannot be replicated, minimum RPO, supported features) and the datastores "
        "vSphere Replication can use as targets on the target vCenter."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "pairing_id": _PAIRING_ID,
            "vcenter_id": _id_param(
                "Id (GUID) of the vCenter the VM runs on, for example local_vc_id from "
                "vr.pairing.list."
            ),
            "vm_id": _id_param("Managed object id of the VM in that vCenter."),
            "target_vcenter_id": _id_param(
                "Optional. Id (GUID) of the vCenter to list target datastores on. "
                "Default: vcenter_id."
            ),
        },
        "required": ["pairing_id", "vcenter_id", "vm_id"],
        "additionalProperties": False,
        "examples": [
            {
                "pairing_id": "<pairing-id>",
                "vcenter_id": "<source-vcenter-id>",
                "vm_id": "<vm-id>",
                "target_vcenter_id": "<target-vcenter-id>",
            }
        ],
    },
    response_schema=_list_schema(
        "The VM's capability and the target datastores, one flat row each.",
        _DATASTORE_ROW,
        vm_id=_field(_NULL_STR, "The VM that was checked."),
        vcenter_id=_field(_NULL_STR, "The vCenter of the VM."),
        target_vcenter_id=_field(_NULL_STR, "The vCenter the datastores are on."),
        vm_validation_status=_field(_NULL_STR, "OK, or why the VM cannot be replicated."),
        replication_possible={
            "type": "boolean",
            "description": "True when the status is OK and no reason blocks replication.",
        },
        replication_not_supported_reasons={
            "type": "array",
            "description": "Reasons the VM cannot be replicated; empty when none.",
            "items": {"type": "string", "description": "One reason."},
        },
        min_rpo_mins=_field(_NULL_INT, "Lowest RPO this VM supports, in minutes."),
        capability={
            "type": "object",
            "description": "Features the VM supports for replication.",
            "properties": {
                "quiescing_supported": _field(_NULL_BOOL, "Guest quiescing."),
                "network_compression_supported": _field(_NULL_BOOL, "Compression."),
                "mpit_supported": _field(_NULL_BOOL, "Point-in-time copies."),
                "lwd_encryption_supported": _field(_NULL_BOOL, "Traffic encryption."),
                "vm_encrypted": _field(_NULL_BOOL, "True when the VM is encrypted."),
            },
        },
    ),
    group_key=GROUP_REPLICATIONS,
    tags=(*_TAGS, "replication", "datastore", "preflight"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call before a replication is configured: to check that the VM can be "
            "replicated, which RPO it supports, and which datastores on the target "
            "vCenter can hold the replica."
        ),
        "output_shape": (
            "{vm_id, vcenter_id, target_vcenter_id, vm_validation_status, "
            "replication_possible, replication_not_supported_reasons, min_rpo_mins, "
            "capability: {quiescing_supported, network_compression_supported, "
            "mpit_supported, ...}, items: [{id, name, type, file_system_type, capacity, "
            "free_space, overall_status, accessible, maintenance_mode}], count, "
            "more_rows, errors}. Sizes are bytes."
        ),
        "next_step": (
            "If replication_possible is false, report vm_validation_status and the "
            "reasons. Otherwise pick an accessible datastore with enough free_space and "
            "overall_status GREEN. A target vCenter of the remote site can need a "
            "remote-site login first; errors and the vendor error then say so."
        ),
        "result_scalars": {
            "keys": [
                "vm_id",
                "vcenter_id",
                "target_vcenter_id",
                "vm_validation_status",
                "replication_possible",
                "min_rpo_mins",
                "count",
                "more_rows",
            ]
        },
    },
)

_TASK_GET = VrTypedOp(
    op_id="vr.task.get",
    handler_attr="task_get",
    summary="Read the state, progress and error of one vSphere Replication task.",
    description=(
        "Reads one task by id: what it does, the object it runs on, its status "
        "(QUEUED, RUNNING, SUCCESS, ERROR or CANCELLED), progress, times, the error if "
        "it failed and the result if it succeeded."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "task_id": _id_param("Id of the task, as the call that started it returned it."),
        },
        "required": ["task_id"],
        "additionalProperties": False,
        "examples": [{"task_id": "<task-id>"}],
    },
    response_schema={
        "type": "object",
        "description": "One task.",
        "properties": {
            "id": _field(_NULL_STR, "Id of the task."),
            "description": _field(_NULL_STR, "What the task does."),
            "entity": _field(_NULL_STR, "Id of the object the task runs on."),
            "entity_name": _field(_NULL_STR, "Name of that object."),
            "status": _field(_NULL_STR, "QUEUED, RUNNING, SUCCESS, ERROR or CANCELLED."),
            "progress": _field(_NULL_INT, "Progress, in percent."),
            "queued_time": _field(_NULL_INT, "When it was queued, milliseconds since 1970."),
            "start_time": _field(_NULL_INT, "When it started, milliseconds since 1970."),
            "complete_time": _field(_NULL_INT, "When it ended, milliseconds since 1970."),
            "error": _field(_NULL_STR, "Error text when the status is ERROR."),
            "error_details": _field(_NULL_STR, "Details of the error."),
            "result": {
                "type": ["object", "null"],
                "description": "The task's result when the status is SUCCESS.",
            },
        },
    },
    group_key=GROUP_TASKS,
    tags=(*_TAGS, "task"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to follow a vSphere Replication task, for example one that a "
            "replication change started, until it ends."
        ),
        "output_shape": (
            "{id, description, description_id, entity, entity_name, status, progress, "
            "queued_time, start_time, complete_time, error, error_details, result}. "
            "Times are milliseconds since 1970."
        ),
        "next_step": (
            "While status is QUEUED or RUNNING, call again later. On ERROR, report error "
            "and error_details. On SUCCESS, use result."
        ),
    },
)

VR_READ_OPS: Final[tuple[VrTypedOp, ...]] = (
    _PAIRING_LIST,
    _REPLICATION_LIST,
    _REPLICATION_ISSUES,
    _VM_CHECK,
    _TASK_GET,
)
