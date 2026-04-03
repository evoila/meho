# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GitHub Repository Operations.

Operations for listing repositories in the configured GitHub organization.
"""

from meho_app.modules.connectors.base import OperationDefinition

REPO_OPERATIONS = [
    OperationDefinition(
        operation_id="list_repositories",
        name="List Repositories",
        description=(
            "List repositories in the configured GitHub organization with "
            "name, description, default branch, language, and visibility."
        ),
        category="repositories",
        parameters=[
            {
                "name": "type",
                "type": "string",
                "required": False,
                "description": "Filter by repo type: all, public, private, forks, sources, member (default: all)",
            },
            {
                "name": "sort",
                "type": "string",
                "required": False,
                "description": "Sort by: created, updated, pushed, full_name (default: pushed)",
            },
        ],
        example='{"type": "all", "sort": "pushed"}',
        response_entity_type="Repository",
        response_identifier_field="full_name",
        response_display_name_field="name",
    ),
]
