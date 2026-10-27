# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameter and response schemas of the Azure security and governance reads.

The operation table in :mod:`meho_backplane.connectors.azure.security` uses
them. Every list answer has the same frame: the rows under ``value``, their
number in ``count``, and the paging fields of
:meth:`~meho_backplane.connectors.azure._paging.PagedRows.as_dict`.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.azure._model import TEXT_OR_NULL

__all__ = [
    "ACTIVITY_LIST_PARAMS",
    "ACTIVITY_LIST_RESPONSE",
    "ALERT_LIST_PARAMS",
    "ALERT_LIST_RESPONSE",
    "CONTROL_LIST_RESPONSE",
    "PLAN_LIST_RESPONSE",
    "POLICY_ASSIGNMENT_LIST_RESPONSE",
    "POLICY_SUMMARY_RESPONSE",
    "RECOMMENDATION_LIST_PARAMS",
    "RECOMMENDATION_LIST_RESPONSE",
    "ROLE_ASSIGNMENT_LIST_PARAMS",
    "ROLE_ASSIGNMENT_LIST_RESPONSE",
    "SCORE_LIST_RESPONSE",
]

_INT_OR_NULL: Final[list[str]] = ["integer", "null"]
_NUMBER_OR_NULL: Final[list[str]] = ["number", "null"]

_EXAMPLE_SUBSCRIPTION: Final[str] = "00000000-0000-0000-0000-000000000000"


def _text(description: str) -> dict[str, Any]:
    return {"type": TEXT_OR_NULL, "description": description}


def _count(description: str) -> dict[str, Any]:
    return {"type": _INT_OR_NULL, "description": description}


#: The paging fields every list answer carries next to ``value``.
_LIST_FIELDS: Final[dict[str, Any]] = {
    "count": {"type": "integer", "description": "How many rows value holds."},
    "truncated": {
        "type": "boolean",
        "description": "True when MEHO stopped reading pages at one of its limits.",
    },
    "omission": _text("What was left out and how to narrow the request, when truncated."),
    "omission_kind": _text("Which limit stopped the list: row_limit, page_limit or time_limit."),
    "pages_read": {"type": "integer", "description": "How many pages MEHO read from Azure."},
}


def _list_response(row: dict[str, Any], rows_text: str, **extra: dict[str, Any]) -> dict[str, Any]:
    """A list answer: *row* under ``value``, the paging fields and *extra*."""
    return {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {"type": "object", "properties": row},
                "description": (
                    f"{rows_text} More than 50 rows come back as a link to the stored result."
                ),
            },
            **_LIST_FIELDS,
            **extra,
        },
        "required": ["value", "count", "truncated", "pages_read"],
    }


_SCORE_FIELDS: Final[dict[str, Any]] = {
    "score": {"type": _NUMBER_OR_NULL, "description": "The current score."},
    "max_score": {"type": _NUMBER_OR_NULL, "description": "The most the score can be."},
    "percentage": {
        "type": _NUMBER_OR_NULL,
        "description": "The score divided by the most score, as a percent from 0 to 100.",
    },
}

SCORE_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    {
        "name": _text("The name of the secure score, usually ascScore."),
        "display_name": _text("The name for people. Text from Azure."),
        **_SCORE_FIELDS,
    },
    "One row per secure score, usually one.",
)

CONTROL_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    {
        "name": _text("The id of the control."),
        "display_name": _text("The name of the control, for example Enable MFA. Text from Azure."),
        **_SCORE_FIELDS,
        "healthy_resource_count": _count("Resources that meet the control."),
        "unhealthy_resource_count": _count("Resources that do not meet the control."),
        "not_applicable_resource_count": _count("Resources the control does not apply to."),
        "recommendation_names": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The recommendations in the control. Each matches the name field of "
                "azure.security.recommendation.list."
            ),
        },
    },
    "One row per security control.",
)

RECOMMENDATION_LIST_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["Unhealthy", "Healthy", "NotApplicable", "All"],
            "default": "Unhealthy",
            "description": (
                "Which recommendations to return: Unhealthy (the default, what needs "
                "fixing), Healthy, NotApplicable or All."
            ),
        },
    },
    "additionalProperties": False,
    "examples": [{}, {"status": "All"}],
}

RECOMMENDATION_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    {
        "name": _text(
            "The id of the recommendation type. It repeats for every resource it applies to."
        ),
        "display_name": _text("The recommendation for people. Text from Azure."),
        "severity": _text("Critical, High, Medium or Low; null when Azure gives none."),
        "status": _text("Healthy, Unhealthy or NotApplicable."),
        "resource_id": _text("The Azure resource id of the assessed resource."),
    },
    "One row per resource and recommendation.",
    status_filter=_text(
        "The status filter MEHO applied: Unhealthy, Healthy, NotApplicable or All."
    ),
)

ALERT_LIST_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "days": {
            "type": "integer",
            "minimum": 1,
            "maximum": 90,
            "default": 7,
            "description": "How many days back to list alerts, from now: 1 to 90, 7 by default.",
        },
    },
    "additionalProperties": False,
    "examples": [{}, {"days": 30}],
}

ALERT_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    {
        "name": _text("The id of the alert."),
        "display_name": _text("The alert title. Text from Azure, can hold attacker-chosen names."),
        "alert_type": _text("The kind of alert, a code such as VM_EICAR."),
        "severity": _text("High, Medium, Low or Informational."),
        "status": _text("Active, InProgress, Resolved or Dismissed."),
        "time": _text("When Defender made the alert (UTC, ISO 8601)."),
        "resource_id": _text("The Azure resource id the alert is about, when Azure names one."),
        "compromised_entity": _text("The name of the affected resource. Text from Azure."),
    },
    "One row per alert in the window.",
    window={
        "type": "object",
        "description": "The window MEHO kept alerts for.",
        "properties": {
            "days": {"type": "integer", "description": "The number of days back."},
            "from": {"type": "string", "description": "Start of the window (UTC, ISO 8601)."},
            "to": {"type": "string", "description": "End of the window, now (UTC, ISO 8601)."},
        },
    },
)

PLAN_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    {
        "name": _text("The plan, for example VirtualMachines, SqlServers or Containers."),
        "tier": _text("Standard (the plan is on) or Free (the plan is off)."),
        "sub_plan": _text("The variant of the plan, for example P1 or P2, when there is one."),
        "deprecated": {
            "type": "boolean",
            "description": "True for an old plan that Azure replaced with another.",
        },
    },
    "One row per Defender plan.",
)

_POLICY_COUNTS: Final[dict[str, Any]] = {
    "non_compliant_resources": _count("Resources that do not comply."),
    "non_compliant_policies": _count("Policies with at least one non-compliant resource."),
    "compliant_resources": _count("Resources that comply."),
}

POLICY_SUMMARY_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        **_POLICY_COUNTS,
        "value": {
            "type": "array",
            "description": (
                "One row per policy assignment, the most non-compliant resources first. "
                "More than 50 rows come back as a link to the stored result."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "assignment_id": _text(
                        "The policy assignment id (Azure writes it in lower case)."
                    ),
                    "assignment_name": _text("The last part of the assignment id."),
                    "policy_set_definition_id": _text(
                        "The initiative (policy set) id, when the assignment is an initiative."
                    ),
                    **_POLICY_COUNTS,
                },
            },
        },
        "count": {"type": "integer", "description": "How many assignment rows value holds."},
    },
    "required": ["value", "count"],
}

POLICY_ASSIGNMENT_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    {
        "id": _text("The policy assignment id."),
        "name": _text("The name of the assignment."),
        "display_name": _text("The name for people. Text from Azure."),
        "scope": _text(
            "Where the assignment is set: a management group, the subscription, a "
            "resource group or a resource."
        ),
        "policy_definition_id": _text("The id of the assigned policy or initiative."),
        "is_initiative": {
            "type": "boolean",
            "description": "True when policy_definition_id names an initiative (a policy set).",
        },
        "enforcement_mode": _text("Default (the policy acts) or DoNotEnforce (it only reports)."),
    },
    "One row per policy assignment.",
)

ROLE_ASSIGNMENT_LIST_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "scope": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1024,
            "description": (
                "Optional. A scope inside the target's subscription: /subscriptions/<id>, "
                "/subscriptions/<id>/resourceGroups/<name>, or a resource id. The whole "
                "subscription when not given."
            ),
        },
    },
    "additionalProperties": False,
    "examples": [
        {},
        {"scope": f"/subscriptions/{_EXAMPLE_SUBSCRIPTION}/resourceGroups/example-rg"},
    ],
}

ROLE_ASSIGNMENT_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    {
        "name": _text("The id of the role assignment."),
        "principal_id": _text("The object id of the user, group or app that has the role."),
        "principal_type": _text("User, Group, ServicePrincipal, ForeignGroup or Device."),
        "role_name": _text("The role, for example Reader; null when the role was not found."),
        "role_definition_id": _text("The id of the role."),
        "scope": _text("Where the role applies: the subscription, a resource group or a resource."),
    },
    "One row per role assignment on the scope, above it or below it.",
    scope=_text("The scope MEHO read, with its first slash."),
)

ACTIVITY_LIST_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "from": {
            "type": "string",
            "format": "date-time",
            "maxLength": 64,
            "description": (
                "Start of the window in ISO 8601, for example 2026-10-26T00:00:00Z (UTC when "
                "no offset is given). At most 90 days back; an older time is moved to 90 "
                "days back. 24 hours before to when not given."
            ),
        },
        "to": {
            "type": "string",
            "format": "date-time",
            "maxLength": 64,
            "description": (
                "End of the window in ISO 8601. Now when not given; a time after now is "
                "moved to now."
            ),
        },
        "resource_group": {
            "type": "string",
            "minLength": 1,
            "maxLength": 90,
            "description": "Optional. Only events of this resource group; Azure filters by it.",
        },
        "caller": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": (
                "Optional. Only events of this caller, a sign-in name such as "
                "user@example.com or an app id. MEHO compares it without regard to case "
                "after reading."
            ),
        },
        "status": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "description": (
                "Optional. Only events with this status, for example Succeeded, Failed, "
                "Started or Accepted. MEHO compares it without regard to case after reading."
            ),
        },
    },
    "additionalProperties": False,
    "examples": [
        {},
        {
            "from": "2026-10-20T00:00:00Z",
            "to": "2026-10-27T00:00:00Z",
            "resource_group": "example-rg",
            "status": "Failed",
        },
    ],
}

ACTIVITY_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    {
        "time": _text("When the event happened (UTC, ISO 8601)."),
        "operation": _text(
            "The Azure operation, for example Microsoft.Compute/virtualMachines/write."
        ),
        "status": _text("The status, for example Started, Accepted, Succeeded or Failed."),
        "caller": _text("Who did it: a sign-in name or the app id of a service principal."),
        "resource_id": _text("The Azure resource id the event is about."),
        "correlation_id": _text("Shared by all events of one operation."),
        "level": _text("Critical, Error, Warning, Informational or Verbose."),
    },
    "One row per event in the window.",
    window={
        "type": "object",
        "description": "The window MEHO read.",
        "properties": {
            "from": {"type": "string", "description": "Start of the window (UTC, ISO 8601)."},
            "to": {"type": "string", "description": "End of the window (UTC, ISO 8601)."},
            "clamped": {
                "type": "boolean",
                "description": (
                    "True when MEHO moved the window into the last 90 days: a from more "
                    "than 90 days back, or a to after now."
                ),
            },
        },
    },
)
