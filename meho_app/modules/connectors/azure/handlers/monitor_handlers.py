# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Azure Monitor handler mixin (Phase 92).

Handlers for Azure Monitor operations: metrics, metric definitions,
metric namespaces, alerts, activity log, action groups.
Uses native async Azure SDK clients.
"""

from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.azure.helpers import (
    _format_azure_timestamp,
)

if TYPE_CHECKING:
    from meho_app.modules.connectors.azure.connector import AzureConnector

logger = get_logger(__name__)


class MonitorHandlerMixin:
    """Mixin providing Azure Monitor operation handlers.

    Covers metrics queries, metric definitions, alerts, activity log,
    and action groups. All methods use native async Azure SDK calls.
    """

    if TYPE_CHECKING:
        _monitor_client: Any
        _subscription_id: str
        _resource_group_filter: str | None

    # =========================================================================
    # METRICS OPERATIONS
    # =========================================================================

    async def _handle_get_azure_metrics(  # type: ignore[misc]  # NOSONAR
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Query metrics for any Azure resource.

        Args via params:
            resource_uri: Full ARM resource URI (required).
            timespan: ISO 8601 duration (default PT1H).
            interval: Granularity (default PT5M).
            metricnames: Comma-separated metric names (optional).
            aggregation: Aggregation type (default Average).
        """
        resource_uri = params["resource_uri"]
        timespan = params.get("timespan", "PT1H")
        interval = params.get("interval", "PT5M")
        metricnames = params.get("metricnames")
        aggregation = params.get("aggregation", "Average")

        kwargs: dict[str, Any] = {
            "resource_uri": resource_uri,
            "timespan": timespan,
            "interval": interval,
            "aggregation": aggregation,
        }
        if metricnames:
            kwargs["metricnames"] = metricnames

        response = await self._monitor_client.metrics.list(**kwargs)

        results: list[dict[str, Any]] = []
        for metric in response.value:
            metric_data: dict[str, Any] = {
                "name": metric.name.value if metric.name else None,
                "unit": str(metric.unit) if metric.unit else None,
                "timeseries": [],
            }
            for ts in metric.timeseries or []:
                data_points: list[dict[str, Any]] = []
                for dp in ts.data or []:
                    point: dict[str, Any] = {
                        "timestamp": _format_azure_timestamp(dp.time_stamp),
                    }
                    if dp.average is not None:
                        point["average"] = dp.average
                    if dp.total is not None:
                        point["total"] = dp.total
                    if dp.minimum is not None:
                        point["minimum"] = dp.minimum
                    if dp.maximum is not None:
                        point["maximum"] = dp.maximum
                    if dp.count is not None:
                        point["count"] = dp.count
                    data_points.append(point)
                metric_data["timeseries"].append(
                    {
                        "data": data_points,
                    }
                )
            results.append(metric_data)

        return results

    async def _handle_list_azure_metric_definitions(  # type: ignore[misc]  # NOSONAR (cognitive complexity)
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List available metric definitions for a resource."""
        resource_uri = params["resource_uri"]

        results: list[dict[str, Any]] = []
        async for defn in self._monitor_client.metric_definitions.list(resource_uri):
            supported_aggs = []
            for agg in defn.supported_aggregation_types or []:
                supported_aggs.append(str(agg))

            results.append(
                {
                    "name": defn.name.value if defn.name else None,
                    "display_name": defn.name.localized_value if defn.name else None,
                    "unit": str(defn.unit) if defn.unit else None,
                    "primary_aggregation_type": str(defn.primary_aggregation_type)
                    if defn.primary_aggregation_type
                    else None,
                    "supported_aggregation_types": supported_aggs,
                    "metric_availabilities": [
                        {
                            "time_grain": str(a.time_grain) if a.time_grain else None,
                            "retention": str(a.retention) if a.retention else None,
                        }
                        for a in (defn.metric_availabilities or [])
                    ],
                }
            )

        return results

    async def _handle_list_azure_metric_namespaces(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List metric namespaces for a resource."""
        resource_uri = params["resource_uri"]

        results: list[dict[str, Any]] = []
        async for ns in self._monitor_client.metric_namespaces.list(resource_uri):
            results.append(
                {
                    "name": getattr(ns, "name", None),
                    "fully_qualified_name": getattr(ns.properties, "metric_namespace_name", None)
                    if ns.properties
                    else None,
                    "type": getattr(ns, "type", None),
                }
            )

        return results

    # =========================================================================
    # ALERT OPERATIONS
    # =========================================================================

    async def _handle_list_azure_metric_alerts(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List metric alert rules.

        If resource_group is provided, lists alerts in that group.
        Otherwise lists all alerts in the subscription.
        """
        resource_group = params.get("resource_group") or self._resource_group_filter

        results: list[dict[str, Any]] = []

        if resource_group:
            async for alert in self._monitor_client.metric_alerts.list_by_resource_group(
                resource_group
            ):
                results.append(self._serialize_metric_alert(alert))
        else:
            async for alert in self._monitor_client.metric_alerts.list_by_subscription():
                results.append(self._serialize_metric_alert(alert))

        return results

    async def _handle_get_azure_metric_alert(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get metric alert rule details."""
        resource_group = params["resource_group"]
        rule_name = params["rule_name"]

        alert = await self._monitor_client.metric_alerts.get(
            resource_group_name=resource_group,
            rule_name=rule_name,
        )
        return self._serialize_metric_alert(alert)

    @staticmethod
    def _serialize_metric_alert(alert: Any) -> dict[str, Any]:
        """Serialize a metric alert rule to a dictionary."""
        scopes = list(alert.scopes) if alert.scopes else []

        criteria_list: list[dict[str, Any]] = []
        if alert.criteria and hasattr(alert.criteria, "all_of"):
            for criterion in alert.criteria.all_of or []:
                criteria_list.append(
                    {
                        "metric_name": getattr(criterion, "metric_name", None),
                        "metric_namespace": getattr(criterion, "metric_namespace", None),
                        "operator": str(getattr(criterion, "operator", None)),
                        "threshold": getattr(criterion, "threshold", None),
                        "time_aggregation": str(getattr(criterion, "time_aggregation", None)),
                    }
                )

        return {
            "id": alert.id,
            "name": alert.name,
            "description": getattr(alert, "description", None),
            "severity": getattr(alert, "severity", None),
            "enabled": getattr(alert, "enabled", None),
            "scopes": scopes,
            "evaluation_frequency": str(alert.evaluation_frequency)
            if alert.evaluation_frequency
            else None,
            "window_size": str(alert.window_size) if alert.window_size else None,
            "criteria": criteria_list,
            "auto_mitigate": getattr(alert, "auto_mitigate", None),
        }

    # =========================================================================
    # ACTIVITY LOG OPERATIONS
    # =========================================================================

    async def _handle_list_azure_activity_log(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List activity log events.

        Supports OData filter expressions for scoping events.
        If no filter is provided, defaults to the last 1 hour.
        """
        filter_expr = params.get("filter")

        if not filter_expr:
            # Default to recent events if no filter
            from datetime import UTC, datetime, timedelta

            end = datetime.now(UTC)
            start = end - timedelta(hours=1)
            filter_expr = (
                f"eventTimestamp ge '{start.isoformat()}' and eventTimestamp le '{end.isoformat()}'"
            )

        results: list[dict[str, Any]] = []
        async for event in self._monitor_client.activity_logs.list(filter=filter_expr):
            results.append(
                {
                    "event_id": getattr(event, "event_data_id", None),
                    "operation_name": event.operation_name.value if event.operation_name else None,
                    "status": event.status.value if event.status else None,
                    "category": event.category.value if event.category else None,
                    "level": str(event.level) if event.level else None,
                    "caller": getattr(event, "caller", None),
                    "resource_id": getattr(event, "resource_id", None),
                    "timestamp": _format_azure_timestamp(getattr(event, "event_timestamp", None)),
                    "description": getattr(event, "description", None),
                }
            )

        return results

    # =========================================================================
    # ACTION GROUP OPERATIONS
    # =========================================================================

    async def _handle_list_azure_action_groups(  # type: ignore[misc]
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List notification action groups.

        If resource_group is provided, lists action groups in that group.
        Otherwise lists all in the subscription.
        """
        resource_group = params.get("resource_group") or self._resource_group_filter

        results: list[dict[str, Any]] = []

        if resource_group:
            async for ag in self._monitor_client.action_groups.list_by_resource_group(
                resource_group
            ):
                results.append(self._serialize_action_group(ag))
        else:
            async for ag in self._monitor_client.action_groups.list_by_subscription_id():
                results.append(self._serialize_action_group(ag))

        return results

    @staticmethod
    def _serialize_action_group(ag: Any) -> dict[str, Any]:
        """Serialize an action group to a dictionary."""
        email_receivers = [
            {"name": r.name, "email": r.email_address} for r in (ag.email_receivers or [])
        ]
        webhook_receivers = [
            {"name": r.name, "uri": r.service_uri} for r in (ag.webhook_receivers or [])
        ]

        return {
            "id": ag.id,
            "name": ag.name,
            "short_name": getattr(ag, "group_short_name", None),
            "enabled": getattr(ag, "enabled", None),
            "email_receivers": email_receivers,
            "webhook_receivers": webhook_receivers,
        }
