# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tempo Query Handler Mixin.

Escape hatch for arbitrary TraceQL queries. The operation is marked as
requiring WRITE trust level (operator approval) in the OperationDefinition.
Trust enforcement happens at the agent level, not here.
"""

from typing import Any

from meho_app.modules.connectors.observability.time_range import TimeRange
from meho_app.modules.connectors.tempo.serializers import serialize_trace_search


class QueryHandlerMixin:
    """Mixin for Tempo TraceQL escape hatch handler."""

    # These will be provided by TempoConnector (base class)
    async def _search_traces(self, params: dict) -> dict: ...  # type: ignore[empty-body]

    async def _query_traceql_handler(self, params: dict[str, Any]) -> dict:
        """
        Execute arbitrary TraceQL query.

        Passes the raw TraceQL expression to Tempo search API via the `q` parameter.
        Serializes results using the standard trace search serializer.
        """
        traceql = params["traceql"]
        time_range_str = params.get("time_range", "1h")
        limit = params.get("limit", 20)

        time_range = TimeRange.from_relative(time_range_str)

        search_params: dict[str, Any] = {
            "q": traceql,
            "limit": limit,
            "start": str(int(time_range.start.timestamp())),
            "end": str(int(time_range.end.timestamp())),
        }

        response = await self._search_traces(search_params)
        result = serialize_trace_search(response, limit)
        result["query"] = traceql
        result["query_type"] = "traceql"
        result["time_range"] = time_range_str
        return result
