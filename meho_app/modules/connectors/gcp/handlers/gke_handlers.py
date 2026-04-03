# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GCP GKE Handlers (TASK-102)

Handlers for Google Kubernetes Engine operations.
"""

import asyncio
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.gcp.serializers import (
    serialize_cluster,
    serialize_node_pool,
)

if TYPE_CHECKING:
    from meho_app.modules.connectors.gcp.connector import GCPConnector

logger = get_logger(__name__)


class GKEHandlerMixin:
    """Mixin providing GKE operation handlers."""

    # Type hints for IDE support
    if TYPE_CHECKING:
        _container_client: Any
        project_id: str
        default_region: str
        default_zone: str

    def _get_cluster_parent(self: "GCPConnector", location: str | None = None) -> str:  # type: ignore[misc]
        """Get the parent path for cluster operations."""
        loc = location or "-"  # "-" means all locations
        return f"projects/{self.project_id}/locations/{loc}"

    def _get_cluster_name_path(  # type: ignore[misc]
        self: "GCPConnector", cluster_name: str, location: str | None = None
    ) -> str:
        """Get the full cluster name path."""
        loc = location or self.default_region
        return f"projects/{self.project_id}/locations/{loc}/clusters/{cluster_name}"

    # =========================================================================
    # CLUSTER OPERATIONS
    # =========================================================================

    async def _handle_list_clusters(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List GKE clusters."""
        location = params.get("location", "-")  # "-" for all locations

        parent = self._get_cluster_parent(location)

        response = await asyncio.to_thread(
            lambda: self._container_client.list_clusters(parent=parent)
        )

        return [serialize_cluster(c) for c in response.clusters]

    async def _handle_get_cluster(self: "GCPConnector", params: dict[str, Any]) -> dict[str, Any]:  # type: ignore[misc]
        """Get cluster details."""
        cluster_name = params["cluster_name"]
        location = params.get("location", self.default_region)

        name = self._get_cluster_name_path(cluster_name, location)

        cluster = await asyncio.to_thread(lambda: self._container_client.get_cluster(name=name))

        return serialize_cluster(cluster)

    async def _handle_get_cluster_health(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get cluster health status."""
        cluster_name = params["cluster_name"]
        location = params.get("location", self.default_region)

        name = self._get_cluster_name_path(cluster_name, location)

        cluster = await asyncio.to_thread(lambda: self._container_client.get_cluster(name=name))

        # Aggregate health information
        node_pool_health = []
        healthy_pools = 0

        for np in cluster.node_pools or []:
            status = np.status.name if np.status else "UNKNOWN"
            is_healthy = status in ("RUNNING", "RECONCILING")
            if is_healthy:
                healthy_pools += 1

            node_pool_health.append(
                {
                    "name": np.name,
                    "status": status,
                    "status_message": np.status_message,
                    "is_healthy": is_healthy,
                }
            )
        cluster_status = cluster.status.name if cluster.status else "UNKNOWN"

        return {
            "cluster_name": cluster.name,
            "location": cluster.location,
            "status": cluster_status,
            "status_message": cluster.status_message,
            "is_healthy": cluster_status == "RUNNING",
            "master_version": cluster.current_master_version,
            "node_count": cluster.current_node_count,
            "node_pools": node_pool_health,
            "healthy_node_pools": healthy_pools,
            "total_node_pools": len(node_pool_health),
            "conditions": [
                {
                    "code": c.code.name if c.code else None,
                    "message": c.message,
                }
                for c in (cluster.conditions or [])
            ],
        }

    # =========================================================================
    # NODE POOL OPERATIONS
    # =========================================================================

    async def _handle_list_node_pools(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List node pools in a cluster."""
        cluster_name = params["cluster_name"]
        location = params.get("location", self.default_region)

        parent = self._get_cluster_name_path(cluster_name, location)

        response = await asyncio.to_thread(
            lambda: self._container_client.list_node_pools(parent=parent)
        )

        return [serialize_node_pool(np) for np in response.node_pools]

    async def _handle_get_node_pool(self: "GCPConnector", params: dict[str, Any]) -> dict[str, Any]:  # type: ignore[misc]
        """Get node pool details."""
        cluster_name = params["cluster_name"]
        node_pool_name = params["node_pool_name"]
        location = params.get("location", self.default_region)

        name = f"{self._get_cluster_name_path(cluster_name, location)}/nodePools/{node_pool_name}"

        node_pool = await asyncio.to_thread(lambda: self._container_client.get_node_pool(name=name))

        return serialize_node_pool(node_pool)

    # =========================================================================
    # CLUSTER CREDENTIALS & OPERATIONS
    # =========================================================================

    async def _handle_get_cluster_credentials(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get cluster credentials info for kubectl configuration."""
        cluster_name = params["cluster_name"]
        location = params.get("location", self.default_region)

        name = self._get_cluster_name_path(cluster_name, location)

        cluster = await asyncio.to_thread(lambda: self._container_client.get_cluster(name=name))

        return {
            "cluster_name": cluster.name,
            "endpoint": cluster.endpoint,
            "ca_certificate": cluster.master_auth.cluster_ca_certificate
            if cluster.master_auth
            else None,
            "location": cluster.location,
            "project_id": self.project_id,
            "kubectl_command": (
                f"gcloud container clusters get-credentials {cluster.name} "
                f"--region {cluster.location} --project {self.project_id}"
            ),
        }

    async def _handle_list_cluster_operations(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List recent cluster operations."""
        location = params.get("location", "-")

        parent = self._get_cluster_parent(location)

        response = await asyncio.to_thread(
            lambda: self._container_client.list_operations(parent=parent)
        )

        operations = []
        for op in response.operations or []:
            operations.append(
                {
                    "name": op.name,
                    "operation_type": op.operation_type.name if op.operation_type else None,
                    "status": op.status.name if op.status else None,
                    "status_message": op.status_message,
                    "target_link": op.target_link,
                    "start_time": op.start_time,
                    "end_time": op.end_time,
                    "progress": {
                        "current": op.progress.current if op.progress else 0,
                        "total": op.progress.total if op.progress else 0,
                    }
                    if op.progress
                    else None,
                }
            )

        return operations
