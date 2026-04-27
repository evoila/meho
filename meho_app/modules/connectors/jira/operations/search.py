# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Jira Search Operations.

Structured search (agent never writes JQL directly), recent changes,
and raw JQL escape hatch (classified as WRITE for approval).
"""

from meho_app.modules.connectors.base import OperationDefinition

SEARCH_OPERATIONS = [
    OperationDefinition(
        operation_id="search_issues",
        name="Search Jira Issues",
        description="Search Jira issues using structured filters. Builds JQL internally "
        "from the provided parameters -- the agent never needs to write JQL. "
        "Returns issues with summary, status, assignee, priority, type, labels, "
        "and a description preview (first 200 chars, markdown).",
        category="search",
        parameters=[
            {
                "name": "project",
                "type": "string",
                "required": False,
                "description": "Jira project key (e.g., 'PROJ')",
            },
            {
                "name": "status",
                "type": "string",
                "required": False,
                "description": "Issue status filter (e.g., 'Open', 'In Progress', 'Done')",
            },
            {
                "name": "type",
                "type": "string",
                "required": False,
                "description": "Issue type filter (e.g., 'Bug', 'Task', 'Story', 'Epic')",
            },
            {
                "name": "assignee",
                "type": "string",
                "required": False,
                "description": "Assignee filter (account ID or 'currentUser()')",
            },
            {
                "name": "priority",
                "type": "string",
                "required": False,
                "description": "Priority filter (e.g., 'Critical', 'High', 'Medium', 'Low')",
            },
            {
                "name": "labels",
                "type": "array",
                "required": False,
                "description": "Filter by labels (all must match)",
            },
            {
                "name": "text",
                "type": "string",
                "required": False,
                "description": "Full-text search across summary and description",
            },
            {
                "name": "updated_after",
                "type": "string",
                "required": False,
                "description": "Only issues updated after this ISO date (e.g., '2024-01-15')",
            },
            {
                "name": "created_after",
                "type": "string",
                "required": False,
                "description": "Only issues created after this ISO date (e.g., '2024-01-15')",
            },
            {
                "name": "max_results",
                "type": "integer",
                "required": False,
                "description": "Maximum results to return (default: 20, max: 100)",
            },
            {
                "name": "next_page_token",
                "type": "string",
                "required": False,
                "description": "Pagination token from previous search results",
            },
        ],
        example='{"project": "PROJ", "status": "Open", "type": "Bug", "max_results": 10}',
    ),
    OperationDefinition(
        operation_id="get_recent_changes",
        name="Get Recent Changes",
        description="Get issues recently created or updated in a project. Convenience "
        "shortcut that builds a time-windowed JQL query. Returns same format "
        "as search_issues.",
        category="search",
        parameters=[
            {
                "name": "project",
                "type": "string",
                "required": True,
                "description": "Jira project key (e.g., 'PROJ')",
            },
            {
                "name": "hours",
                "type": "integer",
                "required": False,
                "description": "Look back window in hours (default: 24)",
            },
            {
                "name": "max_results",
                "type": "integer",
                "required": False,
                "description": "Maximum results to return (default: 20)",
            },
        ],
        example='{"project": "PROJ", "hours": 12}',
    ),
    OperationDefinition(
        operation_id="search_by_jql",
        name="Search by JQL",
        description="Execute a raw JQL query for complex searches the structured operations "
        "cannot express. Escape hatch -- requires approval because arbitrary JQL "
        "can be expensive or scan large datasets. Use search_issues first for "
        "common queries.",
        category="search",
        parameters=[
            {
                "name": "jql",
                "type": "string",
                "required": True,
                "description": "Raw JQL query string",
            },
            {
                "name": "max_results",
                "type": "integer",
                "required": False,
                "description": "Maximum results to return (default: 20)",
            },
            {
                "name": "next_page_token",
                "type": "string",
                "required": False,
                "description": "Pagination token from previous search results",
            },
        ],
        example='{"jql": "project = PROJ AND priority = Critical AND updated >= -1d"}',
    ),
]
