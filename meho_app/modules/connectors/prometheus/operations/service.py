# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Prometheus Service Operations.

RED metrics (Rate, Error rate, Duration) for services.
"""

from meho_app.modules.connectors.base import OperationDefinition

SERVICE_OPERATIONS = [
    OperationDefinition(
        operation_id="get_red_metrics",
        name="Get RED Metrics",
        description="Get RED (Rate, Error rate, Duration) metrics for a service. Returns request rate, "
        "error rate, and latency percentiles (p50, p95, p99) as summary statistics. "
        "The service_name is matched against multiple common label names (service, service_name, job). "
        "The histogram_metric parameter defaults to 'http_request_duration_seconds' but can be "
        "overridden for services using custom metric names.",
        category="service",
        parameters=[
            {
                "name": "service_name",
                "type": "string",
                "required": True,
                "description": "Name of the service to query RED metrics for",
            },
            {
                "name": "time_range",
                "type": "string",
                "required": False,
                "description": "Time range to query (e.g., '1h', '30m', '6h', '24h', '7d'). Default: '1h'",
            },
            {
                "name": "histogram_metric",
                "type": "string",
                "required": False,
                "description": "Name of the histogram metric for latency (default: 'http_request_duration_seconds')",
            },
        ],
        example="get_red_metrics(service_name='api-gateway')",
    ),
]
