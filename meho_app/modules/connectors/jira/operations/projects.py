# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Jira Project Operations.

Discovery operation for listing accessible projects.
"""

from meho_app.modules.connectors.base import OperationDefinition

PROJECT_OPERATIONS = [
    OperationDefinition(
        operation_id="list_projects",
        name="List Projects",
        description="List accessible Jira projects. Returns project key, name, "
        "and project type. Use the optional search parameter to filter "
        "by project name.",
        category="projects",
        parameters=[
            {
                "name": "max_results",
                "type": "integer",
                "required": False,
                "description": "Maximum results to return (default: 50)",
            },
            {
                "name": "search",
                "type": "string",
                "required": False,
                "description": "Filter projects by name (case-insensitive substring match)",
            },
        ],
        example='{"search": "prod"}',
    ),
]
