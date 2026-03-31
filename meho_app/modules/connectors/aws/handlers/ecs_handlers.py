# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS ECS Handlers.

Handlers for ECS operations: clusters, services, and tasks.
Note: ECS list operations return ARNs only -- a describe call is always
needed to get resource details.
"""

import asyncio
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.aws.serializers import (
    serialize_ecs_cluster,
    serialize_ecs_service,
    serialize_ecs_task,
)

if TYPE_CHECKING:
    from meho_app.modules.connectors.aws.connector import AWSConnector

logger = get_logger(__name__)


class ECSHandlerMixin:
    """Mixin providing ECS operation handlers."""

    # =========================================================================
    # CLUSTER OPERATIONS
    # =========================================================================

    async def _handle_list_ecs_clusters(
        self: "AWSConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        List ECS clusters with full details.

        ECS list_clusters returns only ARNs, so we follow up with
        describe_clusters to get full details.

        Args:
            params: Optional keys: region.

        Returns:
            List of serialized ECS clusters.
        """
        client = self._get_client("ecs", params.get("region"))

        def _list_and_describe() -> list[dict[str, Any]]:
            paginator = client.get_paginator("list_clusters")
            cluster_arns: list[str] = []
            for page in paginator.paginate():
                cluster_arns.extend(page.get("clusterArns", []))

            if not cluster_arns:
                return []

            # describe_clusters accepts up to 100 ARNs at a time
            results: list[dict[str, Any]] = []
            for i in range(0, len(cluster_arns), 100):
                batch = cluster_arns[i : i + 100]
                response = client.describe_clusters(clusters=batch)
                results.extend(response.get("clusters", []))
            return results

        raw = await asyncio.to_thread(_list_and_describe)
        return [serialize_ecs_cluster(c) for c in raw]

    # =========================================================================
    # SERVICE OPERATIONS
    # =========================================================================

    async def _handle_list_ecs_services(
        self: "AWSConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        List ECS services in a cluster with full details.

        Requires cluster param (name or ARN -- Pitfall 3).

        Args:
            params: Required keys: cluster. Optional keys: region.

        Returns:
            List of serialized ECS services.
        """
        client = self._get_client("ecs", params.get("region"))
        cluster = params["cluster"]

        def _list_and_describe() -> list[dict[str, Any]]:
            paginator = client.get_paginator("list_services")
            service_arns: list[str] = []
            for page in paginator.paginate(cluster=cluster):
                service_arns.extend(page.get("serviceArns", []))

            if not service_arns:
                return []

            # describe_services accepts up to 10 services at a time
            results: list[dict[str, Any]] = []
            for i in range(0, len(service_arns), 10):
                batch = service_arns[i : i + 10]
                response = client.describe_services(cluster=cluster, services=batch)
                results.extend(response.get("services", []))
            return results

        raw = await asyncio.to_thread(_list_and_describe)
        return [serialize_ecs_service(s) for s in raw]

    async def _handle_get_ecs_service(
        self: "AWSConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Get details for a specific ECS service.

        Args:
            params: Required keys: cluster, service_name. Optional keys: region.

        Returns:
            Serialized ECS service.
        """
        client = self._get_client("ecs", params.get("region"))
        cluster = params["cluster"]
        service_name = params["service_name"]

        def _describe() -> dict[str, Any]:
            response = client.describe_services(
                cluster=cluster, services=[service_name]
            )
            services = response.get("services", [])
            return services[0] if services else {}

        raw = await asyncio.to_thread(_describe)
        return serialize_ecs_service(raw)

    # =========================================================================
    # TASK OPERATIONS
    # =========================================================================

    async def _handle_list_ecs_tasks(
        self: "AWSConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        List ECS tasks in a cluster, optionally filtered by service.

        Args:
            params: Required keys: cluster. Optional keys: service_name, region.

        Returns:
            List of serialized ECS tasks.
        """
        client = self._get_client("ecs", params.get("region"))
        cluster = params["cluster"]
        service_name = params.get("service_name")

        def _list_and_describe() -> list[dict[str, Any]]:
            paginator = client.get_paginator("list_tasks")
            paginate_kwargs: dict[str, Any] = {"cluster": cluster}
            if service_name:
                paginate_kwargs["serviceName"] = service_name

            task_arns: list[str] = []
            for page in paginator.paginate(**paginate_kwargs):
                task_arns.extend(page.get("taskArns", []))

            if not task_arns:
                return []

            # describe_tasks accepts up to 100 tasks at a time
            results: list[dict[str, Any]] = []
            for i in range(0, len(task_arns), 100):
                batch = task_arns[i : i + 100]
                response = client.describe_tasks(cluster=cluster, tasks=batch)
                results.extend(response.get("tasks", []))
            return results

        raw = await asyncio.to_thread(_list_and_describe)
        return [serialize_ecs_task(t) for t in raw]

    async def _handle_get_ecs_task(
        self: "AWSConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Get details for a specific ECS task.

        Args:
            params: Required keys: cluster, task_arn. Optional keys: region.

        Returns:
            Serialized ECS task.
        """
        client = self._get_client("ecs", params.get("region"))
        cluster = params["cluster"]
        task_arn = params["task_arn"]

        def _describe() -> dict[str, Any]:
            response = client.describe_tasks(cluster=cluster, tasks=[task_arn])
            tasks = response.get("tasks", [])
            return tasks[0] if tasks else {}

        raw = await asyncio.to_thread(_describe)
        return serialize_ecs_task(raw)
