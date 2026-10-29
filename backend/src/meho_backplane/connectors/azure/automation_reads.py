# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the Azure Automation reads (group ``automation``).

Each ``*_impl`` function is the body behind a thin method of
:class:`~meho_backplane.connectors.azure.automation.AutomationOps`. The
operation table and the guidance for the AI are in
:mod:`meho_backplane.connectors.azure.automation`, the schemas and name rules
in :mod:`meho_backplane.connectors.azure.automation_schemas`.

Every read returns the same shape::

    {"value": [...rows...], "truncated": false,
     "omissions": {"count": 0, "items": []}, ...blocks}

``value`` holds the rows. ``truncated`` is true when rows are missing, and
``omissions`` says what is missing and why (``what``, ``reason``,
``detail``). The blocks are objects, so the ``result_objects`` hint of each
operation keeps them next to the stored-result link when the rows go behind
one. Every list follows ``nextLink`` with the shared paging helper and its
limits.

Only the fields of the rows below are copied from Azure's answers. Runbook
content links, runbook and job schedule parameters, job output and the
account's hybrid worker URL are never read, because they can hold secrets.
Every list is read through
:class:`~meho_backplane.connectors.azure.automation_trace.TraceMaskingCaller`,
so the flight recorder's trace leaves those fields out too. Names and
descriptions from Azure are copied as data; they never go into a path or a
filter.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, cast

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.azure._errors import (
    AzureApiError,
    AzureForbiddenError,
    AzureNotFoundError,
    AzureProviderNotRegisteredError,
    AzureThrottledError,
    AzureUrlRefusedError,
)
from meho_backplane.connectors.azure._paging import list_all_pages
from meho_backplane.connectors.azure._paths import (
    AUTOMATION_ACCOUNTS_PATH,
    AUTOMATION_API_VERSION,
    AUTOMATION_JOB_SCHEDULES_PATH,
    AUTOMATION_JOBS_PATH,
    AUTOMATION_RUNBOOKS_PATH,
    AUTOMATION_SCHEDULES_PATH,
)
from meho_backplane.connectors.azure.automation_schemas import (
    ACCOUNT_PATTERN,
    DEFAULT_JOB_DAYS,
    JOB_STATUSES,
    MAX_JOB_DAYS,
    RESOURCE_GROUP_PATTERN,
)
from meho_backplane.connectors.azure.automation_trace import TraceMaskingCaller
from meho_backplane.connectors.azure.session import AzureTargetLike, resolve_target_config

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "AutomationParameterError",
    "account_list_impl",
    "account_names",
    "job_filter",
    "job_list_impl",
    "runbook_list_impl",
    "runbooks_by_schedule",
    "schedule_list_impl",
]

_RESOURCE_GROUP_RE: Final[re.Pattern[str]] = re.compile(RESOURCE_GROUP_PATTERN)
_ACCOUNT_RE: Final[re.Pattern[str]] = re.compile(ACCOUNT_PATTERN)

#: Errors that stop one part of a read made of several parts (the links of
#: the schedules). The rest still counts, and this part becomes an omission.
#: A login error is not in this list: it stops the whole read.
_PART_ERRORS: Final[tuple[type[Exception], ...]] = (
    AzureApiError,
    AzureNotFoundError,
    AzureUrlRefusedError,
    httpx.HTTPError,
)


class AutomationParameterError(ValueError):
    """A parameter of an Azure Automation read is not usable. Raised before any request.

    The message names the target and the parameter, never the value. The
    dispatcher returns it as ``connector_error`` with ``remediation``.
    """

    def __init__(self, message: str, *, remediation: str) -> None:
        super().__init__(message)
        self.remediation = remediation


def _utc_now() -> datetime:
    """The current time in UTC. Tests replace it."""
    return datetime.now(UTC)


# ----------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------


def _refuse(target_name: str, field: str, remediation: str) -> AutomationParameterError:
    return AutomationParameterError(
        f"Azure target {target_name!r}: '{field}' is not usable. {remediation}",
        remediation=remediation,
    )


def _checked_name(
    params: Mapping[str, Any], field: str, pattern: re.Pattern[str], rule: str, *, target_name: str
) -> str:
    """``params[field]`` when the whole of it matches *pattern*.

    ``fullmatch`` is used on purpose: a ``$`` in a pattern also matches
    before a line end, so the schema check alone would let ``"name\\n"`` pass.
    """
    value = params.get(field)
    if isinstance(value, str) and pattern.fullmatch(value):
        return value
    raise _refuse(
        target_name,
        field,
        f"Pass {rule}, as azure.automation.account.list returns it.",
    )


def account_names(params: Mapping[str, Any], *, target_name: str) -> dict[str, str]:
    """The checked ``resource_group`` and ``account``, as path values."""
    return {
        "resource_group": _checked_name(
            params,
            "resource_group",
            _RESOURCE_GROUP_RE,
            "a resource group name (letters, digits, _ - . ( ), not ending with a dot)",
            target_name=target_name,
        ),
        "account": _checked_name(
            params,
            "account",
            _ACCOUNT_RE,
            "an Automation account name (6 to 50 letters, digits and -, starting with a letter)",
            target_name=target_name,
        ),
    }


def _job_days(params: Mapping[str, Any], *, target_name: str) -> int:
    value = params.get("days")
    if value is None:
        return DEFAULT_JOB_DAYS
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_JOB_DAYS:
        return value
    raise _refuse(target_name, "days", f"Pass a whole number from 1 to {MAX_JOB_DAYS}.")


def _job_status(params: Mapping[str, Any], *, target_name: str) -> str | None:
    value = params.get("status")
    if value is None or value in JOB_STATUSES:
        return value
    raise _refuse(target_name, "status", f"Pass one of {', '.join(JOB_STATUSES)}.")


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def job_filter(since: datetime, status: str | None) -> str:
    """The ``$filter`` of the job list: jobs that started at or after *since*.

    The form is the one Microsoft's own Az.Automation PowerShell module
    sends (``Get-AzAutomationJob -StartTime -Status``): an unquoted UTC time
    with seven fraction digits, and the state in single quotes. *status* is
    always one of :data:`JOB_STATUSES`, so it cannot change the filter.
    """
    clauses = [f"properties/startTime ge {since.astimezone(UTC):%Y-%m-%dT%H:%M:%S}.0000000Z"]
    if status is not None:
        clauses.append(f"properties/status eq '{status}'")
    return " and ".join(clauses)


# ----------------------------------------------------------------------
# Rows
# ----------------------------------------------------------------------


def _props(item: Mapping[str, Any]) -> Mapping[str, Any]:
    props = item.get("properties")
    return props if isinstance(props, Mapping) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: object) -> int | float | None:
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def _flag(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _texts(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _ints(value: object) -> list[int]:
    return [item for item in value if _int(item) is not None] if isinstance(value, list) else []


def _name_of(value: object) -> str | None:
    """The ``name`` of Azure's ``{"name": ...}`` reference to a runbook or schedule."""
    return _text(value.get("name")) if isinstance(value, Mapping) else None


def resource_group_of(resource_id: object) -> str | None:
    """The resource group in ``/subscriptions/<id>/resourceGroups/<name>/...``, or ``None``."""
    parts = resource_id.split("/") if isinstance(resource_id, str) else []
    if (
        len(parts) > 4
        and parts[1].lower() == "subscriptions"
        and parts[3].lower() == "resourcegroups"
    ):
        return parts[4] or None
    return None


def account_row(item: Mapping[str, Any]) -> dict[str, Any]:
    """One row of ``azure.automation.account.list``."""
    props = _props(item)
    return {
        "name": _text(item.get("name")),
        "id": _text(item.get("id")),
        "resource_group": resource_group_of(item.get("id")),
        "location": _text(item.get("location")),
        "state": _text(props.get("state")),
        "sku": _name_of(props.get("sku")),
        "creation_time": _text(props.get("creationTime")),
        "last_modified_time": _text(props.get("lastModifiedTime")),
    }


def runbook_row(item: Mapping[str, Any]) -> dict[str, Any]:
    """One row of ``azure.automation.runbook.list``. The content links are never read."""
    props = _props(item)
    return {
        "name": _text(item.get("name")),
        "runbook_type": _text(props.get("runbookType")),
        "state": _text(props.get("state")),
        "description": _text(props.get("description")),
        "creation_time": _text(props.get("creationTime")),
        "last_modified_time": _text(props.get("lastModifiedTime")),
        "last_modified_by": _text(props.get("lastModifiedBy")),
    }


def job_row(item: Mapping[str, Any]) -> dict[str, Any]:
    """One row of ``azure.automation.job.list``."""
    props = _props(item)
    return {
        "name": _text(item.get("name")),
        "job_id": _text(props.get("jobId")),
        "runbook": _name_of(props.get("runbook")),
        "status": _text(props.get("status")),
        "creation_time": _text(props.get("creationTime")),
        "start_time": _text(props.get("startTime")),
        "end_time": _text(props.get("endTime")),
        "started_by": _text(props.get("startedBy")),
        "run_on": _text(props.get("runOn")),
    }


def runbooks_by_schedule(items: Iterable[Mapping[str, Any]]) -> dict[str, list[str]]:
    """The runbook names per schedule name (in lower case), from the job schedules.

    Only the two names are read. The ``parameters`` of a job schedule (the
    values the runbook gets, which can be secret) are never read.
    """
    links: dict[str, set[str]] = {}
    for item in items:
        props = _props(item)
        schedule = _name_of(props.get("schedule"))
        runbook = _name_of(props.get("runbook"))
        if schedule and runbook:
            links.setdefault(schedule.casefold(), set()).add(runbook)
    return {schedule: sorted(names) for schedule, names in links.items()}


def _occurrences(value: object) -> list[str]:
    """Azure's monthly occurrences as ``"<week> <day>"``, for example ``"1 Monday"``."""
    found: list[str] = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, Mapping):
            week, day = _int(item.get("occurrence")), _text(item.get("day"))
            if week is not None and day:
                found.append(f"{week} {day}")
    return found


def schedule_row(item: Mapping[str, Any], links: Mapping[str, list[str]] | None) -> dict[str, Any]:
    """One row of ``azure.automation.schedule.list``.

    *links* comes from :func:`runbooks_by_schedule`; ``None`` means the links
    could not be read, so ``runbooks`` is ``None`` (not known) and not ``[]``.
    """
    props = _props(item)
    advanced = props.get("advancedSchedule")
    plan: Mapping[str, Any] = advanced if isinstance(advanced, Mapping) else {}
    name = _text(item.get("name"))
    return {
        "name": name,
        "enabled": _flag(props.get("isEnabled")),
        "frequency": _text(props.get("frequency")),
        "interval": _number(props.get("interval")),
        "next_run": _text(props.get("nextRun")),
        "start_time": _text(props.get("startTime")),
        "expiry_time": _text(props.get("expiryTime")),
        "time_zone": _text(props.get("timeZone")),
        "week_days": _texts(plan.get("weekDays")),
        "month_days": _ints(plan.get("monthDays")),
        "monthly_occurrences": _occurrences(plan.get("monthlyOccurrences")),
        "description": _text(props.get("description")),
        "runbooks": None if links is None else list(links.get((name or "").casefold(), [])),
    }


# ----------------------------------------------------------------------
# Reading
# ----------------------------------------------------------------------


def _reason(exc: Exception) -> str:
    if isinstance(exc, AzureForbiddenError):
        return "no_permission"
    if isinstance(exc, AzureNotFoundError):
        return "not_found"
    if isinstance(exc, AzureProviderNotRegisteredError):
        return "provider_not_registered"
    if isinstance(exc, AzureThrottledError):
        return "throttled"
    return "read_failed"


def _detail(exc: Exception) -> str:
    """MEHO's own text about the error. A lost connection gets a fixed text."""
    if isinstance(exc, httpx.HTTPError):
        return f"Azure did not answer ({type(exc).__name__}). Try again later."
    return str(exc)


class _Reader:
    """Reads the lists of one call and keeps what was left out."""

    def __init__(
        self, connector: AzureConnector, operator: Operator, target: AzureTargetLike
    ) -> None:
        # send() sees the connector through the caller, so the trace masks secrets.
        self.caller = cast("AzureConnector", TraceMaskingCaller(connector))
        self.operator = operator
        self.target = target
        self.subscription_id = resolve_target_config(target).subscription_id
        self.omissions: list[dict[str, str]] = []

    def path(self, template: str, **names: str) -> str:
        """Fill *template* with the target's subscription and the checked *names*."""
        return template.format(subscription_id=self.subscription_id, **names)

    async def rows(
        self, path: str, what: str, *, params: dict[str, Any] | None = None
    ) -> list[Mapping[str, Any]]:
        """Every row of one list. A paging limit becomes an omission; an error is raised."""
        paged = await list_all_pages(
            self.caller,
            self.target,
            path,
            operator=self.operator,
            api_version=AUTOMATION_API_VERSION,
            params=params,
        )
        if paged.omission is not None:
            self.omissions.append(
                {
                    "what": what,
                    "reason": str(paged.omission.get("kind")),
                    "detail": str(paged.omission.get("message")),
                }
            )
        return [item for item in paged.rows if isinstance(item, Mapping)]

    async def part(self, path: str, what: str) -> list[Mapping[str, Any]] | None:
        """Like :meth:`rows`, but an error becomes an omission and ``None``."""
        try:
            return await self.rows(path, what)
        except _PART_ERRORS as exc:
            self.omissions.append({"what": what, "reason": _reason(exc), "detail": _detail(exc)})
            return None

    def answer(self, rows: list[dict[str, Any]], **blocks: Any) -> dict[str, Any]:
        items = list(self.omissions)
        return {
            "value": rows,
            "truncated": bool(items),
            "omissions": {"count": len(items), "items": items},
            **blocks,
        }


# ----------------------------------------------------------------------
# The operations
# ----------------------------------------------------------------------


async def account_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.automation.account.list``: every Automation account of the subscription."""
    del params  # the operation takes no parameters
    reader = _Reader(connector, operator, target)
    items = await reader.rows(reader.path(AUTOMATION_ACCOUNTS_PATH), "Automation accounts")
    return reader.answer([account_row(item) for item in items])


async def runbook_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.automation.runbook.list``: the runbooks of one account, without their code."""
    names = account_names(params, target_name=target.name)
    reader = _Reader(connector, operator, target)
    items = await reader.rows(reader.path(AUTOMATION_RUNBOOKS_PATH, **names), "runbooks")
    return reader.answer([runbook_row(item) for item in items])


async def job_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.automation.job.list``: the jobs that started in the last ``days`` days.

    Azure narrows the list with :func:`job_filter`: the start time and, when
    given, the state. A job that has not started yet has no start time, so
    it can be missing from the list.
    """
    names = account_names(params, target_name=target.name)
    days = _job_days(params, target_name=target.name)
    status = _job_status(params, target_name=target.name)
    now = _utc_now().replace(microsecond=0)
    since = now - timedelta(days=days)
    reader = _Reader(connector, operator, target)
    items = await reader.rows(
        reader.path(AUTOMATION_JOBS_PATH, **names),
        "jobs",
        params={"$filter": job_filter(since, status)},
    )
    return reader.answer(
        [job_row(item) for item in items],
        window={"days": days, "from": _iso(since), "to": _iso(now)},
        status_filter=status,
    )


async def schedule_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.automation.schedule.list``: the schedules of one account and their runbooks.

    Reads the schedules, then the job schedules that link each schedule to
    its runbooks. When the links cannot be read, the schedules still come
    back, with ``runbooks`` set to ``None`` and an omission that says why.
    """
    names = account_names(params, target_name=target.name)
    reader = _Reader(connector, operator, target)
    items = await reader.rows(reader.path(AUTOMATION_SCHEDULES_PATH, **names), "schedules")
    link_items = await reader.part(
        reader.path(AUTOMATION_JOB_SCHEDULES_PATH, **names), "runbook links"
    )
    links = None if link_items is None else runbooks_by_schedule(link_items)
    return reader.answer([schedule_row(item, links) for item in items])
