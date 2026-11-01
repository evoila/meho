# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schemas and AI guidance of the vSAN cluster operations of #4446."""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites.schemas import (
    CLUSTER_MOID_PATTERN,
    DATASTORE_MOID_PATTERN,
)

__all__ = [
    "HCL_UPDATE_LLM_INSTRUCTIONS",
    "HCL_UPDATE_PARAMETER_SCHEMA",
    "HCL_UPDATE_RESPONSE_SCHEMA",
    "POWER_ACTION_RESPONSE_SCHEMA",
    "POWER_PRECHECK_LLM_INSTRUCTIONS",
    "POWER_PRECHECK_PARAMETER_SCHEMA",
    "POWER_PRECHECK_RESPONSE_SCHEMA",
    "SHUTDOWN_LLM_INSTRUCTIONS",
    "SHUTDOWN_PARAMETER_SCHEMA",
    "START_LLM_INSTRUCTIONS",
    "START_PARAMETER_SCHEMA",
]

_STR_OR_NULL: Final[dict[str, Any]] = {"type": ["string", "null"]}
_INT_OR_NULL: Final[dict[str, Any]] = {"type": ["integer", "null"]}

_CLUSTER: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": CLUSTER_MOID_PATTERN,
    "description": "Managed object id of the vSAN cluster, for example <cluster-id>.",
}

_ISSUES: Final[dict[str, Any]] = {
    "type": "array",
    "description": "Problems; an error issue means the call failed.",
    "items": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "Where the problem is."},
            "severity": {"type": "string", "description": "error or warning."},
            "message": {"type": "string", "description": "The problem in plain words."},
        },
    },
}
_GUIDANCE: Final[dict[str, Any]] = {"type": "string", "description": "What to do next."}

_STATUS_READ: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "description": "The read that follows the task: vmware.task.info with the task id.",
    "properties": {
        "op_id": {"type": "string", "description": "The op to call: vmware.task.info."},
        "params": {
            "type": "object",
            "description": "Its params.",
            "properties": {"task": {"type": "string", "description": "The task id."}},
        },
    },
}

_DATASTORE_FILE: Final[dict[str, Any]] = {
    "datastore": {
        "type": "string",
        "pattern": DATASTORE_MOID_PATTERN,
        "description": "Managed object id of the datastore with the file, for example "
        "<datastore-id>.",
    },
    "path": {
        "type": "string",
        "minLength": 1,
        "description": "Path of the file, relative to the datastore root, for example "
        "<folder>/<file>.json.",
    },
}

# ---------------------------------------------------------------------------
# vmware.composite.vsan.hcl.update
# ---------------------------------------------------------------------------

HCL_UPDATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hcl": {
            "description": "Where the new HCL database comes from. Leave out to keep it.",
            "oneOf": [
                {
                    "type": "object",
                    "description": "vCenter downloads it.",
                    "properties": {
                        "source": {
                            "type": "string",
                            "enum": ["web"],
                            "description": "web: vCenter downloads the database.",
                        },
                        "url": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Download address. Leave out for the vendor's "
                            "standard location.",
                        },
                    },
                    "required": ["source"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "description": "MEHO reads a file from a datastore and sends it.",
                    "properties": {
                        "source": {
                            "type": "string",
                            "enum": ["datastore_file"],
                            "description": "datastore_file: a JSON file, plain or gzipped.",
                        },
                        **_DATASTORE_FILE,
                    },
                    "required": ["source", "datastore", "path"],
                    "additionalProperties": False,
                },
            ],
        },
        "release_catalog": {
            "type": "object",
            "description": "A release catalog file (JSON) on a datastore. Leave out to keep it.",
            "properties": dict(_DATASTORE_FILE),
            "required": ["datastore", "path"],
            "additionalProperties": False,
        },
    },
    "anyOf": [{"required": ["hcl"]}, {"required": ["release_catalog"]}],
    "additionalProperties": False,
    "examples": [
        {"hcl": {"source": "web"}},
        {
            "hcl": {
                "source": "datastore_file",
                "datastore": "datastore-15",
                "path": "<folder>/<file>.json",
            },
            "release_catalog": {"datastore": "datastore-15", "path": "<folder>/<file>.json"},
        },
    ],
}

_STEP_ROW: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "step": {"type": "string", "enum": ["hcl", "release_catalog"], "description": "The step."},
        "source": {
            "type": "string",
            "enum": ["web", "datastore_file"],
            "description": "Where the data came from.",
        },
        "url": {**_STR_OR_NULL, "description": "The download address; null for the default."},
        "file": {**_STR_OR_NULL, "description": "The file, as [datastore] path."},
        "size_bytes": {"type": "integer", "description": "Size of the file read."},
    },
}

HCL_UPDATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["updated", "partly_done", "failed", "invalid_request"],
            "description": "updated: every step ran. partly_done: a later step failed.",
        },
        "steps_ran": {"type": "array", "items": _STEP_ROW, "description": "Steps that ran."},
        "failed_step": {
            "type": ["object", "null"],
            "description": "The step that failed, with the vendor's fault.",
            "properties": {
                "step": {"type": "string", "description": "The step."},
                "type": {**_STR_OR_NULL, "description": "The fault type."},
                "message": {**_STR_OR_NULL, "description": "The vendor's error text."},
            },
        },
        "steps_not_run": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Steps that did not run because an earlier one failed.",
        },
        "issues": _ISSUES,
        "guidance": _GUIDANCE,
    },
    "required": ["status", "steps_ran", "failed_step", "steps_not_run", "guidance"],
}

HCL_UPDATE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call when the vSAN health shows an old HCL database (hardware compatibility list) "
        "or release catalog, or before a hardware check or an update plan."
    ),
    "preconditions": (
        "For a file: put it on a datastore first (vmware.composite.datastore.file.list shows "
        "it). The HCL file is the vendor's JSON file, plain or gzipped."
    ),
    "parameter_hints": {
        "hcl": "{source: web} to let vCenter download it; add url for an inside web server. "
        "{source: datastore_file, datastore, path} for a file.",
        "release_catalog": "{datastore, path} of the release catalog JSON file.",
    },
    "output_shape": (
        "{status: updated|partly_done|failed|invalid_request, steps_ran: [{step, source, url, "
        "file, size_bytes}], failed_step: {step, type, message}|null, steps_not_run, guidance}."
    ),
    "next_step": (
        "updated: call vmware.host.vsan_health to see the hardware checks. partly_done or "
        "failed: MEHO did not undo a step; fix the cause and call again with the steps in "
        "steps_not_run and failed_step."
    ),
}

# ---------------------------------------------------------------------------
# vmware.vsan.cluster.power.precheck
# ---------------------------------------------------------------------------

POWER_PRECHECK_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"cluster": _CLUSTER},
    "required": ["cluster"],
    "additionalProperties": False,
    "examples": [{"cluster": "domain-c7"}],
}

_CHECK_ROW: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "group_name": {**_STR_OR_NULL, "description": "The health group."},
        "test_id": {**_STR_OR_NULL, "description": "The check id."},
        "test_name": {**_STR_OR_NULL, "description": "The check name."},
        "test_health": {**_STR_OR_NULL, "description": "red or yellow."},
        "test_short_description": {**_STR_OR_NULL, "description": "What the check looks at."},
    },
}

_TASK_STATE: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "description": "The task of the last power action, or null when there is none.",
    "properties": {
        "task": {"type": "string", "description": "The vCenter task id."},
        "state": {
            **_STR_OR_NULL,
            "description": "queued, running, success or error; null when vCenter no longer "
            "knows the task.",
        },
        "progress": {**_INT_OR_NULL, "description": "Percent done, when given."},
        "error": {**_STR_OR_NULL, "description": "The vendor's error text when it failed."},
    },
}

_POWER_CONTEXT: Final[dict[str, Any]] = {
    "type": "object",
    "description": "The power state of the cluster.",
    "properties": {
        "current_status": {
            **_STR_OR_NULL,
            "description": "vSAN's power state of the cluster, for example clusterPoweredOn.",
        },
        "ordered_statuses": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The power steps vSAN lists for the cluster, in order.",
        },
        "tracking_task": _TASK_STATE,
        "last_error": {**_STR_OR_NULL, "description": "The last power action error."},
    },
}

POWER_PRECHECK_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": {"type": "string", "description": "The cluster that was checked."},
        "ready_to_shut_down": {
            "type": "boolean",
            "description": "True when no check is red.",
        },
        "overall_health": {**_STR_OR_NULL, "description": "The overall colour of the check."},
        "blocking": {
            "type": "array",
            "items": _CHECK_ROW,
            "description": "Red checks: each one blocks a shut down.",
        },
        "warnings": {"type": "array", "items": _CHECK_ROW, "description": "Yellow checks."},
        "checked_at": {**_STR_OR_NULL, "description": "When vSAN ran the checks."},
        "power": _POWER_CONTEXT,
        "status": {"type": "string", "description": "invalid_request for a wrong input."},
        "issues": _ISSUES,
        "guidance": _GUIDANCE,
    },
    "required": ["cluster", "guidance"],
}

POWER_PRECHECK_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call before a planned shut down of a whole vSAN cluster, to see whether it can be "
        "shut down now and what blocks it. Also call it after a lost reply of a shut down or "
        "start: it shows the power state and the task of the last power action."
    ),
    "parameter_hints": {"cluster": "The cluster moid, for example <cluster-id>."},
    "output_shape": (
        "{cluster, ready_to_shut_down, overall_health, blocking: [{group_name, test_id, "
        "test_name, test_health, test_short_description}], warnings: [...], checked_at, "
        "power: {current_status, ordered_statuses, tracking_task: {task, state, progress, "
        "error}|null, last_error}, guidance}."
    ),
    "next_step": (
        "ready_to_shut_down true: tell the operator the warnings, then call "
        "vmware.composite.vsan.cluster.shutdown. False: report each blocking check."
    ),
}

# ---------------------------------------------------------------------------
# vmware.composite.vsan.cluster.shutdown / .start
# ---------------------------------------------------------------------------

SHUTDOWN_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": _CLUSTER,
        "reason": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": "Why the cluster is shut down; vSAN keeps it. Default: Planned "
            "maintenance.",
        },
    },
    "required": ["cluster"],
    "additionalProperties": False,
    "examples": [{"cluster": "domain-c7", "reason": "<reason>"}],
}

START_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"cluster": _CLUSTER},
    "required": ["cluster"],
    "additionalProperties": False,
    "examples": [{"cluster": "domain-c7"}],
}

POWER_ACTION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "accepted",
                "already_running",
                "unchanged",
                "not_confirmed",
                "invalid_request",
            ],
            "description": "accepted: vCenter took the job; it is not done yet. "
            "already_running: a power task still runs; nothing new started.",
        },
        "cluster": {"type": "string", "description": "The cluster."},
        "target_power_status": {
            "type": "string",
            "enum": ["clusterPoweredOff", "clusterPoweredOn"],
            "description": "The power state asked for.",
        },
        "task": {**_STR_OR_NULL, "description": "The vCenter task id of the job."},
        "status_read": _STATUS_READ,
        "current_status": {
            **_STR_OR_NULL,
            "description": "vSAN's power state of the cluster before the call.",
        },
        "issues": _ISSUES,
        "guidance": _GUIDANCE,
    },
    "required": ["status", "cluster", "target_power_status", "task", "status_read", "guidance"],
}

_JOB_RULES: Final = (
    " Use async mode (async: true) for this call: MEHO then keeps the answer with the task "
    "id under a run handle. After a lost reply do not call again blindly: call "
    "vmware.vsan.cluster.power.precheck, which shows the power state and the last power "
    "task, and decide from that."
)

SHUTDOWN_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to shut down every host of a vSAN cluster for planned work (for example a "
        "power or network change). vSAN stops the VMs and the hosts in the right order."
        + _JOB_RULES
    ),
    "preconditions": (
        "vmware.vsan.cluster.power.precheck says ready_to_shut_down true. The operator knows "
        "every VM of the cluster stops. If this vCenter runs on the cluster, it stops too; "
        "the start then needs a vCenter that runs again."
    ),
    "parameter_hints": {
        "cluster": "The cluster moid, for example <cluster-id>.",
        "reason": "Short reason, for example planned maintenance.",
    },
    "output_shape": (
        "{status: accepted|already_running|unchanged|not_confirmed|invalid_request, cluster, "
        "target_power_status, task, status_read: {op_id: vmware.task.info, params: {task}}, "
        "current_status, guidance}. accepted means started, not done."
    ),
    "next_step": (
        "Read status_read (vmware.task.info with the task id) until the task ends. "
        "already_running: follow that task instead."
    ),
}

START_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to start a vSAN cluster again after a cluster shut down, when its hosts are "
        "powered on and vCenter answers." + _JOB_RULES
    ),
    "preconditions": (
        "The hosts of the cluster are powered on (for example through their out-of-band "
        "management) and vCenter runs."
    ),
    "parameter_hints": {"cluster": "The cluster moid, for example <cluster-id>."},
    "output_shape": SHUTDOWN_LLM_INSTRUCTIONS["output_shape"],
    "next_step": (
        "Read status_read (vmware.task.info with the task id) until the task ends, then call "
        "vmware.host.vsan_health."
    ),
}
