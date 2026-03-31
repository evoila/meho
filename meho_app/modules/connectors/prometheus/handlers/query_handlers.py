# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Prometheus Query Handler Mixin.

Escape hatch for arbitrary PromQL queries. The operation is marked as
requiring WRITE trust level (operator approval) in the OperationDefinition.
Trust enforcement happens at the agent level, not here.
"""

from typing import Any

from meho_app.modules.connectors.observability.time_range import TimeRange


class QueryHandlerMixin:
    """Mixin for Prometheus PromQL escape hatch handler."""

    # These will be provided by PrometheusConnector (base class)
    async def _query_range(self, query: str, time_range: TimeRange) -> list: ...

    async def _query_instant(self, query: str) -> list: ...

    async def _query_promql(self, params: dict[str, Any]) -> dict:
        """
        Execute arbitrary PromQL query.

        Uses instant query if instant=True or no time_range provided.
        Otherwise uses range query with the specified time_range.
        """
        query = params["query"]
        time_range_str = params.get("time_range")
        instant = params.get("instant", False)

        if instant or not time_range_str:
            # Instant query
            result = await self._query_instant(query)
            return {
                "query": query,
                "query_type": "instant",
                "result_type": "vector",
                "result_count": len(result),
                "result": result,
            }
        else:
            # Range query
            time_range = TimeRange.from_relative(time_range_str)
            result = await self._query_range(query, time_range)
            return {
                "query": query,
                "query_type": "range",
                "time_range": time_range_str,
                "result_type": "matrix",
                "result_count": len(result),
                "result": result,
            }
