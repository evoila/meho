# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the Azure cost query and forecast, and the helpers of the cost family.

Each operation function here is the body behind a thin method of
:class:`~meho_backplane.connectors.azure.cost.CostOps`. The operation table
and the guidance for the AI are in :mod:`meho_backplane.connectors.azure.cost`.
Budgets and Advisor are in :mod:`meho_backplane.connectors.azure.cost_lists`;
the public price list, which needs no login, is in
:mod:`meho_backplane.connectors.azure.cost_prices`.

Every call here goes to Azure Resource Manager through
:func:`~meho_backplane.connectors.azure._http.send`: the target's token, one
retry budget, the URL check. The cost query and the forecast are POST calls
that only read, so they are sent again after a 5xx or a lost connection, like
a GET.

The metadata of each list (``period``, ``scope``, ``totals``, ``query``,
``filters``, ``omissions``) is kept in objects. The operations name these
objects in their ``result_objects`` hint, so they stay next to the link when
a long list goes to a stored result.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.adapters.http import json_payload_or_empty
from meho_backplane.connectors.azure._errors import AzureApiError, AzureThrottledError
from meho_backplane.connectors.azure._http import check_azure_url, send
from meho_backplane.connectors.azure._paging import PagedRows, PageLimits, _Pager
from meho_backplane.connectors.azure._paths import (
    COST_FORECAST_PATH,
    COST_MANAGEMENT_API_VERSION,
    COST_QUERY_PATH,
)
from meho_backplane.connectors.azure.session import AzureTargetLike, resolve_target_config

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "COST_TYPES",
    "GROUP_BY_DIMENSIONS",
    "as_number",
    "check_resource_group",
    "choice_param",
    "cost_errors",
    "cost_forecast_impl",
    "cost_query_impl",
    "cost_scope",
    "omissions_block",
    "resolve_period",
]

#: The ``type`` parameter and Azure's name for it.
COST_TYPES: Final[Mapping[str, str]] = {"actual": "ActualCost", "amortized": "AmortizedCost"}

#: The ``group_by`` names and Azure's dimension for each. A ``tag:<key>`` name
#: groups by a tag key instead.
GROUP_BY_DIMENSIONS: Final[Mapping[str, str]] = {
    "resource_group": "ResourceGroup",
    "service": "ServiceName",
    "resource_type": "ResourceType",
    "location": "ResourceLocation",
    "meter": "Meter",
}

_TAG_PREFIX: Final[str] = "tag:"

#: An Azure tag key: 1 to 512 characters, none of them < > % & \ ? / or a control character.
_TAG_KEY: Final[re.Pattern[str]] = re.compile(r"[^\x00-\x1f<>%&\\?/]{1,512}")

#: An Azure resource group name: 1 to 90 letters, digits, underscores,
#: hyphens, periods and parentheses. The last character is not a period.
_RESOURCE_GROUP: Final[re.Pattern[str]] = re.compile(r"[-\w.()]{1,90}")

_DATE: Final[re.Pattern[str]] = re.compile(r"\d{4}-\d{2}-\d{2}")

#: MEHO asks Azure to sum this column. Azure names the answer's column
#: ``PreTaxCost`` or ``Cost``, depending on the agreement type; both are read.
_COST_AGGREGATION: Final[dict[str, Any]] = {"totalCost": {"name": "PreTaxCost", "function": "Sum"}}
_COST_COLUMNS: Final[frozenset[str]] = frozenset({"pretaxcost", "cost", "totalcost"})

#: Azure's column names and the key of each in MEHO's rows. Other columns
#: keep Azure's own name.
_COLUMN_KEYS: Final[Mapping[str, str]] = {
    "usagedate": "date",
    "currency": "currency",
    "coststatus": "status",
    "resourcegroup": "resource_group",
    "resourcegroupname": "resource_group",
    "servicename": "service",
    "resourcetype": "resource_type",
    "resourcelocation": "location",
    "meter": "meter",
    "tagkey": "tag_key",
    "tagvalue": "tag_value",
}

#: What a customer must change when Azure answers a cost read with one of
#: these error codes.
_COST_HINTS: Final[Mapping[str, str]] = {
    "AccountCostDisabled": (
        "The Enterprise Agreement hides costs from account owners and subscription "
        "users. An enterprise administrator must switch on 'Account owners can view "
        "charges' (AO view charges): Cost Management + Billing > Billing scopes > the "
        "billing account > Policies."
    ),
    "DepartmentCostDisabled": (
        "The Enterprise Agreement hides costs from department administrators. An "
        "enterprise administrator must switch on 'Department admins can view charges' "
        "(DA view charges): Cost Management + Billing > Billing scopes > the billing "
        "account > Policies."
    ),
    "IndirectCostDisabled": (
        "The partner that sells this Enterprise Agreement has not published its prices, "
        "so Azure shows no costs. Ask the partner to switch on cost visibility."
    ),
    "FailedDependency": (
        "Azure cannot forecast this scope: it needs about one week of cost data, and it "
        "cannot forecast costs in more than one currency. Wait, or ask for one resource "
        "group with costs in one currency."
    ),
}

#: The fix when Azure keeps answering 401 to a cost read after a new login.
_COST_401_HINT: Final[str] = (
    "If azure.about works for this target, the login is fine and the billing account "
    "hides costs. On an Enterprise Agreement, an enterprise administrator must switch "
    "on 'Account owners can view charges' (AO view charges). On a Microsoft Customer "
    "Agreement, a billing profile owner must set the billing profile's 'Azure charges' "
    "policy to Yes. The login also needs the Cost Management Reader role (or Reader) on "
    "the subscription or resource group."
)


def _today() -> date:
    """Today in UTC. Azure's cost data uses UTC days. Tests replace it."""
    return datetime.now(UTC).date()


# ----------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------


def choice_param(params: Mapping[str, Any], name: str, allowed: tuple[str, ...]) -> str | None:
    """Return the parameter *name*, which must be one of *allowed* or not set."""
    value = params.get(name)
    if value is None:
        return None
    if isinstance(value, str) and value in allowed:
        return value
    raise ValueError(f"{name} must be one of {', '.join(allowed)}.")


def _cost_type(params: Mapping[str, Any]) -> str:
    return choice_param(params, "type", tuple(COST_TYPES)) or "actual"


def check_resource_group(name: object) -> str:
    """Return *name* when it is a valid resource group name, so it is safe in a path."""
    if isinstance(name, str) and _RESOURCE_GROUP.fullmatch(name) and not name.endswith("."):
        return name
    raise ValueError(
        "resource_group must be a resource group name: 1 to 90 letters, digits, "
        "underscores, hyphens, periods or parentheses, not ending with a period."
    )


def cost_scope(target: AzureTargetLike, params: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return Azure's scope text and the ``scope`` block of the result."""
    subscription_id = resolve_target_config(target).subscription_id
    group = params.get("resource_group")
    scope = f"subscriptions/{subscription_id}"
    if group is not None:
        scope += f"/resourceGroups/{check_resource_group(group)}"
    return scope, {"subscription_id": subscription_id, "resource_group": group}


def _parse_date(name: str, value: object) -> date:
    if isinstance(value, str) and _DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError(f"{name} must be a date written as YYYY-MM-DD.")


def resolve_period(params: Mapping[str, Any]) -> tuple[str, date, date]:
    """Return the period's name and its first and last day (UTC).

    ``period`` is ``month_to_date`` (the default) or ``last_month``;
    ``from`` and ``to`` give a period of one's own instead.
    """
    name = choice_param(params, "period", ("month_to_date", "last_month"))
    start, end = params.get("from"), params.get("to")
    if start is None and end is None:
        today = _today()
        first_of_month = today.replace(day=1)
        if name == "last_month":
            last = first_of_month - timedelta(days=1)
            return name, last.replace(day=1), last
        return "month_to_date", first_of_month, today
    if name is not None:
        raise ValueError("Give period, or from and to, not both.")
    if start is None or end is None:
        raise ValueError("Give both from and to (YYYY-MM-DD).")
    first, last = _parse_date("from", start), _parse_date("to", end)
    if first > last:
        raise ValueError("from must not be after to.")
    return "custom", first, last


def _current_month() -> tuple[date, date]:
    """The first and the last day of this month (UTC)."""
    first = _today().replace(day=1)
    next_first = (first + timedelta(days=32)).replace(day=1)
    return first, next_first - timedelta(days=1)


def _time_period(first: date, last: date) -> dict[str, str]:
    return {"from": f"{first.isoformat()}T00:00:00Z", "to": f"{last.isoformat()}T23:59:59Z"}


def _group_by(value: object) -> list[tuple[str, dict[str, str]]]:
    """Return each ``group_by`` name with Azure's grouping for it."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 2:
        raise ValueError("group_by takes at most two names.")
    groupings: list[tuple[str, dict[str, str]]] = []
    for name in value:
        if isinstance(name, str) and name in GROUP_BY_DIMENSIONS:
            groupings.append((name, {"type": "Dimension", "name": GROUP_BY_DIMENSIONS[name]}))
        elif (
            isinstance(name, str)
            and name.startswith(_TAG_PREFIX)
            and _TAG_KEY.fullmatch(name[len(_TAG_PREFIX) :])
        ):
            groupings.append((name, {"type": "TagKey", "name": name[len(_TAG_PREFIX) :]}))
        else:
            raise ValueError(
                "group_by names are resource_group, service, resource_type, location, "
                "meter, or tag:<tag key>."
            )
    if len({name for name, _ in groupings}) != len(groupings):
        raise ValueError("group_by must not name the same thing twice.")
    return groupings


# ----------------------------------------------------------------------
# Answers
# ----------------------------------------------------------------------


def as_number(value: object) -> float | None:
    """*value* as a finite number, or ``None``. Azure sends some numbers as text."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _usage_date(value: object) -> object:
    """Write Azure's ``UsageDate`` (``20260901`` or a date-time text) as ``2026-09-01``."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        value = str(int(value))
    if isinstance(value, str):
        if re.fullmatch(r"\d{8}", value):
            return f"{value[:4]}-{value[4:6]}-{value[6:]}"
        if _DATE.match(value):
            return value[:10]
    return value


def _column_keys(columns: object) -> tuple[list[str], str | None]:
    """MEHO's key for each column, in order, and the name of Azure's cost column."""
    keys: list[str] = []
    cost_column: str | None = None
    for column in columns if isinstance(columns, list) else []:
        raw = column.get("name") if isinstance(column, dict) else None
        name = raw if isinstance(raw, str) else ""
        if name.lower() in _COST_COLUMNS and cost_column is None:
            cost_column = name
            keys.append("cost")
        else:
            keys.append(_COLUMN_KEYS.get(name.lower(), name))
    return keys, cost_column


def _rows(keys: list[str], rows: object) -> list[dict[str, Any]]:
    """Turn Azure's rows (lists in column order) into objects."""
    items: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, list):
            continue
        item = dict(zip(keys, row, strict=False))
        if "date" in item:
            item["date"] = _usage_date(item["date"])
        items.append(item)
    return items


async def _post_all_pages(
    connector: AzureConnector,
    target: AzureTargetLike,
    path: str,
    body: dict[str, Any],
    *,
    operator: Operator,
) -> tuple[PagedRows, str | None]:
    """POST a cost query or forecast and follow ``nextLink`` with the same body.

    Azure puts the next page's token into ``nextLink``; the next page is the
    same request body sent to that link. Every link goes through the URL
    check first. The page, time and row limits of :mod:`._paging` apply.
    Returns the rows as objects and the name of Azure's cost column.
    """
    pager = _Pager(PageLimits())
    cost_column: str | None = None
    response = await send(
        connector,
        target,
        "POST",
        path,
        operator=operator,
        params={"api-version": COST_MANAGEMENT_API_VERSION},
        json=body,
        repeatable=True,
    )
    while True:
        answer = json_payload_or_empty(response)
        properties = answer.get("properties") if isinstance(answer, dict) else None
        page: dict[str, Any] = properties if isinstance(properties, dict) else {}
        keys, column = _column_keys(page.get("columns"))
        cost_column = cost_column or column
        rows = _rows(keys, page.get("rows"))
        if rows and column is None:
            raise ValueError(
                f"Azure target {target.name!r}: Azure's cost answer has no cost column "
                "(PreTaxCost or Cost), so MEHO cannot read the amounts."
            )
        if not pager.add(rows):
            return pager.result, cost_column
        next_link = page.get("nextLink")
        if not isinstance(next_link, str) or not next_link:
            return pager.result, cost_column
        check_azure_url(target, next_link)
        if not pager.may_read_more():
            return pager.result, cost_column
        response = await send(
            connector, target, "POST", next_link, operator=operator, json=body, repeatable=True
        )


def omissions_block(paged: PagedRows) -> dict[str, Any]:
    """The ``omissions`` block: whether a limit cut the list, and how far it got."""
    omission = paged.omission or {}
    return {
        "truncated": paged.truncated,
        "kind": omission.get("kind"),
        "message": omission.get("message"),
        "pages_read": paged.pages,
    }


def _sum(items: list[dict[str, Any]]) -> float:
    costs = [as_number(item.get("cost")) for item in items]
    return round(math.fsum(cost for cost in costs if cost is not None), 2)


def _currencies(items: list[dict[str, Any]]) -> list[str]:
    return sorted({item["currency"] for item in items if isinstance(item.get("currency"), str)})


def _totals(items: list[dict[str, Any]]) -> dict[str, Any]:
    """The sum of the rows. With rows in more than one currency there is no sum."""
    currencies = _currencies(items)
    single = len(currencies) <= 1
    return {
        "cost": _sum(items) if single else None,
        "currency": currencies[0] if len(currencies) == 1 else None,
        "currencies": currencies,
        "rows": len(items),
    }


def _forecast_totals(items: list[dict[str, Any]]) -> dict[str, Any]:
    """The month total: cost so far (Actual rows) plus the expected rest (Forecast rows)."""
    currencies = _currencies(items)
    single = len(currencies) <= 1

    def part(status: str) -> float | None:
        if not single:
            return None
        return _sum([item for item in items if str(item.get("status", "")).lower() == status])

    actual, forecast = part("actual"), part("forecast")
    return {
        "actual_cost": actual,
        "forecast_cost": forecast,
        "month_total": None if actual is None or forecast is None else round(actual + forecast, 2),
        "currency": currencies[0] if len(currencies) == 1 else None,
        "currencies": currencies,
        "rows": len(items),
    }


# ----------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------


def _with_cost_hint(exc: AzureApiError, target_name: str) -> AzureApiError | None:
    """The same error with MEHO's fix, when Azure's error code is a known cost case."""
    hint = _COST_HINTS.get(exc.azure_error_code or "")
    if hint is None or isinstance(exc, AzureThrottledError):
        return None
    return type(exc)(
        f"Azure target {target_name!r}: Azure refused the cost read (HTTP {exc.http_status}, "
        f"Azure error code {exc.azure_error_code}). {hint}",
        http_status=exc.http_status,
        azure_error_code=exc.azure_error_code,
        azure_message=exc.azure_message,
        remediation=hint,
    )


@contextmanager
def cost_errors(target: AzureTargetLike) -> Iterator[None]:
    """Name the setting to switch on when Azure hides cost data from the login.

    Only Azure's own 401 to the cost call is rewritten. A login that
    Microsoft Entra ID refused (``stage="establish"``) keeps its own error,
    because there the secret is the problem, not a billing setting.
    """
    try:
        yield
    except ConnectorAuthError as exc:
        if exc.stage == "establish":
            raise
        raise ConnectorAuthError(
            f"Azure target {target.name!r}: Azure refused the cost data with HTTP 401, also "
            f"after a new login. {_COST_401_HINT}",
            status_code=exc.status_code,
            cause=exc.cause,
            target_name=exc.target_name,
            host=exc.host,
            secret_ref=exc.secret_ref,
            remediation=_COST_401_HINT,
            stage=exc.stage,
        ) from exc
    except AzureApiError as exc:
        hinted = _with_cost_hint(exc, target.name)
        if hinted is None:
            raise
        raise hinted from exc


# ----------------------------------------------------------------------
# Operations
# ----------------------------------------------------------------------


async def cost_query_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.cost.query``: ``POST /{scope}/providers/Microsoft.CostManagement/query``.

    Reads the actual or amortized cost of the subscription or one resource
    group for a period, as one total per group or per day, grouped by up to
    two dimensions. MEHO always sends the period as dates (``Custom``), so
    the result shows exactly which days it covers.
    """
    cost_type = _cost_type(params)
    granularity = choice_param(params, "granularity", ("total", "daily")) or "total"
    groupings = _group_by(params.get("group_by"))
    name, first, last = resolve_period(params)
    scope, scope_block = cost_scope(target, params)
    dataset: dict[str, Any] = {
        "granularity": "Daily" if granularity == "daily" else "None",
        "aggregation": _COST_AGGREGATION,
    }
    if groupings:
        dataset["grouping"] = [grouping for _, grouping in groupings]
    body = {
        "type": COST_TYPES[cost_type],
        "timeframe": "Custom",
        "timePeriod": _time_period(first, last),
        "dataset": dataset,
    }
    with cost_errors(target):
        paged, cost_column = await _post_all_pages(
            connector, target, COST_QUERY_PATH.format(scope=scope), body, operator=operator
        )
    return {
        "items": paged.rows,
        "totals": _totals(paged.rows),
        "period": {
            "name": name,
            "from": first.isoformat(),
            "to": last.isoformat(),
            "granularity": granularity,
        },
        "scope": scope_block,
        "query": {
            "cost_type": cost_type,
            "group_by": [label for label, _ in groupings],
            "cost_column": cost_column,
        },
        "omissions": omissions_block(paged),
    }


async def cost_forecast_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.cost.forecast``: ``POST /{scope}/providers/Microsoft.CostManagement/forecast``.

    Reads this month per day: the cost so far (``Actual`` rows) and the
    expected cost of the remaining days (``Forecast`` rows), and adds them up
    to the expected month total.
    """
    cost_type = _cost_type(params)
    scope, scope_block = cost_scope(target, params)
    first, last = _current_month()
    body = {
        "type": COST_TYPES[cost_type],
        "timeframe": "Custom",
        "timePeriod": _time_period(first, last),
        "dataset": {"granularity": "Daily", "aggregation": _COST_AGGREGATION},
        "includeActualCost": True,
        "includeFreshPartialCost": False,
    }
    with cost_errors(target):
        paged, cost_column = await _post_all_pages(
            connector, target, COST_FORECAST_PATH.format(scope=scope), body, operator=operator
        )
    return {
        "items": paged.rows,
        "totals": _forecast_totals(paged.rows),
        "period": {
            "name": "current_month",
            "from": first.isoformat(),
            "to": last.isoformat(),
            "granularity": "daily",
        },
        "scope": scope_block,
        "query": {"cost_type": cost_type, "include_actual_cost": True, "cost_column": cost_column},
        "omissions": omissions_block(paged),
    }
