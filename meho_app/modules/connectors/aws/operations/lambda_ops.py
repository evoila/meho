# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS Lambda Operation Definitions.

Operations for Lambda function management.
Note: File named lambda_ops.py (NOT lambda.py) to avoid Python keyword
collision (Pitfall 5).
"""

from meho_app.modules.connectors.base import OperationDefinition

LAMBDA_OPERATIONS = [
    OperationDefinition(
        operation_id="list_functions",
        name="List Lambda Functions",
        description=(
            "List all Lambda functions with runtime, handler, code size, "
            "memory, timeout, state, and layer information."
        ),
        category="compute",
        parameters=[
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": "AWS region override",
            },
        ],
        example="list_functions",
    ),
    OperationDefinition(
        operation_id="get_function",
        name="Get Lambda Function Details",
        description=(
            "Get detailed information about a specific Lambda function "
            "including configuration, runtime, handler, layers, and state."
        ),
        category="compute",
        parameters=[
            {
                "name": "function_name",
                "type": "string",
                "required": True,
                "description": "Function name or ARN",
            },
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": "AWS region override",
            },
        ],
        example="get_function function_name=my-function",
    ),
]
