# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tempo Query Operations.

Escape hatch for arbitrary TraceQL queries.
"""

from meho_app.modules.connectors.base import OperationDefinition

QUERY_OPERATIONS = [
    OperationDefinition(
        operation_id="query_traceql",
        name="Execute TraceQL Query",
        description="Execute a raw TraceQL query against Tempo. REQUIRES APPROVAL: Arbitrary "
        "TraceQL queries require WRITE trust level and must be approved by the operator. "
        "Use pre-defined operations first. Common patterns: "
        "`{span.http.status_code >= 500}`, "
        '`{resource.service.name = "api"} && {span.db.system = "redis"}`.',
        category="query",
        parameters=[
            {
                "name": "traceql",
                "type": "string",
                "required": True,
                "description": "Raw TraceQL expression to execute",
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
                "description": "Maximum number of traces to return. Default: 20",
            },
        ],
        example="query_traceql(traceql='{span.http.status_code >= 500}', time_range='1h')",
    ),
]
