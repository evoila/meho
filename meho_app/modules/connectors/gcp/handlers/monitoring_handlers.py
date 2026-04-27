# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GCP Cloud Monitoring Handlers (TASK-102)

Handlers for Cloud Monitoring metrics and alert operations.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.gcp.serializers import (
    serialize_alert_policy,
    serialize_metric_descriptor,
    serialize_time_series,
)

if TYPE_CHECKING:
    from meho_app.modules.connectors.gcp.connector import GCPConnector

logger = get_logger(__name__)


class MonitoringHandlerMixin:
    """Mixin providing Cloud Monitoring operation handlers."""

    # Type hints for IDE support
    if TYPE_CHECKING:
        _monitoring_client: Any
        _alert_policy_client: Any
        _credentials: Any
        project_id: str
        default_zone: str

    def _get_project_name(self: "GCPConnector") -> str:  # type: ignore[misc]
        """Get the project name for monitoring APIs."""
        return f"projects/{self.project_id}"

    # =========================================================================
    # METRIC OPERATIONS
    # =========================================================================

    async def _handle_list_metric_descriptors(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List available metric descriptors."""
        filter_str = params.get("filter")

        # Build filter for metric type prefix
        api_filter = None
        if filter_str:
            api_filter = f'metric.type = starts_with("{filter_str}")'

        request = {
            "name": self._get_project_name(),
        }
        if api_filter:
            request["filter"] = api_filter

        descriptors = await asyncio.to_thread(
            lambda: list(self._monitoring_client.list_metric_descriptors(request=request))
        )

        return [serialize_metric_descriptor(d) for d in descriptors]

    async def _handle_get_metric_descriptor(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get a specific metric descriptor."""
        metric_type = params["metric_type"]

        name = f"{self._get_project_name()}/metricDescriptors/{metric_type}"

        descriptor = await asyncio.to_thread(
            lambda: self._monitoring_client.get_metric_descriptor(name=name)
        )

        return serialize_metric_descriptor(descriptor)

    async def _handle_get_time_series(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Get time series data for a metric."""
        from google.cloud import monitoring_v3

        metric_type = params["metric_type"]
        minutes = params.get("minutes", 60)
        resource_type = params.get("resource_type")
        instance_id = params.get("instance_id")  # Numeric instance ID (required for gce_instance)
        zone = params.get("zone")

        # Build time interval
        now = datetime.now(UTC)
        interval = monitoring_v3.TimeInterval(
            end_time=now,
            start_time=now - timedelta(minutes=minutes),
        )

        # Build filter
        # IMPORTANT: Cloud Monitoring uses instance_id (numeric), NOT instance_name!
        filter_parts = [f'metric.type = "{metric_type}"']
        if resource_type:
            filter_parts.append(f'resource.type = "{resource_type}"')
        if instance_id:
            filter_parts.append(f'resource.labels.instance_id = "{instance_id}"')
        if zone:
            filter_parts.append(f'resource.labels.zone = "{zone}"')

        filter_str = " AND ".join(filter_parts)
        logger.debug(f"📊 Monitoring filter: {filter_str}")

        request = monitoring_v3.ListTimeSeriesRequest(
            name=self._get_project_name(),
            filter=filter_str,
            interval=interval,
            view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        )

        time_series_list = await asyncio.to_thread(
            lambda: list(self._monitoring_client.list_time_series(request=request))
        )

        return [serialize_time_series(ts) for ts in time_series_list]

    async def _handle_get_instance_metrics(  # type: ignore[misc]  # NOSONAR
        self: "GCPConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get common metrics for a Compute Engine instance.

        Note: Cloud Monitoring uses numeric instance_id, not instance_name.
        This method automatically resolves the name to ID.

        If zone is not provided, we search all zones to find the instance.
        """
        instance_name = params["instance_name"]
        zone = params.get("zone")  # Don't use default - we'll search if not provided
        minutes = params.get("minutes", 60)

        # Step 1: Find the instance (search all zones if zone not provided)
        instance_details = None

        if zone:
            # Zone provided - direct lookup
            try:
                instance_details = await self._handle_get_instance(
                    {
                        "instance_name": instance_name,
                        "zone": zone,
                    }
                )
            except Exception as e:
                logger.warning(f"Instance not found in zone {zone}: {e}")

        if not instance_details:
            # No zone or not found - search all zones
            logger.info(f"🔍 Searching for instance '{instance_name}' across all zones...")
            try:
                all_instances = await self._handle_list_instances(
                    {
                        "filter": f"name={instance_name}",
                    }
                )
                for inst in all_instances:
                    if inst.get("name") == instance_name:
                        instance_details = inst
                        zone = inst.get("zone")
                        logger.info(f"✅ Found instance '{instance_name}' in zone: {zone}")
                        break
            except Exception as e:
                logger.error(f"❌ Failed to search for instance: {e}")

        if not instance_details:
            return {
                "instance_name": instance_name,
                "error": f"Instance '{instance_name}' not found in any zone. Check the instance name.",
            }

        # Step 2: Extract instance ID (required for Cloud Monitoring)
        instance_id = instance_details.get("id")
        if not instance_id:
            return {
                "instance_name": instance_name,
                "zone": zone,
                "error": "Could not resolve instance ID from instance details",
            }

        logger.info(f"📊 Resolved instance '{instance_name}' (zone={zone}) to ID: {instance_id}")

        # Define common instance metrics
        metrics_to_fetch = [
            ("compute.googleapis.com/instance/cpu/utilization", "cpu_utilization"),
            ("compute.googleapis.com/instance/disk/read_bytes_count", "disk_read_bytes"),
            ("compute.googleapis.com/instance/disk/write_bytes_count", "disk_write_bytes"),
            (
                "compute.googleapis.com/instance/network/received_bytes_count",
                "network_received_bytes",
            ),
            ("compute.googleapis.com/instance/network/sent_bytes_count", "network_sent_bytes"),
        ]

        result = {
            "instance_name": instance_name,
            "instance_id": instance_id,
            "zone": zone,
            "time_range_minutes": minutes,
            "metrics": {},
        }

        for metric_type, metric_key in metrics_to_fetch:
            try:
                # Use instance_id (numeric) for Cloud Monitoring filter
                time_series = await self._handle_get_time_series(
                    {
                        "metric_type": metric_type,
                        "minutes": minutes,
                        "resource_type": "gce_instance",
                        "instance_id": instance_id,  # Use numeric ID, not name!
                        "zone": zone,
                    }
                )

                if time_series:
                    # Get the latest value and summary
                    all_points = []
                    for ts in time_series:
                        all_points.extend(ts.get("points", []))

                    if all_points:
                        values = [p["value"] for p in all_points if p["value"] is not None]
                        if values:
                            result["metrics"][metric_key] = {
                                "latest": values[0] if values else None,
                                "average": sum(values) / len(values) if values else None,
                                "min": min(values) if values else None,
                                "max": max(values) if values else None,
                                "point_count": len(values),
                            }
                else:
                    result["metrics"][metric_key] = {"data": "No data points in time range"}

            except Exception as e:
                logger.warning(f"Failed to fetch metric {metric_type}: {e}", exc_info=True)
                result["metrics"][metric_key] = {"error": str(e)}

        return result

    # =========================================================================
    # ALERT POLICY OPERATIONS
    # =========================================================================

    async def _handle_list_alert_policies(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List alert policies."""
        filter_str = params.get("filter")

        request = {
            "name": self._get_project_name(),
        }
        if filter_str:
            request["filter"] = filter_str

        policies = await asyncio.to_thread(
            lambda: list(self._alert_policy_client.list_alert_policies(request=request))
        )

        return [serialize_alert_policy(p) for p in policies]

    async def _handle_get_alert_policy(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get alert policy details."""
        policy_name = params["policy_name"]

        # Check if it's a full name or display name
        if not policy_name.startswith("projects/"):
            # Search by display name
            policies = await self._handle_list_alert_policies({})
            for p in policies:
                if p["display_name"] == policy_name:
                    return p
            raise ValueError(f"Alert policy not found: {policy_name}")

        policy = await asyncio.to_thread(
            lambda: self._alert_policy_client.get_alert_policy(name=policy_name)
        )

        return serialize_alert_policy(policy)

    async def _handle_list_notification_channels(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List notification channels."""
        from google.cloud import monitoring_v3

        notification_client = monitoring_v3.NotificationChannelServiceClient(
            credentials=self._credentials
        )

        request = monitoring_v3.ListNotificationChannelsRequest(
            name=self._get_project_name(),
        )

        channels = await asyncio.to_thread(
            lambda: list(notification_client.list_notification_channels(request=request))
        )

        return [
            {
                "name": c.name,
                "display_name": c.display_name,
                "type": c.type_,
                "description": c.description,
                "labels": dict(c.labels) if c.labels else {},
                "enabled": c.enabled.value if hasattr(c.enabled, "value") else c.enabled,
                "verification_status": c.verification_status.name
                if c.verification_status
                else None,
            }
            for c in channels
        ]

    async def _handle_list_uptime_checks(  # type: ignore[misc]  # NOSONAR
        self: "GCPConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List uptime check configurations."""
        from google.cloud import monitoring_v3

        uptime_client = monitoring_v3.UptimeCheckServiceClient(credentials=self._credentials)

        request = monitoring_v3.ListUptimeCheckConfigsRequest(
            parent=self._get_project_name(),
        )

        configs = await asyncio.to_thread(
            lambda: list(uptime_client.list_uptime_check_configs(request=request))
        )

        return [
            {
                "name": c.name,
                "display_name": c.display_name,
                "period": c.period.seconds if c.period else None,
                "timeout": c.timeout.seconds if c.timeout else None,
                "monitored_resource": {
                    "type": c.monitored_resource.type_ if c.monitored_resource else None,
                    "labels": dict(c.monitored_resource.labels)
                    if c.monitored_resource and c.monitored_resource.labels
                    else {},
                }
                if c.monitored_resource
                else None,
                "http_check": {
                    "path": c.http_check.path if c.http_check else None,
                    "port": c.http_check.port if c.http_check else None,
                    "use_ssl": c.http_check.use_ssl if c.http_check else None,
                }
                if c.http_check
                else None,
                "tcp_check": {
                    "port": c.tcp_check.port if c.tcp_check else None,
                }
                if c.tcp_check
                else None,
            }
            for c in configs
        ]
