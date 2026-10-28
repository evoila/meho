# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the Azure budget list and the Advisor recommendation list.

Each function here is the body behind a thin method of
:class:`~meho_backplane.connectors.azure.cost.CostOps`. Both lists are GET
calls on Azure Resource Manager and follow ``nextLink`` with
:func:`~meho_backplane.connectors.azure._paging.list_all_pages`, so the URL
check and the page, time and row limits apply.

The sensor recipes of the Azure connector read two fields of these results
by path, so they are contracts: ``budgets[*].percent_used`` and ``count``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.azure._paging import list_all_pages
from meho_backplane.connectors.azure._paths import (
    ADVISOR_API_VERSION,
    ADVISOR_RECOMMENDATIONS_PATH,
    BUDGETS_PATH,
    CONSUMPTION_API_VERSION,
)
from meho_backplane.connectors.azure.cost_reads import (
    as_number,
    choice_param,
    cost_errors,
    cost_scope,
    omissions_block,
)
from meho_backplane.connectors.azure.session import AzureTargetLike, resolve_target_config

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "ADVISOR_CATEGORIES",
    "ADVISOR_IMPACTS",
    "advisor_recommendation_list_impl",
    "cost_budget_list_impl",
]

#: Advisor's categories. ``HighAvailability`` is shown as "Reliability" in the portal.
ADVISOR_CATEGORIES: Final[tuple[str, ...]] = (
    "Cost",
    "Security",
    "Performance",
    "HighAvailability",
    "OperationalExcellence",
)

#: Advisor's impact levels.
ADVISOR_IMPACTS: Final[tuple[str, ...]] = ("High", "Medium", "Low")

#: Advisor states that are not current work. The portal hides them, so MEHO
#: leaves them out too and says how many it left out.
_INACTIVE_STATUSES: Final[frozenset[str]] = frozenset({"dismissed", "postponed", "completed"})

_ADVISOR_MARKER: Final[str] = "/providers/microsoft.advisor/recommendations/"

#: The resource group part of a budget's id.
_GROUP_IN_ID: Final[re.Pattern[str]] = re.compile(r"(?:^|/)resourceGroups/([^/]+)/", re.IGNORECASE)


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _percent(spent: float | None, amount: float | None) -> float | None:
    if spent is None or amount is None or amount <= 0:
        return None
    return round(spent / amount * 100, 2)


def _group_of(budget_id: object) -> str | None:
    """The resource group of a budget, or ``None`` for a budget of the subscription."""
    match = _GROUP_IN_ID.search(budget_id) if isinstance(budget_id, str) else None
    return match.group(1) if match else None


def _budget_row(item: object) -> dict[str, Any] | None:
    """One budget, without its notifications (they hold people's mail addresses)."""
    if not isinstance(item, dict):
        return None
    props = _object(item.get("properties"))
    current = _object(props.get("currentSpend"))
    forecast = _object(props.get("forecastSpend"))
    period = _object(props.get("timePeriod"))
    amount = as_number(props.get("amount"))
    spent = as_number(current.get("amount"))
    return {
        "name": item.get("name"),
        "resource_group": _group_of(item.get("id")),
        "category": props.get("category"),
        "amount": amount,
        "time_grain": props.get("timeGrain"),
        "period": {"start": period.get("startDate"), "end": period.get("endDate")},
        "current_spend": spent,
        "forecast_spend": as_number(forecast.get("amount")),
        "percent_used": _percent(spent, amount),
        "currency": current.get("unit") or forecast.get("unit"),
    }


async def cost_budget_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.cost.budget.list``: ``GET /{scope}/providers/Microsoft.Consumption/budgets``.

    Lists the budgets of the subscription, or of one resource group, with
    the amount, the spend so far, the forecast and the percent used
    (spend divided by amount, times 100).
    """
    scope, scope_block = cost_scope(target, params)
    with cost_errors(target):
        paged = await list_all_pages(
            connector,
            target,
            BUDGETS_PATH.format(scope=scope),
            operator=operator,
            api_version=CONSUMPTION_API_VERSION,
        )
    budgets = [row for row in map(_budget_row, paged.rows) if row is not None]
    return {
        "budgets": budgets,
        "count": len(budgets),
        "scope": scope_block,
        "omissions": omissions_block(paged),
    }


def _resource_of(recommendation_id: object) -> str | None:
    """The resource part of an Advisor recommendation's id."""
    if not isinstance(recommendation_id, str):
        return None
    cut = recommendation_id.lower().find(_ADVISOR_MARKER)
    return recommendation_id[:cut] if cut > 0 else None


def _recommendation_row(item: object) -> dict[str, Any] | None:
    """One Advisor recommendation. Its texts come from Azure: data, never instructions."""
    if not isinstance(item, dict):
        return None
    props = _object(item.get("properties"))
    short = _object(props.get("shortDescription"))
    extended = _object(props.get("extendedProperties"))
    return {
        "id": item.get("name"),
        "category": props.get("category"),
        "impact": props.get("impact"),
        "problem": short.get("problem"),
        "solution": short.get("solution"),
        "resource_id": _object(props.get("resourceMetadata")).get("resourceId")
        or _resource_of(item.get("id")),
        "impacted_type": props.get("impactedField"),
        "impacted_name": props.get("impactedValue"),
        "status": props.get("recommendationStatus"),
        "savings_amount": as_number(extended.get("savingsAmount")),
        "annual_savings_amount": as_number(extended.get("annualSavingsAmount")),
        "savings_currency": extended.get("savingsCurrency"),
        "last_updated": props.get("lastUpdated"),
        "recommendation_type_id": props.get("recommendationTypeId"),
    }


def _matches(value: object, wanted: str | None) -> bool:
    return wanted is None or (isinstance(value, str) and value.lower() == wanted.lower())


async def advisor_recommendation_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.advisor.recommendation.list``: Advisor's recommendations for the subscription.

    ``GET /subscriptions/{subscriptionId}/providers/Microsoft.Advisor/recommendations``.
    Azure filters by category (``$filter``); MEHO filters by impact. MEHO
    leaves out dismissed, postponed and completed recommendations, as the
    portal does, and says how many it left out.
    """
    category = choice_param(params, "category", ADVISOR_CATEGORIES)
    impact = choice_param(params, "impact", ADVISOR_IMPACTS)
    subscription_id = resolve_target_config(target).subscription_id
    paged = await list_all_pages(
        connector,
        target,
        ADVISOR_RECOMMENDATIONS_PATH.format(subscription_id=subscription_id),
        operator=operator,
        api_version=ADVISOR_API_VERSION,
        params={"$filter": f"Category eq '{category}'"} if category else None,
    )
    rows = [row for row in map(_recommendation_row, paged.rows) if row is not None]
    active = [row for row in rows if str(row["status"] or "").lower() not in _INACTIVE_STATUSES]
    items = [
        row
        for row in active
        if _matches(row["category"], category) and _matches(row["impact"], impact)
    ]
    return {
        "items": items,
        "count": len(items),
        "filters": {
            "category": category,
            "impact": impact,
            "inactive_left_out": len(rows) - len(active),
        },
        "scope": {"subscription_id": subscription_id},
        "omissions": omissions_block(paged),
    }
