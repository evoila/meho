# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schemas and AI guidance of the two alarm changes (#4383, part 1).

``vmware.composite.alarm.acknowledge`` and ``vmware.composite.alarm.reset``;
the handlers are in :mod:`._alarm`. The alarm rows have the shape of
``vmware.alarm.triggered.list``.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ALARM_ACKNOWLEDGE_LLM_INSTRUCTIONS",
    "ALARM_ACKNOWLEDGE_PARAMETER_SCHEMA",
    "ALARM_ACKNOWLEDGE_RESPONSE_SCHEMA",
    "ALARM_RESET_LLM_INSTRUCTIONS",
    "ALARM_RESET_PARAMETER_SCHEMA",
    "ALARM_RESET_RESPONSE_SCHEMA",
]

_ALARM_ID_FIELD: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 128,
    "description": (
        "Managed object id of the alarm: alarm_id of a vmware.alarm.triggered.list row."
    ),
}
_ENTITY_ID_FIELD: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 128,
    "description": (
        "Managed object id of the object the alarm is triggered on: entity_id of the same row."
    ),
}

_ALARM_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "One triggered alarm, in the shape of vmware.alarm.triggered.list.",
    "properties": {
        "alarm_id": {"type": "string", "description": "Managed object id of the alarm."},
        "alarm_name": {"type": ["string", "null"], "description": "Name of the alarm."},
        "trigger_kind": {
            "type": ["string", "null"],
            "description": "event, metric, mixed or null (not known).",
        },
        "entity_id": {"type": "string", "description": "Managed object id of the object."},
        "entity_type": {"type": "string", "description": "Type of the object."},
        "entity_name": {"type": ["string", "null"], "description": "Name of the object."},
        "status": {"type": ["string", "null"], "description": "red or yellow."},
        "time": {"type": ["string", "null"], "description": "When it was triggered."},
        "acknowledged": {"type": "boolean", "description": "True when acknowledged."},
        "acknowledged_by": {
            "type": ["string", "null"],
            "description": "User who acknowledged it.",
        },
        "acknowledged_time": {
            "type": ["string", "null"],
            "description": "When it was acknowledged.",
        },
    },
}

_ISSUES_SCHEMA: dict[str, Any] = {
    "type": "array",
    "description": "Present when the call failed or the change is not confirmed.",
    "items": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "Where the problem is."},
            "severity": {"type": "string", "description": "error, warning or info."},
            "message": {"type": "string", "description": "What happened."},
        },
    },
}

ALARM_ACKNOWLEDGE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"alarm_id": _ALARM_ID_FIELD, "entity_id": _ENTITY_ID_FIELD},
    "required": ["alarm_id", "entity_id"],
    "additionalProperties": False,
    "examples": [{"alarm_id": "<alarm-id>", "entity_id": "<entity-id>"}],
}

ALARM_ACKNOWLEDGE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "acknowledged",
                "unchanged",
                "not_triggered",
                "not_verified",
                "no_alarm_manager",
            ],
            "description": "What happened. Only acknowledged changed something.",
        },
        "alarm": {
            **_ALARM_ROW_SCHEMA,
            "type": ["object", "null"],
            "description": "The alarm, with who acknowledged it and when; null when not found.",
        },
        "issues": _ISSUES_SCHEMA,
        "guidance": {"type": ["string", "null"], "description": "What to do next."},
    },
    "required": ["status", "alarm"],
}

ALARM_RESET_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string", "enum": ["red", "yellow"]},
            "description": "Reset only alarms with these statuses: red, yellow, or both.",
        },
        "object_kind": {
            "type": "string",
            "enum": ["all", "host", "vm"],
            "description": (
                "Reset only alarms on hosts (host), on VMs (vm), or on any object (all). "
                "Use all for an alarm on the vCenter itself, a cluster or a datastore."
            ),
        },
        "trigger_kind": {
            "type": "string",
            "enum": ["all", "event", "metric"],
            "description": (
                "Reset only alarms fired by an event (event), by a metric or a state "
                "(metric), or both (all). trigger_kind of a vmware.alarm.triggered.list "
                "row tells which."
            ),
        },
    },
    "required": ["status", "object_kind", "trigger_kind"],
    "additionalProperties": False,
    "examples": [{"status": ["red"], "object_kind": "host", "trigger_kind": "event"}],
}

ALARM_RESET_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["reset", "unchanged", "not_verified", "no_alarm_manager"],
            "description": "What happened. reset: at least one alarm is green now.",
        },
        "filter": {
            "type": "object",
            "description": "The filter the reset used.",
            "properties": {
                "status": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Alarm statuses.",
                },
                "object_kind": {"type": "string", "description": "all, host or vm."},
                "trigger_kind": {"type": "string", "description": "all, event or metric."},
            },
        },
        "cleared": {
            "type": ["array", "null"],
            "description": "Alarms that were triggered before the reset and are not now.",
            "items": _ALARM_ROW_SCHEMA,
        },
        "still_triggered": {
            "type": ["array", "null"],
            "description": "Alarms that match the filter and are still triggered.",
            "items": _ALARM_ROW_SCHEMA,
        },
        "issues": _ISSUES_SCHEMA,
        "guidance": {"type": ["string", "null"], "description": "What to do next."},
    },
    "required": ["status", "filter", "cleared", "still_triggered"],
}

ALARM_ACKNOWLEDGE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to acknowledge one triggered alarm on one object, so its actions (emails, "
        "traps) stop while someone works on the cause. The alarm stays red or yellow. It "
        "is safe to run twice: an acknowledged alarm gives unchanged."
    ),
    "preconditions": (
        "Take alarm_id and entity_id from one row of vmware.alarm.triggered.list. The "
        "target is a vCenter. The call waits for approval before it runs."
    ),
    "parameter_hints": {
        "alarm_id": "alarm_id of the row you picked. Do not guess an id.",
        "entity_id": "entity_id of the same row.",
    },
    "output_shape": (
        "{status: acknowledged|unchanged|not_triggered|not_verified|no_alarm_manager, "
        "alarm: {alarm_id, alarm_name, entity_id, entity_type, entity_name, status, time, "
        "acknowledged, acknowledged_by, acknowledged_time} or null, issues, guidance}. "
        "Only acknowledged changed something."
    ),
    "next_step": (
        "Fix the cause of the alarm. When it is fixed and the alarm does not turn green by "
        "itself, call vmware.composite.alarm.reset. On not_triggered, list the alarms again."
    ),
}

ALARM_RESET_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to reset triggered alarms to green after their cause is fixed, for example "
        "an alarm whose rule has no green condition. vCenter cannot reset one alarm "
        "alone: this resets every triggered alarm in the vCenter that has one of the "
        "statuses, the object kind and the trigger kind you give."
    ),
    "preconditions": (
        "Fix the cause first. A reset hides the problem until the alarm fires again, and "
        "an event alarm fires again only on a new event. Call vmware.alarm.triggered.list "
        "first and choose the narrowest filter that catches your alarm: its status, host or "
        "vm when it is on a host or a VM (else all), and its trigger_kind (all when it is "
        "mixed or null). The call waits for approval before it runs."
    ),
    "parameter_hints": {
        "status": "e.g. ['red'].",
        "object_kind": "host, vm, or all (the vCenter itself, a cluster, a datastore).",
        "trigger_kind": "event, metric, or all.",
    },
    "output_shape": (
        "{status: reset|unchanged|not_verified|no_alarm_manager, filter: {status, "
        "object_kind, trigger_kind}, cleared: [alarm rows], still_triggered: [alarm rows], "
        "issues, guidance}. cleared lists every alarm the reset turned green, also ones "
        "you did not mean."
    ),
    "next_step": (
        "Check cleared: tell the user about every alarm in it that they did not mean to "
        "reset. An alarm in still_triggered still has its cause, or needs another filter."
    ),
}
