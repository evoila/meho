# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Prometheus Connector.

Extends ObservabilityHTTPConnector for Prometheus API access with handler
mixins for infrastructure, service, discovery, and query operations.

Example:
    connector = PrometheusConnector(
        connector_id="abc123",
        config={
            "base_url": "http://prometheus:9090",
            "auth_type": "none",
        },
        credentials={},
    )

    async with connector:
        ok = await connector.test_connection()
        result = await connector.execute("list_targets", {})
"""

import time
from collections.abc import Callable
from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.base import (
    OperationDefinition,
    OperationResult,
    TypeDefinition,
)
from meho_app.modules.connectors.observability.base import ObservabilityHTTPConnector
from meho_app.modules.connectors.observability.time_range import TimeRange
from meho_app.modules.connectors.prometheus.handlers import (
    DiscoveryHandlerMixin,
    InfrastructureHandlerMixin,
    QueryHandlerMixin,
    ServiceHandlerMixin,
)
from meho_app.modules.connectors.prometheus.operations import PROMETHEUS_OPERATIONS
from meho_app.modules.connectors.prometheus.types import PROMETHEUS_TYPES

logger = get_logger(__name__)


class PrometheusConnector(
    ObservabilityHTTPConnector,
    InfrastructureHandlerMixin,
    ServiceHandlerMixin,
    DiscoveryHandlerMixin,
    QueryHandlerMixin,
):
    """
    Prometheus connector using httpx for native Prometheus HTTP API access.

    Provides 14 pre-defined operations across four categories:
    - Infrastructure metrics (CPU, memory, disk, network) -- 8 ops
    - Service RED metrics (rate, errors, duration) -- 1 op
    - Discovery (targets, metrics, alerts, rules) -- 4 ops
    - Escape hatch (query_promql) -- 1 op
    """

    def __init__(
        self,
        connector_id: str,
        config: dict[str, Any],
        credentials: dict[str, Any],
    ) -> None:
        super().__init__(connector_id, config, credentials)

        # Prometheus version (detected on test_connection)
        self.prometheus_version: str | None = None

        # Build operation dispatch table from handler mixins
        self._operation_handlers: dict[str, Callable] = self._build_operation_handlers()

    async def test_connection(self) -> bool:
        """
        Test connection via /api/v1/status/buildinfo.

        Confirms auth, reachability, and returns Prometheus version.
        """
        try:
            data = await self._get("/api/v1/status/buildinfo")
            if data.get("status") == "success":
                self.prometheus_version = data.get("data", {}).get("version")
                logger.info(
                    f"Prometheus connection verified: {self.base_url} "
                    f"(version: {self.prometheus_version})"
                )
                return True
            return False
        except Exception as e:
            logger.warning(f"Prometheus connection test failed: {e}")
            return False

    async def _execute_operation(
        self,
        operation_id: str,
        parameters: dict[str, Any],
    ) -> OperationResult:
        """Execute a Prometheus operation."""
        start_time = time.time()

        if not self._is_connected:
            await self.connect()

        handler = self._operation_handlers.get(operation_id)
        if not handler:
            return OperationResult(
                success=False,
                error=f"Unknown operation: {operation_id}",
                error_code="NOT_FOUND",
                operation_id=operation_id,
            )

        try:
            result = await handler(parameters)
            duration_ms = (time.time() - start_time) * 1000

            logger.info(f"{operation_id}: completed in {duration_ms:.1f}ms")

            return OperationResult(
                success=True,
                data=result,
                operation_id=operation_id,
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.error(f"{operation_id} failed: {e}", exc_info=True)

            error_code = self._map_http_error(e)

            return OperationResult(
                success=False,
                error=str(e),
                error_code=error_code,
                operation_id=operation_id,
                duration_ms=duration_ms,
            )

    def _build_operation_handlers(self) -> dict[str, Callable]:
        """Map operation IDs to handler methods from mixins."""
        return {
            # Infrastructure (8)
            "get_pod_cpu": self._get_pod_cpu,
            "get_namespace_cpu": self._get_namespace_cpu,
            "get_node_cpu": self._get_node_cpu,
            "get_pod_memory": self._get_pod_memory,
            "get_namespace_memory": self._get_namespace_memory,
            "get_node_memory": self._get_node_memory,
            "get_disk_usage": self._get_disk_usage,
            "get_network_io": self._get_network_io,
            # Service (1)
            "get_red_metrics": self._get_red_metrics,
            # Discovery (4)
            "list_targets": self._list_targets,
            "discover_metrics": self._discover_metrics,
            "list_alerts": self._list_alerts,
            "list_alert_rules": self._list_alert_rules,
            # Query (1)
            "query_promql": self._query_promql,
        }

    def get_operations(self) -> list[OperationDefinition]:
        """Get Prometheus operations for registration."""
        return list(PROMETHEUS_OPERATIONS)

    def get_types(self) -> list[TypeDefinition]:
        """Get Prometheus types for registration."""
        return PROMETHEUS_TYPES

    # =========================================================================
    # QUERY HELPERS
    # =========================================================================

    async def _query_range(self, query: str, time_range: TimeRange) -> list:
        """
        Execute Prometheus range query.

        Args:
            query: PromQL query string
            time_range: TimeRange with start, end, step

        Returns:
            List of result series from data.result
        """
        params = time_range.to_prometheus_params()
        params["query"] = query
        data = await self._get("/api/v1/query_range", params=params)
        return list(data.get("data", {}).get("result", []))

    async def _query_instant(self, query: str) -> list:
        """
        Execute Prometheus instant query.

        Args:
            query: PromQL query string

        Returns:
            List of result series from data.result
        """
        data = await self._get("/api/v1/query", params={"query": query})
        return list(data.get("data", {}).get("result", []))
