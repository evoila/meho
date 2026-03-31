# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS EKS Handlers.

Handlers for EKS operations: clusters and node groups.
"""

import asyncio
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.aws.serializers import (
    serialize_eks_cluster,
    serialize_eks_node_group,
)

if TYPE_CHECKING:
    from meho_app.modules.connectors.aws.connector import AWSConnector

logger = get_logger(__name__)


class EKSHandlerMixin:
    """Mixin providing EKS operation handlers."""

    # =========================================================================
    # CLUSTER OPERATIONS
    # =========================================================================

    async def _handle_list_eks_clusters(
        self: "AWSConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        List EKS clusters with full details.

        EKS list_clusters returns only cluster names, so we follow up with
        describe_cluster for each to get full details.

        Args:
            params: Optional keys: region.

        Returns:
            List of serialized EKS clusters.
        """
        client = self._get_client("eks", params.get("region"))

        def _list_and_describe() -> list[dict[str, Any]]:
            paginator = client.get_paginator("list_clusters")
            cluster_names: list[str] = []
            for page in paginator.paginate():
                cluster_names.extend(page.get("clusters", []))

            results: list[dict[str, Any]] = []
            for name in cluster_names:
                response = client.describe_cluster(name=name)
                cluster = response.get("cluster")
                if cluster:
                    results.append(cluster)
            return results

        raw = await asyncio.to_thread(_list_and_describe)
        return [serialize_eks_cluster(c) for c in raw]

    async def _handle_get_eks_cluster(
        self: "AWSConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Get details for a specific EKS cluster.

        Args:
            params: Required keys: cluster_name. Optional keys: region.

        Returns:
            Serialized EKS cluster.
        """
        client = self._get_client("eks", params.get("region"))
        cluster_name = params["cluster_name"]

        def _describe() -> dict[str, Any]:
            response = client.describe_cluster(name=cluster_name)
            return response.get("cluster", {})

        raw = await asyncio.to_thread(_describe)
        return serialize_eks_cluster(raw)

    # =========================================================================
    # NODE GROUP OPERATIONS
    # =========================================================================

    async def _handle_list_eks_node_groups(
        self: "AWSConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        List EKS node groups for a cluster with full details.

        Args:
            params: Required keys: cluster_name. Optional keys: region.

        Returns:
            List of serialized EKS node groups.
        """
        client = self._get_client("eks", params.get("region"))
        cluster_name = params["cluster_name"]

        def _list_and_describe() -> list[dict[str, Any]]:
            paginator = client.get_paginator("list_nodegroups")
            nodegroup_names: list[str] = []
            for page in paginator.paginate(clusterName=cluster_name):
                nodegroup_names.extend(page.get("nodegroups", []))

            results: list[dict[str, Any]] = []
            for name in nodegroup_names:
                response = client.describe_nodegroup(
                    clusterName=cluster_name, nodegroupName=name
                )
                ng = response.get("nodegroup")
                if ng:
                    results.append(ng)
            return results

        raw = await asyncio.to_thread(_list_and_describe)
        return [serialize_eks_node_group(ng) for ng in raw]

    async def _handle_get_eks_node_group(
        self: "AWSConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Get details for a specific EKS node group.

        Args:
            params: Required keys: cluster_name, node_group_name.
                    Optional keys: region.

        Returns:
            Serialized EKS node group.
        """
        client = self._get_client("eks", params.get("region"))
        cluster_name = params["cluster_name"]
        node_group_name = params["node_group_name"]

        def _describe() -> dict[str, Any]:
            response = client.describe_nodegroup(
                clusterName=cluster_name, nodegroupName=node_group_name
            )
            return response.get("nodegroup", {})

        raw = await asyncio.to_thread(_describe)
        return serialize_eks_node_group(raw)
