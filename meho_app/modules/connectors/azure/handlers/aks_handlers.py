# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Azure AKS handler mixin (Phase 92).

Handlers for Azure Kubernetes Service operations: clusters, node pools,
credentials, and upgrade profiles. Uses native async Azure SDK clients.
"""

from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.azure.helpers import (
    _safe_list,
)
from meho_app.modules.connectors.azure.serializers import (
    serialize_azure_aks_cluster,
    serialize_azure_node_pool,
)

if TYPE_CHECKING:
    from meho_app.modules.connectors.azure.connector import AzureConnector

logger = get_logger(__name__)


class AKSHandlerMixin:
    """Mixin providing Azure AKS operation handlers.

    Covers AKS cluster management, node pools, credentials, and
    upgrade profiles. All methods use native async Azure SDK calls.
    """

    if TYPE_CHECKING:
        _container_client: Any
        _subscription_id: str
        _resource_group_filter: str | None

    # =========================================================================
    # CLUSTER OPERATIONS
    # =========================================================================

    async def _handle_list_azure_aks_clusters(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List AKS clusters.

        If resource_group is provided, lists clusters in that group.
        Otherwise falls back to resource_group_filter, then lists all.
        """
        resource_group = params.get("resource_group") or self._resource_group_filter

        results: list[dict[str, Any]] = []

        if resource_group:
            async for cluster in self._container_client.managed_clusters.list_by_resource_group(
                resource_group
            ):
                results.append(serialize_azure_aks_cluster(cluster))
        else:
            async for cluster in self._container_client.managed_clusters.list():
                results.append(serialize_azure_aks_cluster(cluster))

        return results

    async def _handle_get_azure_aks_cluster(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get AKS cluster details."""
        resource_group = params["resource_group"]
        cluster_name = params["cluster_name"]

        cluster = await self._container_client.managed_clusters.get(
            resource_group_name=resource_group,
            resource_name=cluster_name,
        )
        return serialize_azure_aks_cluster(cluster)

    async def _handle_get_azure_aks_cluster_health(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get AKS cluster health summary.

        Composite operation that extracts health-relevant information
        from the cluster details: provisioning state, power state,
        and per-pool health summary.
        """
        resource_group = params["resource_group"]
        cluster_name = params["cluster_name"]

        cluster = await self._container_client.managed_clusters.get(
            resource_group_name=resource_group,
            resource_name=cluster_name,
        )

        # Extract power state
        power_state = None
        if hasattr(cluster, "power_state") and cluster.power_state:
            code = getattr(cluster.power_state, "code", None)
            if code and hasattr(code, "value"):
                power_state = code.value
            elif code:
                power_state = str(code)

        # Summarize agent pools
        pool_summaries: list[dict[str, Any]] = []
        for pool in _safe_list(getattr(cluster, "agent_pool_profiles", None)):
            pool_power = None
            if hasattr(pool, "power_state") and pool.power_state:
                pc = getattr(pool.power_state, "code", None)
                if pc and hasattr(pc, "value"):
                    pool_power = pc.value
                elif pc:
                    pool_power = str(pc)

            pool_summaries.append(
                {
                    "name": pool.name,
                    "count": getattr(pool, "count", None),
                    "provisioning_state": getattr(pool, "provisioning_state", None),
                    "power_state": pool_power,
                }
            )

        return {
            "cluster_name": cluster.name,
            "resource_group": resource_group,
            "provisioning_state": cluster.provisioning_state,
            "power_state": power_state,
            "kubernetes_version": cluster.kubernetes_version,
            "fqdn": cluster.fqdn,
            "agent_pools": pool_summaries,
        }

    # =========================================================================
    # NODE POOL OPERATIONS
    # =========================================================================

    async def _handle_list_azure_aks_node_pools(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List node pools for an AKS cluster."""
        resource_group = params["resource_group"]
        cluster_name = params["cluster_name"]

        results: list[dict[str, Any]] = []
        async for pool in self._container_client.agent_pools.list(
            resource_group_name=resource_group,
            resource_name=cluster_name,
        ):
            results.append(serialize_azure_node_pool(pool))

        return results

    async def _handle_get_azure_aks_node_pool(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get node pool details."""
        resource_group = params["resource_group"]
        cluster_name = params["cluster_name"]
        pool_name = params["pool_name"]

        pool = await self._container_client.agent_pools.get(
            resource_group_name=resource_group,
            resource_name=cluster_name,
            agent_pool_name=pool_name,
        )
        return serialize_azure_node_pool(pool)

    # =========================================================================
    # CREDENTIALS & UPGRADES
    # =========================================================================

    async def _handle_get_azure_aks_credentials(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get AKS cluster credential info.

        Extracts kubeconfig-relevant information from the cluster details.
        Does not return secrets -- provides metadata for kubeconfig construction.
        """
        resource_group = params["resource_group"]
        cluster_name = params["cluster_name"]

        cluster = await self._container_client.managed_clusters.get(
            resource_group_name=resource_group,
            resource_name=cluster_name,
        )

        aad_profile = None
        if hasattr(cluster, "aad_profile") and cluster.aad_profile:
            aad_profile = {
                "managed": getattr(cluster.aad_profile, "managed", None),
                "enable_azure_rbac": getattr(cluster.aad_profile, "enable_azure_rbac", None),
                "tenant_id": getattr(cluster.aad_profile, "tenant_id", None),
            }

        return {
            "cluster_name": cluster.name,
            "resource_group": resource_group,
            "fqdn": cluster.fqdn,
            "private_fqdn": getattr(cluster, "private_fqdn", None),
            "api_server_access_profile": {
                "authorized_ip_ranges": list(
                    cluster.api_server_access_profile.authorized_ip_ranges or []
                )
                if cluster.api_server_access_profile
                else [],
                "enable_private_cluster": getattr(
                    cluster.api_server_access_profile, "enable_private_cluster", None
                )
                if cluster.api_server_access_profile
                else None,
            },
            "aad_profile": aad_profile,
            "node_resource_group": getattr(cluster, "node_resource_group", None),
        }

    async def _handle_list_azure_aks_upgrades(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """List available AKS cluster upgrades."""
        resource_group = params["resource_group"]
        cluster_name = params["cluster_name"]

        profile = await self._container_client.managed_clusters.get_upgrade_profile(
            resource_group_name=resource_group,
            resource_name=cluster_name,
        )

        # Parse control plane upgrade profile
        control_plane = None
        if profile.control_plane_profile:
            cp = profile.control_plane_profile
            upgrades = []
            for u in _safe_list(getattr(cp, "upgrades", None)):
                upgrades.append(
                    {
                        "kubernetes_version": getattr(u, "kubernetes_version", None),
                        "is_preview": getattr(u, "is_preview", None),
                    }
                )
            control_plane = {
                "kubernetes_version": cp.kubernetes_version,
                "upgrades": upgrades,
            }

        # Parse agent pool upgrade profiles
        agent_pools: list[dict[str, Any]] = []
        for ap in _safe_list(getattr(profile, "agent_pool_profiles", None)):
            ap_upgrades = []
            for u in _safe_list(getattr(ap, "upgrades", None)):
                ap_upgrades.append(
                    {
                        "kubernetes_version": getattr(u, "kubernetes_version", None),
                        "is_preview": getattr(u, "is_preview", None),
                    }
                )
            agent_pools.append(
                {
                    "name": ap.name,
                    "kubernetes_version": ap.kubernetes_version,
                    "os_type": str(ap.os_type) if ap.os_type else None,
                    "upgrades": ap_upgrades,
                }
            )

        return {
            "cluster_name": cluster_name,
            "resource_group": resource_group,
            "control_plane_profile": control_plane,
            "agent_pool_profiles": agent_pools,
        }
