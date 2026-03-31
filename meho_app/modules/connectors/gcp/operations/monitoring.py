# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GCP Cloud Monitoring Operation Definitions (TASK-102)

Operations for accessing Cloud Monitoring metrics and alert policies.
"""

from meho_app.modules.connectors.base import OperationDefinition

MONITORING_OPERATIONS = [
    # Metric Operations
    OperationDefinition(
        operation_id="list_metric_descriptors",
        name="List Metric Descriptors",
        description="List available metric types in Cloud Monitoring. Returns metric type, display name, description, and labels.",
        category="monitoring",
        parameters=[
            {
                "name": "filter",
                "type": "string",
                "required": False,
                "description": "Filter by metric type prefix (e.g., 'compute.googleapis.com')",
            },
        ],
        example="list_metric_descriptors(filter='compute.googleapis.com')",
        response_entity_type="MetricDescriptor",
        response_identifier_field="type",
        response_display_name_field="display_name",
    ),
    OperationDefinition(
        operation_id="get_metric_descriptor",
        name="Get Metric Descriptor",
        description="Get details about a specific metric type including its labels, value type, and metric kind.",
        category="monitoring",
        parameters=[
            {
                "name": "metric_type",
                "type": "string",
                "required": True,
                "description": "Full metric type (e.g., 'compute.googleapis.com/instance/cpu/utilization')",
            },
        ],
        example="get_metric_descriptor(metric_type='compute.googleapis.com/instance/cpu/utilization')",
        response_entity_type="MetricDescriptor",
        response_identifier_field="type",
        response_display_name_field="display_name",
    ),
    OperationDefinition(
        operation_id="get_time_series",
        name="Get Time Series Data",
        description="Query time series data for a metric. Returns data points with timestamps and values. Note: For gce_instance resources, use instance_id (numeric), not instance_name.",
        category="monitoring",
        parameters=[
            {
                "name": "metric_type",
                "type": "string",
                "required": True,
                "description": "Metric type to query",
            },
            {
                "name": "minutes",
                "type": "integer",
                "required": False,
                "description": "Time range in minutes (default: 60)",
            },
            {
                "name": "resource_type",
                "type": "string",
                "required": False,
                "description": "Resource type filter (e.g., 'gce_instance')",
            },
            {
                "name": "instance_id",
                "type": "string",
                "required": False,
                "description": "Numeric instance ID (from get_instance). Required for gce_instance metrics.",
            },
            {"name": "zone", "type": "string", "required": False, "description": "Zone filter"},
        ],
        example="get_time_series(metric_type='compute.googleapis.com/instance/cpu/utilization', instance_id='1234567890', zone='us-central1-a')",
        response_entity_type="TimeSeries",
        response_identifier_field="metric",
        response_display_name_field="metric",
    ),
    OperationDefinition(
        operation_id="get_instance_metrics",
        name="Get Instance Metrics",
        description="Get common metrics for a Compute Engine instance including CPU, disk, and network utilization. Automatically resolves instance name to ID for Cloud Monitoring.",
        category="monitoring",
        parameters=[
            {
                "name": "instance_name",
                "type": "string",
                "required": True,
                "description": "Name of the instance (will be resolved to numeric ID internally)",
            },
            {
                "name": "zone",
                "type": "string",
                "required": False,
                "description": "Zone of the instance",
            },
            {
                "name": "minutes",
                "type": "integer",
                "required": False,
                "description": "Time range in minutes (default: 60)",
            },
        ],
        example="get_instance_metrics(instance_name='my-vm', zone='us-central1-a')",
        response_entity_type="InstanceMetrics",
        response_identifier_field="instance_name",
        response_display_name_field="instance_name",
    ),
    # Alert Policy Operations
    OperationDefinition(
        operation_id="list_alert_policies",
        name="List Alert Policies",
        description="List all alert policies in the project. Returns policy name, display name, enabled status, and conditions.",
        category="monitoring",
        parameters=[
            {
                "name": "filter",
                "type": "string",
                "required": False,
                "description": "Filter expression",
            },
        ],
        example="list_alert_policies()",
        response_entity_type="AlertPolicy",
        response_identifier_field="name",
        response_display_name_field="display_name",
    ),
    OperationDefinition(
        operation_id="get_alert_policy",
        name="Get Alert Policy Details",
        description="Get detailed information about a specific alert policy including conditions, documentation, and notification channels.",
        category="monitoring",
        parameters=[
            {
                "name": "policy_name",
                "type": "string",
                "required": True,
                "description": "Full policy name or display name",
            },
        ],
        example="get_alert_policy(policy_name='High CPU Alert')",
        response_entity_type="AlertPolicy",
        response_identifier_field="name",
        response_display_name_field="display_name",
    ),
    OperationDefinition(
        operation_id="list_notification_channels",
        name="List Notification Channels",
        description="List all notification channels configured for alerts (email, SMS, PagerDuty, etc.).",
        category="monitoring",
        parameters=[],
        example="list_notification_channels()",
        response_entity_type="NotificationChannel",
        response_identifier_field="name",
        response_display_name_field="display_name",
    ),
    # Uptime Check Operations
    OperationDefinition(
        operation_id="list_uptime_checks",
        name="List Uptime Checks",
        description="List all uptime check configurations. Returns check name, type (HTTP, HTTPS, TCP), and target.",
        category="monitoring",
        parameters=[],
        example="list_uptime_checks()",
        response_entity_type="UptimeCheckConfig",
        response_identifier_field="name",
        response_display_name_field="display_name",
    ),
]
