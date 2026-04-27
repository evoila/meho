# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GitHub Pull Request Operations.

Operations for listing and getting pull request details.
"""

from meho_app.modules.connectors.base import OperationDefinition

PULL_OPERATIONS = [
    OperationDefinition(
        operation_id="list_pull_requests",
        name="List Pull Requests",
        description=(
            "List pull requests in a repository with title, state, author, "
            "and branch info. Supports filtering by state (open/closed/all)."
        ),
        category="pull_requests",
        parameters=[
            {
                "name": "repo",
                "type": "string",
                "required": True,
                "description": "Repository name (without org prefix)",
            },
            {
                "name": "state",
                "type": "string",
                "required": False,
                "description": "Filter by state: open, closed, all (default: all)",
            },
        ],
        example='{"repo": "my-service", "state": "open"}',
        response_entity_type="PullRequest",
        response_identifier_field="number",
        response_display_name_field="title",
    ),
    OperationDefinition(
        operation_id="get_pull_request",
        name="Get Pull Request",
        description=(
            "Get detailed pull request information including title, state, "
            "author, head/base refs, merge status, and timestamps."
        ),
        category="pull_requests",
        parameters=[
            {
                "name": "repo",
                "type": "string",
                "required": True,
                "description": "Repository name (without org prefix)",
            },
            {
                "name": "pull_number",
                "type": "integer",
                "required": True,
                "description": "Pull request number",
            },
        ],
        example='{"repo": "my-service", "pull_number": 42}',
        response_entity_type="PullRequest",
        response_identifier_field="number",
        response_display_name_field="title",
    ),
]
