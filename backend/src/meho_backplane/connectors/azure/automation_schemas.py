# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Name rules, parameter schemas and response schemas of the Azure Automation reads.

The operation table in :mod:`meho_backplane.connectors.azure.automation` uses
these schemas. The dispatcher checks every call against the parameter schema
before the body runs. The bodies in
:mod:`meho_backplane.connectors.azure.automation_reads` check the names again
with the same patterns and a full match, because a schema ``$`` also matches
before a line end.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.azure._model import TEXT_OR_NULL

__all__ = [
    "ACCOUNT_LIST_RESPONSE",
    "ACCOUNT_PARAMS",
    "ACCOUNT_PATTERN",
    "DEFAULT_JOB_DAYS",
    "JOB_LIST_RESPONSE",
    "JOB_PARAMS",
    "JOB_STATUSES",
    "MAX_JOB_DAYS",
    "RESOURCE_GROUP_PATTERN",
    "RUNBOOK_LIST_RESPONSE",
    "SCHEDULE_LIST_RESPONSE",
]

#: Azure's rule for a resource group name: 1 to 90 letters, digits, ``_``,
#: ``-``, ``.``, ``(`` and ``)``, not ending with ``.``.
RESOURCE_GROUP_PATTERN: Final[str] = r"^[-\w.()]{0,89}[-\w()]$"

#: Azure's rule for an Automation account name: 6 to 50 letters, digits and
#: ``-``, starting with a letter and ending with a letter or a digit. So the
#: name is always one part of the path.
ACCOUNT_PATTERN: Final[str] = r"^[A-Za-z][A-Za-z0-9-]{4,48}[A-Za-z0-9]$"

#: The job states of API version 2024-10-23 (``JobStatus``).
JOB_STATUSES: Final[tuple[str, ...]] = (
    "New",
    "Activating",
    "Running",
    "Completed",
    "Failed",
    "Stopped",
    "Blocked",
    "Suspended",
    "Disconnected",
    "Suspending",
    "Stopping",
    "Resuming",
    "Removing",
)

#: The days ``azure.automation.job.list`` reads when the caller gives none.
DEFAULT_JOB_DAYS: Final[int] = 7

#: The most days ``azure.automation.job.list`` reads. Azure removes a job 30
#: days after its last change, so older jobs do not exist.
MAX_JOB_DAYS: Final[int] = 30

# ----------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------

_RESOURCE_GROUP: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 90,
    "pattern": RESOURCE_GROUP_PATTERN,
    "description": (
        "The resource group of the Automation account, as azure.automation.account.list "
        "returns it (resource_group)."
    ),
}

_ACCOUNT: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 6,
    "maxLength": 50,
    "pattern": ACCOUNT_PATTERN,
    "description": (
        "The Automation account name, as azure.automation.account.list returns it (name)."
    ),
}

_EXAMPLE_ACCOUNT: Final[dict[str, str]] = {
    "resource_group": "rg-automation-example",
    "account": "example-automation",
}

#: The parameters of a read of one Automation account.
ACCOUNT_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"resource_group": _RESOURCE_GROUP, "account": _ACCOUNT},
    "required": ["resource_group", "account"],
    "additionalProperties": False,
    "examples": [_EXAMPLE_ACCOUNT],
}

#: The parameters of ``azure.automation.job.list``.
JOB_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "resource_group": _RESOURCE_GROUP,
        "account": _ACCOUNT,
        "days": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_JOB_DAYS,
            "description": (
                f"How many days back to read jobs, by their start time. {DEFAULT_JOB_DAYS} "
                f"when left out; at most {MAX_JOB_DAYS}, because Azure keeps a job for "
                f"{MAX_JOB_DAYS} days."
            ),
        },
        "status": {
            "type": "string",
            "enum": list(JOB_STATUSES),
            "description": (
                "Only jobs in this state, for example Failed, Running or Suspended. "
                "Leave it out for every state."
            ),
        },
    },
    "required": ["resource_group", "account"],
    "additionalProperties": False,
    "examples": [
        _EXAMPLE_ACCOUNT,
        {**_EXAMPLE_ACCOUNT, "days": 3, "status": "Failed"},
    ],
}

# ----------------------------------------------------------------------
# Responses
# ----------------------------------------------------------------------


def _fields(**fields: tuple[Any, str]) -> dict[str, Any]:
    return {name: {"type": kind, "description": text} for name, (kind, text) in fields.items()}


_OMISSIONS: Final[dict[str, Any]] = {
    "type": "object",
    "description": "What is missing from value, and why.",
    "properties": {
        "count": {"type": "integer", "description": "How many parts are missing."},
        "items": {
            "type": "array",
            "description": "One item per missing part.",
            "items": {
                "type": "object",
                "properties": _fields(
                    what=("string", "The part that is missing, for example 'jobs'."),
                    reason=(
                        "string",
                        "page_limit, time_limit, row_limit, no_permission, not_found, "
                        "provider_not_registered, throttled or read_failed.",
                    ),
                    detail=("string", "MEHO's own text: what happened and what to do."),
                ),
            },
        },
    },
}

_TRUNCATED: Final[dict[str, Any]] = {
    "type": "boolean",
    "description": (
        "True when something is missing from the answer (rows, or the runbook links "
        "of the schedules); omissions says what and why."
    ),
}


def _list_response(row: dict[str, Any], **blocks: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {"type": "object", "properties": row},
                "description": "One row each. A long list comes back as a stored-result link.",
            },
            "truncated": _TRUNCATED,
            "omissions": _OMISSIONS,
            **blocks,
        },
        "required": ["value", "truncated", "omissions"],
    }


#: The answer of ``azure.automation.account.list``.
ACCOUNT_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    _fields(
        name=(TEXT_OR_NULL, "The Automation account name."),
        id=(TEXT_OR_NULL, "The account's Azure resource ID."),
        resource_group=(TEXT_OR_NULL, "The resource group of the account."),
        location=(TEXT_OR_NULL, "The Azure region of the account."),
        state=(TEXT_OR_NULL, "Ok, Unavailable or Suspended."),
        sku=(TEXT_OR_NULL, "The pricing tier: Free or Basic."),
        creation_time=(TEXT_OR_NULL, "When the account was made (ISO 8601)."),
        last_modified_time=(TEXT_OR_NULL, "When the account last changed (ISO 8601)."),
    )
)

#: The answer of ``azure.automation.runbook.list``.
RUNBOOK_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    _fields(
        name=(TEXT_OR_NULL, "The runbook name."),
        runbook_type=(
            TEXT_OR_NULL,
            "The kind of runbook, for example PowerShell, PowerShell72, Python3 or "
            "GraphPowerShell.",
        ),
        state=(TEXT_OR_NULL, "New (never published), Edit (a draft is open) or Published."),
        description=(TEXT_OR_NULL, "The description, as set in Azure."),
        creation_time=(TEXT_OR_NULL, "When the runbook was made (ISO 8601)."),
        last_modified_time=(TEXT_OR_NULL, "When the runbook last changed (ISO 8601)."),
        last_modified_by=(TEXT_OR_NULL, "Who changed the runbook last, as Azure names them."),
    )
)

#: The answer of ``azure.automation.job.list``.
JOB_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    _fields(
        name=(TEXT_OR_NULL, "The job's resource name."),
        job_id=(TEXT_OR_NULL, "The job ID (a GUID), as the Azure portal shows it."),
        runbook=(TEXT_OR_NULL, "The name of the runbook the job ran."),
        status=(TEXT_OR_NULL, "Completed, Failed, Stopped, Suspended, Running, New and others."),
        creation_time=(TEXT_OR_NULL, "When the job was made (ISO 8601)."),
        start_time=(TEXT_OR_NULL, "When the job started (ISO 8601), or null."),
        end_time=(TEXT_OR_NULL, "When the job ended (ISO 8601), or null while it runs."),
        started_by=(TEXT_OR_NULL, "Who or what started the job, as Azure names it."),
        run_on=(
            TEXT_OR_NULL,
            "The Hybrid Runbook Worker group the job ran on, or null when it ran in Azure.",
        ),
    ),
    window={
        "type": "object",
        "description": "The time window MEHO asked Azure for (UTC).",
        "properties": {
            "days": {"type": "integer", "description": "How many days back."},
            "from": {"type": "string", "description": "The start of the window (ISO 8601)."},
            "to": {"type": "string", "description": "The end of the window: the time of the read."},
        },
    },
    status_filter={
        "type": TEXT_OR_NULL,
        "description": "The job state the list was narrowed to, or null for every state.",
    },
)

#: The answer of ``azure.automation.schedule.list``.
SCHEDULE_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    {
        **_fields(
            name=(TEXT_OR_NULL, "The schedule name."),
            enabled=(["boolean", "null"], "Whether the schedule starts jobs."),
            frequency=(TEXT_OR_NULL, "OneTime, Minute, Hour, Day, Week or Month."),
            interval=(["number", "null"], "Every how many units of frequency it runs."),
            next_run=(TEXT_OR_NULL, "The next run (ISO 8601), or null."),
            start_time=(TEXT_OR_NULL, "The first run (ISO 8601)."),
            expiry_time=(TEXT_OR_NULL, "When the schedule ends (ISO 8601)."),
            time_zone=(TEXT_OR_NULL, "The time zone of the schedule, for example UTC."),
            description=(TEXT_OR_NULL, "The description, as set in Azure."),
        ),
        "week_days": {
            "type": "array",
            "items": {"type": "string"},
            "description": "For a weekly schedule: the days it runs on, for example Monday.",
        },
        "month_days": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "For a monthly schedule: the days of the month it runs on.",
        },
        "monthly_occurrences": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "For a monthly schedule: the week and day it runs on, for example '1 Monday' "
                "(the first Monday; -1 is the last)."
            ),
        },
        "runbooks": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": (
                "The names of the runbooks the schedule starts. An empty list means none; "
                "null means MEHO could not read the links (omissions says why)."
            ),
        },
    }
)
