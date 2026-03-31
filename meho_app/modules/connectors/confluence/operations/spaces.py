# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Confluence Space Operations.

Discovery operation for listing accessible spaces.
"""

from meho_app.modules.connectors.base import OperationDefinition

SPACE_OPERATIONS = [
    OperationDefinition(
        operation_id="list_spaces",
        name="List Spaces",
        description="List accessible Confluence spaces with keys and names. Use this "
        "to discover available spaces before scoping searches. Returns "
        "space ID, key, name, type, and URL.",
        category="spaces",
        parameters=[
            {
                "name": "max_results",
                "type": "integer",
                "required": False,
                "description": "Maximum results to return (default: 25)",
            },
        ],
        example='{"max_results": 10}',
    ),
]
