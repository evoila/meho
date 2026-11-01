# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pure helpers of the Azure security and governance reads.

Two kinds of functions, none of them calls Azure:

* **Rows.** Each ``*_row`` function turns one item of an Azure answer into
  one row of a MEHO answer. An item that is not an object is left out by
  :func:`object_rows`. Every field is read with a type check, so an odd
  answer gives ``None`` in a field, never an error.
* **Parameter checks.** :func:`role_scope`, :func:`resource_group_name` and
  :func:`activity_window` check a caller's value before it goes into a
  request. A value that is not usable raises :class:`AzureParameterError`
  before any request is made.

Text from Azure (names, alert and recommendation texts, callers) is copied
as data. It is never used to build a request.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from meho_backplane.connectors.azure._paging import PagedRows
from meho_backplane.connectors.azure.session import AzureTargetConfig

__all__ = [
    "ACTIVITY_DEFAULT_HOURS",
    "ACTIVITY_MAX_DAYS",
    "ActivityWindow",
    "AzureParameterError",
    "activity_row",
    "activity_window",
    "alert_row",
    "assessment_row",
    "control_row",
    "iso_time",
    "list_answer",
    "object_rows",
    "parse_time",
    "plan_row",
    "policy_assignment_row",
    "policy_summary",
    "resource_group_name",
    "role_assignment_row",
    "role_names_by_id",
    "role_scope",
    "secure_score_row",
    "severity_by_name",
    "subscription_scope",
]

#: How far back the activity log goes. Azure keeps 90 days of events.
ACTIVITY_MAX_DAYS: Final[int] = 90

#: The window of ``azure.activity.list`` when the caller gives no ``from``.
ACTIVITY_DEFAULT_HOURS: Final[int] = 24

#: One part of a scope or a resource group name: letters and digits (any
#: script), ``_``, ``-``, ``.``, ``(`` and ``)``. No quote, slash, ``?``,
#: ``#``, ``%`` or space can pass, so a value changes neither a path nor a
#: filter.
_NAME_PART: Final[re.Pattern[str]] = re.compile(r"[\w.()\-]+")

#: Longest resource group name Azure allows.
_RESOURCE_GROUP_MAX: Final[int] = 90

#: The part of an assessment id that follows the id of the assessed resource.
_ASSESSMENT_MARK: Final[str] = "/providers/microsoft.security/assessments/"


class AzureParameterError(ValueError):
    """A parameter of an Azure read is not usable. Raised before any request.

    The message names the target and the parameter, never the value. The
    dispatcher returns it as ``connector_error`` with ``remediation``.
    """

    def __init__(self, message: str, *, remediation: str) -> None:
        super().__init__(message)
        self.remediation = remediation


# ----------------------------------------------------------------------
# Reading Azure's answers
# ----------------------------------------------------------------------


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def _properties(item: Mapping[str, Any]) -> dict[str, Any]:
    properties = item.get("properties")
    return properties if isinstance(properties, dict) else {}


def _last_part(resource_id: str | None) -> str | None:
    """The last part of an Azure id: the name or GUID after the last slash."""
    if not resource_id:
        return None
    return resource_id.rstrip("/").rsplit("/", 1)[-1] or None


def _localized(value: object) -> str | None:
    """The fixed ``value`` of Azure's ``{value, localizedValue}`` pair, else the text."""
    if isinstance(value, dict):
        return _text(value.get("value")) or _text(value.get("localizedValue"))
    return _text(value)


def iso_time(moment: datetime) -> str:
    """*moment* in UTC as ``YYYY-MM-DDTHH:MM:SSZ``, the form Azure's filters take."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: object) -> datetime | None:
    """An ISO 8601 time as a UTC time; ``None`` when it is not one.

    A time without an offset is read as UTC. A time that does not fit in
    UTC (the first or last day of the calendar with an offset) is not one.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def subscription_scope(config: AzureTargetConfig) -> str:
    """The ``{scope}`` value of the whole subscription: ``subscriptions/<id>``."""
    return f"subscriptions/{config.subscription_id}"


def object_rows(
    items: list[Any], to_row: Callable[[dict[str, Any]], dict[str, Any]]
) -> list[dict[str, Any]]:
    """Turn every object in *items* into a row with *to_row*; leave out the rest."""
    return [to_row(item) for item in items if isinstance(item, dict)]


def list_answer(paged: PagedRows, rows: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """The answer of a list read.

    ``value`` holds *rows* and ``count`` their number; then come the paging
    fields of :meth:`PagedRows.as_dict` (they describe what MEHO read from
    Azure, before any filter) and *extra*.
    """
    paging = {key: value for key, value in paged.as_dict().items() if key != "value"}
    return {"value": rows, "count": len(rows), **paging, **extra}


# ----------------------------------------------------------------------
# Defender for Cloud rows
# ----------------------------------------------------------------------


def _score_fields(score: object) -> dict[str, Any]:
    """``score``, ``max_score`` and ``percentage`` (0 to 100) from Azure's score object.

    Azure gives the percentage as a ratio from 0 to 1 (0.6033 is 60.33 %).
    """
    data = score if isinstance(score, dict) else {}
    ratio = _number(data.get("percentage"))
    return {
        "score": _number(data.get("current")),
        "max_score": _number(data.get("max")),
        "percentage": round(ratio * 100, 2) if ratio is not None else None,
    }


def secure_score_row(item: dict[str, Any]) -> dict[str, Any]:
    """One row of ``azure.security.score.list`` from one ``SecureScoreItem``."""
    properties = _properties(item)
    return {
        "name": _text(item.get("name")),
        "display_name": _text(properties.get("displayName")),
        **_score_fields(properties.get("score")),
    }


def control_row(item: dict[str, Any]) -> dict[str, Any]:
    """One row of ``azure.security.control.list`` from one control with its definition."""
    properties = _properties(item)
    definition = properties.get("definition")
    links = (
        _properties(definition).get("assessmentDefinitions")
        if isinstance(definition, dict)
        else None
    )
    names = [
        name
        for link in (links if isinstance(links, list) else [])
        if isinstance(link, dict) and (name := _last_part(_text(link.get("id"))))
    ]
    return {
        "name": _text(item.get("name")),
        "display_name": _text(properties.get("displayName")),
        **_score_fields(properties.get("score")),
        "healthy_resource_count": _int(properties.get("healthyResourceCount")),
        "unhealthy_resource_count": _int(properties.get("unhealthyResourceCount")),
        "not_applicable_resource_count": _int(properties.get("notApplicableResourceCount")),
        "recommendation_names": names,
    }


def _assessed_resource_id(item: dict[str, Any], properties: dict[str, Any]) -> str | None:
    """The id of the resource an assessment is about.

    Read from ``resourceDetails.id``, with the key read without regard to
    case (Azure's own data sometimes writes ``Id``). When that is missing,
    it is the assessment id without its end
    (``<resource id>/providers/Microsoft.Security/assessments/<name>``).
    """
    details = properties.get("resourceDetails")
    if isinstance(details, dict):
        for key, value in details.items():
            if isinstance(key, str) and key.lower() == "id" and isinstance(value, str) and value:
                return value
    assessment_id = _text(item.get("id"))
    if assessment_id:
        cut = assessment_id.lower().rfind(_ASSESSMENT_MARK)
        if cut > 0:
            return assessment_id[:cut]
    return None


def assessment_row(item: dict[str, Any]) -> dict[str, Any]:
    """One row of ``azure.security.recommendation.list`` from one assessment.

    ``severity`` is ``None`` when the assessment carries no metadata; the
    caller fills it from :func:`severity_by_name`.
    """
    properties = _properties(item)
    status = properties.get("status")
    metadata = properties.get("metadata")
    meta: dict[str, Any] = metadata if isinstance(metadata, dict) else {}
    return {
        "name": _text(item.get("name")),
        "display_name": _text(properties.get("displayName")) or _text(meta.get("displayName")),
        "severity": _text(meta.get("severity")),
        "status": _text(status.get("code")) if isinstance(status, dict) else None,
        "resource_id": _assessed_resource_id(item, properties),
    }


def severity_by_name(items: list[Any]) -> dict[str, str]:
    """The severity of every recommendation type, by its lower-case name.

    *items* are Azure's assessment metadata entries.
    """
    severities: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"))
        severity = _text(_properties(item).get("severity"))
        if name and severity:
            severities[name.lower()] = severity
    return severities


def _alert_resource_id(properties: dict[str, Any]) -> str | None:
    identifiers = properties.get("resourceIdentifiers")
    for identifier in identifiers if isinstance(identifiers, list) else []:
        if isinstance(identifier, dict) and identifier.get("type") == "AzureResource":
            resource_id = _text(identifier.get("azureResourceId"))
            if resource_id:
                return resource_id
    return None


def alert_row(item: dict[str, Any]) -> dict[str, Any]:
    """One row of ``azure.security.alert.list`` from one alert."""
    properties = _properties(item)
    return {
        "name": _text(item.get("name")),
        "display_name": _text(properties.get("alertDisplayName")),
        "alert_type": _text(properties.get("alertType")),
        "severity": _text(properties.get("severity")),
        "status": _text(properties.get("status")),
        "time": _text(properties.get("timeGeneratedUtc")) or _text(properties.get("startTimeUtc")),
        "resource_id": _alert_resource_id(properties),
        "compromised_entity": _text(properties.get("compromisedEntity")),
    }


def plan_row(item: dict[str, Any]) -> dict[str, Any]:
    """One row of ``azure.security.plan.list`` from one Defender pricing."""
    properties = _properties(item)
    return {
        "name": _text(item.get("name")),
        "tier": _text(properties.get("pricingTier")),
        "sub_plan": _text(properties.get("subPlan")),
        "deprecated": properties.get("deprecated") is True,
    }


# ----------------------------------------------------------------------
# Azure Policy, role assignment and activity log rows
# ----------------------------------------------------------------------


def _policy_counts(results: object) -> dict[str, int | None]:
    """The counts of one ``SummaryResults`` object.

    ``compliant_resources`` comes from the ``resourceDetails`` entry with the
    state ``compliant``; it is 0 when the list has no such entry and
    ``None`` when Azure sent no list.
    """
    data = results if isinstance(results, dict) else {}
    details = data.get("resourceDetails")
    compliant: int | None = None
    if isinstance(details, list):
        compliant = 0
        for detail in details:
            state = detail.get("complianceState") if isinstance(detail, dict) else None
            if isinstance(state, str) and state.lower() == "compliant":
                compliant = _int(detail.get("count"))
    return {
        "non_compliant_resources": _int(data.get("nonCompliantResources")),
        "non_compliant_policies": _int(data.get("nonCompliantPolicies")),
        "compliant_resources": compliant,
    }


def _assignment_summary_row(item: dict[str, Any]) -> dict[str, Any]:
    assignment_id = _text(item.get("policyAssignmentId"))
    return {
        "assignment_id": assignment_id,
        "assignment_name": _last_part(assignment_id),
        "policy_set_definition_id": _text(item.get("policySetDefinitionId")),
        **_policy_counts(item.get("results")),
    }


def policy_summary(answer: dict[str, Any]) -> dict[str, Any]:
    """The answer of ``azure.policy.compliance.summary`` from Azure's ``SummarizeResults``.

    Azure sends one summary in ``value``. Its totals stay at the top; its
    policy assignments become the rows.
    """
    summaries = answer.get("value")
    first = summaries[0] if isinstance(summaries, list) and summaries else None
    summary: dict[str, Any] = first if isinstance(first, dict) else {}
    assignments = summary.get("policyAssignments")
    rows = object_rows(
        assignments if isinstance(assignments, list) else [], _assignment_summary_row
    )
    return {**_policy_counts(summary.get("results")), "value": rows, "count": len(rows)}


def policy_assignment_row(item: dict[str, Any]) -> dict[str, Any]:
    """One row of ``azure.policy.assignment.list`` from one policy assignment.

    A missing ``enforcementMode`` is ``Default``, Azure's documented default.
    """
    properties = _properties(item)
    definition_id = _text(properties.get("policyDefinitionId"))
    return {
        "id": _text(item.get("id")),
        "name": _text(item.get("name")),
        "display_name": _text(properties.get("displayName")),
        "scope": _text(properties.get("scope")),
        "policy_definition_id": definition_id,
        "is_initiative": definition_id is not None
        and "/policysetdefinitions/" in definition_id.lower(),
        "enforcement_mode": _text(properties.get("enforcementMode")) or "Default",
    }


def role_names_by_id(items: list[Any]) -> dict[str, str]:
    """The name of every role, by the lower-case GUID of its role definition.

    *items* are Azure's role definitions. The GUID is the last part of a
    role definition id, which is the same whether the id starts with the
    subscription or not.
    """
    names: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        guid = _text(item.get("name"))
        role = _text(_properties(item).get("roleName"))
        if guid and role:
            names[guid.lower()] = role
    return names


def role_assignment_row(item: dict[str, Any], role_names: Mapping[str, str]) -> dict[str, Any]:
    """One row of ``azure.role.assignment.list`` from one role assignment."""
    properties = _properties(item)
    role_id = _text(properties.get("roleDefinitionId"))
    guid = _last_part(role_id)
    return {
        "name": _text(item.get("name")),
        "principal_id": _text(properties.get("principalId")),
        "principal_type": _text(properties.get("principalType")),
        "role_name": role_names.get(guid.lower()) if guid else None,
        "role_definition_id": role_id,
        "scope": _text(properties.get("scope")),
    }


def activity_row(item: dict[str, Any]) -> dict[str, Any]:
    """One row of ``azure.activity.list`` from one activity log event.

    Only these fields are kept. The event's ``claims``, ``httpRequest``
    (with the caller's IP address) and ``properties`` are left out.
    """
    return {
        "time": _text(item.get("eventTimestamp")),
        "operation": _localized(item.get("operationName")),
        "status": _localized(item.get("status")),
        "caller": _text(item.get("caller")),
        "resource_id": _text(item.get("resourceId")),
        "correlation_id": _text(item.get("correlationId")),
        "level": _text(item.get("level")),
    }


# ----------------------------------------------------------------------
# Parameter checks
# ----------------------------------------------------------------------


def role_scope(value: object, config: AzureTargetConfig, *, target_name: str) -> str:
    """The ``{scope}`` of ``azure.role.assignment.list``, without its first slash.

    No value means the whole subscription. A value must start with the
    target's own subscription (``/subscriptions/<id>``); every further part
    may hold only the characters of :data:`_NAME_PART` and must not be
    ``.`` or ``..``. So the scope never leaves the subscription and never
    changes the path in another way.
    """
    if value is None or value == "":
        return subscription_scope(config)
    remediation = (
        "Pass a scope inside the target's subscription: /subscriptions/<its id>, "
        "/subscriptions/<its id>/resourceGroups/<name>, or a resource id; or leave "
        "it out for the whole subscription. azure.about shows the subscription id."
    )
    parts = value.strip().strip("/").split("/") if isinstance(value, str) else []
    inside = (
        len(parts) >= 2
        and parts[0].lower() == "subscriptions"
        and parts[1].lower() == config.subscription_id
    )
    if not inside:
        raise AzureParameterError(
            f"Azure target {target_name!r}: 'scope' is not inside the target's "
            f"subscription. {remediation}",
            remediation=remediation,
        )
    rest = parts[2:]
    if any(part in (".", "..") or not _NAME_PART.fullmatch(part) for part in rest):
        raise AzureParameterError(
            f"Azure target {target_name!r}: 'scope' may hold only letters, digits, '_', "
            f"'-', '.', '(' and ')' between its slashes. {remediation}",
            remediation=remediation,
        )
    return "/".join(["subscriptions", config.subscription_id, *rest])


def resource_group_name(value: object, *, target_name: str) -> str | None:
    """A checked resource group name, or ``None`` when there is none.

    The name goes into the activity log filter, so it may hold only the
    characters of :data:`_NAME_PART` (no quote) and at most 90 of them.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str) and len(value) <= _RESOURCE_GROUP_MAX and _NAME_PART.fullmatch(value):
        return value
    remediation = (
        "Pass the name of a resource group: letters, digits, '_', '-', '.', '(' and ')', "
        "at most 90 characters."
    )
    raise AzureParameterError(
        f"Azure target {target_name!r}: 'resource_group' is not a resource group name. "
        f"{remediation}",
        remediation=remediation,
    )


@dataclass(frozen=True)
class ActivityWindow:
    """The time window of one activity log read."""

    start: datetime
    end: datetime
    clamped: bool

    def as_dict(self) -> dict[str, Any]:
        return {"from": iso_time(self.start), "to": iso_time(self.end), "clamped": self.clamped}


def _caller_time(params: Mapping[str, Any], key: str, *, target_name: str) -> datetime | None:
    value = params.get(key)
    if value is None or value == "":
        return None
    parsed = parse_time(value)
    if parsed is None:
        remediation = f"Pass '{key}' as an ISO 8601 time, for example 2026-10-27T09:00:00Z."
        raise AzureParameterError(
            f"Azure target {target_name!r}: '{key}' is not a time. {remediation}",
            remediation=remediation,
        )
    return parsed


def activity_window(
    params: Mapping[str, Any], now: datetime, *, target_name: str
) -> ActivityWindow:
    """The window of ``azure.activity.list`` from the caller's ``from`` and ``to``.

    ``to`` is *now* when not given, and a ``to`` after *now* is moved to *now*.
    ``from`` is :data:`ACTIVITY_DEFAULT_HOURS` before ``to`` when not given,
    and a ``from`` more than :data:`ACTIVITY_MAX_DAYS` days back is moved to
    that limit. ``clamped`` says that MEHO moved one of them. A window that
    is empty after that raises :class:`AzureParameterError`. The checks run
    before any arithmetic on the caller's times, so no time can overflow.
    """
    asked_end = _caller_time(params, "to", target_name=target_name)
    asked_start = _caller_time(params, "from", target_name=target_name)
    oldest = now - timedelta(days=ACTIVITY_MAX_DAYS)
    moved_end = asked_end is not None and asked_end > now
    end = now if asked_end is None or moved_end else asked_end
    if end <= oldest:
        raise _empty_window(target_name)
    start = end - timedelta(hours=ACTIVITY_DEFAULT_HOURS) if asked_start is None else asked_start
    clamped = moved_end or start < oldest
    start = max(start, oldest)
    if start >= end:
        raise _empty_window(target_name)
    return ActivityWindow(start=start, end=end, clamped=clamped)


def _empty_window(target_name: str) -> AzureParameterError:
    remediation = (
        "Pass 'from' before 'to', with 'to' inside the last 90 days. The activity log "
        "keeps 90 days."
    )
    return AzureParameterError(
        f"Azure target {target_name!r}: the time window is empty. {remediation}",
        remediation=remediation,
    )
