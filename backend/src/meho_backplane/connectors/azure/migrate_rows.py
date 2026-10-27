# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Rows of the Azure Migrate reads: checked names, row shapes and counts.

Nothing here calls Azure. :mod:`meho_backplane.connectors.azure.migrate_reads`
reads the lists and uses these functions to turn Azure's answers into rows.
The later Azure Migrate writes reuse the same names and rows.

Results from a CSV import and from the appliance stay apart. Every machine
row has ``source``: ``appliance``, ``import`` or ``unknown``. It comes from
the kind of site the machine was discovered in (:func:`machine_origin`),
never from a guess.

Every name that goes into a path (resource group, project, group,
assessment) is checked first, so a name can never change the path.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Final

__all__ = [
    "MIGRATE_NAME_PATTERN",
    "READINESS_VALUES",
    "RESOURCE_GROUP_PATTERN",
    "SITE_SOURCES",
    "SOURCES",
    "assessed_machine_row",
    "assessment_row",
    "group_row",
    "is_migrate_name",
    "machine_origin",
    "machine_row",
    "migrate_name_param",
    "project_params",
    "project_row",
    "readiness_counts",
    "resource_group_of",
    "resource_group_param",
    "site_row",
    "sort_by_source_and_name",
    "source_counts",
]

#: A resource group name as Azure allows it: letters, digits, ``_``, ``-``,
#: ``.``, ``(`` and ``)``, 1 to 90 characters, not ending with ``.``.
RESOURCE_GROUP_PATTERN: Final[str] = r"^[-\w.()]{0,89}[-\w()]$"

#: An Azure Migrate name (project, group, assessment): Azure's own rule, plus
#: no ``%``, ``\``, control character, and not ``.`` or ``..``. So the name
#: always stays one part of the path.
MIGRATE_NAME_PATTERN: Final[str] = r"^(?!\.\.?$)[^<>&:?/#%\\\x00-\x1f\x7f]{1,260}$"

_RESOURCE_GROUP_RE: Final[re.Pattern[str]] = re.compile(RESOURCE_GROUP_PATTERN)
_MIGRATE_NAME_RE: Final[re.Pattern[str]] = re.compile(MIGRATE_NAME_PATTERN)

#: Where the data of a machine came from.
SOURCES: Final[tuple[str, ...]] = ("appliance", "import", "unknown")

#: Azure's readiness values of an assessed machine (``suitability``).
READINESS_VALUES: Final[tuple[str, ...]] = (
    "Suitable",
    "ConditionallySuitable",
    "NotSuitable",
    "ReadinessUnknown",
    "Unknown",
)

#: The five site kinds and the source of the machines in them. A master site
#: holds no machines; it links the other sites of a project.
SITE_SOURCES: Final[Mapping[str, str | None]] = {
    "vmware": "appliance",
    "hyperv": "appliance",
    "server": "appliance",
    "import": "import",
    "master": None,
}

#: Site types in the ID of a discovered machine (lower case), with their kind.
_MACHINE_SITE_TYPES: Final[Mapping[str, str]] = {
    "vmwaresites": "vmware",
    "hypervsites": "hyperv",
    "serversites": "server",
    "importsites": "import",
}


# ----------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------


def _checked(value: object, *, field: str, pattern: re.Pattern[str], rule: str) -> str:
    """Return *value* when the whole of it matches *pattern*, else fail naming *field*.

    ``fullmatch`` is used on purpose: a ``$`` in a pattern also matches
    before a line end, so a schema check alone would let ``"name\\n"`` pass.
    """
    if isinstance(value, str) and pattern.fullmatch(value):
        return value
    raise ValueError(f"{field} must be {rule}.")


def resource_group_param(params: Mapping[str, Any]) -> str:
    """The checked ``resource_group`` parameter."""
    return _checked(
        params.get("resource_group"),
        field="resource_group",
        pattern=_RESOURCE_GROUP_RE,
        rule="a resource group name (letters, digits, _ - . ( ), not ending with a dot)",
    )


def is_migrate_name(value: object) -> bool:
    """True when *value* may go into a path as a project, group or assessment name."""
    return isinstance(value, str) and _MIGRATE_NAME_RE.fullmatch(value) is not None


def migrate_name_param(params: Mapping[str, Any], key: str) -> str:
    """The checked Azure Migrate name in ``params[key]`` (project, group or assessment)."""
    return _checked(
        params.get(key),
        field=key,
        pattern=_MIGRATE_NAME_RE,
        rule="an Azure Migrate name without < > & : ? / # % \\ or control characters",
    )


def project_params(params: Mapping[str, Any]) -> dict[str, str]:
    """The checked ``resource_group`` and ``project``, as path values."""
    return {
        "resource_group": resource_group_param(params),
        "project": migrate_name_param(params, "project"),
    }


# ----------------------------------------------------------------------
# Values from Azure
# ----------------------------------------------------------------------


def _props(item: Mapping[str, Any]) -> Mapping[str, Any]:
    props = item.get("properties")
    return props if isinstance(props, Mapping) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def _flag(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _texts(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _provider_pairs(resource_id: object, provider: str) -> list[tuple[str, str, int]]:
    """The ``(type, name, index of the name)`` pairs after ``/providers/<provider>/``.

    An Azure resource ID goes on in pairs after its provider, for example
    ``.../providers/Microsoft.Migrate/assessmentProjects/<p>/groups/<g>``.
    The type is in lower case. Reading by position means a resource group or
    a project that happens to be called like a resource type cannot confuse it.
    """
    parts = resource_id.split("/") if isinstance(resource_id, str) else []
    for at in range(len(parts) - 1):
        if parts[at].lower() == "providers" and parts[at + 1].lower() == provider:
            return [
                (parts[i].lower(), parts[i + 1], i + 1)
                for i in range(at + 2, len(parts) - 1, 2)
                if parts[i + 1]
            ]
    return []


def _name_in_id(resource_id: object, provider: str, resource_type: str) -> str | None:
    """The name of the first *resource_type* after *provider* in a resource ID, or ``None``."""
    for kind, name, _at in _provider_pairs(resource_id, provider):
        if kind == resource_type:
            return name
    return None


def _id_up_to(resource_id: object, provider: str, resource_type: str) -> str | None:
    """The resource ID up to the name of the first *resource_type*, or ``None``."""
    for kind, _name, at in _provider_pairs(resource_id, provider):
        if kind == resource_type and isinstance(resource_id, str):
            return "/".join(resource_id.split("/")[: at + 1])
    return None


def resource_group_of(resource_id: object) -> str | None:
    """The resource group in ``/subscriptions/<s>/resourceGroups/<rg>/...``, or ``None``."""
    parts = resource_id.split("/") if isinstance(resource_id, str) else []
    if len(parts) < 5 or parts[1].lower() != "subscriptions":
        return None
    return (parts[4] or None) if parts[3].lower() == "resourcegroups" else None


def _names(resource_ids: object) -> list[str]:
    """The last part of each resource ID (the name of a group or an assessment)."""
    names = [resource_id.rstrip("/").rsplit("/", 1)[-1] for resource_id in _texts(resource_ids)]
    return [name for name in names if name]


def machine_origin(discovery_id: object) -> dict[str, str | None]:
    """Where the data of a machine came from, read from the ID of its discovered machine.

    The ID looks like
    ``.../providers/Microsoft.OffAzure/<site type>/<site>/machines/<machine>``.
    A VMware, Hyper-V or physical server site means the Azure Migrate
    appliance found the machine (``appliance``). An import site means a CSV
    import (``import``). Any other ID gives ``unknown``, never a guess.
    """
    pairs = _provider_pairs(discovery_id, "microsoft.offazure")
    kind = _MACHINE_SITE_TYPES.get(pairs[0][0]) if pairs else None
    if kind is None:
        return {"source": "unknown", "site_kind": None, "site": None}
    return {"source": SITE_SOURCES[kind], "site_kind": kind, "site": pairs[0][1]}


# ----------------------------------------------------------------------
# Rows
# ----------------------------------------------------------------------


def project_row(item: Mapping[str, Any]) -> dict[str, Any]:
    """One assessment project. ``migrate_project`` is the project name the portal shows."""
    props = _props(item)
    solution = props.get("assessmentSolutionId")
    return {
        "name": _text(item.get("name")),
        "id": _text(item.get("id")),
        "resource_group": resource_group_of(item.get("id")),
        "location": _text(item.get("location")),
        "status": _text(props.get("projectStatus")),
        "migrate_project": _name_in_id(solution, "microsoft.migrate", "migrateprojects"),
        "migrate_project_id": _id_up_to(solution, "microsoft.migrate", "migrateprojects"),
        "created": _text(props.get("createdTimestamp")),
        "updated": _text(props.get("updatedTimestamp")),
    }


def site_row(item: Mapping[str, Any], kind: str) -> dict[str, Any]:
    """One discovery site of *kind*. The appliance fields are ``None`` for other kinds."""
    props = _props(item)
    agent = props.get("agentDetails")
    agent = agent if isinstance(agent, Mapping) else {}
    return {
        "name": _text(item.get("name")),
        "id": _text(item.get("id")),
        "kind": kind,
        "source": SITE_SOURCES.get(kind),
        "location": _text(item.get("location")),
        "provisioning_state": _text(props.get("provisioningState")),
        "appliance_name": _text(props.get("applianceName")),
        "agent_version": _text(agent.get("version")),
        "agent_last_heartbeat": _text(agent.get("lastHeartBeatUtc")),
        "master_site": _name_in_id(props.get("masterSiteId"), "microsoft.offazure", "mastersites"),
    }


def machine_row(item: Mapping[str, Any]) -> dict[str, Any]:
    """One machine of an assessment project, with the source of its data.

    ``id`` is the machine's resource ID in the project, the value a group
    change names. ``name`` is Azure's name for it (a GUID); ``display_name``
    is the name people see.
    """
    props = _props(item)
    discovery_id = _text(props.get("discoveryMachineArmId"))
    return {
        "name": _text(item.get("name")),
        "display_name": _text(props.get("displayName")),
        "id": _text(item.get("id")),
        **machine_origin(discovery_id),
        "discovery_machine_id": discovery_id,
        "os_type": _text(props.get("operatingSystemType")),
        "os_name": _text(props.get("operatingSystemName")),
        "os_version": _text(props.get("operatingSystemVersion")),
        "cores": _number(props.get("numberOfCores")),
        "memory_mb": _number(props.get("megabytesOfMemory")),
        "boot_type": _text(props.get("bootType")),
        "groups": _names(props.get("groups")),
        "created": _text(props.get("createdTimestamp")),
        "updated": _text(props.get("updatedTimestamp")),
    }


def group_row(item: Mapping[str, Any]) -> dict[str, Any]:
    """One group of machines. ``type`` is ``Import`` for a group of imported machines."""
    props = _props(item)
    return {
        "name": _text(item.get("name")),
        "id": _text(item.get("id")),
        "status": _text(props.get("groupStatus")),
        "type": _text(props.get("groupType")),
        "machine_count": _number(props.get("machineCount")),
        "assessments": _names(props.get("assessments")),
        "assessment_types": _texts(props.get("supportedAssessmentTypes")),
        "assessments_running": _flag(props.get("areAssessmentsRunning")),
        "created": _text(props.get("createdTimestamp")),
        "updated": _text(props.get("updatedTimestamp")),
    }


def _readiness_summary(value: object) -> dict[str, int | float]:
    if not isinstance(value, Mapping):
        return {}
    counts = {str(key): _number(count) for key, count in value.items()}
    return {key: count for key, count in counts.items() if count is not None}


def assessment_row(item: Mapping[str, Any], *, group: str | None = None) -> dict[str, Any]:
    """One Azure VM assessment, with the settings its numbers depend on.

    ``readiness_summary`` is Azure's own count of machines per readiness.
    The costs are monthly totals in ``currency``.
    """
    props = _props(item)
    return {
        "name": _text(item.get("name")),
        "group": group or _name_in_id(item.get("id"), "microsoft.migrate", "groups"),
        "id": _text(item.get("id")),
        "type": _text(props.get("assessmentType")),
        "status": _text(props.get("status")),
        "stage": _text(props.get("stage")),
        "sizing_criterion": _text(props.get("sizingCriterion")),
        "region": _text(props.get("azureLocation")),
        "currency": _text(props.get("currency")),
        "machine_count": _number(props.get("numberOfMachines")),
        "readiness_summary": _readiness_summary(props.get("suitabilitySummary")),
        "group_type": _text(props.get("groupType")),
        "monthly_compute_cost": _number(props.get("monthlyComputeCost")),
        "monthly_storage_cost": _number(props.get("monthlyStorageCost")),
        "monthly_bandwidth_cost": _number(props.get("monthlyBandwidthCost")),
        "reserved_instance": _text(props.get("reservedInstance")),
        "hybrid_benefit": _text(props.get("azureHybridUseBenefit")),
        "comfort_factor": _number(props.get("scalingFactor")),
        "percentile": _text(props.get("percentile")),
        "time_range": _text(props.get("timeRange")),
        "confidence_rating_percent": _number(props.get("confidenceRatingInPercentage")),
        "prices_timestamp": _text(props.get("pricesTimestamp")),
        "created": _text(props.get("createdTimestamp")),
        "updated": _text(props.get("updatedTimestamp")),
    }


def assessed_machine_row(item: Mapping[str, Any]) -> dict[str, Any]:
    """One machine of an assessment: readiness, recommended size and monthly costs.

    A utilization value is ``None`` when Azure has none; it is never made 0.
    """
    props = _props(item)
    discovery_id = _text(props.get("datacenterMachineArmId"))
    return {
        "name": _text(item.get("name")),
        "display_name": _text(props.get("displayName")),
        "id": _text(item.get("id")),
        **machine_origin(discovery_id),
        "discovery_machine_id": discovery_id,
        "readiness": _text(props.get("suitability")),
        "readiness_explanation": _text(props.get("suitabilityExplanation")),
        "readiness_detail": _text(props.get("suitabilityDetail")),
        "recommended_size": _text(props.get("recommendedSize")),
        "monthly_compute_cost": _number(props.get("monthlyComputeCostForRecommendedSize")),
        "monthly_storage_cost": _number(props.get("monthlyStorageCost")),
        "monthly_bandwidth_cost": _number(props.get("monthlyBandwidthCost")),
        "cores": _number(props.get("numberOfCores")),
        "memory_mb": _number(props.get("megabytesOfMemory")),
        "os_name": _text(props.get("operatingSystemName")),
        "cpu_utilization_percent": _number(props.get("percentageCoresUtilization")),
        "memory_utilization_percent": _number(props.get("percentageMemoryUtilization")),
        "confidence_rating_percent": _number(props.get("confidenceRatingInPercentage")),
    }


# ----------------------------------------------------------------------
# Order and counts
# ----------------------------------------------------------------------


def _source_and_name(row: Mapping[str, Any]) -> tuple[int, str]:
    name = row.get("display_name") or row.get("name") or ""
    return SOURCES.index(row["source"]), str(name).lower()


def sort_by_source_and_name(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Appliance machines first, then imported ones, then unknown; each by name."""
    return sorted(rows, key=_source_and_name)


def source_counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """How many of *rows* have each ``source``."""
    counts = dict.fromkeys(SOURCES, 0)
    for row in rows:
        counts[row["source"]] += 1
    return counts


def readiness_counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """How many of *rows* have each readiness. A missing value counts as ``Unknown``."""
    counts = dict.fromkeys((*READINESS_VALUES, "Other"), 0)
    for row in rows:
        value = row.get("readiness") or "Unknown"
        counts[value if value in READINESS_VALUES else "Other"] += 1
    return counts
