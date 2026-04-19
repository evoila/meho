# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Confluence Search Operations.

Structured search (agent never writes CQL directly), recent changes,
and raw CQL escape hatch (classified as WRITE for approval).
"""

from meho_app.modules.connectors.base import OperationDefinition

SEARCH_OPERATIONS = [
    OperationDefinition(
        operation_id="search_pages",
        name="Search Confluence Pages",
        description="Search Confluence pages using structured filters. Builds CQL internally "
        "from the provided parameters -- the agent never needs to write CQL. "
        "Returns pages with title, space, last modified date, and excerpt.",
        category="search",
        parameters=[
            {
                "name": "space_key",
                "type": "string",
                "required": False,
                "description": "Confluence space key (e.g., 'DEV', 'OPS')",
            },
            {
                "name": "title",
                "type": "string",
                "required": False,
                "description": "Page title filter (fuzzy match via CQL ~)",
            },
            {
                "name": "labels",
                "type": "array",
                "required": False,
                "description": "Filter by labels (each label gets its own CQL clause)",
            },
            {
                "name": "content_type",
                "type": "string",
                "required": False,
                "description": "Content type filter (default: 'page'). Options: page, blogpost",
            },
            {
                "name": "modified_after",
                "type": "string",
                "required": False,
                "description": "Only pages modified after this ISO date (e.g., '2024-01-15')",
            },
            {
                "name": "text",
                "type": "string",
                "required": False,
                "description": "Full-text search across page content",
            },
            {
                "name": "max_results",
                "type": "integer",
                "required": False,
                "description": "Maximum results to return (default: 20, max: 100)",
            },
        ],
        example='{"space_key": "OPS", "text": "runbook", "max_results": 10}',
    ),
    OperationDefinition(
        operation_id="get_recent_changes",
        name="Get Recent Changes",
        description="Get recently modified Confluence pages. Useful for checking if runbooks "
        "or docs changed before an incident. Builds a time-windowed CQL query "
        "automatically. Returns same format as search_pages.",
        category="search",
        parameters=[
            {
                "name": "space_key",
                "type": "string",
                "required": False,
                "description": "Confluence space key to scope results (e.g., 'OPS')",
            },
            {
                "name": "hours",
                "type": "integer",
                "required": False,
                "description": "Look back window in hours (default: 24)",
            },
            {
                "name": "content_type",
                "type": "string",
                "required": False,
                "description": "Content type filter (default: 'page')",
            },
            {
                "name": "max_results",
                "type": "integer",
                "required": False,
                "description": "Maximum results to return (default: 20)",
            },
        ],
        example='{"space_key": "OPS", "hours": 12}',
    ),
    OperationDefinition(
        operation_id="search_by_cql",
        name="Search by CQL",
        description="Execute a raw CQL query for complex searches the structured operations "
        "cannot express. Escape hatch -- requires approval because arbitrary CQL "
        "can be expensive or scan large datasets. Use search_pages first for "
        "common queries.",
        category="search",
        parameters=[
            {
                "name": "cql",
                "type": "string",
                "required": True,
                "description": "Raw CQL query string",
            },
            {
                "name": "max_results",
                "type": "integer",
                "required": False,
                "description": "Maximum results to return (default: 20)",
            },
        ],
        example='{"cql": "space = OPS AND label = runbook AND lastModified >= now(\\"-7d\\")"}',
    ),
]
