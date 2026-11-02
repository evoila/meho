# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schemas and AI guidance of ``vmware.vsan.resync.status`` and ``vmware.vsan.object.health``.

Kept apart from :mod:`.typed_ops_vsan_objects` so the handler module stays
small. The enums are the vendor values of ``VsanSyncReason_enum`` and
``VsanSyncStatus_enum`` (vendor VI/JSON file of vCenter 9.0).
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.typed_ops_vsan_common import CLUSTER_PARAM_SCHEMA

__all__ = [
    "OBJECT_HEALTH_LLM_INSTRUCTIONS",
    "OBJECT_HEALTH_RESPONSE_SCHEMA",
    "RESYNC_LLM_INSTRUCTIONS",
    "RESYNC_PARAMETER_SCHEMA",
    "RESYNC_RESPONSE_SCHEMA",
]

#: Why a component resyncs (``VsanSyncReason_enum`` without the unknown value).
SYNC_REASONS: Final = (
    "repair",
    "evacuate",
    "rebalance",
    "reconfigure",
    "dying_evacuate",
    "stale",
    "merge_concat",
    "object_format_change",
)
#: The state of a resync (``VsanSyncStatus_enum`` without the unknown value).
SYNC_STATUSES: Final = ("active", "queued", "suspended")


def _nullable(kind: str, description: str) -> dict[str, Any]:
    return {"type": [kind, "null"], "description": description}


RESYNC_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "cluster": CLUSTER_PARAM_SCHEMA,
        "max_objects": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1000,
            "default": 100,
            "description": (
                "How many syncing objects to list with their components, up to 1000. "
                "0 gives only the totals. Default 100."
            ),
        },
        "status": {
            "type": "string",
            "enum": list(SYNC_STATUSES),
            "description": "List only objects in this resync state. Default: any state.",
        },
        "reason": {
            "type": "string",
            "enum": list(SYNC_REASONS),
            "description": (
                "List only objects that resync for this reason, for example 'evacuate' "
                "(a host goes into maintenance mode) or 'repair'. Default: any reason."
            ),
        },
    },
    "required": ["cluster"],
    "additionalProperties": False,
    "examples": [{"cluster": "<cluster-id>"}, {"cluster": "<cluster-id>", "max_objects": 0}],
}

RESYNC_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "cluster": {"type": "string", "description": "Managed object id of the cluster."},
        "objects_left": _nullable("integer", "Number of objects that still resync."),
        "bytes_left": _nullable("integer", "Bytes that still have to resync."),
        "seconds_left": _nullable("integer", "Estimated seconds until all resyncs end."),
        "by_status": {
            "type": "array",
            "description": "Totals for active, queued and suspended resyncs.",
            "items": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": list(SYNC_STATUSES),
                        "description": "The resync state.",
                    },
                    "objects": _nullable("integer", "Number of objects in this state."),
                    "bytes_left": _nullable("integer", "Bytes left in this state."),
                    "seconds_left": _nullable("integer", "Estimated seconds left."),
                },
            },
        },
        "components": {
            "type": "array",
            "description": "One row per syncing component of the listed objects.",
            "items": {
                "type": "object",
                "properties": {
                    "object_uuid": _nullable("string", "vSAN UUID of the object."),
                    "component_uuid": _nullable("string", "UUID of the component."),
                    "host_uuid": _nullable("string", "vSAN node UUID of the host."),
                    "disk_uuid": _nullable("string", "UUID of the disk."),
                    "bytes_left": _nullable("integer", "Bytes this component has to resync."),
                    "seconds_left": _nullable("integer", "Estimated seconds left."),
                    "reasons": _nullable(
                        "string", "Why it resyncs, for example 'evacuate' or 'repair'."
                    ),
                },
            },
        },
    },
    "required": ["cluster", "objects_left", "bytes_left", "by_status", "components"],
}

RESYNC_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call before or after a host maintenance, after a disk or host failure, or when "
        "vSAN is slow, to see how much data still resyncs and how long it takes."
    ),
    "parameter_hints": {
        "cluster": "Cluster moid from vmware.cluster.list.",
        "max_objects": "0 for the totals only.",
    },
    "output_shape": (
        "{cluster, objects_left, bytes_left, seconds_left, by_status: [{status, objects, "
        "bytes_left, seconds_left}], components: [{object_uuid, component_uuid, host_uuid, "
        "disk_uuid, bytes_left, seconds_left, reasons}]}. objects_left 0 means nothing "
        "resyncs. A long list comes back as a result handle. A failed vSAN call returns an "
        "error with vCenter's fault."
    ),
    "next_step": (
        "Wait until objects_left is 0 before the next host goes into maintenance mode. To "
        "map host_uuid to a host, call vmware.vsan.cluster.info."
    ),
}

OBJECT_HEALTH_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "cluster": {"type": "string", "description": "Managed object id of the cluster."},
        "summary": {
            "type": "object",
            "description": "Number of objects per plain health group.",
            "properties": {
                "healthy": {"type": "integer", "description": "Healthy objects."},
                "reduced_redundancy": {
                    "type": "integer",
                    "description": "Objects with fewer copies than their policy asks for.",
                },
                "inaccessible": {"type": "integer", "description": "Objects no one can read."},
                "other": {
                    "type": "integer",
                    "description": "Objects in another state, such as a data move.",
                },
            },
        },
        "by_state": {
            "type": "array",
            "description": "Number of objects per vSAN health state.",
            "items": {
                "type": "object",
                "properties": {
                    "health": _nullable("string", "The vSAN health state, as vSAN names it."),
                    "health_group": {
                        "type": "string",
                        "description": "healthy, reduced_redundancy, inaccessible or other.",
                    },
                    "objects": {"type": "integer", "description": "Number of objects."},
                },
            },
        },
        "unhealthy_objects": {
            "type": "array",
            "description": "One row per object that is not healthy.",
            "items": {
                "type": "object",
                "properties": {
                    "object_uuid": {"type": "string", "description": "vSAN UUID of the object."},
                    "health": _nullable("string", "The vSAN health state."),
                    "health_group": {
                        "type": "string",
                        "description": "reduced_redundancy, inaccessible or other.",
                    },
                    "object_type": _nullable(
                        "string", "vdisk, vmswap, namespace, vmem, ... (null when not known)."
                    ),
                    "vm": _nullable("string", "Managed object id of the VM it belongs to."),
                    "vm_instance_uuid": _nullable("string", "Instance UUID of that VM."),
                    "description": _nullable("string", "vSAN's description, often a file path."),
                    "storage_policy": _nullable("string", "Name of the object's storage policy."),
                },
            },
        },
        "checked_at": _nullable("string", "Time of the health check."),
        "from_cache": {"type": "boolean", "description": "True when from_cache was asked."},
    },
    "required": ["cluster", "summary", "by_state", "unhealthy_objects"],
}

OBJECT_HEALTH_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to see whether vSAN data is safe: how many objects have reduced redundancy "
        "or are inaccessible, and which VMs they belong to."
    ),
    "parameter_hints": {
        "cluster": "Cluster moid from vmware.cluster.list.",
        "from_cache": "true while a host is down or in maintenance mode. Default false.",
    },
    "output_shape": (
        "{cluster, summary: {healthy, reduced_redundancy, inaccessible, other}, by_state: "
        "[{health, health_group, objects}], unhealthy_objects: [{object_uuid, health, "
        "health_group, object_type, vm, vm_instance_uuid, description, storage_policy}], "
        "checked_at, from_cache}. A failed vSAN call returns an error with vCenter's fault; "
        "a wrong cluster id gives not_found."
    ),
    "next_step": (
        "Report inaccessible objects first, with their VMs. For reduced redundancy, call "
        "vmware.vsan.resync.status to see whether vSAN is already repairing them."
    ),
}
