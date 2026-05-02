# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GitHub Commit Operations.

Operations for listing commits and comparing refs.
"""

from meho_app.modules.connectors.base import OperationDefinition

COMMIT_OPERATIONS = [
    OperationDefinition(
        operation_id="list_commits",
        name="List Commits",
        description=(
            "List recent commits on a repository branch with SHA, author, "
            "message, and timestamp. Defaults to the repository's default "
            "branch if no branch is specified."
        ),
        category="commits",
        parameters=[
            {
                "name": "repo",
                "type": "string",
                "required": True,
                "description": "Repository name (without org prefix)",
            },
            {
                "name": "branch",
                "type": "string",
                "required": False,
                "description": "Branch name to list commits from (defaults to repo's default branch)",
            },
            {
                "name": "per_page",
                "type": "integer",
                "required": False,
                "description": "Number of commits per page (default: 30, max: 100)",
            },
        ],
        example='{"repo": "my-service", "branch": "main"}',
        response_entity_type="Commit",
        response_identifier_field="sha",
        response_display_name_field="message",
    ),
    OperationDefinition(
        operation_id="compare_refs",
        name="Compare Refs",
        description=(
            "Compare two git refs (branches, tags, or SHAs) to see commits "
            "and files changed between them. Shows status (ahead/behind/"
            "diverged/identical), commit list, and file-level changes."
        ),
        category="commits",
        parameters=[
            {
                "name": "repo",
                "type": "string",
                "required": True,
                "description": "Repository name (without org prefix)",
            },
            {
                "name": "base",
                "type": "string",
                "required": True,
                "description": "Base ref (branch, tag, or SHA) for comparison",
            },
            {
                "name": "head",
                "type": "string",
                "required": True,
                "description": "Head ref (branch, tag, or SHA) for comparison",
            },
        ],
        example='{"repo": "my-service", "base": "main", "head": "feature-branch"}',
        response_entity_type="Comparison",
        response_identifier_field="status",
        response_display_name_field="status",
    ),
]
