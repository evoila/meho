# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameter and response schemas of the Azure Arc and patching operations.

The operation table is in :mod:`meho_backplane.connectors.azure.arc`. The
schemas tell the AI which fields it may send and what comes back. The name
patterns here are also the checks the operation bodies run before a name goes
into a URL path (:mod:`.arc_reads`, :mod:`.arc_assess`).
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.azure._model import TEXT_OR_NULL

__all__ = [
    "ARC_MACHINE_NAME_CHARS",
    "ARC_MACHINE_NAME_MAX_LENGTH",
    "ASSESSMENT_LIST_RESPONSE",
    "ASSESSMENT_START_PARAMS",
    "ASSESSMENT_START_RESPONSE",
    "ASSESSMENT_SUMMARY_FIELDS",
    "INSTALLATION_DEFAULT_DAYS",
    "INSTALLATION_LIST_PARAMS",
    "INSTALLATION_LIST_RESPONSE",
    "INSTALLATION_MAX_DAYS",
    "INSTALLATION_SUMMARY_FIELDS",
    "MACHINE_ID_MAX_LENGTH",
    "MACHINE_LIST_RESPONSE",
    "MACHINE_SHOW_PARAMS",
    "MACHINE_SHOW_RESPONSE",
    "MACHINE_SUMMARY_FIELDS",
    "RESOURCE_GROUP_CHARS",
    "RESOURCE_GROUP_MAX_LENGTH",
    "WINDOW_FIELDS",
]

#: A resource group name: letters, digits, ``_``, ``-``, ``.``, ``(`` and
#: ``)``, 1 to 90 characters, not ending with ``.`` (Microsoft's naming rules).
#: Not ending with ``.`` also rules out ``.`` and ``..``, which could move the
#: URL path to another subscription.
RESOURCE_GROUP_CHARS: Final[str] = r"[-\w.()]{0,89}[-\w()]"
RESOURCE_GROUP_MAX_LENGTH: Final[int] = 90

#: An Azure Arc machine name: letters, digits, ``_``, ``-`` and ``.``, 1 to 54
#: characters (the rule in Azure's API description). It may not start with
#: ``.``, so ``.`` and ``..`` never reach a URL path.
ARC_MACHINE_NAME_CHARS: Final[str] = r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,53}"
ARC_MACHINE_NAME_MAX_LENGTH: Final[int] = 54

#: Longest machine ID MEHO accepts.
MACHINE_ID_MAX_LENGTH: Final[int] = 512

#: Azure keeps the results of installation runs for 30 days.
INSTALLATION_MAX_DAYS: Final[int] = 30
INSTALLATION_DEFAULT_DAYS: Final[int] = 30

#: The fields of each list's ``summary`` object, and of the ``window`` object
#: of the installation list. The ``result_objects`` hint of each list names the
#: same fields, so they stay next to a link to the stored result.
MACHINE_SUMMARY_FIELDS: Final[tuple[str, ...]] = (
    "machines",
    "connected",
    "disconnected",
    "error",
    "awaiting_connection",
    "other_status",
)
ASSESSMENT_SUMMARY_FIELDS: Final[tuple[str, ...]] = (
    "machines",
    "needing_critical_or_security",
    "critical_patches",
    "security_patches",
    "other_patches",
    "reboot_pending",
    "failed_assessments",
)
INSTALLATION_SUMMARY_FIELDS: Final[tuple[str, ...]] = (
    "runs",
    "machines",
    "failed_runs",
    "installed_patches",
    "failed_patches",
)
WINDOW_FIELDS: Final[tuple[str, ...]] = ("days", "from", "to")

#: ``(?!\n)``: the validator uses Python's ``re``, where ``$`` also matches
#: before a final newline, so without it ``"name\n"`` would pass.
_RESOURCE_GROUP_PATTERN: Final[str] = f"^{RESOURCE_GROUP_CHARS}$(?!\\n)"
_ARC_MACHINE_NAME_PATTERN: Final[str] = f"^{ARC_MACHINE_NAME_CHARS}$(?!\\n)"

_EXAMPLE_SUBSCRIPTION: Final[str] = "00000000-0000-0000-0000-000000000000"
_EXAMPLE_GROUP: Final[str] = "rg-servers-example"


def _text(description: str) -> dict[str, Any]:
    return {"type": TEXT_OR_NULL, "description": description}


def _count(description: str) -> dict[str, Any]:
    return {"type": "integer", "description": description}


def _count_or_null(description: str) -> dict[str, Any]:
    return {"type": ["integer", "null"], "description": description}


def _flag(description: str) -> dict[str, Any]:
    return {"type": ["boolean", "null"], "description": description}


def _object(fields: dict[str, Any], description: str) -> dict[str, Any]:
    return {"type": "object", "properties": fields, "description": description}


_ERROR: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "description": "Azure's error code and message, or null. The message is text from Azure.",
    "properties": {
        "code": _text("Azure's error code."),
        "message": _text("Azure's error text, with secrets filtered out."),
    },
}

_PAGING_FIELDS: Final[dict[str, Any]] = {
    "truncated": {
        "type": "boolean",
        "description": "True when MEHO stopped reading pages at one of its limits.",
    },
    "omission": _text("What was left out and how to narrow the request, when truncated."),
    "omission_kind": _text(
        "Which limit stopped the list: row_limit, page_limit, time_limit or result_truncated."
    ),
    "pages_read": _count("How many pages MEHO read from Azure."),
}


def _list_response(row: dict[str, Any], what: str, extra: dict[str, Any]) -> dict[str, Any]:
    """The answer of a list: rows under ``value``, the extra objects, and the paging."""
    return {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {"type": "object", "properties": row},
                "description": (
                    f"One row per {what}. More than 50 rows (or more than 4 KB) come back "
                    "as a link to the stored result; read them with result_query."
                ),
            },
            **extra,
            **_PAGING_FIELDS,
        },
        "required": ["value", "summary", "truncated", "pages_read"],
    }


# ----------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------

MACHINE_SHOW_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "resource_group": {
            "type": "string",
            "minLength": 1,
            "maxLength": RESOURCE_GROUP_MAX_LENGTH,
            "pattern": _RESOURCE_GROUP_PATTERN,
            "description": (
                "The resource group of the Arc machine, as Azure shows it (resource_group "
                "of azure.arc.machine.list). Letters, digits, '_', '-', '.', '(' and ')'; "
                "it does not end with '.'."
            ),
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": ARC_MACHINE_NAME_MAX_LENGTH,
            "pattern": _ARC_MACHINE_NAME_PATTERN,
            "description": (
                "The name of the Arc machine, as Azure shows it (name of "
                "azure.arc.machine.list). Letters, digits, '_', '-' and '.'; it does "
                "not start with '.'."
            ),
        },
    },
    "required": ["resource_group", "name"],
    "additionalProperties": False,
    "examples": [{"resource_group": _EXAMPLE_GROUP, "name": "arc-server-01"}],
}

INSTALLATION_LIST_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "days": {
            "type": "integer",
            "minimum": 1,
            "maximum": INSTALLATION_MAX_DAYS,
            "default": INSTALLATION_DEFAULT_DAYS,
            "description": (
                "How many days back to read, 1 to 30 (default 30). Azure keeps the "
                "results of installation runs for 30 days."
            ),
        },
    },
    "additionalProperties": False,
    "examples": [{}, {"days": 7}],
}

_EXAMPLE_VM_ID: Final[str] = (
    f"/subscriptions/{_EXAMPLE_SUBSCRIPTION}/resourceGroups/{_EXAMPLE_GROUP}"
    + "/providers/Microsoft.Compute/virtualMachines/vm-app-01"
)
_EXAMPLE_ARC_ID: Final[str] = (
    f"/subscriptions/{_EXAMPLE_SUBSCRIPTION}/resourceGroups/{_EXAMPLE_GROUP}"
    + "/providers/Microsoft.HybridCompute/machines/arc-server-01"
)

ASSESSMENT_START_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "machine_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": MACHINE_ID_MAX_LENGTH,
            "pattern": "^/subscriptions/",
            "description": (
                "The Azure resource ID of one VM (Microsoft.Compute/virtualMachines) or "
                "one Arc machine (Microsoft.HybridCompute/machines) in this target's "
                "subscription, as Azure shows it: id of azure.arc.machine.list or "
                "machine_id of azure.patching.assessment.list."
            ),
        },
    },
    "required": ["machine_id"],
    "additionalProperties": False,
    "examples": [{"machine_id": _EXAMPLE_VM_ID}, {"machine_id": _EXAMPLE_ARC_ID}],
}


# ----------------------------------------------------------------------
# Arc machines
# ----------------------------------------------------------------------

_MACHINE_ROW: Final[dict[str, Any]] = {
    "name": _text("The name of the Arc machine in Azure."),
    "resource_group": _text("The resource group, read from the Azure resource ID."),
    "location": _text("The Azure region the machine is registered in, for example westeurope."),
    "id": _text("The Azure resource ID of the machine."),
    "kind": _text(
        "Where the machine runs, when Azure knows it: AVS, HCI, SCVMM, VMware, EPS, GCP or AWS."
    ),
    "status": _text(
        "The connection status of the machine's agent: Connected, Disconnected, Error or "
        "AwaitingConnection."
    ),
    "last_status_change": _text("When the status last changed (ISO 8601, UTC)."),
    "agent_version": _text("The version of the Azure Connected Machine agent on the machine."),
    "os_name": _text("The operating system, for example windows or linux."),
    "os_version": _text("The operating system version."),
    "os_type": _text("windows or linux."),
    "os_sku": _text("The product name of the operating system."),
    "provisioning_state": _text("Succeeded, Failed or another provisioning state from Azure."),
    "tags": {"type": "object", "description": "The Azure tags of the machine (name to value)."},
}

_MACHINE_SUMMARY: Final[dict[str, Any]] = _object(
    {
        "machines": _count("How many Arc machines the list holds."),
        "connected": _count("Machines with status Connected."),
        "disconnected": _count("Machines with status Disconnected."),
        "error": _count("Machines with status Error."),
        "awaiting_connection": _count("Machines with status AwaitingConnection."),
        "other_status": _count("Machines with another status or none."),
    },
    "Counts over every row read. Stays inline when the rows go behind a link.",
)

MACHINE_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    _MACHINE_ROW, "Arc machine", {"summary": _MACHINE_SUMMARY}
)

MACHINE_SHOW_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        **_MACHINE_ROW,
        "display_name": _text("The display name of the machine."),
        "computer_name": _text("The host name the operating system reports."),
        "machine_fqdn": _text("The full DNS name of the machine."),
        "domain_name": _text("The Windows domain of the machine, if any."),
        "os_edition": _text("The edition of the operating system."),
        "vm_id": _text("The unique ID of the machine in Azure Arc."),
        "assessment_mode": _text(
            "How missing updates are checked: AutomaticByPlatform (Azure checks every 24 "
            "hours) or ImageDefault (only when asked)."
        ),
        "patch_mode": _text(
            "How updates are installed: AutomaticByPlatform, AutomaticByOS, Manual or ImageDefault."
        ),
        "extension_service": _text(
            "The state of the agent's extension service (Running or Stopped). Update checks "
            "need it."
        ),
        "guest_configuration_service": _text(
            "The state of the agent's guest configuration service (Running or Stopped)."
        ),
        "errors": {
            "type": "array",
            "items": _ERROR,
            "description": "The errors Azure reports for the machine's agent, if any.",
        },
    },
    "required": ["name", "id", "status"],
}


# ----------------------------------------------------------------------
# Stored patching results
# ----------------------------------------------------------------------

_MACHINE_OF_RESULT: Final[dict[str, Any]] = {
    "machine_name": _text("The name of the VM or Arc machine."),
    "machine_id": _text(
        "The Azure resource ID of the machine; pass it to azure.patching.assessment.start."
    ),
    "machine_type": _text(
        "The Azure resource type of the machine: Microsoft.Compute/virtualMachines (an "
        "Azure VM) or Microsoft.HybridCompute/machines (an Arc machine)."
    ),
    "resource_group": _text("The resource group of the machine."),
    "location": _text("The Azure region of the machine."),
    "os_type": _text("Windows or Linux."),
}

_ASSESSMENT_ROW: Final[dict[str, Any]] = {
    **_MACHINE_OF_RESULT,
    "status": _text(
        "How the last check ended: Succeeded, Failed, CompletedWithWarnings, InProgress or Unknown."
    ),
    "assessed_at": _text("When the last check finished (ISO 8601, UTC)."),
    "started_by": _text("User (someone asked for it) or Platform (Azure's own check)."),
    "reboot_pending": _flag("True when the machine waits for a restart to finish updates."),
    "critical": _count_or_null("Missing critical updates; null when Azure reported no counts."),
    "security": _count_or_null("Missing security updates; null when Azure reported no counts."),
    "other": _count_or_null(
        "Every other missing update (definitions, rollups, tools, ...); null when unknown."
    ),
    "patch_service": _text("The update service the machine uses, for example WU, APT or YUM."),
    "error": _ERROR,
}

_ASSESSMENT_SUMMARY: Final[dict[str, Any]] = _object(
    {
        "machines": _count("How many machines the list holds."),
        "needing_critical_or_security": _count(
            "Machines with at least one missing critical or security update."
        ),
        "critical_patches": _count("Missing critical updates, added up over all machines."),
        "security_patches": _count("Missing security updates, added up over all machines."),
        "other_patches": _count("Other missing updates, added up over all machines."),
        "reboot_pending": _count("Machines that wait for a restart."),
        "failed_assessments": _count("Machines whose last check failed."),
    },
    "Counts over every row read. Stays inline when the rows go behind a link.",
)

ASSESSMENT_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    _ASSESSMENT_ROW, "machine with a stored update check", {"summary": _ASSESSMENT_SUMMARY}
)

_INSTALLATION_ROW: Final[dict[str, Any]] = {
    **_MACHINE_OF_RESULT,
    "run_id": _text("The ID of the installation run."),
    "status": _text(
        "How the run ended: Succeeded, Failed, CompletedWithWarnings, InProgress or NotStarted."
    ),
    "started_at": _text("When the run started on the machine (ISO 8601, UTC)."),
    "last_modified": _text("When Azure last updated the record of the run (ISO 8601, UTC)."),
    "started_by": _text("User (someone started it) or Platform (a schedule or Azure)."),
    "installed": _count_or_null("Updates installed by the run."),
    "failed": _count_or_null("Updates that failed to install."),
    "pending": _count_or_null("Selected updates that still wait to be installed."),
    "excluded": _count_or_null("Updates left out by an exclusion rule."),
    "not_selected": _count_or_null("Available updates the run did not select."),
    "reboot_status": _text("NotNeeded, Required, Started, Failed or Completed."),
    "maintenance_window_exceeded": _flag("True when the run did not fit in its time window."),
    "error": _ERROR,
}

_INSTALLATION_SUMMARY: Final[dict[str, Any]] = _object(
    {
        "runs": _count("How many installation runs the list holds."),
        "machines": _count("How many different machines had a run."),
        "failed_runs": _count("Runs with status Failed."),
        "installed_patches": _count("Updates installed, added up over all runs."),
        "failed_patches": _count("Updates that failed, added up over all runs."),
    },
    "Counts over every row read. Stays inline when the rows go behind a link.",
)

_WINDOW: Final[dict[str, Any]] = _object(
    {
        "days": _count("How many days back the list reaches."),
        "from": _text("The start of the time window (ISO 8601, UTC)."),
        "to": _text("The end of the time window: when MEHO asked (ISO 8601, UTC)."),
    },
    "The time window of the runs, by when Azure last updated each record.",
)

INSTALLATION_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    _INSTALLATION_ROW,
    "installation run",
    {"window": _WINDOW, "summary": _INSTALLATION_SUMMARY},
)


# ----------------------------------------------------------------------
# Patch assessment start
# ----------------------------------------------------------------------

_ASSESSMENT_RESULT: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "description": "The result of the check, when it ended; null while it still runs.",
    "properties": {
        "status": _text(
            "How the check ended: Succeeded, Failed, CompletedWithWarnings or Unknown."
        ),
        "started_at": _text("When the check started (ISO 8601, UTC)."),
        "last_modified": _text("When the check finished, when Azure says so (Arc machines)."),
        "reboot_pending": _flag("True when the machine waits for a restart."),
        "os_type": _text("Windows or Linux, when Azure says so (Arc machines)."),
        "critical": _count_or_null("Missing critical updates."),
        "security": _count_or_null("Missing security updates."),
        "other": _count_or_null("Every other missing update."),
        "critical_and_security": _count_or_null("Missing critical and security updates."),
        "by_class": {
            "type": ["object", "null"],
            "description": "Missing updates per class, with Azure's class names.",
        },
        "activity_id": _text("Azure's ID of the check, to find it in Azure's logs."),
        "error": _ERROR,
    },
}

_OPERATION: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "description": (
        "The operation reference: pass it to azure.operation.status to read the outcome "
        "later. Null when Azure answered at once."
    ),
    "properties": {
        "poll_url": {"type": "string", "description": "The URL Azure named for polling."},
        "poll_kind": {"type": "string", "description": "location or azure-asyncoperation."},
        "resource_id": _text("The machine the check runs on."),
        "started_at": _text("When MEHO started the check (ISO 8601)."),
        "retry_after_seconds": {
            "type": ["number", "null"],
            "description": "How long Azure asked to wait before the first poll.",
        },
    },
}

ASSESSMENT_START_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "machine_id": _text("The machine the check ran on."),
        "machine_type": _text(
            "Microsoft.Compute/virtualMachines or Microsoft.HybridCompute/machines."
        ),
        "status": {
            "type": "string",
            "description": (
                "The state of Azure's operation: Succeeded, Failed, Canceled, or a running "
                "state such as InProgress."
            ),
        },
        "done": {
            "type": "boolean",
            "description": "True when the operation ended; false when MEHO stopped waiting.",
        },
        "assessment": _ASSESSMENT_RESULT,
        "error": _ERROR,
        "retry_after_seconds": {
            "type": ["number", "null"],
            "description": "How long Azure asks to wait before the next poll, while it runs.",
        },
        "operation": _OPERATION,
    },
    "required": ["machine_id", "machine_type", "status", "done"],
}
