# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Azure Monitor Operation Definitions (Phase 92).

Operations for Azure Monitor: metrics queries, metric definitions,
metric namespaces, alert rules, activity log, and action groups.
"""

from meho_app.modules.connectors.base import OperationDefinition

MONITOR_OPERATIONS = [
    # Metrics Operations
    OperationDefinition(
        operation_id="get_azure_metrics",
        name="Get Azure Metrics",
        description=(
            "Query Azure Monitor metrics for any resource by its ARM resource URI. "
            "Returns metric name, unit, and time series data points with timestamp and "
            "aggregated values (average, total, min, max, count). Supports custom "
            "timespan, interval, metric name filtering, and aggregation type."
        ),
        category="monitor",
        parameters=[
            {
                "name": "resource_uri",
                "type": "string",
                "required": True,
                "description": "Full ARM resource URI (e.g., /subscriptions/.../providers/Microsoft.Compute/virtualMachines/my-vm)",
            },
            {
                "name": "timespan",
                "type": "string",
                "required": False,
                "description": "ISO 8601 duration (default: PT1H = last 1 hour). Examples: PT30M, PT4H, P1D",
            },
            {
                "name": "interval",
                "type": "string",
                "required": False,
                "description": "Metric granularity (default: PT5M = 5 minutes). Examples: PT1M, PT15M, PT1H",
            },
            {
                "name": "metricnames",
                "type": "string",
                "required": False,
                "description": "Comma-separated metric names (e.g., 'Percentage CPU,Available Memory Bytes')",
            },
            {
                "name": "aggregation",
                "type": "string",
                "required": False,
                "description": "Aggregation type: Average, Total, Minimum, Maximum, Count (default: Average)",
            },
        ],
        example="get_azure_metrics(resource_uri='/subscriptions/.../virtualMachines/my-vm', metricnames='Percentage CPU')",
    ),
    OperationDefinition(
        operation_id="list_azure_metric_definitions",
        name="List Azure Metric Definitions",
        description=(
            "List all available metric definitions for a resource. Returns metric name, "
            "display name, unit, primary aggregation type, supported aggregation types, "
            "and available time grains. Use this to discover what metrics are available "
            "before querying get_azure_metrics."
        ),
        category="monitor",
        parameters=[
            {
                "name": "resource_uri",
                "type": "string",
                "required": True,
                "description": "Full ARM resource URI to list available metrics for",
            },
        ],
        example="list_azure_metric_definitions(resource_uri='/subscriptions/.../virtualMachines/my-vm')",
    ),
    OperationDefinition(
        operation_id="list_azure_metric_namespaces",
        name="List Azure Metric Namespaces",
        description=(
            "List metric namespaces for a resource. Returns namespace name and "
            "fully qualified namespace name. Useful for understanding which metric "
            "providers emit metrics for a resource."
        ),
        category="monitor",
        parameters=[
            {
                "name": "resource_uri",
                "type": "string",
                "required": True,
                "description": "Full ARM resource URI to list metric namespaces for",
            },
        ],
        example="list_azure_metric_namespaces(resource_uri='/subscriptions/.../virtualMachines/my-vm')",
    ),
    # Alert Operations
    OperationDefinition(
        operation_id="list_azure_metric_alerts",
        name="List Azure Metric Alert Rules",
        description=(
            "List metric alert rules in the subscription or a specific resource group. "
            "Returns alert name, description, severity, enabled status, scopes, "
            "evaluation frequency, window size, and criteria (metric name, operator, "
            "threshold, aggregation)."
        ),
        category="monitor",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": False,
                "description": "Resource group to list alerts from (default: all in subscription)",
            },
        ],
        example="list_azure_metric_alerts(resource_group='my-rg')",
    ),
    OperationDefinition(
        operation_id="get_azure_metric_alert",
        name="Get Azure Metric Alert Details",
        description=(
            "Get detailed information about a specific metric alert rule including "
            "severity, evaluation criteria, thresholds, scopes, and action groups."
        ),
        category="monitor",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": True,
                "description": "Resource group containing the alert rule",
            },
            {
                "name": "rule_name",
                "type": "string",
                "required": True,
                "description": "Name of the metric alert rule",
            },
        ],
        example="get_azure_metric_alert(resource_group='my-rg', rule_name='high-cpu-alert')",
    ),
    # Activity Log Operations
    OperationDefinition(
        operation_id="list_azure_activity_log",
        name="List Azure Activity Log",
        description=(
            "List activity log events with optional OData filter. Returns event ID, "
            "operation name, status, category, level, caller, resource ID, timestamp, "
            "and description. Defaults to last 1 hour if no filter provided."
        ),
        category="monitor",
        parameters=[
            {
                "name": "filter",
                "type": "string",
                "required": False,
                "description": "OData filter expression (e.g., \"eventTimestamp ge '2026-03-27' and resourceGroupName eq 'my-rg'\")",
            },
            {
                "name": "timespan",
                "type": "string",
                "required": False,
                "description": "Time range for events (used to build filter if no explicit filter provided)",
            },
        ],
        example="list_azure_activity_log(filter=\"resourceGroupName eq 'my-rg'\")",
    ),
    # Action Group Operations
    OperationDefinition(
        operation_id="list_azure_action_groups",
        name="List Azure Action Groups",
        description=(
            "List notification action groups used by alert rules. Returns group name, "
            "short name, enabled status, and configured receivers (email, webhook)."
        ),
        category="monitor",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": False,
                "description": "Resource group to list action groups from (default: all in subscription)",
            },
        ],
        example="list_azure_action_groups()",
    ),
]
