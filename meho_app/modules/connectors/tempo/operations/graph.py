# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tempo Graph Operations.

Service dependency graph and trace-derived metrics.
"""

from meho_app.modules.connectors.base import OperationDefinition

GRAPH_OPERATIONS = [
    OperationDefinition(
        operation_id="get_service_graph",
        name="Get Service Graph",
        description="Retrieve service dependency graph with two tables: nodes (service_name | "
        "span_count | error_rate | avg_duration_ms) and edges (source -> target | "
        "call_rate | error_rate | p50_ms | p95_ms). Includes summary header: total "
        "services, total edges, highest error rate service, highest latency edge. "
        "Requires metrics-generator enabled in Tempo.",
        category="graph",
        parameters=[
            {
                "name": "time_range",
                "type": "string",
                "required": False,
                "description": "Time range for graph data (e.g., '1h', '30m'). Default: 1h",
            },
        ],
        example="get_service_graph(time_range='1h')",
    ),
    OperationDefinition(
        operation_id="get_trace_metrics",
        name="Get Trace Metrics",
        description="Get trace-derived metrics per service: span_count, error_count, "
        "avg_duration_ms, p95_duration_ms. Derived from recent trace search results.",
        category="graph",
        parameters=[
            {
                "name": "service_name",
                "type": "string",
                "required": False,
                "description": "Filter to a specific service (omit for all services)",
            },
            {
                "name": "time_range",
                "type": "string",
                "required": False,
                "description": "Time range for metrics (e.g., '1h', '30m'). Default: 1h",
            },
        ],
        example="get_trace_metrics(service_name='checkout-service')",
    ),
]
