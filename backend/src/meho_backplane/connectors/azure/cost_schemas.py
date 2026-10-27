# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameter and response schemas of the Azure cost family.

The operation table in :mod:`meho_backplane.connectors.azure.cost` uses them.
The dispatcher checks every call's parameters against the parameter schema
before the operation runs; the bodies check the same rules once more, so a
path can never carry a value that is not allowed.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.azure.cost_lists import ADVISOR_CATEGORIES, ADVISOR_IMPACTS
from meho_backplane.connectors.azure.cost_prices import PRICE_CURRENCIES, PRICE_TYPES
from meho_backplane.connectors.azure.cost_reads import COST_TYPES, GROUP_BY_DIMENSIONS

__all__ = [
    "ADVISOR_LIST_PARAMS",
    "ADVISOR_LIST_RESPONSE",
    "BUDGET_LIST_PARAMS",
    "BUDGET_LIST_RESPONSE",
    "COST_FORECAST_PARAMS",
    "COST_FORECAST_RESPONSE",
    "COST_QUERY_PARAMS",
    "COST_QUERY_RESPONSE",
    "PRICE_LIST_PARAMS",
    "PRICE_LIST_RESPONSE",
]

_TEXT: Final[list[str]] = ["string", "null"]
_NUMBER: Final[list[str]] = ["number", "null"]
_DATE_PATTERN: Final[str] = r"^\d{4}-\d{2}-\d{2}$"


def _field(kind: str | list[str], description: str) -> dict[str, Any]:
    return {"type": kind, "description": description}


def _object(description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "description": description, "properties": properties}


# ----------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------

_RESOURCE_GROUP_PARAM: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 90,
    "pattern": r"^[-\w.()]+$",
    "description": (
        "Optional. One resource group of the subscription, by name. Without it, the "
        "whole subscription."
    ),
}

_COST_TYPE_PARAM: Final[dict[str, Any]] = {
    "type": "string",
    "enum": list(COST_TYPES),
    "default": "actual",
    "description": (
        "actual (the default): the cost as billed; a reservation bought in the period "
        "counts in full. amortized: reservation and savings plan purchases are spread "
        "over their term."
    ),
}

COST_QUERY_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "type": _COST_TYPE_PARAM,
        "period": {
            "type": "string",
            "enum": ["month_to_date", "last_month"],
            "description": (
                "month_to_date (the default): from the 1st of this month to today. "
                "last_month: the whole previous calendar month. Days are UTC. Leave it "
                "out when you give from and to."
            ),
        },
        "from": {
            "type": "string",
            "pattern": _DATE_PATTERN,
            "description": "First day of your own period, YYYY-MM-DD (UTC). Give it with to.",
        },
        "to": {
            "type": "string",
            "pattern": _DATE_PATTERN,
            "description": "Last day of your own period, YYYY-MM-DD (UTC), included.",
        },
        "granularity": {
            "type": "string",
            "enum": ["total", "daily"],
            "default": "total",
            "description": (
                "total (the default): one row per group for the whole period. daily: "
                "one row per day and group."
            ),
        },
        "group_by": {
            "type": "array",
            "maxItems": 2,
            "uniqueItems": True,
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": 516,
                "pattern": rf"^({'|'.join(GROUP_BY_DIMENSIONS)}|tag:.+)$",
                "description": (
                    "One of resource_group, service, resource_type, location, meter, or "
                    "tag:<tag key> (for example tag:costcenter)."
                ),
            },
            "description": (
                "Up to two things to group the cost by. Without it, one total row (or one "
                "row per day)."
            ),
        },
        "resource_group": _RESOURCE_GROUP_PARAM,
    },
    "additionalProperties": False,
    "examples": [
        {"period": "last_month", "group_by": ["resource_group", "service"]},
        {"period": "month_to_date", "granularity": "daily"},
        {"type": "amortized", "from": "2026-09-01", "to": "2026-09-30", "resource_group": "rg-a"},
    ],
}

COST_FORECAST_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"type": _COST_TYPE_PARAM, "resource_group": _RESOURCE_GROUP_PARAM},
    "additionalProperties": False,
    "examples": [{}, {"type": "amortized", "resource_group": "rg-a"}],
}

BUDGET_LIST_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"resource_group": _RESOURCE_GROUP_PARAM},
    "additionalProperties": False,
    "examples": [{}, {"resource_group": "rg-a"}],
}

ADVISOR_LIST_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": list(ADVISOR_CATEGORIES),
            "description": (
                "Optional. Only this category. HighAvailability is shown as Reliability "
                "in the Azure portal."
            ),
        },
        "impact": {
            "type": "string",
            "enum": list(ADVISOR_IMPACTS),
            "description": "Optional. Only this impact: High, Medium or Low.",
        },
    },
    "additionalProperties": False,
    "examples": [{"category": "Cost"}, {"impact": "High"}, {}],
}

_PRICE_TEXT: Final[dict[str, Any]] = {"type": "string", "minLength": 1, "maxLength": 200}

PRICE_LIST_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "service_name": {
            **_PRICE_TEXT,
            "description": "The service, exactly as Azure writes it, for example Virtual Machines.",
        },
        "region": {
            **_PRICE_TEXT,
            "description": "The Azure region name, for example westeurope.",
        },
        "sku": {
            **_PRICE_TEXT,
            "description": "The size name Azure uses, for example Standard_D4s_v5.",
        },
        "price_type": {
            "type": "string",
            "enum": list(PRICE_TYPES),
            "description": (
                "Consumption (pay-as-you-go), Reservation (1 or 3 years) or DevTestConsumption."
            ),
        },
        "product_name": {
            **_PRICE_TEXT,
            "description": "The product, for example Virtual Machines Dsv5 Series Windows.",
        },
        "meter_name": {
            **_PRICE_TEXT,
            "description": "The meter, for example D4s v5 or D4s v5 Spot.",
        },
        "currency": {
            "type": "string",
            "enum": list(PRICE_CURRENCIES),
            "default": "EUR",
            "description": (
                "The currency of the prices. Default EUR. Prices in other currencies than "
                "USD are reference values, not quotes."
            ),
        },
    },
    "additionalProperties": False,
    "examples": [
        {"service_name": "Virtual Machines", "region": "westeurope", "sku": "Standard_D4s_v5"},
        {"sku": "Standard_D4s_v5", "region": "westeurope", "price_type": "Reservation"},
        {"service_name": "Storage", "region": "westeurope", "currency": "USD"},
    ],
}

# ----------------------------------------------------------------------
# Responses
# ----------------------------------------------------------------------

_OMISSIONS: Final[dict[str, Any]] = _object(
    "Whether a limit cut the list. Nothing is cut silently.",
    {
        "truncated": _field("boolean", "True when MEHO stopped before the end of the list."),
        "kind": _field(_TEXT, "page_limit, time_limit or row_limit; null when complete."),
        "message": _field(_TEXT, "What was left out and how to narrow the request."),
        "pages_read": _field("integer", "How many pages MEHO read."),
    },
)

_SCOPE: Final[dict[str, Any]] = _object(
    "What was read.",
    {
        "subscription_id": _field("string", "The subscription of the target."),
        "resource_group": _field(_TEXT, "The resource group, or null for the subscription."),
    },
)

_PERIOD: Final[dict[str, Any]] = _object(
    "The days the numbers cover (UTC, both included).",
    {
        "name": _field("string", "month_to_date, last_month, current_month or custom."),
        "from": _field("string", "First day, YYYY-MM-DD."),
        "to": _field("string", "Last day, YYYY-MM-DD."),
        "granularity": _field("string", "total (one row per group) or daily."),
    },
)

_COST_ROW: Final[dict[str, Any]] = _object(
    "One row of cost. Group columns that Azure adds keep Azure's name.",
    {
        "cost": _field(_NUMBER, "The cost, in currency."),
        "currency": _field(_TEXT, "The currency of cost, for example EUR."),
        "date": _field(_TEXT, "The day, YYYY-MM-DD. Only with granularity daily."),
        "resource_group": _field(_TEXT, "With group_by resource_group."),
        "service": _field(_TEXT, "With group_by service, for example Virtual Machines."),
        "resource_type": _field(_TEXT, "With group_by resource_type."),
        "location": _field(_TEXT, "With group_by location."),
        "meter": _field(_TEXT, "With group_by meter."),
        "tag_key": _field(_TEXT, "With group_by tag:<key>, when Azure names the key."),
        "tag_value": _field(_TEXT, "With group_by tag:<key>, the tag's value."),
    },
)

COST_QUERY_RESPONSE: Final[dict[str, Any]] = _object(
    "The cost of the period.",
    {
        "items": {"type": "array", "description": "The rows.", "items": _COST_ROW},
        "totals": _object(
            "The sum of the rows.",
            {
                "cost": _field(_NUMBER, "Sum, 2 decimals; null with more than one currency."),
                "currency": _field(_TEXT, "The one currency of the rows, or null."),
                "currencies": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Every currency in the rows.",
                },
                "rows": _field("integer", "How many rows."),
            },
        ),
        "period": _PERIOD,
        "scope": _SCOPE,
        "query": _object(
            "What was asked.",
            {
                "cost_type": _field("string", "actual or amortized."),
                "group_by": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The group_by names.",
                },
                "cost_column": _field(_TEXT, "Azure's cost column: PreTaxCost or Cost."),
            },
        ),
        "omissions": _OMISSIONS,
    },
)

COST_FORECAST_RESPONSE: Final[dict[str, Any]] = _object(
    "This month's cost so far and Azure's estimate for the rest.",
    {
        "items": {
            "type": "array",
            "description": "One row per day and status.",
            "items": _object(
                "One day.",
                {
                    "date": _field(_TEXT, "The day, YYYY-MM-DD."),
                    "status": _field(_TEXT, "Actual (already used) or Forecast (estimate)."),
                    "cost": _field(_NUMBER, "The cost, in currency."),
                    "currency": _field(_TEXT, "The currency of cost."),
                },
            ),
        },
        "totals": _object(
            "The month in numbers.",
            {
                "actual_cost": _field(_NUMBER, "Sum of the Actual rows."),
                "forecast_cost": _field(_NUMBER, "Sum of the Forecast rows."),
                "month_total": _field(_NUMBER, "actual_cost plus forecast_cost."),
                "currency": _field(_TEXT, "The one currency of the rows, or null."),
                "currencies": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Every currency in the rows.",
                },
                "rows": _field("integer", "How many rows."),
            },
        ),
        "period": _PERIOD,
        "scope": _SCOPE,
        "query": _object(
            "What was asked.",
            {
                "cost_type": _field("string", "actual or amortized."),
                "include_actual_cost": _field("boolean", "Always true: past days are actual."),
                "cost_column": _field(_TEXT, "Azure's name of the cost column."),
            },
        ),
        "omissions": _OMISSIONS,
    },
)

BUDGET_LIST_RESPONSE: Final[dict[str, Any]] = _object(
    "The budgets.",
    {
        "budgets": {
            "type": "array",
            "description": "One entry per budget.",
            "items": _object(
                "One budget. Its notification settings are left out.",
                {
                    "name": _field(_TEXT, "The budget's name (text from Azure)."),
                    "resource_group": _field(
                        _TEXT, "The budget's resource group, or null for the subscription."
                    ),
                    "category": _field(_TEXT, "Cost."),
                    "amount": _field(_NUMBER, "The budget amount per time grain."),
                    "time_grain": _field(_TEXT, "Monthly, Quarterly, Annually, or billing."),
                    "period": _object(
                        "When the budget runs.",
                        {
                            "start": _field(_TEXT, "Start date (as Azure gives it)."),
                            "end": _field(_TEXT, "End date, or null."),
                        },
                    ),
                    "current_spend": _field(_NUMBER, "Spend in the current time grain."),
                    "forecast_spend": _field(_NUMBER, "Forecast spend, or null."),
                    "percent_used": _field(_NUMBER, "current_spend / amount x 100, 2 decimals."),
                    "currency": _field(_TEXT, "The currency of the amounts."),
                },
            ),
        },
        "count": _field("integer", "How many budgets."),
        "scope": _SCOPE,
        "omissions": _OMISSIONS,
    },
)

ADVISOR_LIST_RESPONSE: Final[dict[str, Any]] = _object(
    "Azure Advisor's current recommendations.",
    {
        "items": {
            "type": "array",
            "description": "One entry per recommendation.",
            "items": _object(
                "One recommendation. Its texts come from Azure.",
                {
                    "id": _field(_TEXT, "The recommendation's name in Azure."),
                    "category": _field(_TEXT, "Cost, Security, Performance, ..."),
                    "impact": _field(_TEXT, "High, Medium or Low."),
                    "problem": _field(_TEXT, "What Advisor found (text from Azure)."),
                    "solution": _field(_TEXT, "What Advisor suggests (text from Azure)."),
                    "resource_id": _field(_TEXT, "The Azure resource id it is about."),
                    "impacted_type": _field(_TEXT, "The resource type."),
                    "impacted_name": _field(_TEXT, "The resource name (text from Azure)."),
                    "status": _field(_TEXT, "Azure's status, for example New."),
                    "savings_amount": _field(_NUMBER, "Azure's savings estimate, or null."),
                    "annual_savings_amount": _field(_NUMBER, "Yearly savings, or null."),
                    "savings_currency": _field(_TEXT, "The currency of the savings."),
                    "last_updated": _field(_TEXT, "When the recommendation last changed."),
                    "recommendation_type_id": _field(_TEXT, "The kind of recommendation."),
                },
            ),
        },
        "count": _field("integer", "How many items."),
        "filters": _object(
            "What was asked and what was left out.",
            {
                "category": _field(_TEXT, "The category asked for, or null."),
                "impact": _field(_TEXT, "The impact asked for, or null."),
                "inactive_left_out": _field(
                    "integer", "Dismissed, postponed or completed recommendations left out."
                ),
            },
        ),
        "scope": _object(
            "What was read.",
            {"subscription_id": _field("string", "The subscription of the target.")},
        ),
        "omissions": _OMISSIONS,
    },
)

PRICE_LIST_RESPONSE: Final[dict[str, Any]] = _object(
    "Public Azure retail prices.",
    {
        "items": {
            "type": "array",
            "description": "One entry per price.",
            "items": _object(
                "One price. Names come from Azure.",
                {
                    "service": _field(_TEXT, "Service, for example Virtual Machines."),
                    "product": _field(_TEXT, "Product, for example Virtual Machines Dsv5 Series."),
                    "sku": _field(_TEXT, "SKU name, for example D4s v5."),
                    "arm_sku": _field(_TEXT, "Size name Azure uses, for example Standard_D4s_v5."),
                    "meter_id": _field(_TEXT, "The meter's id."),
                    "meter_name": _field(_TEXT, "The meter's name."),
                    "region": _field(_TEXT, "Region name, for example westeurope."),
                    "location": _field(_TEXT, "Azure's location label, for example EU West."),
                    "unit": _field(_TEXT, "Unit of the price, for example 1 Hour."),
                    "retail_price": _field(_NUMBER, "Price per unit without discount."),
                    "unit_price": _field(_NUMBER, "The same as retail_price."),
                    "price_type": _field(_TEXT, "Consumption, Reservation or DevTestConsumption."),
                    "reservation_term": _field(_TEXT, "1 Year or 3 Years for a Reservation."),
                    "effective_start_date": _field(_TEXT, "Since when the price is valid."),
                    "currency": _field(_TEXT, "The currency of the price."),
                    "tier_minimum_units": _field(_NUMBER, "Units needed for this price."),
                    "is_primary_meter_region": _field(
                        ["boolean", "null"], "True for the meter Azure bills with."
                    ),
                },
            ),
        },
        "lookup": _object(
            "How the prices were read.",
            {
                "looked_up_at": _field("string", "When MEHO read the prices (UTC)."),
                "currency": _field("string", "The currency asked for."),
                "reference_only": _field("boolean", "True when the currency is not USD."),
                "note": _field("string", "What the prices are and are not."),
                "filter": _field("string", "The filter MEHO sent."),
                "api_version": _field("string", "The price list's API version."),
                "rows": _field("integer", "How many prices."),
            },
        ),
        "omissions": _OMISSIONS,
    },
)
