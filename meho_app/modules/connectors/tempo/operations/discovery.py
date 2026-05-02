# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tempo Discovery Operations.

Tag listing and tag value enumeration for trace filtering.
"""

from meho_app.modules.connectors.base import OperationDefinition

DISCOVERY_OPERATIONS = [
    OperationDefinition(
        operation_id="list_tags",
        name="List Trace Tags",
        description="Discover available trace tags (span attributes). Returns tag names that can "
        "be used for filtering in search_traces. Common tags: service.name, http.method, "
        "http.status_code, db.system.",
        category="discovery",
        parameters=[],
        example="list_tags()",
    ),
    OperationDefinition(
        operation_id="list_tag_values",
        name="List Tag Values",
        description="Get all values for a specific trace tag. Use after list_tags to discover "
        "what services, HTTP methods, or database systems are represented in traces.",
        category="discovery",
        parameters=[
            {
                "name": "tag",
                "type": "string",
                "required": True,
                "description": "Tag name to get values for (e.g., 'service.name', 'http.method')",
            },
        ],
        example="list_tag_values(tag='service.name')",
    ),
]
