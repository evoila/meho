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
same time do not edit the same lines.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "AKS_API_VERSION",
    "AKS_CLUSTERS_PATH",
    "AKS_CLUSTER_PATH",
    "AKS_KUBERNETES_VERSIONS_PATH",
    "AKS_NODE_POOLS_PATH",
    "AKS_UPGRADE_PROFILE_PATH",
    "LOCATIONS_PATH",
    "RESOURCE_GRAPH_API_VERSION",
    "RESOURCE_GRAPH_PATH",
    "SUBSCRIPTIONS_API_VERSION",
    "SUBSCRIPTION_PATH",
    "locations_path",
    "subscription_path",
]

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

# ---- avs (Task #5091) ----

# ---- compute (Task #5083) ----

# ---- cost (Task #5088) ----

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
