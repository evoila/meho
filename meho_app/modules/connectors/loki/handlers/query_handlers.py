# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Loki Query Handler Mixin.

Escape hatch for arbitrary LogQL queries. The operation is marked as
requiring WRITE trust level (operator approval) in the OperationDefinition.
Trust enforcement happens at the agent level, not here.
"""

import re
from typing import Any

from meho_app.modules.connectors.loki.serializers import serialize_log_streams
from meho_app.modules.connectors.observability.time_range import TimeRange

# Patterns that indicate a metric query (returns numeric data, not log lines)
_METRIC_FUNCTIONS = re.compile(
    r"^\s*(count_over_time|rate|bytes_rate|bytes_over_time|sum|avg|min|max|"
    r"stddev|stdvar|quantile_over_time|first_over_time|last_over_time|"
    r"absent_over_time|topk|bottomk|sort|sort_desc)\s*\(",
    re.IGNORECASE,
)


class QueryHandlerMixin:
    """Mixin for Loki LogQL escape hatch handler."""

    # These will be provided by LokiConnector (base class)
    async def _query_range_log(self, logql: str, params: dict) -> dict: ...

    async def _query_logql(self, params: dict[str, Any]) -> dict:
        """
        Execute arbitrary LogQL query.

        Determines if the query is a metric query or log query and routes
        appropriately. Both use the same Loki query_range endpoint.
        """
        query = params["query"]
        time_range_str = params.get("time_range", "1h")
        limit = params.get("limit", 100)

        time_range = TimeRange.from_relative(time_range_str)

        is_metric = bool(_METRIC_FUNCTIONS.match(query))

        query_params: dict[str, Any] = {
            "start": str(int(time_range.start.timestamp())),
            "end": str(int(time_range.end.timestamp())),
        }

        if is_metric:
            # Metric queries need step for range queries
            query_params["step"] = time_range.step
        else:
            # Log queries use limit and direction
            query_params["limit"] = limit
            query_params["direction"] = "backward"

        response = await self._query_range_log(query, query_params)

        if is_metric:
            # Return raw metric result with metadata
            result_data = response.get("data", {}).get("result", [])
            return {
                "query": query,
                "query_type": "metric",
                "time_range": time_range_str,
                "result_type": response.get("data", {}).get("resultType", "matrix"),
                "result_count": len(result_data),
                "result": result_data,
            }
        else:
            # Serialize log results for structured output
            serialized = serialize_log_streams(response, limit)
            serialized["query"] = query
            serialized["query_type"] = "log"
            serialized["time_range"] = time_range_str
            return serialized
