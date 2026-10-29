# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the Azure Kubernetes Service (AKS) reads.

Each ``*_impl`` function is the body behind a thin method of
:class:`~meho_backplane.connectors.azure.aks.AksOps`. The operation table,
the group text and the guidance for the AI are in
:mod:`meho_backplane.connectors.azure.aks`; how Azure's answers become rows,
and how parameters are checked, is in
:mod:`meho_backplane.connectors.azure.aks_rows`.

Every function here only reads, with GET. None of them calls an action that
returns a kubeconfig or another cluster login. Every parameter is checked
before the login and before any request. Lists are read with
:func:`~meho_backplane.connectors.azure._paging.list_all_pages`, so every
``nextLink`` is checked before the token goes to it and the paging limits
hold.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.azure._paging import list_all_pages
from meho_backplane.connectors.azure._paths import (
    AKS_API_VERSION,
    AKS_CLUSTER_PATH,
    AKS_CLUSTERS_PATH,
    AKS_KUBERNETES_VERSIONS_PATH,
    AKS_NODE_POOLS_PATH,
    AKS_UPGRADE_PROFILE_PATH,
)
from meho_backplane.connectors.azure.aks_rows import (
    cluster_detail,
    cluster_name,
    cluster_row,
    default_version,
    list_answer,
    location_name,
    node_pool_row,
    object_rows,
    resource_group_name,
    version_rows,
)
from meho_backplane.connectors.azure.session import AzureTargetLike, resolve_target_config

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "aks_cluster_list_impl",
    "aks_cluster_show_impl",
    "aks_nodepool_list_impl",
    "aks_version_list_impl",
]


async def aks_cluster_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.aks.cluster.list``: every AKS cluster of the subscription."""
    del params  # the operation takes no parameters
    config = resolve_target_config(target)
    paged = await list_all_pages(
        connector,
        target,
        AKS_CLUSTERS_PATH.format(subscription_id=config.subscription_id),
        operator=operator,
        api_version=AKS_API_VERSION,
    )
    return list_answer(paged, object_rows(paged.rows, cluster_row))


async def aks_cluster_show_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.aks.cluster.show``: one cluster and its upgrade options.

    Two GETs: the cluster, then its upgrade profile (the versions the
    control plane and each node pool can move to).
    """
    config = resolve_target_config(target)
    resource_group = resource_group_name(params.get("resource_group"), target_name=target.name)
    names = {
        "subscription_id": config.subscription_id,
        "resource_group": resource_group,
        "cluster": cluster_name(params.get("name"), "name", target_name=target.name),
    }
    cluster = await connector.arm_get(
        target, AKS_CLUSTER_PATH.format(**names), operator=operator, api_version=AKS_API_VERSION
    )
    upgrade_profile = await connector.arm_get(
        target,
        AKS_UPGRADE_PROFILE_PATH.format(**names),
        operator=operator,
        api_version=AKS_API_VERSION,
    )
    return cluster_detail(cluster, upgrade_profile)


async def aks_nodepool_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.aks.nodepool.list``: the node pools (agent pools) of one cluster."""
    config = resolve_target_config(target)
    resource_group = resource_group_name(params.get("resource_group"), target_name=target.name)
    cluster = cluster_name(params.get("cluster"), "cluster", target_name=target.name)
    paged = await list_all_pages(
        connector,
        target,
        AKS_NODE_POOLS_PATH.format(
            subscription_id=config.subscription_id,
            resource_group=resource_group,
            cluster=cluster,
        ),
        operator=operator,
        api_version=AKS_API_VERSION,
    )
    rows = object_rows(paged.rows, node_pool_row)
    return list_answer(paged, rows, cluster=cluster, resource_group=resource_group)


async def aks_version_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.aks.version.list``: the Kubernetes versions AKS offers in one region.

    One GET: Azure answers this list in one piece, with no next page.
    """
    config = resolve_target_config(target)
    location = location_name(params.get("location"), target_name=target.name)
    answer = await connector.arm_get(
        target,
        AKS_KUBERNETES_VERSIONS_PATH.format(
            subscription_id=config.subscription_id, location=location
        ),
        operator=operator,
        api_version=AKS_API_VERSION,
    )
    rows = version_rows(answer)
    return {
        "value": rows,
        "count": len(rows),
        "location": location,
        "default_version": default_version(rows),
    }
