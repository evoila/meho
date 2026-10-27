# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hand-written Azure Resource Manager paths and API versions.

Every path the Azure connector calls is a constant in this module, so one CI
check can compare each of them with Azure's own API description files. That
check arrives with the pinned Azure spec files; from then on a typo or a
renamed endpoint fails in CI and not on a customer system. Later reads add
their paths here.

Azure pins an API version per call (the ``api-version`` query parameter). The
versions are kept next to their paths, one per family of operations.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "ADVISOR_API_VERSION",
    "ADVISOR_RECOMMENDATIONS_PATH",
    "BUDGETS_PATH",
    "CONSUMPTION_API_VERSION",
    "COST_FORECAST_PATH",
    "COST_MANAGEMENT_API_VERSION",
    "COST_QUERY_PATH",
    "RESOURCE_GRAPH_API_VERSION",
    "RESOURCE_GRAPH_PATH",
    "RETAIL_PRICES_API_VERSION",
    "RETAIL_PRICES_URL",
    "SUBSCRIPTIONS_API_VERSION",
    "SUBSCRIPTION_PATH",
    "subscription_path",
]

#: ``GET /subscriptions/{subscriptionId}``: one subscription (Subscriptions - Get).
SUBSCRIPTION_PATH: Final[str] = "/subscriptions/{subscription_id}"

#: The API version of the subscription reads (Microsoft.Resources).
SUBSCRIPTIONS_API_VERSION: Final[str] = "2022-12-01"

#: ``POST /providers/Microsoft.ResourceGraph/resources``: one Resource Graph
#: query (Resources - Resources). A read, even though it is a POST.
RESOURCE_GRAPH_PATH: Final[str] = "/providers/Microsoft.ResourceGraph/resources"

#: The API version of Resource Graph queries (Microsoft.ResourceGraph).
RESOURCE_GRAPH_API_VERSION: Final[str] = "2024-04-01"


def subscription_path(subscription_id: str) -> str:
    """Return the path of one subscription.

    *subscription_id* must already be checked as a GUID (see
    :func:`~meho_backplane.connectors.azure.session.resolve_target_config`), so
    it cannot change the path.
    """
    return SUBSCRIPTION_PATH.format(subscription_id=subscription_id)


# ---- cost (Task #5088) ----

#: ``POST /{scope}/providers/Microsoft.CostManagement/query``: the cost of a
#: subscription or a resource group (Query - Usage). A read, even though it is
#: a POST. ``scope`` is ``subscriptions/{id}`` or
#: ``subscriptions/{id}/resourceGroups/{name}``, without a leading slash.
COST_QUERY_PATH: Final[str] = "/{scope}/providers/Microsoft.CostManagement/query"

#: ``POST /{scope}/providers/Microsoft.CostManagement/forecast``: the expected
#: cost of a scope (Forecast - Usage). A read, even though it is a POST.
COST_FORECAST_PATH: Final[str] = "/{scope}/providers/Microsoft.CostManagement/forecast"

#: The API version of the cost query and the forecast (Microsoft.CostManagement).
COST_MANAGEMENT_API_VERSION: Final[str] = "2026-06-01"

#: ``GET /{scope}/providers/Microsoft.Consumption/budgets``: the budgets of a
#: subscription or a resource group (Budgets - List).
BUDGETS_PATH: Final[str] = "/{scope}/providers/Microsoft.Consumption/budgets"

#: The API version of the budget reads (Microsoft.Consumption).
CONSUMPTION_API_VERSION: Final[str] = "2026-06-01"

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.Advisor/recommendations``:
#: Azure Advisor's recommendations for one subscription (Recommendations - List).
ADVISOR_RECOMMENDATIONS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Advisor/recommendations"
)

#: The API version of the Advisor reads (Microsoft.Advisor).
ADVISOR_API_VERSION: Final[str] = "2026-10-01"

#: The public Azure Retail Prices API. It is not Azure Resource Manager: it
#: needs no login and gets no token. Microsoft publishes no API description
#: file for it, so it is a full URL and not a ``*_PATH`` constant, and the spec
#: path check leaves it out.
RETAIL_PRICES_URL: Final[str] = "https://prices.azure.com/api/retail/prices"

#: The API version of the price list. From this version on, filter values must
#: match in upper and lower case.
RETAIL_PRICES_API_VERSION: Final[str] = "2023-01-01-preview"
