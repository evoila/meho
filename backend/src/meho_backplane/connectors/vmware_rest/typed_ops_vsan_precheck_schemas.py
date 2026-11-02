# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schemas and AI guidance of ``vmware.vsan.maintenance.precheck``.

The vSAN data options are the vendor values of
``VsanHostDecommissionModeObjectAction_enum``; the check states are
``VsanResourceCheckStatusType_enum`` (vendor VI/JSON file of vCenter 9.0).
"""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "PRECHECK_LLM_INSTRUCTIONS",
    "PRECHECK_PARAMETER_SCHEMA",
    "PRECHECK_RESPONSE_SCHEMA",
    "VSAN_MODES",
]

#: The vSAN data options of a host's maintenance mode.
VSAN_MODES: Final = ("ensureObjectAccessibility", "evacuateAllData", "noAction")


def _nullable(kind: str, description: str) -> dict[str, Any]:
    return {"type": [kind, "null"], "description": description}


def _uuid_list(description: str) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "string", "description": "vSAN UUID of one object."},
        "description": description,
    }


PRECHECK_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "minLength": 1,
            "description": "Managed object id of the host, for example 'host-21'.",
        },
        "vsan_mode": {
            "type": "string",
            "enum": list(VSAN_MODES),
            "default": "ensureObjectAccessibility",
            "description": (
                "The vSAN data option to check. ensureObjectAccessibility: move only what is "
                "needed to keep all data reachable (vCenter's default). evacuateAllData: move "
                "all data off the host. noAction: move nothing. Default "
                "ensureObjectAccessibility."
            ),
        },
    },
    "required": ["host"],
    "additionalProperties": False,
    "examples": [
        {"host": "<host-id>"},
        {"host": "<host-id>", "vsan_mode": "evacuateAllData"},
    ],
}

PRECHECK_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "host": {"type": "string", "description": "Managed object id of the host."},
        "cluster": {"type": "string", "description": "Managed object id of its cluster."},
        "vsan_mode": {
            "type": "string",
            "enum": list(VSAN_MODES),
            "description": "The vSAN data option that was checked.",
        },
        "check_status": _nullable(
            "string",
            "State of the check: resourceCheckCompleted, resourceCheckRunning, "
            "resourceCheckFailed, ...",
        ),
        "result": _nullable(
            "string",
            "green: enough room. yellow: enough room, but the cluster has an issue. red: not "
            "enough room or data would become inaccessible. null while the check runs.",
        ),
        "checked_at": _nullable("string", "Time of the check result."),
        "data_to_move_bytes": _nullable("integer", "Data that would move off the host, in bytes."),
        "data_to_resync_bytes": _nullable(
            "integer", "Resync on the host that must end first, in bytes."
        ),
        "inaccessible_objects": {
            "type": "integer",
            "description": "Number of objects that would become inaccessible.",
        },
        "inaccessible_object_uuids": _uuid_list("Objects that would become inaccessible."),
        "non_compliant_objects": {
            "type": "integer",
            "description": "Number of objects that would no longer meet their storage policy.",
        },
        "non_compliant_object_uuids": _uuid_list(
            "Objects that would no longer meet their storage policy."
        ),
        "messages": {
            "type": "array",
            "items": {"type": "string", "description": "One message of vSAN."},
            "description": "vSAN's messages about the check.",
        },
        "entities": {
            "type": "array",
            "description": "One row per fault domain and per host with its space before and after.",
            "items": {
                "type": "object",
                "properties": {
                    "level": {
                        "type": "string",
                        "enum": ["fault_domain", "host"],
                        "description": "fault_domain or host.",
                    },
                    "name": _nullable("string", "Name of the fault domain or host."),
                    "host": _nullable("string", "Managed object id of the host."),
                    "is_new": _nullable(
                        "boolean", "True for a host vSAN says to add to make room."
                    ),
                    "capacity_bytes": _nullable("integer", "Capacity now, in bytes."),
                    "used_bytes": _nullable("integer", "Used space now, in bytes."),
                    "capacity_after_bytes": _nullable("integer", "Capacity after, in bytes."),
                    "used_after_bytes": _nullable("integer", "Used space after, in bytes."),
                    "extra_capacity_needed_bytes": _nullable(
                        "integer", "Capacity vSAN says to add, in bytes."
                    ),
                    "components_after": _nullable("integer", "vSAN components after."),
                    "max_components": _nullable("integer", "Most vSAN components it can hold."),
                },
            },
        },
        "task": {"type": "string", "description": "Id of the vCenter task of the check."},
        "note": _nullable("string", "Plain text when the check did not end in time, else null."),
    },
    "required": ["host", "cluster", "vsan_mode", "check_status", "result", "task", "note"],
}

PRECHECK_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call before a host goes into maintenance mode, to see what each vSAN data option "
        "would do to vSAN data. It changes nothing; it runs a check task in vCenter, which "
        "can take a minute or more."
    ),
    "parameter_hints": {
        "host": "Host moid, for example from vmware.host.usage.",
        "vsan_mode": "Check each option the operator considers, one call per option.",
    },
    "output_shape": (
        "{host, cluster, vsan_mode, check_status, result, checked_at, data_to_move_bytes, "
        "data_to_resync_bytes, inaccessible_objects, inaccessible_object_uuids, "
        "non_compliant_objects, non_compliant_object_uuids, messages, entities: [{level, "
        "name, host, is_new, capacity_bytes, used_bytes, capacity_after_bytes, "
        "used_after_bytes, extra_capacity_needed_bytes, components_after, max_components}], "
        "task, note}. note is set when the check did not end in time. A failed check "
        "returns an error with vCenter's fault."
    ),
    "next_step": (
        "If result is red or inaccessible_objects is above 0, do not use that option. Tell "
        "the operator the data to move. Check vmware.vsan.resync.status first: running "
        "resyncs should end before maintenance."
    ),
}
