# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tempo Trace Operations.

Search, retrieval, progressive disclosure, and convenience shortcuts
for distributed trace analysis.
"""

from meho_app.modules.connectors.base import OperationDefinition

TRACE_OPERATIONS = [
    OperationDefinition(
        operation_id="search_traces",
        name="Search Traces",
        description="Search traces by service, operation, duration, and status. Returns compact "
        "one-liner per trace: trace_id | root_service | root_operation | duration_ms | "
        "span_count | error_count. Default 20 traces, 1 hour range.",
        category="traces",
        parameters=[
            {
                "name": "service_name",
                "type": "string",
                "required": False,
                "description": "Service name to filter traces by",
            },
            {
                "name": "operation",
                "type": "string",
                "required": False,
                "description": "Operation/span name to filter traces by",
            },
            {
                "name": "min_duration",
                "type": "string",
                "required": False,
                "description": "Minimum trace duration (e.g., '100ms', '1s')",
            },
            {
                "name": "max_duration",
                "type": "string",
                "required": False,
                "description": "Maximum trace duration (e.g., '5s', '10s')",
            },
            {
                "name": "status",
                "type": "string",
                "required": False,
                "description": "Trace status filter: 'error' or 'ok'",
            },
            {
                "name": "tags",
                "type": "object",
                "required": False,
                "description": "Key:value tag pairs for filtering (e.g., {'http.method': 'POST'})",
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
                "description": "Maximum number of traces to return. Default: 20",
            },
        ],
        example="search_traces(service_name='checkout-service', min_duration='500ms', time_range='1h')",
    ),
    OperationDefinition(
        operation_id="get_trace",
        name="Get Trace",
        description="Retrieve full trace as flat span table: timestamp | service | operation | "
        "duration_ms | status | span_id | parent_span_id. Core diagnostic attributes only "
        "(HTTP method, url, status_code when present). Top 50 spans by duration if trace "
        "has many spans. Use get_span_details for deep dive into individual spans.",
        category="traces",
        parameters=[
            {
                "name": "trace_id",
                "type": "string",
                "required": True,
                "description": "Trace ID to retrieve (hex string)",
            },
        ],
        example="get_trace(trace_id='abc123def456')",
    ),
    OperationDefinition(
        operation_id="get_span_details",
        name="Get Span Details",
        description="Get full unredacted details for a single span including all custom tags, "
        "db.statement, exception stacktrace, and resource attributes. Use after get_trace "
        "to deep-dive into a specific span.",
        category="traces",
        parameters=[
            {
                "name": "trace_id",
                "type": "string",
                "required": True,
                "description": "Trace ID containing the span",
            },
            {
                "name": "span_id",
                "type": "string",
                "required": True,
                "description": "Span ID to retrieve details for",
            },
        ],
        example="get_span_details(trace_id='abc123', span_id='span456')",
    ),
    OperationDefinition(
        operation_id="get_slow_traces",
        name="Get Slow Traces",
        description="Find slow traces (default >1s). Shortcut for search_traces pre-filtered by "
        "duration threshold. Same compact one-liner output.",
        category="traces",
        parameters=[
            {
                "name": "service_name",
                "type": "string",
                "required": False,
                "description": "Service name to filter traces by",
            },
            {
                "name": "min_duration",
                "type": "string",
                "required": False,
                "description": "Duration threshold (default '1s'). Traces slower than this are returned",
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
                "description": "Maximum number of traces to return. Default: 20",
            },
        ],
        example="get_slow_traces(service_name='api-gateway', min_duration='2s')",
    ),
    OperationDefinition(
        operation_id="get_error_traces",
        name="Get Error Traces",
        description="Find traces with errors. Shortcut for search_traces pre-filtered to "
        "status=error. Same compact one-liner output.",
        category="traces",
        parameters=[
            {
                "name": "service_name",
                "type": "string",
                "required": False,
                "description": "Service name to filter traces by",
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
                "description": "Maximum number of traces to return. Default: 20",
            },
        ],
        example="get_error_traces(service_name='payment-service', time_range='30m')",
    ),
]
