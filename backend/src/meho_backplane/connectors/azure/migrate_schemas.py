# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameter and response schemas of the Azure Migrate reads.

The operation table in :mod:`meho_backplane.connectors.azure.migrate` uses
these schemas. The dispatcher checks every call against the parameter
schema before the body runs; the names must match the same patterns as the
checks in :mod:`meho_backplane.connectors.azure.migrate_rows`, which the
bodies run again (a schema ``$`` also matches before a line end).
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.azure.migrate_rows import (
    MIGRATE_NAME_PATTERN,
    READINESS_VALUES,
    RESOURCE_GROUP_PATTERN,
    SOURCES,
)

__all__ = [
    "ASSESSED_MACHINE_LIST_RESPONSE",
    "ASSESSMENT_INLINE",
    "ASSESSMENT_LIST_RESPONSE",
    "GROUP_LIST_RESPONSE",
    "MACHINE_LIST_RESPONSE",
    "PROJECT_LIST_RESPONSE",
    "READINESS_KEYS",
    "SITE_KIND_KEYS",
    "SITE_LIST_RESPONSE",
    "params_schema",
]

_TEXT: Final[list[str]] = ["string", "null"]
_NUMBER: Final[list[str]] = ["number", "null"]

# ----------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------

_RESOURCE_GROUP: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 90,
    "pattern": RESOURCE_GROUP_PATTERN,
    "description": (
        "The resource group of the Azure Migrate project, as azure.migrate.project.list "
        "returns it (resource_group)."
    ),
}


def _name(description: str) -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": 260,
        "pattern": MIGRATE_NAME_PATTERN,
        "description": description,
    }


_PROJECT: Final[dict[str, Any]] = _name(
    "The assessment project name, as azure.migrate.project.list returns it (name)."
)
_GROUP: Final[dict[str, Any]] = _name("The group name, as azure.migrate.group.list returns it.")
_ASSESSMENT: Final[dict[str, Any]] = _name(
    "The assessment name, as azure.migrate.assessment.list returns it."
)

_EXAMPLE: Final[dict[str, str]] = {
    "resource_group": "rg-migrate-example",
    "project": "example-assessment-project",
    "group": "example-group",
    "assessment": "example-assessment",
}


def params_schema(*names: str, required: tuple[str, ...] | None = None) -> dict[str, Any]:
    """An object schema with the named parameters, all required unless *required* says."""
    known = {
        "resource_group": _RESOURCE_GROUP,
        "project": _PROJECT,
        "group": _GROUP,
        "assessment": _ASSESSMENT,
    }
    return {
        "type": "object",
        "properties": {name: known[name] for name in names},
        "required": list(names if required is None else required),
        "additionalProperties": False,
        "examples": [{name: _EXAMPLE[name] for name in names}],
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
                    what=("string", "The part that is missing, for example 'vmware sites'."),
                    reason=(
                        "string",
                        "page_limit, time_limit, row_limit, no_permission, not_found, "
                        "provider_not_registered, throttled, read_failed, "
                        "name_not_usable or group_limit.",
                    ),
                    detail=("string", "MEHO's own text: what happened and what to do."),
                ),
            },
        },
    },
}

_ORIGIN: Final[dict[str, Any]] = _fields(
    source=(
        "string",
        "Where the data came from: appliance (the Azure Migrate appliance found it), "
        "import (a CSV file) or unknown.",
    ),
    site_kind=(_TEXT, "vmware, hyperv, server or import: the kind of site."),
    site=(_TEXT, "The name of the discovery site."),
    discovery_machine_id=(_TEXT, "The resource ID of the discovered machine in its site."),
)


def _counts(keys: tuple[str, ...], description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "properties": {key: {"type": ["integer", "null"], "description": key} for key in keys},
    }


def _list_response(row: dict[str, Any], **blocks: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "description": "The rows.",
                "items": {"type": "object", "properties": row},
            },
            "truncated": {"type": "boolean", "description": "True when rows are missing."},
            "omissions": _OMISSIONS,
            **blocks,
        },
        "required": ["value", "truncated", "omissions"],
    }


_WHEN: Final[tuple[Any, str]] = (_TEXT, "A time (ISO 8601).")

_PROJECT_ROW: Final[dict[str, Any]] = _fields(
    name=(_TEXT, "The assessment project name. Pass it as project."),
    id=(_TEXT, "The resource ID of the assessment project."),
    resource_group=(_TEXT, "Its resource group. Pass it as resource_group."),
    location=(_TEXT, "The Azure region that keeps the project data."),
    status=(_TEXT, "Active or Inactive."),
    migrate_project=(_TEXT, "The Azure Migrate project name the portal shows."),
    migrate_project_id=(_TEXT, "The resource ID of that Azure Migrate project."),
    created=_WHEN,
    updated=_WHEN,
)

_SITE_ROW: Final[dict[str, Any]] = _fields(
    name=(_TEXT, "The site name."),
    id=(_TEXT, "The resource ID of the site."),
    kind=("string", "vmware, hyperv, server, import or master."),
    source=(_TEXT, "appliance, import, or null for a master site."),
    location=(_TEXT, "The Azure region of the site."),
    provisioning_state=(_TEXT, "Azure's state of the site."),
    appliance_name=(_TEXT, "The Azure Migrate appliance of an appliance site."),
    agent_version=(_TEXT, "The version of the appliance agent."),
    agent_last_heartbeat=(_TEXT, "When the appliance last reported (ISO 8601)."),
    master_site=(_TEXT, "The master site this site belongs to."),
)

_MACHINE_ROW: Final[dict[str, Any]] = _fields(
    name=(_TEXT, "Azure's name of the machine (a GUID)."),
    display_name=(_TEXT, "The machine name people see."),
    id=(_TEXT, "The resource ID of the machine in the project."),
    **{key: (value["type"], value["description"]) for key, value in _ORIGIN.items()},
    os_type=(_TEXT, "The operating system type."),
    os_name=(_TEXT, "The operating system name."),
    os_version=(_TEXT, "The operating system version."),
    cores=(_NUMBER, "CPU cores."),
    memory_mb=(_NUMBER, "Memory in MB."),
    boot_type=(_TEXT, "BIOS, EFI or another value."),
    groups=("array", "The names of the groups the machine is in."),
    created=_WHEN,
    updated=_WHEN,
)

_GROUP_ROW: Final[dict[str, Any]] = _fields(
    name=(_TEXT, "The group name. Pass it as group."),
    id=(_TEXT, "The resource ID of the group."),
    status=(_TEXT, "Created, Updated, Running, Completed or Invalid."),
    type=(_TEXT, "Default, or Import for a group of imported machines."),
    machine_count=(_NUMBER, "How many machines the group has."),
    assessments=("array", "The names of the assessments of the group."),
    assessment_types=("array", "The kinds of assessment the group supports."),
    assessments_running=(["boolean", "null"], "True while Azure computes an assessment."),
    created=_WHEN,
    updated=_WHEN,
)

_ASSESSMENT_ROW: Final[dict[str, Any]] = _fields(
    name=(_TEXT, "The assessment name. Pass it as assessment."),
    group=(_TEXT, "The group the assessment belongs to."),
    id=(_TEXT, "The resource ID of the assessment."),
    type=(_TEXT, "The kind of assessment, for example MachineAssessment."),
    status=(_TEXT, "Completed means the numbers are ready; OutOfSync or OutDated means old."),
    stage=(_TEXT, "InProgress, UnderReview or Approved."),
    sizing_criterion=(_TEXT, "PerformanceBased or AsOnPremises."),
    region=(_TEXT, "The Azure region the assessment prices for."),
    currency=(_TEXT, "The currency of every cost."),
    machine_count=(_NUMBER, "How many machines Azure assessed."),
    readiness_summary=("object", "Azure's count of machines per readiness."),
    group_type=(_TEXT, "Default, or Import for a group of imported machines."),
    monthly_compute_cost=(_NUMBER, "Monthly compute cost of all machines."),
    monthly_storage_cost=(_NUMBER, "Monthly storage cost of all machines."),
    monthly_bandwidth_cost=(_NUMBER, "Monthly bandwidth cost of all machines."),
    reserved_instance=(_TEXT, "The reserved instance term the prices use, if any."),
    hybrid_benefit=(_TEXT, "Whether the prices use Azure Hybrid Benefit."),
    comfort_factor=(_NUMBER, "The comfort factor (scaling factor) on top of the usage."),
    percentile=(_TEXT, "The percentile of usage used for performance sizing."),
    time_range=(_TEXT, "The time range of usage used for performance sizing."),
    confidence_rating_percent=(_NUMBER, "Azure's confidence in the result, in percent."),
    prices_timestamp=(_TEXT, "The date of the prices Azure used."),
    created=_WHEN,
    updated=_WHEN,
)

_ASSESSED_MACHINE_ROW: Final[dict[str, Any]] = _fields(
    name=(_TEXT, "Azure's name of the assessed machine."),
    display_name=(_TEXT, "The machine name people see."),
    id=(_TEXT, "The resource ID of the assessed machine."),
    **{key: (value["type"], value["description"]) for key, value in _ORIGIN.items()},
    readiness=(_TEXT, "Suitable, ConditionallySuitable, NotSuitable, ReadinessUnknown, Unknown."),
    readiness_explanation=(_TEXT, "Why the machine is not fully ready, if it is not."),
    readiness_detail=(_TEXT, "More about the readiness, for example a missing input."),
    recommended_size=(_TEXT, "The recommended Azure VM size."),
    monthly_compute_cost=(_NUMBER, "Monthly compute cost of the recommended size."),
    monthly_storage_cost=(_NUMBER, "Monthly storage cost."),
    monthly_bandwidth_cost=(_NUMBER, "Monthly bandwidth cost."),
    cores=(_NUMBER, "CPU cores of the machine."),
    memory_mb=(_NUMBER, "Memory of the machine in MB."),
    os_name=(_TEXT, "The operating system name."),
    cpu_utilization_percent=(_NUMBER, "CPU use in percent; null when Azure has none."),
    memory_utilization_percent=(_NUMBER, "Memory use in percent; null when Azure has none."),
    confidence_rating_percent=(_NUMBER, "Azure's confidence in the result, in percent."),
)

SITE_KIND_KEYS: Final[tuple[str, ...]] = ("vmware", "hyperv", "server", "import", "master")
READINESS_KEYS: Final[tuple[str, ...]] = (*READINESS_VALUES, "Other")
ASSESSMENT_INLINE: Final[list[str]] = [
    "name",
    "group",
    "status",
    "sizing_criterion",
    "region",
    "currency",
    "machine_count",
    "updated",
]


PROJECT_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(_PROJECT_ROW)

SITE_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    _SITE_ROW, kind_counts=_counts(SITE_KIND_KEYS, "Sites per kind.")
)

MACHINE_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    _MACHINE_ROW, source_counts=_counts(SOURCES, "Machines per source.")
)

GROUP_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(_GROUP_ROW)

ASSESSMENT_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(_ASSESSMENT_ROW)

ASSESSED_MACHINE_LIST_RESPONSE: Final[dict[str, Any]] = _list_response(
    _ASSESSED_MACHINE_ROW,
    assessment={
        "type": "object",
        "description": "The assessment, with the settings its numbers depend on.",
        "properties": _ASSESSMENT_ROW,
    },
    source_counts=_counts(SOURCES, "Machines per source."),
    readiness_counts=_counts(READINESS_KEYS, "Machines per readiness."),
)
