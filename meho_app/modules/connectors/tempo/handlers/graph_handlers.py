# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tempo Graph Handler Mixin.

Handles service dependency graph retrieval and trace-derived metrics.
"""

from typing import Any

from meho_app.modules.connectors.observability.time_range import TimeRange
from meho_app.modules.connectors.tempo.serializers import (
    serialize_service_graph,
    serialize_trace_metrics,
)


class GraphHandlerMixin:
    """Mixin for Tempo service graph and trace metrics handlers."""

    # These will be provided by TempoConnector (base class)
    async def _get_service_graphs(self) -> dict: ...

    async def _search_traces(self, params: dict) -> dict: ...

    async def _get_trace(self, trace_id: str) -> dict: ...

    async def _get_service_graph_handler(self, params: dict[str, Any]) -> dict:
        """
        Retrieve service dependency graph.

        Calls Tempo service graph API and serializes as nodes + edges tables.
        Handles 404/empty gracefully (metrics-generator not enabled).
        """
        response = await self._get_service_graphs()
        return serialize_service_graph(response)

    async def _get_trace_metrics_handler(self, params: dict[str, Any]) -> dict:
        """
        Derive per-service metrics from recent trace search results.

        Searches recent traces with a higher limit, then aggregates
        span counts, error counts, and durations by service.
        """
        time_range = TimeRange.from_relative(params.get("time_range", "1h"))

        search_params: dict[str, Any] = {
            "limit": 100,
            "start": str(int(time_range.start.timestamp())),
            "end": str(int(time_range.end.timestamp())),
        }

        # Optional service filter
        service_name = params.get("service_name")
        if service_name:
            search_params["tags"] = f'resource.service.name="{service_name}"'

        # Get trace summaries
        search_response = await self._search_traces(search_params)
        traces_raw = search_response.get("traces", [])

        # Aggregate per-service metrics from trace-level data
        aggregated: dict[str, dict[str, Any]] = {}

        for trace in traces_raw:
            trace.get("traceID", trace.get("traceId", ""))
            root_service = trace.get("rootServiceName", "unknown")
            duration_ms = trace.get("durationMs", 0)

            if "durationMs" not in trace and "duration" in trace:
                try:
                    duration_ms = int(trace["duration"]) / 1_000_000
                except (ValueError, TypeError):
                    duration_ms = 0

            span_count = trace.get("spanCount", 0)
            error_count = trace.get("errorCount", 0)

            if root_service not in aggregated:
                aggregated[root_service] = {
                    "span_count": 0,
                    "error_count": 0,
                    "durations": [],
                }

            aggregated[root_service]["span_count"] += span_count
            aggregated[root_service]["error_count"] += error_count
            aggregated[root_service]["durations"].append(float(duration_ms))

        return serialize_trace_metrics(aggregated)
