# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GitHub Deployment and Commit Status Operations.

Operations for listing deployments and checking commit CI/CD status.
"""

from meho_app.modules.connectors.base import OperationDefinition

DEPLOYMENT_OPERATIONS = [
    OperationDefinition(
        operation_id="list_deployments",
        name="List Deployments",
        description=(
            "List deployments for a repository with environment, SHA, ref, "
            "and status history. Optionally filter by environment name."
        ),
        category="deployments",
        parameters=[
            {
                "name": "repo",
                "type": "string",
                "required": True,
                "description": "Repository name (without org prefix)",
            },
            {
                "name": "environment",
                "type": "string",
                "required": False,
                "description": "Filter by deployment environment name (e.g., production, staging)",
            },
        ],
        example='{"repo": "my-service", "environment": "production"}',
        response_entity_type="Deployment",
        response_identifier_field="id",
        response_display_name_field="environment",
    ),
    OperationDefinition(
        operation_id="get_commit_status",
        name="Get Commit Status",
        description=(
            "Get combined CI/CD status for a commit by merging both legacy "
            "commit statuses and GitHub Actions check runs. Queries two "
            "endpoints to provide a complete picture of all CI/CD checks."
        ),
        category="checks",
        parameters=[
            {
                "name": "repo",
                "type": "string",
                "required": True,
                "description": "Repository name (without org prefix)",
            },
            {
                "name": "ref",
                "type": "string",
                "required": True,
                "description": "Git ref (SHA, branch, or tag) to check status for",
            },
        ],
        example='{"repo": "my-service", "ref": "main"}',
        response_entity_type="CommitStatus",
        response_identifier_field="state",
        response_display_name_field="state",
    ),
]
