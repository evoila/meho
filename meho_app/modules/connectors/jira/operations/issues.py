# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Jira Issue Operations.

CRUD operations for Jira issues: get details, create, add comment,
and workflow transitions. Write operations accept markdown and
auto-convert to ADF for the Jira API.
"""

from meho_app.modules.connectors.base import OperationDefinition

ISSUE_OPERATIONS = [
    OperationDefinition(
        operation_id="get_issue",
        name="Get Issue Details",
        description="Get full details of a Jira issue including comments. Description "
        "and comments are returned as clean markdown (ADF is converted "
        "automatically). Custom field names are human-readable, never "
        "customfield_XXXXX.",
        category="issues",
        parameters=[
            {
                "name": "issue_key",
                "type": "string",
                "required": True,
                "description": "Jira issue key (e.g., 'PROJ-123')",
            },
        ],
        example='{"issue_key": "PROJ-123"}',
        response_entity_type="Issue",
        response_identifier_field="key",
        response_display_name_field="summary",
    ),
    OperationDefinition(
        operation_id="create_issue",
        name="Create Issue",
        description="Create a new Jira issue with markdown description. The markdown "
        "description is auto-converted to Atlassian Document Format (ADF) "
        "for the Jira API. Requires WRITE trust approval.",
        category="issues",
        parameters=[
            {
                "name": "project",
                "type": "string",
                "required": True,
                "description": "Jira project key (e.g., 'PROJ')",
            },
            {
                "name": "issue_type",
                "type": "string",
                "required": True,
                "description": "Issue type (e.g., 'Bug', 'Task', 'Story', 'Epic')",
            },
            {
                "name": "summary",
                "type": "string",
                "required": True,
                "description": "Issue summary/title",
            },
            {
                "name": "description",
                "type": "string",
                "required": False,
                "description": "Issue description in markdown (auto-converted to ADF)",
            },
            {
                "name": "priority",
                "type": "string",
                "required": False,
                "description": "Priority name (e.g., 'Critical', 'High', 'Medium', 'Low')",
            },
            {
                "name": "labels",
                "type": "array",
                "required": False,
                "description": "List of labels to apply",
            },
            {
                "name": "assignee",
                "type": "string",
                "required": False,
                "description": "Assignee account ID or email",
            },
        ],
        example='{"project": "PROJ", "issue_type": "Bug", "summary": "Login fails", '
        '"description": "## Steps\\n1. Go to login\\n2. Enter creds\\n3. Error 500"}',
    ),
    OperationDefinition(
        operation_id="add_comment",
        name="Add Comment",
        description="Add a comment to a Jira issue. The markdown body is auto-converted "
        "to Atlassian Document Format (ADF) for the Jira API. Requires WRITE "
        "trust approval.",
        category="issues",
        parameters=[
            {
                "name": "issue_key",
                "type": "string",
                "required": True,
                "description": "Jira issue key (e.g., 'PROJ-123')",
            },
            {
                "name": "body",
                "type": "string",
                "required": True,
                "description": "Comment body in markdown (auto-converted to ADF)",
            },
        ],
        example='{"issue_key": "PROJ-123", "body": "Investigation complete. '
        'Root cause: **memory leak** in auth service."}',
    ),
    OperationDefinition(
        operation_id="transition_issue",
        name="Transition Issue",
        description="Change the status of a Jira issue through its workflow. Fetches "
        "available transitions first, then performs the transition matching "
        "the target status name. If no matching transition is found, returns "
        "the list of available transitions. Requires WRITE trust approval.",
        category="issues",
        parameters=[
            {
                "name": "issue_key",
                "type": "string",
                "required": True,
                "description": "Jira issue key (e.g., 'PROJ-123')",
            },
            {
                "name": "target_status",
                "type": "string",
                "required": True,
                "description": "Target status name (e.g., 'In Progress', 'Done', 'To Do')",
            },
        ],
        example='{"issue_key": "PROJ-123", "target_status": "In Progress"}',
    ),
]
