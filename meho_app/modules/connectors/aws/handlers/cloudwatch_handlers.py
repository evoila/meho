# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS CloudWatch Handlers.

Handlers for CloudWatch operations: metrics, time series, alarms, and alarm history.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.aws.serializers import (
    serialize_cloudwatch_alarm,
    serialize_cloudwatch_metric,
    serialize_metric_data_result,
)

if TYPE_CHECKING:
    from meho_app.modules.connectors.aws.connector import AWSConnector

logger = get_logger(__name__)


class CloudWatchHandlerMixin:
    """Mixin providing CloudWatch operation handlers."""

    # =========================================================================
    # METRIC OPERATIONS
    # =========================================================================

    async def _handle_list_metric_descriptors(  # type: ignore[misc]
        self: "AWSConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        List CloudWatch metrics, optionally filtered by namespace.

        Args:
            params: Optional keys: namespace, region.

        Returns:
            List of serialized CloudWatch metric descriptors.
        """
        client = self._get_client("cloudwatch", params.get("region"))
        namespace = params.get("namespace")

        def _list_paginated() -> list[dict[str, Any]]:
            paginator = client.get_paginator("list_metrics")
            paginate_kwargs: dict[str, Any] = {}
            if namespace:
                paginate_kwargs["Namespace"] = namespace

            results: list[dict[str, Any]] = []
            for page in paginator.paginate(**paginate_kwargs):
                results.extend(page.get("Metrics", []))
            return results

        raw = await asyncio.to_thread(_list_paginated)
        return [serialize_cloudwatch_metric(m) for m in raw]

    async def _handle_get_time_series(  # type: ignore[misc]
        self: "AWSConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        Get CloudWatch metric time series data.

        Args:
            params: Required keys: namespace, metric_name.
                    Optional keys: dimensions, minutes (default 60),
                    period (default 300), statistics (default ["Average"]), region.

        Returns:
            List of serialized metric data results.
        """
        client = self._get_client("cloudwatch", params.get("region"))
        namespace = params["namespace"]
        metric_name = params["metric_name"]
        dimensions = params.get("dimensions", [])
        minutes = params.get("minutes", 60)
        period = params.get("period", 300)
        statistics = params.get("statistics", ["Average"])

        now = datetime.now(UTC)
        start_time = now - timedelta(minutes=minutes)

        # Build dimensions in boto3 format
        boto3_dimensions = []
        for dim in dimensions:
            boto3_dimensions.append(
                {
                    "Name": dim.get("Name", dim.get("name", "")),
                    "Value": dim.get("Value", dim.get("value", "")),
                }
            )

        # Build metric data query
        metric_data_queries = [
            {
                "Id": "m1",
                "MetricStat": {
                    "Metric": {
                        "Namespace": namespace,
                        "MetricName": metric_name,
                        "Dimensions": boto3_dimensions,
                    },
                    "Period": period,
                    "Stat": statistics[0] if statistics else "Average",
                },
                "ReturnData": True,
            }
        ]

        def _get_data() -> list[dict[str, Any]]:
            response = client.get_metric_data(
                MetricDataQueries=metric_data_queries,
                StartTime=start_time,
                EndTime=now,
            )
            results: list[dict[str, Any]] = response.get("MetricDataResults", [])
            return results

        raw = await asyncio.to_thread(_get_data)
        return [serialize_metric_data_result(r) for r in raw]

    # =========================================================================
    # ALARM OPERATIONS
    # =========================================================================

    async def _handle_list_alarms(  # type: ignore[misc]
        self: "AWSConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        List CloudWatch alarms, optionally filtered by state.

        Args:
            params: Optional keys: state_value (OK, ALARM, INSUFFICIENT_DATA), region.

        Returns:
            List of serialized CloudWatch alarms.
        """
        client = self._get_client("cloudwatch", params.get("region"))
        state_value = params.get("state_value")

        def _list_paginated() -> list[dict[str, Any]]:
            paginator = client.get_paginator("describe_alarms")
            paginate_kwargs: dict[str, Any] = {}
            if state_value:
                paginate_kwargs["StateValue"] = state_value

            results: list[dict[str, Any]] = []
            for page in paginator.paginate(**paginate_kwargs):
                results.extend(page.get("MetricAlarms", []))
            return results

        raw = await asyncio.to_thread(_list_paginated)
        return [serialize_cloudwatch_alarm(a) for a in raw]

    async def _handle_get_alarm_history(  # type: ignore[misc]
        self: "AWSConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        Get history for a specific CloudWatch alarm.

        Args:
            params: Required keys: alarm_name. Optional keys: region.

        Returns:
            List of alarm history items.
        """
        client = self._get_client("cloudwatch", params.get("region"))
        alarm_name = params["alarm_name"]

        def _get_history() -> list[dict[str, Any]]:
            response = client.describe_alarm_history(AlarmName=alarm_name)
            items: list[dict[str, Any]] = response.get("AlarmHistoryItems", [])
            return items

        raw = await asyncio.to_thread(_get_history)
        return [
            {
                "timestamp": item.get("Timestamp", "").isoformat()
                if hasattr(item.get("Timestamp", ""), "isoformat")
                else str(item.get("Timestamp", "")),
                "type": item.get("HistoryItemType", ""),
                "summary": item.get("HistorySummary", ""),
            }
            for item in raw
        ]
