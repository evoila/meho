# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Loki Log Search Operations.

Search, error filtering, context retrieval, volume analysis, and pattern detection.
"""

from meho_app.modules.connectors.base import OperationDefinition

DESC_ARBITRARY_KEYVALUE_LABEL_PAIRS_FOR = "Arbitrary key:value label pairs for filtering"
DESC_SERVICE_NAME_TO_FILTER_LOGS = "Service name to filter logs by (maps to service_name label)"
KUBERNETES_NAMESPACE_TO_FILTER_LOGS_BY = "Kubernetes namespace to filter logs by"
POD_NAME_TO_FILTER_LOGS_BY = "Pod name to filter logs by"

LOG_SEARCH_OPERATIONS = [
    OperationDefinition(
        operation_id="search_logs",
        name="Search Logs",
        description="Search logs with label filters, severity, and text filter. Results include summary "
        "stats (total matched, severity breakdown, time range) plus structured log lines "
        "(timestamp | severity | source | message). Newest first. Agent builds label "
        "combinations; connector builds LogQL internally.",
        category="log_search",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": False,
                "description": KUBERNETES_NAMESPACE_TO_FILTER_LOGS_BY,
            },
            {
                "name": "pod",
                "type": "string",
                "required": False,
                "description": POD_NAME_TO_FILTER_LOGS_BY,
            },
            {
                "name": "service",
                "type": "string",
                "required": False,
                "description": DESC_SERVICE_NAME_TO_FILTER_LOGS,
            },
            {
                "name": "container",
                "type": "string",
                "required": False,
                "description": "Container name to filter logs by",
            },
            {
                "name": "labels",
                "type": "object",
                "required": False,
                "description": DESC_ARBITRARY_KEYVALUE_LABEL_PAIRS_FOR,
            },
            {
                "name": "severity",
                "type": "string",
                "required": False,
                "description": "Severity level filter: 'error', 'warn', 'info', 'debug'",
            },
            {
                "name": "text_filter",
                "type": "string",
                "required": False,
                "description": "Substring match in log lines (case-sensitive)",
            },
            {
                "name": "time_range",
                "type": "string",
                "required": False,
                "description": "Time range to search (e.g., '1h', '30m', '6h'). Default: 1h",
            },
            {
                "name": "limit",
                "type": "integer",
                "required": False,
                "description": "Maximum number of log lines to return. Default: 100",
            },
        ],
        example="search_logs(namespace='payments', severity='error', time_range='1h')",
    ),
    OperationDefinition(
        operation_id="get_error_logs",
        name="Get Error Logs",
        description="Retrieve error and warning logs. Shortcut for search_logs with severity "
        "pre-filtered to error/warn/fatal levels. Same output format.",
        category="log_search",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": False,
                "description": KUBERNETES_NAMESPACE_TO_FILTER_LOGS_BY,
            },
            {
                "name": "pod",
                "type": "string",
                "required": False,
                "description": POD_NAME_TO_FILTER_LOGS_BY,
            },
            {
                "name": "service",
                "type": "string",
                "required": False,
                "description": DESC_SERVICE_NAME_TO_FILTER_LOGS,
            },
            {
                "name": "container",
                "type": "string",
                "required": False,
                "description": "Container name to filter logs by",
            },
            {
                "name": "labels",
                "type": "object",
                "required": False,
                "description": DESC_ARBITRARY_KEYVALUE_LABEL_PAIRS_FOR,
            },
            {
                "name": "text_filter",
                "type": "string",
                "required": False,
                "description": "Substring match in log lines (case-sensitive)",
            },
            {
                "name": "time_range",
                "type": "string",
                "required": False,
                "description": "Time range to search (e.g., '1h', '30m', '6h'). Default: 1h",
            },
            {
                "name": "limit",
                "type": "integer",
                "required": False,
                "description": "Maximum number of log lines to return. Default: 100",
            },
        ],
        example="get_error_logs(namespace='checkout', time_range='30m')",
    ),
    OperationDefinition(
        operation_id="get_log_context",
        name="Get Log Context",
        description="Retrieve log lines surrounding a specific timestamp for incident context. "
        "Returns lines before and after the timestamp.",
        category="log_search",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Kubernetes namespace (required for context lookup)",
            },
            {
                "name": "pod",
                "type": "string",
                "required": False,
                "description": POD_NAME_TO_FILTER_LOGS_BY,
            },
            {
                "name": "service",
                "type": "string",
                "required": False,
                "description": DESC_SERVICE_NAME_TO_FILTER_LOGS,
            },
            {
                "name": "labels",
                "type": "object",
                "required": False,
                "description": DESC_ARBITRARY_KEYVALUE_LABEL_PAIRS_FOR,
            },
            {
                "name": "timestamp",
                "type": "string",
                "required": True,
                "description": "Center timestamp (ISO8601 or Unix nanoseconds) to get context around",
            },
            {
                "name": "before_lines",
                "type": "integer",
                "required": False,
                "description": "Number of log lines to retrieve before the timestamp. Default: 20",
            },
            {
                "name": "after_lines",
                "type": "integer",
                "required": False,
                "description": "Number of log lines to retrieve after the timestamp. Default: 20",
            },
        ],
        example="get_log_context(namespace='payments', timestamp='2026-03-05T10:30:00Z', before_lines=30)",
    ),
    OperationDefinition(
        operation_id="get_log_volume",
        name="Get Log Volume",
        description="Query log volume statistics over time. Returns counts/rates bucketed by time "
        "interval. Useful for detecting log spikes, outage windows, and volume trends. "
        "Returns stats only, no log lines.",
        category="log_search",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": False,
                "description": KUBERNETES_NAMESPACE_TO_FILTER_LOGS_BY,
            },
            {
                "name": "pod",
                "type": "string",
                "required": False,
                "description": POD_NAME_TO_FILTER_LOGS_BY,
            },
            {
                "name": "service",
                "type": "string",
                "required": False,
                "description": DESC_SERVICE_NAME_TO_FILTER_LOGS,
            },
            {
                "name": "labels",
                "type": "object",
                "required": False,
                "description": DESC_ARBITRARY_KEYVALUE_LABEL_PAIRS_FOR,
            },
            {
                "name": "severity",
                "type": "string",
                "required": False,
                "description": "Severity level filter: 'error', 'warn', 'info', 'debug'",
            },
            {
                "name": "time_range",
                "type": "string",
                "required": False,
                "description": "Time range to analyze (e.g., '6h', '24h', '7d'). Default: 6h",
            },
            {
                "name": "step",
                "type": "string",
                "required": False,
                "description": "Bucket interval (e.g., '5m', '15m'). Auto-resolved from time range if omitted.",
            },
        ],
        example="get_log_volume(namespace='payments', time_range='24h')",
    ),
    OperationDefinition(
        operation_id="get_log_patterns",
        name="Get Log Patterns",
        description="Detect repeating log patterns with occurrence counts. Groups similar log lines "
        "by their structural pattern. Returns pattern text with occurrence count and a "
        "sample line. Useful for identifying systemic errors vs one-off events.",
        category="log_search",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": False,
                "description": KUBERNETES_NAMESPACE_TO_FILTER_LOGS_BY,
            },
            {
                "name": "pod",
                "type": "string",
                "required": False,
                "description": POD_NAME_TO_FILTER_LOGS_BY,
            },
            {
                "name": "service",
                "type": "string",
                "required": False,
                "description": DESC_SERVICE_NAME_TO_FILTER_LOGS,
            },
            {
                "name": "labels",
                "type": "object",
                "required": False,
                "description": DESC_ARBITRARY_KEYVALUE_LABEL_PAIRS_FOR,
            },
            {
                "name": "time_range",
                "type": "string",
                "required": False,
                "description": "Time range to analyze (e.g., '1h', '6h'). Default: 1h",
            },
        ],
        example="get_log_patterns(namespace='checkout', time_range='1h')",
    ),
]
