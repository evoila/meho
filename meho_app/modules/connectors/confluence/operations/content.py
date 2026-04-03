# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Confluence Content Operations.

CRUD operations for Confluence pages: get details, create, update,
and add footer comment. Write operations accept markdown and
auto-convert to ADF for the Confluence v2 API.
"""

from meho_app.modules.connectors.base import OperationDefinition

CONFLUENCE_PAGE_ID = "Confluence page ID"

CONTENT_OPERATIONS = [
    OperationDefinition(
        operation_id="get_page",
        name="Get Page",
        description="Get full page content with metadata. Body is converted from ADF "
        "to markdown automatically. Includes title, labels, version info, "
        "last modifier, and child page references.",
        category="content",
        parameters=[
            {
                "name": "page_id",
                "type": "string",
                "required": True,
                "description": CONFLUENCE_PAGE_ID,
            },
        ],
        example='{"page_id": "12345678"}',
        response_entity_type="Page",
        response_identifier_field="id",
        response_display_name_field="title",
    ),
    OperationDefinition(
        operation_id="create_page",
        name="Create Page",
        description="Create a new Confluence page. Markdown content is auto-converted "
        "to Atlassian Document Format (ADF) for the API. Requires WRITE "
        "trust approval.",
        category="content",
        parameters=[
            {
                "name": "space_key",
                "type": "string",
                "required": True,
                "description": "Confluence space key (e.g., 'DEV')",
            },
            {"name": "title", "type": "string", "required": True, "description": "Page title"},
            {
                "name": "content",
                "type": "string",
                "required": True,
                "description": "Page content in markdown (auto-converted to ADF)",
            },
            {
                "name": "parent_page_id",
                "type": "string",
                "required": False,
                "description": "Parent page ID. If omitted, creates at space root.",
            },
        ],
        example='{"space_key": "OPS", "title": "Runbook: Auth Service", '
        '"content": "## Overview\\nSteps for auth service recovery..."}',
    ),
    OperationDefinition(
        operation_id="update_page",
        name="Update Page",
        description="Update page content. Version handling is automatic -- the connector "
        "fetches the current version, increments it, and retries once on "
        "version conflict. Requires WRITE trust approval.",
        category="content",
        parameters=[
            {
                "name": "page_id",
                "type": "string",
                "required": True,
                "description": CONFLUENCE_PAGE_ID,
            },
            {
                "name": "content",
                "type": "string",
                "required": True,
                "description": "New page content in markdown (auto-converted to ADF)",
            },
            {
                "name": "title",
                "type": "string",
                "required": False,
                "description": "New page title (keeps current title if omitted)",
            },
        ],
        example='{"page_id": "12345678", "content": "## Updated\\nNew content here..."}',
    ),
    OperationDefinition(
        operation_id="add_comment",
        name="Add Comment",
        description="Add a footer comment to a Confluence page. Markdown body is "
        "auto-converted to Atlassian Document Format (ADF). Requires WRITE "
        "trust approval.",
        category="content",
        parameters=[
            {
                "name": "page_id",
                "type": "string",
                "required": True,
                "description": CONFLUENCE_PAGE_ID,
            },
            {
                "name": "body",
                "type": "string",
                "required": True,
                "description": "Comment body in markdown (auto-converted to ADF)",
            },
        ],
        example='{"page_id": "12345678", "body": "Investigation complete. '
        'Root cause: **memory leak** in auth service."}',
    ),
]
