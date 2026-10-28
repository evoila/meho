# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hand-written Azure Resource Manager paths and API versions.

Every path the Azure connector calls is a constant in this module, so one CI
check can compare each of them with Azure's own API description files
(``backend/tests/test_connectors_azure_spec_reconcile.py``). A typo, a
renamed endpoint or a wrong API version then fails in CI and not on a
customer system. Later reads add their paths here, and each new ``*_PATH``
constant gets one line in that check.

Azure pins an API version per call (the ``api-version`` query parameter). The
versions are kept next to their paths, one per family of operations. Each
family keeps its paths in its own section below, so families built at the
same time do not edit the same lines. For the same reason the module has no
``__all__``: every family would add its names at the same place. The spec path
check finds the constants with ``vars()``.
"""

from __future__ import annotations

from typing import Final

#: ``GET /subscriptions/{subscriptionId}``: one subscription (Subscriptions - Get).
SUBSCRIPTION_PATH: Final[str] = "/subscriptions/{subscription_id}"

#: ``GET /subscriptions/{subscriptionId}/locations``: the Azure regions of one
#: subscription (Subscriptions - List Locations).
LOCATIONS_PATH: Final[str] = "/subscriptions/{subscription_id}/locations"

#: The API version of the subscription and location reads (Microsoft.Resources).
SUBSCRIPTIONS_API_VERSION: Final[str] = "2022-12-01"

#: ``POST /providers/Microsoft.ResourceGraph/resources``: one Resource Graph
#: query (Resources - Resources). A read, even though it is a POST.
RESOURCE_GRAPH_PATH: Final[str] = "/providers/Microsoft.ResourceGraph/resources"

#: The API version of Resource Graph queries (Microsoft.ResourceGraph).
RESOURCE_GRAPH_API_VERSION: Final[str] = "2024-04-01"

# ---- aks (Task #5087) ----
#
# Azure Kubernetes Service (AKS): clusters, node pools, upgrade options and
# the Kubernetes versions per region. Reads only. MEHO never calls the
# actions that return a kubeconfig (a cluster login): listClusterUserCredential,
# listClusterAdminCredential, listClusterMonitoringUserCredential, and the
# listCredential actions of access profiles and managed namespaces.

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.ContainerService/managedClusters``:
#: every AKS cluster of the subscription (Managed Clusters - List).
AKS_CLUSTERS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.ContainerService/managedClusters"
)

#: ``GET .../resourceGroups/{resourceGroupName}/providers/Microsoft.ContainerService``
#: ``/managedClusters/{resourceName}``: one AKS cluster (Managed Clusters - Get).
AKS_CLUSTER_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.ContainerService/managedClusters/{cluster}"
)

#: ``GET .../managedClusters/{resourceName}/upgradeProfiles/default``: the
#: Kubernetes versions the cluster and its node pools can move to (Managed
#: Clusters - Get Upgrade Profile).
AKS_UPGRADE_PROFILE_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.ContainerService/managedClusters/{cluster}/upgradeProfiles/default"
)

#: ``GET .../managedClusters/{resourceName}/agentPools``: the node pools of one
#: cluster; Azure calls them agent pools (Agent Pools - List).
AKS_NODE_POOLS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.ContainerService/managedClusters/{cluster}/agentPools"
)

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.ContainerService``
#: ``/locations/{location}/kubernetesVersions``: the Kubernetes versions AKS
#: offers in one region (Managed Clusters - List Kubernetes Versions).
AKS_KUBERNETES_VERSIONS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.ContainerService"
    "/locations/{location}/kubernetesVersions"
)

#: The API version of every AKS read (Microsoft.ContainerService).
AKS_API_VERSION: Final[str] = "2026-08-01"

# ---- arc (Task #5084) ----

# ---- automation (Task #5092) ----
#
# Azure Automation (Microsoft.Automation). MEHO reads only these lists: the
# Automation accounts, and per account its runbooks, jobs, schedules and job
# schedules (the links between a schedule and its runbooks). Paths that
# return runbook code, job output or streams, credentials or variables are
# never added here, because they can hold secrets; a test fails when one is.

#: The API version of the Azure Automation reads (Microsoft.Automation).
AUTOMATION_API_VERSION: Final[str] = "2024-10-23"

_AUTOMATION_ACCOUNT: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.Automation/automationAccounts/{account}"
)

#: ``GET``: every Automation account of the subscription (AutomationAccount_List).
AUTOMATION_ACCOUNTS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Automation/automationAccounts"
)

#: ``GET``: the runbooks of one account (Runbook_ListByAutomationAccount).
AUTOMATION_RUNBOOKS_PATH: Final[str] = f"{_AUTOMATION_ACCOUNT}/runbooks"

#: ``GET``: the jobs of one account, narrowed with ``$filter``
#: (Job_ListByAutomationAccount).
AUTOMATION_JOBS_PATH: Final[str] = f"{_AUTOMATION_ACCOUNT}/jobs"

#: ``GET``: the schedules of one account (Schedule_ListByAutomationAccount).
AUTOMATION_SCHEDULES_PATH: Final[str] = f"{_AUTOMATION_ACCOUNT}/schedules"

#: ``GET``: the job schedules of one account, which link a schedule to a
#: runbook (JobSchedule_ListByAutomationAccount).
AUTOMATION_JOB_SCHEDULES_PATH: Final[str] = f"{_AUTOMATION_ACCOUNT}/jobSchedules"

# ---- avs (Task #5091) ----

# ---- compute (Task #5083) ----

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

# ---- inventory (Task #5082) ----

# ---- migrate (Task #5093) ----

# ---- monitor (Task #5090) ----

# ---- network (Task #5085) ----

# ---- security (Task #5089) ----

# ---- storage (Task #5086) ----


def subscription_path(subscription_id: str) -> str:
    """Return the path of one subscription.

    *subscription_id* must already be checked as a GUID (see
    :func:`~meho_backplane.connectors.azure.session.resolve_target_config`), so
    it cannot change the path.
    """
    return SUBSCRIPTION_PATH.format(subscription_id=subscription_id)


def locations_path(subscription_id: str) -> str:
    """Return the path of the regions of one subscription.

    *subscription_id* must already be checked as a GUID, like in
    :func:`subscription_path`.
    """
    return LOCATIONS_PATH.format(subscription_id=subscription_id)
