# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Loki Query Operations.

Escape hatch for arbitrary LogQL queries.
"""

from meho_app.modules.connectors.base import OperationDefinition

QUERY_OPERATIONS = [
    OperationDefinition(
        operation_id="query_logql",
        name="Execute LogQL Query",
        description="Execute an arbitrary LogQL query against Loki. Supports both log queries "
        "(returns lines) and metric queries (count_over_time, rate, etc.). "
        "REQUIRES APPROVAL: Arbitrary LogQL queries require WRITE trust level and must "
        "be approved by the operator. Use pre-defined operations first.",
        category="query",
        parameters=[
            {
                "name": "query",
                "type": "string",
                "required": True,
                "description": "Raw LogQL expression to execute",
            },
            {
                "name": "time_range",
                "type": "string",
                "required": False,
                "description": "Time range for query (e.g., '1h', '30m', '6h'). Default: 1h",
            },
            {
                "name": "limit",
                "type": "integer",
                "required": False,
                "description": "Maximum number of results to return. Default: 100",
            },
        ],
        example='query_logql(query=\'{namespace="payments"} |= "timeout" | json | duration > 5s\')',
    ),
]
