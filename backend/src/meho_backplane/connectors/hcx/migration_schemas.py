# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameter and response schema parts shared by the HCX migration operations."""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.hcx.op_types import string_schema

__all__ = [
    "CANCEL_ANSWER_SCHEMA",
    "GROUP_ID_SCHEMA",
    "GROUP_OR_ID_PARAMS",
    "GROUP_SCHEMA",
    "JOB_ANSWER_SCHEMA",
    "MIGRATION_RESULTS_SCHEMA",
    "NOTES_PROPERTIES",
    "WINDOW_SCHEMA",
]

_ERRORS_SCHEMA: Final[dict[str, Any]] = {
    "type": "array",
    "description": "Errors HCX returned.",
    "items": {
        "type": "object",
        "properties": {
            "code": string_schema("Vendor error code."),
            "message": string_schema("Error text."),
        },
    },
}
_WARNINGS_SCHEMA: Final[dict[str, Any]] = {
    "type": "array",
    "description": "Warnings HCX returned.",
    "items": {"type": "object", "properties": {"message": string_schema("Warning text.")}},
}

#: ``errors`` and ``warnings`` properties of an answer.
NOTES_PROPERTIES: Final[dict[str, Any]] = {"errors": _ERRORS_SCHEMA, "warnings": _WARNINGS_SCHEMA}

GROUP_ID_SCHEMA: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "description": "Id of the mobility group, from hcx.mobility_group.list.",
}


def _text(description: str) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "description": description}


def _site_schema(side: str) -> dict[str, Any]:
    return {
        "type": "object",
        "description": (
            f"The {side} site and vCenter instance. Take the values from a row of "
            "hcx.inventory.list (endpointId, resourceId, ...)."
        ),
        "properties": {
            "endpoint_id": _text(f"Id of the {side} HCX site (endpointId)."),
            "endpoint_name": _text(f"Name of the {side} HCX site."),
            "endpoint_type": _text(f"Vendor type of the {side} site; HCX uses 'VC' by default."),
            "resource_id": _text(f"Id of the {side} vCenter instance (resourceId)."),
            "resource_name": _text(f"Name of the {side} vCenter instance."),
            "resource_type": _text(
                f"Vendor type of the {side} resource; HCX uses 'VC' by default."
            ),
            "compute_resource_id": _text(f"Id of the {side} compute resource, when HCX needs it."),
        },
        "required": ["endpoint_id", "resource_id"],
        "additionalProperties": False,
    }


#: The ``group`` input of start, validate and mobility_group.set.
GROUP_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "description": "A mobility group: the VMs to move, where to and how. One migration per VM.",
    "properties": {
        "name": _text("Name of the mobility group (the migration wave)."),
        "migration_type": {
            "type": "string",
            "enum": ["bulk", "vmotion", "rav", "cold"],
            "description": (
                "bulk (copy, then switch over with a reboot), vmotion (live, one VM at a "
                "time), rav (replication-assisted vMotion: copy, then live switchover) or "
                "cold (a powered-off VM)."
            ),
        },
        "service_mesh_id": _text("Id of the service mesh to use, from hcx.service_mesh.list."),
        "source": _site_schema("source"),
        "destination": _site_schema("destination"),
        "placement": {
            "type": "array",
            "description": "Where the VMs go on the destination: a folder, resource pool, ...",
            "items": {
                "type": "object",
                "properties": {
                    "id": _text("Id of the destination object, from hcx.inventory.compute.list."),
                    "name": _text("Name of the destination object."),
                    "type": _text(
                        "Vendor type of the object, for example 'folder', 'resourcePool' "
                        "or 'dataCenter'."
                    ),
                },
                "required": ["id", "type"],
                "additionalProperties": False,
            },
        },
        "storage": {
            "type": "object",
            "description": "The destination datastore for the VM disks.",
            "properties": {
                "datastore_id": _text("Id of the datastore, from hcx.inventory.datastore.list."),
                "datastore_name": _text("Name of the datastore."),
                "disk_provision_type": _text("Vendor disk format, for example 'thin'."),
            },
            "required": ["datastore_id"],
            "additionalProperties": False,
        },
        "network_mappings": {
            "type": "array",
            "description": "Which source network maps to which destination network.",
            "items": {
                "type": "object",
                "properties": {
                    "source_network_id": _text("Id of the source network."),
                    "source_network_name": _text("Name of the source network."),
                    "source_network_type": _text(
                        "Vendor type of the source network, for example "
                        "'DistributedVirtualPortgroup'."
                    ),
                    "destination_network_id": _text(
                        "Id of the destination network, from hcx.inventory.network.list."
                    ),
                    "destination_network_name": _text("Name of the destination network."),
                    "destination_network_type": _text(
                        "Vendor type of the destination network, for example 'NsxtSegment'."
                    ),
                },
                "required": ["source_network_id", "destination_network_id"],
                "additionalProperties": False,
            },
        },
        "switchover_options": {
            "type": "object",
            "description": "Options HCX applies at the switchover. Unset means the HCX default.",
            "properties": {
                "retain_mac": {"type": "boolean", "description": "Keep the MAC addresses."},
                "force_power_off_vm": {
                    "type": "boolean",
                    "description": "Power the source VM off if the guest does not shut down.",
                },
                "upgrade_hardware": {
                    "type": "boolean",
                    "description": "Upgrade the VM hardware version at the destination.",
                },
                "upgrade_vmtools": {
                    "type": "boolean",
                    "description": "Upgrade VMware Tools at the destination.",
                },
                "remove_snapshots": {
                    "type": "boolean",
                    "description": "Remove the VM snapshots before the move.",
                },
                "remove_isos": {
                    "type": "boolean",
                    "description": "Disconnect mounted ISO images before the move.",
                },
            },
            "additionalProperties": False,
        },
        "vms": {
            "type": "array",
            "minItems": 1,
            "description": "The VMs to move, one migration each.",
            "items": {
                "type": "object",
                "properties": {
                    "vm_id": _text("Id of the source VM as HCX knows it (entityId)."),
                    "vm_name": _text("Name of the source VM."),
                },
                "required": ["vm_id"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["name", "migration_type", "source", "destination", "vms"],
    "additionalProperties": False,
}

#: The per-VM rows of a mobility group answer.
MIGRATION_RESULTS_SCHEMA: Final[dict[str, Any]] = {
    "type": "array",
    "description": "One row per VM migration, with HCX's errors and warnings for it.",
    "items": {
        "type": "object",
        "properties": {
            "migration_id": string_schema("Id of the migration."),
            "vm_id": string_schema("Id of the VM."),
            **NOTES_PROPERTIES,
        },
    },
}

#: The answer of the vendor-job ops (start).
JOB_ANSWER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["accepted", "already_running", "failed", "partial", "no_answer"],
            "description": (
                "accepted: HCX took the start; it is not done. already_running: a migration "
                "of the same group or VMs still runs; nothing was started. failed: HCX "
                "rejected the start (its errors are on the group or on every migration), or "
                "a step failed; see steps. partial: HCX rejected some migrations and started "
                "the others; the rejected ones have errors in migrations. no_answer: a step "
                "got no answer; see steps."
            ),
        },
        "mobility_group_id": string_schema("Id of the mobility group that was started."),
        "migrations": MIGRATION_RESULTS_SCHEMA,
        "running": {
            "type": "array",
            "description": "The migrations that still run, when status is already_running.",
            "items": {
                "type": "object",
                "properties": {
                    "migration_id": string_schema("Id of the running migration."),
                    "vm_id": string_schema("Id of its VM."),
                    "state": string_schema("Vendor state."),
                    "outcome": string_schema("Always running here."),
                },
            },
        },
        **NOTES_PROPERTIES,
        "steps": {
            "type": "array",
            "description": "The steps: create_group (only for a start of VMs) and start_group.",
            "items": {
                "type": "object",
                "properties": {
                    "step": string_schema("Step name."),
                    "state": {
                        "type": "string",
                        "enum": ["done", "failed", "partial", "not_run"],
                        "description": (
                            "What happened to the step. partial: HCX rejected some "
                            "migrations of the start and started the others."
                        ),
                    },
                    "error": string_schema(
                        "HCX's error text when the step failed, with secrets removed."
                    ),
                },
            },
        },
        "status_read": string_schema("The operation that follows the migrations."),
        "status_read_params": {
            "type": "object",
            "description": (
                "The parameters to pass to the status read. Holds only the keys that are "
                "known: the group id, the ids of the running migrations (already_running), "
                "or the VM ids when no group was made. Can be empty."
            ),
            "properties": {
                "mobility_group_id": {"type": "string", "description": "Id of the mobility group."},
                "migration_ids": {
                    "type": "array",
                    "description": "Ids of the migrations that still run.",
                    "items": {"type": "string", "description": "Id of one migration."},
                },
                "vm_ids": {
                    "type": "array",
                    "description": "Ids of the VMs, when no mobility group id is known.",
                    "items": {"type": "string", "description": "Id of one VM (entityId)."},
                },
            },
        },
    },
}

#: The answer of hcx.migration.cancel.
CANCEL_ANSWER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["accepted", "already_ended"],
            "description": (
                "accepted: HCX took the cancel; it is not done. already_ended: every "
                "migration had ended; nothing was sent."
            ),
        },
        "force": {"type": "boolean", "description": "True for a force-cancel."},
        "migrations": {
            "type": "array",
            "description": "One row per migration HCX was asked to cancel.",
            "items": {
                "type": "object",
                "properties": {
                    "migration_id": string_schema("Id of the migration."),
                    "mobility_group_id": string_schema("Id of its mobility group."),
                    "vm_id": string_schema("Id of its VM."),
                    **NOTES_PROPERTIES,
                },
            },
        },
        "already_ended": {
            "type": "array",
            "description": "Migrations that had ended and were not sent.",
            "items": {
                "type": "object",
                "properties": {
                    "migration_id": string_schema("Id of the migration."),
                    "outcome": string_schema("succeeded, failed or cancelled."),
                },
            },
        },
        **NOTES_PROPERTIES,
        "status_read": string_schema("The operation that follows the migrations."),
        "status_read_params": {
            "type": "object",
            "description": "The parameters to pass to the status read.",
            "properties": {
                "migration_ids": {
                    "type": "array",
                    "description": "The migration ids.",
                    "items": {"type": "string", "description": "Id of one migration."},
                }
            },
        },
    },
}

#: A time window: start_time and end_time, or clear.
WINDOW_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "start_time": {
            "type": "string",
            "format": "date-time",
            "description": "Start of the window, ISO 8601 with a time zone, for example ...Z.",
        },
        "end_time": {
            "type": "string",
            "format": "date-time",
            "description": "End of the window, ISO 8601 with a time zone; after start_time.",
        },
        "clear": {
            "type": "boolean",
            "const": True,
            "description": "Clear the window instead of setting one.",
        },
    },
    "oneOf": [{"required": ["start_time", "end_time"]}, {"required": ["clear"]}],
    "additionalProperties": False,
}

#: Parameters of validate and start: an existing group by id, or a planned group.
GROUP_OR_ID_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "mobility_group_id": {
            **GROUP_ID_SCHEMA,
            "description": "Id of an existing mobility group. Give this or group, not both.",
        },
        "group": {
            **GROUP_SCHEMA,
            "description": (
                "A planned mobility group: the VMs, where to and how. Give this or "
                "mobility_group_id, not both."
            ),
        },
    },
    "oneOf": [{"required": ["mobility_group_id"]}, {"required": ["group"]}],
    "additionalProperties": False,
    "examples": [{"mobility_group_id": "<mobility-group-id>"}],
}
