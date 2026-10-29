# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Rows and summaries of the Azure Arc and patching operations.

Every row is built from a fixed list of fields, so nothing else from Azure's
answer is passed on. An Arc machine's answer also holds the agent's settings
(for example its proxy URL), a public key and a TPM certificate; none of them
reaches a result. Error texts from Azure are filtered with
:func:`~meho_backplane.connectors.azure._errors.clean_azure_text`.

A value of the wrong type becomes ``None`` (``null``), never a guess: a
missing count is unknown, not zero.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any, Final

from meho_backplane.connectors.azure._errors import clean_azure_text

__all__ = [
    "Row",
    "assessment_result",
    "assessment_row",
    "assessment_summary",
    "class_counts",
    "error_of",
    "installation_row",
    "installation_summary",
    "machine_detail",
    "machine_of_result",
    "machine_row",
    "machine_summary",
]

Row = dict[str, Any]

#: The path part that follows the machine in the ID of a stored patching result,
#: for example ``.../virtualMachines/vm-01/patchAssessmentResults/latest``.
_RESULT_PARTS: Final[frozenset[str]] = frozenset(
    {"patchassessmentresults", "patchinstallationresults"}
)

#: The connection statuses the machine summary counts, by the summary field.
_STATUS_FIELDS: Final[dict[str, str]] = {
    "connected": "connected",
    "disconnected": "disconnected",
    "error": "error",
    "awaitingconnection": "awaiting_connection",
}


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _count(value: object) -> int | None:
    """A count from Azure: a whole number from 0 up, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _flag(value: object) -> bool | None:
    """A yes or no from Azure. Resource Graph may send it as the text True or False."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return None


def _is(value: object, status: str) -> bool:
    return isinstance(value, str) and value.lower() == status


def _total(rows: Iterable[Row], field: str) -> int:
    """The sum of a count field over *rows*; an unknown count adds nothing."""
    return sum(row.get(field) or 0 for row in rows)


def error_of(value: object) -> dict[str, str | None] | None:
    """Azure's code and message of one error, or of the first error of a list.

    Both are text from Azure, so both are filtered and cut like Azure's error
    text. ``None`` when there is no error.
    """
    if isinstance(value, list):
        value = next((item for item in value if isinstance(item, Mapping)), None)
    if not isinstance(value, Mapping):
        return None
    code = clean_azure_text(value.get("code"))
    message = clean_azure_text(value.get("message"))
    if code is None and message is None:
        return None
    return {"code": code, "message": message}


def _resource_group_of(resource_id: str | None) -> str | None:
    parts = resource_id.split("/") if resource_id else []
    if len(parts) > 4 and parts[3].lower() == "resourcegroups" and parts[4]:
        return parts[4]
    return None


def _tags(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {name: text for name, text in value.items() if isinstance(text, str)}


# ----------------------------------------------------------------------
# Arc machines
# ----------------------------------------------------------------------


def machine_row(item: Mapping[str, Any]) -> Row:
    """One row of ``azure.arc.machine.list`` from one Azure ``Machine``."""
    props = _mapping(item.get("properties"))
    resource_id = _text(item.get("id"))
    return {
        "name": _text(item.get("name")),
        "resource_group": _resource_group_of(resource_id),
        "location": _text(item.get("location")),
        "id": resource_id,
        "kind": _text(item.get("kind")),
        "status": _text(props.get("status")),
        "last_status_change": _text(props.get("lastStatusChange")),
        "agent_version": _text(props.get("agentVersion")),
        "os_name": _text(props.get("osName")),
        "os_version": _text(props.get("osVersion")),
        "os_type": _text(props.get("osType")),
        "os_sku": _text(props.get("osSku")),
        "provisioning_state": _text(props.get("provisioningState")),
        "tags": _tags(item.get("tags")),
    }


def _service_status(services: Mapping[str, Any], name: str) -> str | None:
    return _text(_mapping(services.get(name)).get("status"))


def machine_detail(item: Mapping[str, Any]) -> Row:
    """The answer of ``azure.arc.machine.show``: the row plus the details of one machine."""
    props = _mapping(item.get("properties"))
    profile = _mapping(props.get("osProfile"))
    config_key = (
        "linuxConfiguration" if _is(props.get("osType"), "linux") else "windowsConfiguration"
    )
    settings = _mapping(_mapping(profile.get(config_key)).get("patchSettings"))
    services = _mapping(props.get("serviceStatuses"))
    errors = props.get("errorDetails")
    return {
        **machine_row(item),
        "display_name": _text(props.get("displayName")),
        "computer_name": _text(profile.get("computerName")),
        "machine_fqdn": _text(props.get("machineFqdn")),
        "domain_name": _text(props.get("domainName")),
        "os_edition": _text(props.get("osEdition")),
        "vm_id": _text(props.get("vmId")),
        "assessment_mode": _text(settings.get("assessmentMode")),
        "patch_mode": _text(settings.get("patchMode")),
        "extension_service": _service_status(services, "extensionService"),
        "guest_configuration_service": _service_status(services, "guestConfigurationService"),
        "errors": [
            error
            for error in map(error_of, errors if isinstance(errors, list) else [])
            if error is not None
        ],
    }


def machine_summary(rows: list[Row]) -> Row:
    """Counts by connection status over every machine row."""
    statuses = Counter(str(row.get("status") or "").lower() for row in rows)
    summary: Row = {"machines": len(rows)}
    for status, field in _STATUS_FIELDS.items():
        summary[field] = statuses[status]
    summary["other_status"] = len(rows) - sum(statuses[status] for status in _STATUS_FIELDS)
    return summary


# ----------------------------------------------------------------------
# Stored patching results (Resource Graph)
# ----------------------------------------------------------------------


def machine_of_result(item: Mapping[str, Any], subscription_id: str) -> Row | None:
    """The machine fields of one stored patching result, read from its ID.

    The ID is the machine's ID plus ``/patchAssessmentResults/...`` or
    ``/patchInstallationResults/...``. ``None`` when the ID is not shaped like
    that or names another subscription than *subscription_id* (lower-case):
    such a row is left out, so a result of another subscription never reaches
    the caller.
    """
    row_id = item.get("id")
    parts = row_id.split("/") if isinstance(row_id, str) else []
    lowered = [part.lower() for part in parts]
    end = next((index for index, part in enumerate(lowered) if part in _RESULT_PARTS), 0)
    inside = (
        end >= 9
        and parts[0] == ""
        and lowered[1] == "subscriptions"
        and lowered[2] == subscription_id
        and lowered[3] == "resourcegroups"
        and lowered[5] == "providers"
    )
    if not inside:
        return None
    return {
        "machine_name": parts[8],
        "machine_id": "/".join(parts[:end]),
        "machine_type": f"{parts[6]}/{parts[7]}",
        "resource_group": parts[4],
        "location": _text(item.get("location")),
    }


def _class_numbers(by_class: Mapping[str, Any]) -> dict[str, int]:
    """Azure's count per class, without values that are not counts."""
    numbers: dict[str, int] = {}
    for name, value in by_class.items():
        number = _count(value)
        if isinstance(name, str) and number is not None:
            numbers[name] = number
    return numbers


def _split(numbers: Mapping[str, int]) -> tuple[int, int, int]:
    """Critical, security, and every other class added up."""
    critical = sum(number for name, number in numbers.items() if _is(name, "critical"))
    security = sum(number for name, number in numbers.items() if _is(name, "security"))
    return critical, security, sum(numbers.values()) - critical - security


def class_counts(by_class: object) -> dict[str, int | None]:
    """``critical``, ``security`` and ``other`` from Azure's counts per class.

    Azure counts missing updates per class (``availablePatchCountByClassification``:
    critical, security, definition, updateRollup, ..., other). ``other`` adds
    up every class but critical and security. All three are ``None`` when
    Azure sent no counts.
    """
    if not isinstance(by_class, Mapping):
        return {"critical": None, "security": None, "other": None}
    critical, security, other = _split(_class_numbers(by_class))
    return {"critical": critical, "security": security, "other": other}


def assessment_row(item: Mapping[str, Any], subscription_id: str) -> Row | None:
    """One row of ``azure.patching.assessment.list``, or ``None`` for a row to leave out."""
    machine = machine_of_result(item, subscription_id)
    if machine is None:
        return None
    props = _mapping(item.get("properties"))
    return {
        **machine,
        "os_type": _text(props.get("osType")),
        "status": _text(props.get("status")),
        "assessed_at": _text(props.get("lastModifiedDateTime")),
        "started_by": _text(props.get("startedBy")),
        "reboot_pending": _flag(props.get("rebootPending")),
        **class_counts(props.get("availablePatchCountByClassification")),
        "patch_service": _text(props.get("patchServiceUsed")),
        "error": error_of(props.get("errorDetails")),
    }


def assessment_summary(rows: list[Row]) -> Row:
    """Machine counts and added-up update counts over every assessment row."""
    return {
        "machines": len(rows),
        "needing_critical_or_security": sum(
            1 for row in rows if (row.get("critical") or 0) + (row.get("security") or 0) > 0
        ),
        "critical_patches": _total(rows, "critical"),
        "security_patches": _total(rows, "security"),
        "other_patches": _total(rows, "other"),
        "reboot_pending": sum(1 for row in rows if row.get("reboot_pending") is True),
        "failed_assessments": sum(1 for row in rows if _is(row.get("status"), "failed")),
    }


def installation_row(item: Mapping[str, Any], subscription_id: str) -> Row | None:
    """One row of ``azure.patching.installation.list``, or ``None`` for a row to leave out."""
    machine = machine_of_result(item, subscription_id)
    if machine is None:
        return None
    props = _mapping(item.get("properties"))
    return {
        **machine,
        "os_type": _text(props.get("osType")),
        "run_id": _text(props.get("installationActivityId")) or _text(item.get("name")),
        "status": _text(props.get("status")),
        "started_at": _text(props.get("startDateTime")),
        "last_modified": _text(props.get("lastModifiedDateTime")),
        "started_by": _text(props.get("startedBy")),
        "installed": _count(props.get("installedPatchCount")),
        "failed": _count(props.get("failedPatchCount")),
        "pending": _count(props.get("pendingPatchCount")),
        "excluded": _count(props.get("excludedPatchCount")),
        "not_selected": _count(props.get("notSelectedPatchCount")),
        "reboot_status": _text(props.get("rebootStatus")),
        "maintenance_window_exceeded": _flag(props.get("maintenanceWindowExceeded")),
        "error": error_of(props.get("errorDetails")),
    }


def installation_summary(rows: list[Row]) -> Row:
    """Run, machine and update counts over every installation row."""
    return {
        "runs": len(rows),
        "machines": len({row.get("machine_id") for row in rows}),
        "failed_runs": sum(1 for row in rows if _is(row.get("status"), "failed")),
        "installed_patches": _total(rows, "installed"),
        "failed_patches": _total(rows, "failed"),
    }


# ----------------------------------------------------------------------
# The result of one patch assessment (assessPatches)
# ----------------------------------------------------------------------


def _patch_classes(patches: object) -> dict[str, int] | None:
    """How many of the patches Azure lists have each class, or ``None`` without a list."""
    if not isinstance(patches, list):
        return None
    classes: Counter[str] = Counter()
    for patch in patches:
        names = _mapping(patch).get("classifications")
        classes.update(name for name in (names if isinstance(names, list) else []) if _text(name))
    return dict(classes)


def _arc_counts(by_class: Mapping[str, Any]) -> Row:
    """The counts of an Arc machine's answer: Azure counts per class itself."""
    numbers = _class_numbers(by_class)
    critical, security, other = _split(numbers)
    return {
        "critical": critical,
        "security": security,
        "other": other,
        "critical_and_security": critical + security,
        "by_class": numbers,
    }


def _vm_counts(answer: Mapping[str, Any]) -> Row:
    """The counts of an Azure VM's answer.

    Azure gives two totals (critical and security together, and the rest) and
    a list of the missing updates with their classes. ``critical``,
    ``security`` and ``by_class`` are counted from that list.
    """
    by_class = _patch_classes(answer.get("availablePatches"))
    critical: int | None = None
    security: int | None = None
    if by_class is not None:
        critical, security, _rest = _split(by_class)
    return {
        "critical": critical,
        "security": security,
        "other": _count(answer.get("otherPatchCount")),
        "critical_and_security": _count(answer.get("criticalAndSecurityPatchCount")),
        "by_class": by_class,
    }


def assessment_result(body: object) -> Row:
    """The result of ``assessPatches``, in one shape for an Azure VM and an Arc machine.

    *body* is what Azure returned when the check ended: the answer of the
    ``Location`` URL, or the answer of the call itself. An
    ``Azure-AsyncOperation`` answer keeps it under ``output``.
    """
    answer = _mapping(body)
    output = answer.get("output")
    if isinstance(output, Mapping):
        answer = output
    by_class = answer.get("availablePatchCountByClassification")
    counts = _arc_counts(by_class) if isinstance(by_class, Mapping) else _vm_counts(answer)
    return {
        "status": _text(answer.get("status")),
        "started_at": _text(answer.get("startDateTime")),
        "last_modified": _text(answer.get("lastModifiedDateTime")),
        "reboot_pending": _flag(answer.get("rebootPending")),
        "os_type": _text(answer.get("osType")),
        **counts,
        "activity_id": _text(answer.get("assessmentActivityId")),
        "error": error_of(answer.get("errorDetails") or answer.get("error")),
    }
