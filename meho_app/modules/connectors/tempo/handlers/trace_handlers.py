# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tempo Trace Handler Mixin.

Handles trace search, full trace retrieval, span detail progressive disclosure,
and convenience operations (slow traces, error traces).

All handlers build Tempo search parameters internally -- the agent provides
named parameters and the handler maps them to Tempo API query params.
"""

from typing import Any

from meho_app.modules.connectors.observability.time_range import TimeRange
from meho_app.modules.connectors.tempo.serializers import (
    _parse_otlp_spans,
    serialize_full_trace,
    serialize_span_details,
    serialize_trace_search,
)


class TraceHandlerMixin:
    """Mixin for Tempo trace search, retrieval, span details, and convenience handlers."""

    # These will be provided by TempoConnector (base class)
    async def _search_traces(self, params: dict) -> dict: ...  # type: ignore[empty-body]

    async def _get_trace(self, trace_id: str) -> dict: ...  # type: ignore[empty-body]

    async def _get_tags(self) -> list: ...  # type: ignore[empty-body]

    async def _get_tag_values(self, tag: str) -> list: ...  # type: ignore[empty-body]

    # =========================================================================
    # INTERNAL HELPERS: Build Tempo search parameters
    # =========================================================================

    @staticmethod
    def _build_search_params(params: dict[str, Any]) -> dict[str, Any]:
        """
        Build Tempo search API parameters from named agent parameters.

        Maps agent-friendly parameter names to Tempo API query params.
        Constructs tag-based queries for service_name filtering.
        """
        search_params: dict[str, Any] = {}

        # Duration filters
        min_duration = params.get("min_duration")
        if min_duration:
            search_params["minDuration"] = min_duration

        max_duration = params.get("max_duration")
        if max_duration:
            search_params["maxDuration"] = max_duration

        # Limit
        limit = params.get("limit", 20)
        search_params["limit"] = limit

        # Time range
        time_range = TimeRange.from_relative(params.get("time_range", "1h"))
        search_params["start"] = str(int(time_range.start.timestamp()))
        search_params["end"] = str(int(time_range.end.timestamp()))

        # Build tag-based query parts for TraceQL-style search
        # Tempo search API accepts tags as key=value pairs in the `tags` param
        # or as a TraceQL query in the `q` param
        tag_parts: list[str] = []

        service_name = params.get("service_name")
        if service_name:
            tag_parts.append(f'resource.service.name="{service_name}"')

        operation = params.get("operation")
        if operation:
            tag_parts.append(f'name="{operation}"')

        status = params.get("status")
        if status:
            if status.lower() == "error":
                tag_parts.append("status=error")
            elif status.lower() == "ok":
                tag_parts.append("status=ok")

        # Custom tags
        tags = params.get("tags")
        if tags and isinstance(tags, dict):
            for key, value in tags.items():
                tag_parts.append(f'span.{key}="{value}"')

        # Use tags parameter for Tempo search
        if tag_parts:
            search_params["tags"] = " ".join(tag_parts)

        return search_params

    # =========================================================================
    # HANDLER METHODS
    # =========================================================================

    async def _search_traces_handler(self, params: dict[str, Any]) -> dict:
        """
        Search traces with filters.

        Builds search params from named parameters, queries Tempo,
        returns compact one-liner per trace.
        """
        search_params = self._build_search_params(params)
        limit = params.get("limit", 20)
        response = await self._search_traces(search_params)
        return serialize_trace_search(response, limit)

    async def _get_trace_handler(self, params: dict[str, Any]) -> dict:
        """
        Retrieve full trace by ID.

        Returns flat span table with core diagnostic attributes.
        """
        trace_id = params["trace_id"]
        response = await self._get_trace(trace_id)
        return serialize_full_trace(response)

    async def _get_span_details_handler(self, params: dict[str, Any]) -> dict:
        """
        Get full unredacted details for a single span.

        Retrieves the full trace, finds the target span by ID,
        and returns all attributes/events/links without redaction.
        """
        trace_id = params["trace_id"]
        span_id = params["span_id"]

        response = await self._get_trace(trace_id)
        parsed = _parse_otlp_spans(response)

        # Find the target span
        for _service_name, span, resource_attrs in parsed:
            if span.get("spanId") == span_id:
                return serialize_span_details(span, resource_attrs)

        # Span not found
        return {
            "error": f"Span {span_id} not found in trace {trace_id}",
            "available_spans": [
                {"span_id": span.get("spanId", ""), "operation": span.get("name", "")}
                for _, span, _ in parsed[:20]
            ],
        }

    async def _get_slow_traces_handler(self, params: dict[str, Any]) -> dict:
        """
        Find slow traces (convenience shortcut).

        Delegates to search with min_duration default of "1s".
        """
        # Inject default min_duration if not provided
        search_params = dict(params)
        if "min_duration" not in search_params:
            search_params["min_duration"] = "1s"
        return await self._search_traces_handler(search_params)

    async def _get_error_traces_handler(self, params: dict[str, Any]) -> dict:
        """
        Find traces with errors (convenience shortcut).

        Delegates to search with status="error" pre-filled.
        """
        search_params = dict(params)
        search_params["status"] = "error"
        return await self._search_traces_handler(search_params)
