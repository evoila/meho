# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tempo Connector.

Extends ObservabilityHTTPConnector for Tempo API access with handler
mixins for distributed trace search, service graph, tag discovery, and TraceQL.

Example:
    connector = TempoConnector(
        connector_id="abc123",
        config={
            "base_url": "http://tempo:3200",
            "auth_type": "none",
        },
        credentials={},
    )

    async with connector:
        ok = await connector.test_connection()
        result = await connector.execute("search_traces", {"service_name": "frontend"})
"""

import time
from collections.abc import Callable
from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.base import OperationDefinition, OperationResult, TypeDefinition
from meho_app.modules.connectors.observability.base import ObservabilityHTTPConnector
from meho_app.modules.connectors.tempo.handlers import (
    DiscoveryHandlerMixin,
    GraphHandlerMixin,
    QueryHandlerMixin,
    TraceHandlerMixin,
)
from meho_app.modules.connectors.tempo.operations import TEMPO_OPERATIONS

logger = get_logger(__name__)


class TempoConnector(
    ObservabilityHTTPConnector,
    TraceHandlerMixin,
    DiscoveryHandlerMixin,
    GraphHandlerMixin,
    QueryHandlerMixin,
):
    """
    Tempo connector using httpx for native Tempo HTTP API access.

    Provides 10 pre-defined operations across four categories:
    - Trace search (search_traces, get_trace, get_span_details, get_slow_traces, get_error_traces) -- 5 ops
    - Service graph (get_service_graph, get_trace_metrics) -- 2 ops
    - Tag discovery (list_tags, list_tag_values) -- 2 ops
    - Escape hatch (query_traceql) -- 1 op

    No topology entities -- Tempo is a query engine for traces about entities
    that other connectors (K8s, Prometheus) already track.
    """

    def __init__(
        self,
        connector_id: str,
        config: dict[str, Any],
        credentials: dict[str, Any],
    ):
        super().__init__(connector_id, config, credentials)

        # Tempo version (detected on test_connection)
        self.tempo_version: str | None = None

        # Multi-tenancy: optional org_id for X-Scope-OrgID header
        self._org_id: str | None = config.get("org_id")

        # Build operation dispatch table from handler mixins
        self._operation_handlers: dict[str, Callable] = self._build_operation_handlers()

    # =========================================================================
    # MULTI-TENANCY: Inject X-Scope-OrgID header
    # =========================================================================

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """Execute GET request with optional X-Scope-OrgID header for multi-tenancy."""
        if not self._client:
            await self.connect()
        assert self._client is not None  # noqa: S101 -- runtime assertion for invariant checking

        headers: dict[str, str] = {}
        if self._org_id:
            headers["X-Scope-OrgID"] = self._org_id

        response = await self._client.get(path, params=params, headers=headers)
        response.raise_for_status()
        return response.json()

    # =========================================================================
    # CONNECTION & EXECUTION
    # =========================================================================

    async def test_connection(self) -> bool:
        """
        Test connection via /api/status/buildinfo with /ready fallback.

        Tries buildinfo first (returns version info). If that fails,
        falls back to /ready which returns HTTP 200 with text "ready".
        Stores version if available.
        """
        # Try buildinfo first -- provides version info
        try:
            data = await self._get("/api/status/buildinfo")
            version = data.get("version")
            if not version:
                version = data.get("data", {}).get("version")
            if version:
                self.tempo_version = version
            logger.info(
                f"Tempo connection verified via buildinfo: {self.base_url} "
                f"(version: {self.tempo_version})"
            )
            return True
        except Exception as e:
            logger.debug(f"Tempo buildinfo endpoint failed, trying /ready: {e}")

        # Fallback: /ready endpoint (returns HTTP 200 with text "ready")
        try:
            if not self._client:
                await self.connect()
            assert self._client is not None  # noqa: S101 -- runtime assertion for invariant checking

            headers: dict[str, str] = {}
            if self._org_id:
                headers["X-Scope-OrgID"] = self._org_id

            response = await self._client.get("/ready", headers=headers)
            response.raise_for_status()
            logger.info(f"Tempo connection verified via /ready: {self.base_url}")
            return True
        except Exception as e:
            logger.warning(f"Tempo connection test failed: {e}")
            return False

    async def execute(
        self,
        operation_id: str,
        parameters: dict[str, Any],
    ) -> OperationResult:
        """Execute a Tempo operation."""
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
            # Traces (5)
            "search_traces": self._search_traces_handler,
            "get_trace": self._get_trace_handler,
            "get_span_details": self._get_span_details_handler,
            "get_slow_traces": self._get_slow_traces_handler,
            "get_error_traces": self._get_error_traces_handler,
            # Graph (2)
            "get_service_graph": self._get_service_graph_handler,
            "get_trace_metrics": self._get_trace_metrics_handler,
            # Discovery (2)
            "list_tags": self._list_tags_handler,
            "list_tag_values": self._list_tag_values_handler,
            # Query (1)
            "query_traceql": self._query_traceql_handler,
        }

    def get_operations(self) -> list[OperationDefinition]:
        """Get Tempo operations for registration."""
        return list(TEMPO_OPERATIONS)

    def get_types(self) -> list[TypeDefinition]:
        """Get Tempo types for registration.

        Returns empty list -- Tempo has no topology entities.
        """
        return []

    # =========================================================================
    # TEMPO API QUERY HELPERS
    # =========================================================================

    async def _search_traces(self, params: dict) -> dict:
        """
        Search traces via GET /api/search.

        Args:
            params: Query parameters -- q (TraceQL), tags, minDuration,
                    maxDuration, limit, start, end, spss (spans per span set).

        Returns:
            Full response data from Tempo search API.
        """
        query_params: dict[str, Any] = {}
        for key in ("q", "tags", "minDuration", "maxDuration", "limit", "start", "end", "spss"):
            if key in params:
                query_params[key] = params[key]

        data = await self._get("/api/search", params=query_params or None)
        return data

    async def _get_trace(self, trace_id: str) -> dict:
        """
        Get a single trace via GET /api/traces/{trace_id}.

        Args:
            trace_id: Full trace ID (hex string).

        Returns:
            Full OTLP JSON for the trace.
        """
        data = await self._get(f"/api/traces/{trace_id}")
        return data

    async def _get_service_graphs(self) -> dict:
        """
        Get service graph data via GET /api/metrics/service_graph.

        Requires metrics-generator in Tempo. If the endpoint returns 404,
        returns empty result instead of failing.

        Returns:
            Service graph data (nodes and edges) or empty dict if unavailable.
        """
        try:
            data = await self._get("/api/metrics/service_graph")
            return data
        except Exception as e:
            # Service graph requires metrics-generator which may not be enabled
            if hasattr(e, "response") and hasattr(e.response, "status_code"):  # noqa: SIM102 -- readability preferred over collapse
                if e.response.status_code == 404:
                    logger.info(
                        "Tempo service graph endpoint not available (metrics-generator not enabled)"
                    )
                    return {}
            logger.debug(f"Tempo service graph request failed: {e}")
            return {}

    async def _get_tags(self) -> list:
        """
        Get available tag names via GET /api/search/tags.

        Returns:
            List of tag name strings.
        """
        data = await self._get("/api/search/tags")
        return data.get("tagNames", [])

    async def _get_tag_values(self, tag: str) -> list:
        """
        Get values for a specific tag via GET /api/search/tag/{tag}/values.

        Args:
            tag: Tag name to query values for.

        Returns:
            List of tag value strings.
        """
        data = await self._get(f"/api/search/tag/{tag}/values")
        return data.get("tagValues", [])
