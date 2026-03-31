# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Loki Connector.

Extends ObservabilityHTTPConnector for Loki API access with handler
mixins for log search, error investigation, volume, and label discovery.

Example:
    connector = LokiConnector(
        connector_id="abc123",
        config={
            "base_url": "http://loki:3100",
            "auth_type": "none",
        },
        credentials={},
    )

    async with connector:
        ok = await connector.test_connection()
        result = await connector.execute("search_logs", {"namespace": "default"})
"""

import time
from collections.abc import Callable
from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.base import OperationDefinition, OperationResult, TypeDefinition
from meho_app.modules.connectors.loki.handlers import (
    DiscoveryHandlerMixin,
    LogSearchHandlerMixin,
    QueryHandlerMixin,
)
from meho_app.modules.connectors.loki.operations import LOKI_OPERATIONS
from meho_app.modules.connectors.observability.base import ObservabilityHTTPConnector

logger = get_logger(__name__)


class LokiConnector(
    ObservabilityHTTPConnector,
    LogSearchHandlerMixin,
    DiscoveryHandlerMixin,
    QueryHandlerMixin,
):
    """
    Loki connector using httpx for native Loki HTTP API access.

    Provides 8 pre-defined operations across three categories:
    - Log search (search_logs, get_error_logs, get_log_context, get_log_volume, get_log_patterns) -- 5 ops
    - Label discovery (list_labels, list_label_values) -- 2 ops
    - Escape hatch (query_logql) -- 1 op

    No topology entities -- Loki is a query engine for logs about entities
    that other connectors (K8s, Prometheus) already track.
    """

    def __init__(
        self,
        connector_id: str,
        config: dict[str, Any],
        credentials: dict[str, Any],
    ):
        super().__init__(connector_id, config, credentials)

        # Loki version (detected on test_connection)
        self.loki_version: str | None = None

        # Build operation dispatch table from handler mixins
        self._operation_handlers: dict[str, Callable] = self._build_operation_handlers()

    async def test_connection(self) -> bool:
        """
        Test connection via /loki/api/v1/status/buildinfo with /ready fallback.

        Tries buildinfo first (returns version info). If that fails,
        falls back to /ready which returns HTTP 200 with text "ready".
        Stores version if available.
        """
        # Try buildinfo first -- provides version info
        try:
            data = await self._get("/loki/api/v1/status/buildinfo")
            version = data.get("version")
            if not version:
                version = data.get("data", {}).get("version")
            if version:
                self.loki_version = version
            logger.info(
                f"Loki connection verified via buildinfo: {self.base_url} "
                f"(version: {self.loki_version})"
            )
            return True
        except Exception as e:
            logger.debug(f"Loki buildinfo endpoint failed, trying /ready: {e}")

        # Fallback: /ready endpoint (returns HTTP 200 with text "ready")
        try:
            if not self._client:
                await self.connect()
            assert self._client is not None  # noqa: S101 -- runtime assertion for invariant checking
            response = await self._client.get("/ready")
            response.raise_for_status()
            logger.info(f"Loki connection verified via /ready: {self.base_url}")
            return True
        except Exception as e:
            logger.warning(f"Loki connection test failed: {e}")
            return False

    async def execute(
        self,
        operation_id: str,
        parameters: dict[str, Any],
    ) -> OperationResult:
        """Execute a Loki operation."""
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
            # Log Search (5)
            "search_logs": self._search_logs,
            "get_error_logs": self._get_error_logs,
            "get_log_context": self._get_log_context,
            "get_log_volume": self._get_log_volume,
            "get_log_patterns": self._get_log_patterns,
            # Discovery (2)
            "list_labels": self._list_labels,
            "list_label_values": self._list_label_values,
            # Query (1)
            "query_logql": self._query_logql,
        }

    def get_operations(self) -> list[OperationDefinition]:
        """Get Loki operations for registration."""
        return list(LOKI_OPERATIONS)

    def get_types(self) -> list[TypeDefinition]:
        """Get Loki types for registration.

        Returns empty list -- Loki has no topology entities.
        """
        return []

    # =========================================================================
    # LOKI API QUERY HELPERS
    # =========================================================================

    async def _query_range_log(self, logql: str, params: dict) -> dict:
        """
        Execute Loki range log query via /loki/api/v1/query_range.

        Args:
            logql: LogQL query string
            params: Additional parameters (limit, start, end, direction, step)

        Returns:
            Full response data from Loki.
        """
        query_params: dict[str, Any] = {"query": logql}
        if "limit" in params:
            query_params["limit"] = params["limit"]
        if "start" in params:
            query_params["start"] = params["start"]
        if "end" in params:
            query_params["end"] = params["end"]
        query_params["direction"] = params.get("direction", "backward")
        if "step" in params:
            query_params["step"] = params["step"]

        data = await self._get("/loki/api/v1/query_range", params=query_params)
        return data

    async def _query_log(self, logql: str, params: dict) -> dict:
        """
        Execute Loki instant log query via /loki/api/v1/query.

        Args:
            logql: LogQL query string
            params: Additional parameters (limit, time, direction)

        Returns:
            Full response data from Loki.
        """
        query_params: dict[str, Any] = {"query": logql}
        if "limit" in params:
            query_params["limit"] = params["limit"]
        if "time" in params:
            query_params["time"] = params["time"]
        query_params["direction"] = params.get("direction", "backward")

        data = await self._get("/loki/api/v1/query", params=query_params)
        return data

    async def _get_labels(self, start: str | None = None, end: str | None = None) -> list:
        """
        Get available log labels via /loki/api/v1/labels.

        Args:
            start: Optional start timestamp (epoch seconds or RFC3339)
            end: Optional end timestamp (epoch seconds or RFC3339)

        Returns:
            List of label names.
        """
        params: dict[str, Any] = {}
        if start:
            params["start"] = start
        if end:
            params["end"] = end

        data = await self._get("/loki/api/v1/labels", params=params or None)
        return data.get("data", [])

    async def _get_label_values(
        self,
        label: str,
        start: str | None = None,
        end: str | None = None,
    ) -> list:
        """
        Get values for a specific label via /loki/api/v1/label/{label}/values.

        Args:
            label: Label name to query values for
            start: Optional start timestamp
            end: Optional end timestamp

        Returns:
            List of label values.
        """
        params: dict[str, Any] = {}
        if start:
            params["start"] = start
        if end:
            params["end"] = end

        data = await self._get(
            f"/loki/api/v1/label/{label}/values",
            params=params or None,
        )
        return data.get("data", [])
