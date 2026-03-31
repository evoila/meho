# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Prometheus Service Handler Mixin.

Handles RED (Rate, Error rate, Duration) metric queries for services.
Tries multiple label names (service, service_name, job) to find the service.
"""

from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.observability.data_reduction import summarize_time_series
from meho_app.modules.connectors.observability.time_range import TimeRange

logger = get_logger(__name__)

# Label names to try when matching a service (in priority order)
_SERVICE_LABEL_CANDIDATES = ["service", "service_name", "job"]


class ServiceHandlerMixin:
    """Mixin for Prometheus service RED metric handlers."""

    # These will be provided by PrometheusConnector (base class)
    async def _query_range(self, query: str, time_range: TimeRange) -> list: ...

    async def _query_instant(self, query: str) -> list: ...

    async def _find_service_label(self, service_name: str) -> str | None:
        """
        Find which label name matches the given service in Prometheus.

        Tries each candidate label with an instant query to see which returns data.
        Returns the first label name that matches, or None.
        """
        for label in _SERVICE_LABEL_CANDIDATES:
            query = f'count(http_requests_total{{{label}="{service_name}"}})'
            result = await self._query_instant(query)
            if result:
                # Check if the count is > 0
                for series in result:
                    value = series.get("value", [])
                    if len(value) >= 2:
                        try:
                            count = float(str(value[1]))
                            if count > 0:
                                return label
                        except (ValueError, TypeError):
                            continue
        return None

    async def _get_red_metrics(self, params: dict[str, Any]) -> dict:
        """
        Get RED metrics for a service.

        Runs multiple PromQL queries for rate, error rate, and latency percentiles.
        """
        service_name = params["service_name"]
        time_range = TimeRange.from_relative(params.get("time_range", "1h"))
        histogram_metric = params.get("histogram_metric", "http_request_duration_seconds")

        # Find which label the service uses
        label = await self._find_service_label(service_name)
        if not label:
            # Fall back to "service" if detection fails
            label = "service"
            logger.warning(
                f"Could not detect label for service '{service_name}', falling back to '{label}'"
            )

        # Request rate (requests per second)
        rate_query = f'sum(rate(http_requests_total{{{label}="{service_name}"}}[5m]))'
        rate_result = await self._query_range(rate_query, time_range)
        rate_summary = summarize_time_series(rate_result, label, "request_rate")

        # Error rate (5xx / total)
        error_query = (
            f'sum(rate(http_requests_total{{{label}="{service_name}",status=~"5.."}}[5m]))'
            f' / sum(rate(http_requests_total{{{label}="{service_name}"}}[5m]))'
        )
        error_result = await self._query_range(error_query, time_range)
        error_summary = summarize_time_series(error_result, label, "error_rate")

        # Latency percentiles
        latency_p50_query = (
            f"histogram_quantile(0.5, sum(rate({histogram_metric}_bucket"
            f'{{{label}="{service_name}"}}[5m])) by (le))'
        )
        latency_p95_query = (
            f"histogram_quantile(0.95, sum(rate({histogram_metric}_bucket"
            f'{{{label}="{service_name}"}}[5m])) by (le))'
        )
        latency_p99_query = (
            f"histogram_quantile(0.99, sum(rate({histogram_metric}_bucket"
            f'{{{label}="{service_name}"}}[5m])) by (le))'
        )

        p50_result = await self._query_range(latency_p50_query, time_range)
        p95_result = await self._query_range(latency_p95_query, time_range)
        p99_result = await self._query_range(latency_p99_query, time_range)

        p50_summary = summarize_time_series(p50_result, "le", "latency_p50_seconds")
        p95_summary = summarize_time_series(p95_result, "le", "latency_p95_seconds")
        p99_summary = summarize_time_series(p99_result, "le", "latency_p99_seconds")

        # Build combined result
        result: dict[str, Any] = {
            "service": service_name,
            "label_used": label,
            "time_range": params.get("time_range", "1h"),
            "histogram_metric": histogram_metric,
        }

        # Extract the summary stats from each metric
        if rate_summary.get("items"):
            result["request_rate"] = rate_summary["items"][0].get("request_rate", {})
        else:
            result["request_rate"] = {"current": 0, "note": "no data"}

        if error_summary.get("items"):
            result["error_rate"] = error_summary["items"][0].get("error_rate", {})
        else:
            result["error_rate"] = {"current": 0, "note": "no data (or no errors)"}

        if p50_summary.get("items"):
            result["latency_p50"] = p50_summary["items"][0].get("latency_p50_seconds", {})
        else:
            result["latency_p50"] = {"note": "no histogram data"}

        if p95_summary.get("items"):
            result["latency_p95"] = p95_summary["items"][0].get("latency_p95_seconds", {})
        else:
            result["latency_p95"] = {"note": "no histogram data"}

        if p99_summary.get("items"):
            result["latency_p99"] = p99_summary["items"][0].get("latency_p99_seconds", {})
        else:
            result["latency_p99"] = {"note": "no histogram data"}

        return result
