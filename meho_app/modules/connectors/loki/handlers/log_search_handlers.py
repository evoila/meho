# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Loki Log Search Handler Mixin.

Handles log search, error logs, log context, volume analysis, and pattern detection.
All handlers build LogQL internally -- the agent never writes LogQL syntax.
"""

from typing import Any

from meho_app.modules.connectors.loki.serializers import (
    serialize_log_context,
    serialize_log_patterns,
    serialize_log_streams,
    serialize_log_volume,
)
from meho_app.modules.connectors.observability.time_range import TimeRange


class LogSearchHandlerMixin:
    """Mixin for Loki log search, error, context, volume, and pattern handlers."""

    # These will be provided by LokiConnector (base class)
    async def _query_range_log(self, logql: str, params: dict) -> dict: ...

    async def _query_log(self, logql: str, params: dict) -> dict: ...

    async def _get_labels(self, start: str | None = None, end: str | None = None) -> list: ...

    async def _get_label_values(
        self, label: str, start: str | None = None, end: str | None = None
    ) -> list: ...

    # Also need _get for patterns API endpoint
    async def _get(self, path: str, params: dict | None = None) -> dict: ...

    # =========================================================================
    # INTERNAL HELPERS: LogQL Construction
    # =========================================================================

    @staticmethod
    def _build_log_selector(params: dict[str, Any]) -> str:
        """
        Build LogQL stream selector from named params.

        Constructs {label="value", ...} selector string.
        Loki requires at least one label matcher -- falls back to {job=~".+"}
        if no labels are provided.
        """
        matchers: list[str] = []

        # Named label mappings
        label_map = {
            "namespace": "namespace",
            "pod": "pod",
            "service": "service_name",
            "container": "container",
        }

        for param_name, label_name in label_map.items():
            value = params.get(param_name)
            if value:
                matchers.append(f'{label_name}="{value}"')

        # Arbitrary labels from labels dict
        labels = params.get("labels")
        if labels and isinstance(labels, dict):
            for key, value in labels.items():
                matchers.append(f'{key}="{value}"')

        # Loki requires at least one label matcher
        if not matchers:
            return '{job=~".+"}'

        return "{" + ", ".join(matchers) + "}"

    @staticmethod
    def _build_pipeline(params: dict[str, Any], error_mode: bool = False) -> str:
        """
        Build LogQL pipeline stages for severity filtering and text matching.

        Args:
            params: Operation parameters
            error_mode: If True, hardcode error/warn/fatal severity filter
        """
        stages: list[str] = []

        if error_mode:
            # Hardcoded error filter for get_error_logs
            stages.append('| level=~"error|err|warn|warning|fatal|critical"')
        else:
            severity = params.get("severity")
            if severity:
                severity_lower = severity.lower()
                if severity_lower in ("error", "err"):
                    stages.append('| level=~"error|err"')
                elif severity_lower in ("warn", "warning"):
                    stages.append('| level=~"warn|warning"')
                elif severity_lower == "info":
                    stages.append('| level="info"')
                elif severity_lower == "debug":
                    stages.append('| level="debug"')
                elif severity_lower in ("fatal", "critical"):
                    stages.append('| level=~"fatal|critical"')
                else:
                    stages.append(f'| level="{severity_lower}"')

        text_filter = params.get("text_filter")
        if text_filter:
            # Escape double quotes in the filter value
            escaped = text_filter.replace('"', '\\"')
            stages.append(f'|= "{escaped}"')

        return " ".join(stages)

    # =========================================================================
    # HANDLER METHODS
    # =========================================================================

    async def _search_logs(self, params: dict[str, Any]) -> dict:
        """
        Search logs with label filters, severity, and text filter.

        Builds LogQL from params, queries Loki, returns structured output.
        """
        selector = self._build_log_selector(params)
        pipeline = self._build_pipeline(params)
        logql = f"{selector} {pipeline}".strip()

        limit = params.get("limit", 100)
        time_range = TimeRange.from_relative(params.get("time_range", "1h"))

        response = await self._query_range_log(
            logql,
            {
                "limit": limit,
                "start": str(int(time_range.start.timestamp())),
                "end": str(int(time_range.end.timestamp())),
                "direction": "backward",
            },
        )

        return serialize_log_streams(response, limit)

    async def _get_error_logs(self, params: dict[str, Any]) -> dict:
        """
        Retrieve error and warning logs.

        Same as search_logs but with severity pre-filtered to error/warn/fatal.
        """
        selector = self._build_log_selector(params)
        pipeline = self._build_pipeline(params, error_mode=True)
        logql = f"{selector} {pipeline}".strip()

        limit = params.get("limit", 100)
        time_range = TimeRange.from_relative(params.get("time_range", "1h"))

        response = await self._query_range_log(
            logql,
            {
                "limit": limit,
                "start": str(int(time_range.start.timestamp())),
                "end": str(int(time_range.end.timestamp())),
                "direction": "backward",
            },
        )

        return serialize_log_streams(response, limit)

    async def _get_log_context(self, params: dict[str, Any]) -> dict:
        """
        Retrieve log lines surrounding a specific timestamp.

        Queries a time window centered on the given timestamp and splits
        results into before/after sections.
        """
        timestamp = params["timestamp"]
        before_lines = params.get("before_lines", 20)
        after_lines = params.get("after_lines", 20)

        # Parse the center timestamp
        from meho_app.modules.connectors.loki.serializers import _parse_timestamp_to_ns

        center_ns = _parse_timestamp_to_ns(timestamp)

        # Build a time window: center -5m to center +5m
        center_seconds = center_ns / 1_000_000_000
        window_start = int(center_seconds - 300)  # 5 minutes before
        window_end = int(center_seconds + 300)  # 5 minutes after

        selector = self._build_log_selector(params)
        logql = selector  # No pipeline for context -- we want all lines

        # Query with enough limit to cover both before and after
        total_limit = before_lines + after_lines + 10  # extra buffer

        response = await self._query_range_log(
            logql,
            {
                "limit": total_limit,
                "start": str(window_start),
                "end": str(window_end),
                "direction": "forward",
            },
        )

        return serialize_log_context(response, timestamp, before_lines, after_lines)

    async def _get_log_volume(self, params: dict[str, Any]) -> dict:
        """
        Query log volume statistics over time using count_over_time.

        Returns counts bucketed by time interval for volume trend analysis.
        """
        selector = self._build_log_selector(params)
        pipeline = self._build_pipeline(params)
        base_logql = f"{selector} {pipeline}".strip()

        time_range_str = params.get("time_range", "6h")
        time_range = TimeRange.from_relative(time_range_str)

        # Use step from params or auto-resolve from time range
        step = params.get("step", time_range.step)

        # Build metric query: count_over_time(logql[step])
        metric_logql = f"count_over_time({base_logql}[{step}])"

        response = await self._query_range_log(
            metric_logql,
            {
                "start": str(int(time_range.start.timestamp())),
                "end": str(int(time_range.end.timestamp())),
                "step": step,
            },
        )

        return serialize_log_volume(response)

    async def _get_log_patterns(self, params: dict[str, Any]) -> dict:
        """
        Detect repeating log patterns using Loki's pattern detection API.

        Uses the /loki/api/v1/index/patterns endpoint (Loki 3.x).
        Falls back to a message if the endpoint is unavailable.
        """
        selector = self._build_log_selector(params)
        time_range = TimeRange.from_relative(params.get("time_range", "1h"))

        try:
            # Loki 3.x patterns API
            response = await self._get(
                "/loki/api/v1/index/patterns",
                params={
                    "query": selector,
                    "start": str(int(time_range.start.timestamp())),
                    "end": str(int(time_range.end.timestamp())),
                },
            )
            return serialize_log_patterns(response)
        except Exception:
            # Patterns API not available (older Loki version)
            return {
                "patterns": [],
                "total_patterns": 0,
                "note": "Pattern detection unavailable. This Loki instance may not "
                "support the patterns API (requires Loki 3.x or later).",
            }
