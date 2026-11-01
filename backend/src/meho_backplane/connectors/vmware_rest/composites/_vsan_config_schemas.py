# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameter and response schemas of the five vSAN changes.

The handlers are in :mod:`._vsan_cluster`, :mod:`._vsan_disk_claim` and
:mod:`._vsan_disk_wipe`. The moids are checked by the handlers (they answer
``invalid_request``), so the examples can use neutral placeholders.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "VSAN_CLUSTER_CONFIGURE_PARAMETER_SCHEMA",
    "VSAN_CLUSTER_JOB_RESPONSE_SCHEMA",
    "VSAN_DISK_CLAIM_PARAMETER_SCHEMA",
    "VSAN_DISK_CLAIM_RESPONSE_SCHEMA",
    "VSAN_DISK_WIPE_PARAMETER_SCHEMA",
    "VSAN_DISK_WIPE_RESPONSE_SCHEMA",
    "VSAN_ENCRYPTION_REKEY_PARAMETER_SCHEMA",
    "VSAN_ENCRYPTION_REKEY_RESPONSE_SCHEMA",
    "VSAN_ENCRYPTION_SET_PARAMETER_SCHEMA",
]

_CLUSTER_FIELD: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 128,
    "description": (
        "Managed object id of the cluster, like domain-c<N>. Not the name: read it from "
        "the cluster list (GET:/api/vcenter/cluster) first."
    ),
}
_HOST_FIELD: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 128,
    "description": (
        "Managed object id of the ESXi host, like host-<N>. Not the name: read it from "
        "the host list (GET:/api/vcenter/host) first."
    ),
}


def _disk_list(description: str) -> dict[str, Any]:
    return {
        "type": "array",
        "minItems": 1,
        "maxItems": 64,
        "uniqueItems": True,
        "items": {
            "type": "string",
            "minLength": 1,
            "maxLength": 255,
            "description": "Canonical name of one disk, like naa.<id>.",
        },
        "description": description,
    }


_ALLOW_REDUCED_REDUNDANCY_FIELD: dict[str, Any] = {
    "type": "boolean",
    "description": (
        "Let vSAN go on with less data redundancy while it changes the disks, when the "
        "cluster has too few hosts to keep it. Leave it out unless vSAN refused the change "
        "for that reason."
    ),
}

_ISSUES_SCHEMA: dict[str, Any] = {
    "type": "array",
    "description": "Present when the change failed or is not confirmed.",
    "items": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "Where the problem is."},
            "severity": {"type": "string", "description": "error, warning or info."},
            "message": {"type": "string", "description": "What happened."},
        },
    },
}

_STATUS_READ_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "description": "The read to follow the vCenter task: op_id and params. Null without a task.",
    "properties": {
        "op_id": {"type": "string", "description": "vmware.tasks.recent."},
        "params": {
            "type": "object",
            "description": "The parameters of that read (none; find the task id in its list).",
        },
    },
}

_DISK_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "One disk of the host, as vSAN sees it.",
    "properties": {
        "name": {"type": ["string", "null"], "description": "Canonical name of the disk."},
        "display_name": {"type": ["string", "null"], "description": "Name shown in vCenter."},
        "device_path": {"type": ["string", "null"], "description": "Device path on the host."},
        "ssd": {"type": ["boolean", "null"], "description": "True for a flash disk."},
        "capacity_bytes": {"type": ["integer", "null"], "description": "Size in bytes."},
        "vsan_state": {
            "type": ["string", "null"],
            "description": "inUse (vSAN uses it), eligible (can be claimed) or ineligible.",
        },
        "vsan_uuid": {"type": ["string", "null"], "description": "vSAN id when in use."},
        "vsan_error": {
            "type": ["string", "null"],
            "description": "Why vSAN cannot use the disk, when it says so.",
        },
    },
}

# ---------------------------------------------------------------------------
# vsan.cluster.configure / vsan.encryption.set (vendor jobs)
# ---------------------------------------------------------------------------

VSAN_CLUSTER_CONFIGURE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": _CLUSTER_FIELD,
        "vsan_enabled": {
            "type": "boolean",
            "description": "true turns vSAN on for the cluster, false turns it off.",
        },
        "esa": {
            "type": "boolean",
            "description": (
                "true picks the vSAN Express Storage Architecture (ESA). Only together with "
                "vsan_enabled: true, when vSAN is turned on."
            ),
        },
        "space_efficiency": {
            "type": "string",
            "enum": ["none", "compression", "deduplication_and_compression"],
            "description": (
                "Space efficiency of an OSA cluster: none, compression only, or deduplication "
                "and compression. vSAN writes all data again to change it."
            ),
        },
        "performance_service": {
            "type": "boolean",
            "description": "true turns the vSAN performance service on, false turns it off.",
        },
        "allow_reduced_redundancy": _ALLOW_REDUCED_REDUNDANCY_FIELD,
    },
    "required": ["cluster"],
    "anyOf": [
        {"required": ["vsan_enabled"]},
        {"required": ["space_efficiency"]},
        {"required": ["performance_service"]},
    ],
    "additionalProperties": False,
    "examples": [{"cluster": "<cluster-id>", "vsan_enabled": True, "esa": True}],
}

_ENCRYPTION_AT_REST_FIELD: dict[str, Any] = {
    "type": "object",
    "description": "Data-at-rest encryption: the data on the vSAN disks.",
    "properties": {
        "enabled": {"type": "boolean", "description": "true turns it on, false turns it off."},
        "key_provider": {
            "type": "string",
            "minLength": 1,
            "maxLength": 255,
            "description": (
                "Name of the key provider in vCenter that gives the keys. Leave it out to use "
                "the default key provider."
            ),
        },
        "erase_disks_before_use": {
            "type": "boolean",
            "description": "true erases each disk before vSAN uses it encrypted. It is slow.",
        },
    },
    "required": ["enabled"],
    "additionalProperties": False,
}

_ENCRYPTION_IN_TRANSIT_FIELD: dict[str, Any] = {
    "type": "object",
    "description": "Data-in-transit encryption: the vSAN traffic between the hosts.",
    "properties": {
        "enabled": {"type": "boolean", "description": "true turns it on, false turns it off."},
        "rekey_interval_minutes": {
            "type": "integer",
            "minimum": 30,
            "maximum": 10080,
            "description": "How often vSAN changes the traffic keys, in minutes.",
        },
    },
    "required": ["enabled"],
    "additionalProperties": False,
}

VSAN_ENCRYPTION_SET_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": _CLUSTER_FIELD,
        "data_at_rest": _ENCRYPTION_AT_REST_FIELD,
        "data_in_transit": _ENCRYPTION_IN_TRANSIT_FIELD,
        "allow_reduced_redundancy": _ALLOW_REDUCED_REDUNDANCY_FIELD,
    },
    "required": ["cluster"],
    "anyOf": [{"required": ["data_at_rest"]}, {"required": ["data_in_transit"]}],
    "additionalProperties": False,
    "examples": [
        {
            "cluster": "<cluster-id>",
            "data_at_rest": {"enabled": True, "key_provider": "<key-provider-name>"},
        }
    ],
}

VSAN_CLUSTER_JOB_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["accepted", "already_running", "not_found", "invalid_request"],
            "description": (
                "accepted: vSAN took the job, it is not done yet. already_running: a vSAN job "
                "runs on the cluster, nothing new was started."
            ),
        },
        "cluster": {"type": "string", "description": "The cluster moid."},
        "cluster_name": {"type": ["string", "null"], "description": "The cluster name."},
        "requested": {"type": "object", "description": "The settings that were asked for."},
        "task": {
            "type": ["string", "null"],
            "description": "The vCenter task id of the job (accepted or already_running).",
        },
        "status_read": _STATUS_READ_SCHEMA,
        "guidance": {"type": ["string", "null"], "description": "What to do next."},
    },
    "required": ["status", "cluster", "task", "status_read"],
}

# ---------------------------------------------------------------------------
# vsan.encryption.rekey
# ---------------------------------------------------------------------------

VSAN_ENCRYPTION_REKEY_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": _CLUSTER_FIELD,
        "deep_rekey": {
            "type": "boolean",
            "default": False,
            "description": (
                "false (default): shallow rekey, a new key encryption key only; quick. "
                "true: deep rekey, new data keys and all data is written again; can take hours."
            ),
        },
        "allow_reduced_redundancy": _ALLOW_REDUCED_REDUNDANCY_FIELD,
    },
    "required": ["cluster"],
    "additionalProperties": False,
    "examples": [{"cluster": "<cluster-id>", "deep_rekey": False}],
}

VSAN_ENCRYPTION_REKEY_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["rekeyed", "timeout", "not_found", "invalid_request"],
            "description": "rekeyed: the task ended well. timeout: it still runs.",
        },
        "cluster": {"type": "string", "description": "The cluster moid."},
        "cluster_name": {"type": ["string", "null"], "description": "The cluster name."},
        "requested": {"type": "object", "description": "deep_rekey as asked."},
        "task": {"type": ["string", "null"], "description": "The vCenter task id."},
        "status_read": _STATUS_READ_SCHEMA,
        "issues": _ISSUES_SCHEMA,
        "guidance": {"type": ["string", "null"], "description": "What to do next."},
    },
    "required": ["status", "cluster", "task"],
}

# ---------------------------------------------------------------------------
# vsan.disk.claim
# ---------------------------------------------------------------------------

VSAN_DISK_CLAIM_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": _HOST_FIELD,
        "architecture": {
            "type": "string",
            "enum": ["esa", "osa"],
            "description": (
                "esa: add the disks to the host's storage pool (vSAN ESA). osa: create a disk "
                "group of cache and capacity disks (vSAN OSA). Use the one of the cluster."
            ),
        },
        "disks": _disk_list("ESA only: the disks to add to the storage pool."),
        "cache_disks": _disk_list("OSA only: the flash disk(s) for the cache tier."),
        "capacity_disks": _disk_list("OSA only: the disks for the capacity tier."),
        "osa_type": {
            "type": "string",
            "enum": ["allFlash", "hybrid"],
            "default": "allFlash",
            "description": "OSA only: allFlash (all disks flash) or hybrid (HDD capacity).",
        },
    },
    "required": ["host", "architecture"],
    "oneOf": [
        {
            "properties": {"architecture": {"const": "esa", "description": "An ESA storage pool."}},
            "required": ["disks"],
        },
        {
            "properties": {"architecture": {"const": "osa", "description": "An OSA disk group."}},
            "required": ["cache_disks", "capacity_disks"],
        },
    ],
    "additionalProperties": False,
    "examples": [{"host": "<host-id>", "architecture": "esa", "disks": ["<disk-canonical-name>"]}],
}

VSAN_DISK_CLAIM_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "claimed",
                "unchanged",
                "not_verified",
                "timeout",
                "precondition_failed",
                "invalid_request",
            ],
            "description": "claimed: vSAN uses every named disk now. Only claimed changed disks.",
        },
        "host": {"type": ["string", "null"], "description": "The host moid."},
        "host_name": {"type": ["string", "null"], "description": "The host name."},
        "architecture": {"type": ["string", "null"], "description": "esa or osa."},
        "disks": {
            "type": "array",
            "description": "The named disks: before the claim, or read again after it.",
            "items": _DISK_ROW_SCHEMA,
        },
        "task": {"type": ["string", "null"], "description": "The vCenter task id."},
        "task_state": {"type": ["string", "null"], "description": "The last task state."},
        "issues": _ISSUES_SCHEMA,
        "guidance": {"type": ["string", "null"], "description": "What to do next."},
    },
    "required": ["status", "host", "disks"],
}

# ---------------------------------------------------------------------------
# vsan.disk.wipe
# ---------------------------------------------------------------------------

VSAN_DISK_WIPE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": _HOST_FIELD,
        "disks": _disk_list("The disks to wipe. All data on them is lost."),
        "method": {
            "type": "string",
            "enum": ["partitions", "secure"],
            "default": "partitions",
            "description": (
                "partitions (default): erase the partition table of each disk; quick. secure: "
                "the vSAN secure wipe, which overwrites the whole disk; it can take hours and "
                "only some disks support it."
            ),
        },
    },
    "required": ["host", "disks"],
    "additionalProperties": False,
    "examples": [{"host": "<host-id>", "disks": ["<disk-canonical-name>"]}],
}

_PARTITION_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "One partition of the disk.",
    "properties": {
        "partition": {"type": ["integer", "null"], "description": "Partition number."},
        "type": {"type": ["string", "null"], "description": "Partition type, like vmfs."},
        "start_sector": {"type": ["integer", "null"], "description": "First sector."},
        "end_sector": {"type": ["integer", "null"], "description": "Last sector."},
    },
}

_WIPE_DISK_SCHEMA: dict[str, Any] = {
    **_DISK_ROW_SCHEMA,
    "properties": {
        **_DISK_ROW_SCHEMA["properties"],
        "partitions": {
            "type": "array",
            "description": "The partitions that are erased.",
            "items": _PARTITION_ROW_SCHEMA,
        },
        "wipe_eligible": {
            "type": ["string", "null"],
            "description": "Secure wipe: Yes, No or Unknown, as vSAN says.",
        },
        "wipe_state": {
            "type": ["string", "null"],
            "description": "Secure wipe: Wiping, Success or Failure.",
        },
    },
}

VSAN_DISK_WIPE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "wiped",
                "unchanged",
                "failed_part_way",
                "timeout",
                "precondition_failed",
                "invalid_request",
            ],
            "description": "wiped: every named disk was wiped. Only wiped changed all disks.",
        },
        "object": {
            "type": "object",
            "description": "What was asked: host, host_name, method and the disk names.",
            "properties": {
                "host": {"type": ["string", "null"], "description": "The host moid."},
                "host_name": {"type": ["string", "null"], "description": "The host name."},
                "method": {"type": "string", "description": "partitions or secure."},
                "disks": {
                    "type": "array",
                    "description": "The disk names.",
                    "items": {"type": "string", "description": "One disk name."},
                },
            },
        },
        "disks": {
            "type": "array",
            "description": "The disks before the wipe, with their partitions.",
            "items": _WIPE_DISK_SCHEMA,
        },
        "blockers": {
            "type": "array",
            "description": "Why disks must not be wiped; nothing was wiped when not empty.",
            "items": {
                "type": "object",
                "properties": {
                    "disk": {"type": "string", "description": "The disk name."},
                    "reason": {"type": "string", "description": "Why it is refused."},
                },
            },
        },
        "steps": {
            "type": "array",
            "description": "partitions method: one step per disk, in order.",
            "items": {
                "type": "object",
                "properties": {
                    "disk": {"type": "string", "description": "The disk name."},
                    "device_path": {"type": ["string", "null"], "description": "Its path."},
                    "status": {"type": "string", "description": "done, failed or not_run."},
                    "error": {"type": "string", "description": "vCenter's error, when failed."},
                },
            },
        },
        "disks_after": {
            "type": ["array", "null"],
            "description": "The disks read again after the wipe; null when that read failed.",
            "items": _WIPE_DISK_SCHEMA,
        },
        "task": {"type": ["string", "null"], "description": "secure method: the task id."},
        "task_state": {"type": ["string", "null"], "description": "The last task state."},
        "issues": _ISSUES_SCHEMA,
        "guidance": {"type": ["string", "null"], "description": "What to do next."},
    },
    "required": ["status", "object", "disks", "blockers", "steps"],
}
