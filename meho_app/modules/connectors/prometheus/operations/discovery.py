# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Prometheus Discovery Operations.

Target listing, metric discovery, alerts, alert rules, and PromQL escape hatch.
"""

from meho_app.modules.connectors.base import OperationDefinition

DISCOVERY_OPERATIONS = [
    OperationDefinition(
        operation_id="list_targets",
        name="List Scrape Targets",
        description="List all Prometheus scrape targets with their health status. Returns scrape targets "
        "with job, instance, health status (up/down/unknown), and Kubernetes labels when present "
        "(namespace, pod, node from discovered labels).",
        category="discovery",
        parameters=[],
        example="list_targets()",
        response_entity_type="ScrapeTarget",
        response_identifier_field="instance",
        response_display_name_field="instance",
    ),
    OperationDefinition(
        operation_id="discover_metrics",
        name="Discover Metrics",
        description="Discover available metrics in Prometheus with their types and descriptions. "
        "Metrics are grouped by type (counter, gauge, histogram, summary). "
        "Use the search parameter to filter by name pattern. Limited to top 100 per type "
        "to prevent context overflow.",
        category="discovery",
        parameters=[
            {
                "name": "search",
                "type": "string",
                "required": False,
                "description": "Filter metrics by name pattern (substring match, e.g., 'cpu', 'http', 'node')",
            },
        ],
        example="discover_metrics(search='cpu')",
    ),
    OperationDefinition(
        operation_id="list_alerts",
        name="List Active Alerts",
        description="List all active alerts in Prometheus. Returns alert name, state "
        "(firing/pending/inactive), labels, annotations, activeAt timestamp, and value.",
        category="discovery",
        parameters=[],
        example="list_alerts()",
    ),
    OperationDefinition(
        operation_id="list_alert_rules",
        name="List Alert Rules",
        description="List all alert and recording rules in Prometheus. Returns rule name, query, "
        "duration, labels, state, health, and count of active alerts. "
        "Optionally filter by rule type (alerting or recording).",
        category="discovery",
        parameters=[
            {
                "name": "type",
                "type": "string",
                "required": False,
                "description": "Filter by rule type: 'alerting' or 'recording'. Returns all if not specified.",
            },
        ],
        example="list_alert_rules(type='alerting')",
    ),
    OperationDefinition(
        operation_id="query_promql",
        name="Execute PromQL Query",
        description="Execute an arbitrary PromQL query against Prometheus. Supports both instant and "
        "range queries. Returns raw result with metadata (resultType, result count). "
        "REQUIRES APPROVAL: Arbitrary PromQL queries require WRITE trust level and must "
        "be approved by the operator. Use pre-defined operations (get_pod_cpu, get_red_metrics, "
        "etc.) for common queries instead.",
        category="query",
        parameters=[
            {
                "name": "query",
                "type": "string",
                "required": True,
                "description": "PromQL query string to execute",
            },
            {
                "name": "time_range",
                "type": "string",
                "required": False,
                "description": "Time range for range query (e.g., '1h', '30m', '6h'). If not set, uses instant query.",
            },
            {
                "name": "instant",
                "type": "boolean",
                "required": False,
                "description": "Force instant query (default: false). If true or no time_range, uses instant query.",
            },
        ],
        example="query_promql(query='up', instant=true)",
    ),
]
