# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
ArgoCD History Operations.

Operations for sync history and revision metadata inspection.
"""

from meho_app.modules.connectors.base import OperationDefinition

HISTORY_OPERATIONS = [
    OperationDefinition(
        operation_id="get_sync_history",
        name="Get Sync History",
        description=(
            "View sync/operation history showing deployment revisions, "
            "timestamps, and deployment IDs. Use deployment IDs for "
            "rollback operations."
        ),
        category="history",
        parameters=[
            {
                "name": "application",
                "type": "string",
                "required": True,
                "description": "Application name",
            },
            {
                "name": "app_namespace",
                "type": "string",
                "required": False,
                "description": "Application namespace (for apps in non-default namespace)",
            },
            {
                "name": "max_entries",
                "type": "integer",
                "required": False,
                "description": "Maximum history entries to return (default: 10)",
            },
        ],
        example='{"application": "my-app", "max_entries": 5}',
    ),
    OperationDefinition(
        operation_id="get_revision_metadata",
        name="Get Revision Metadata",
        description=(
            "View revision metadata including commit message, author, and "
            "date for a deployed revision."
        ),
        category="history",
        parameters=[
            {
                "name": "application",
                "type": "string",
                "required": True,
                "description": "Application name",
            },
            {
                "name": "revision",
                "type": "string",
                "required": True,
                "description": "Git SHA or tag of the revision",
            },
            {
                "name": "app_namespace",
                "type": "string",
                "required": False,
                "description": "Application namespace (for apps in non-default namespace)",
            },
        ],
        example='{"application": "my-app", "revision": "abc1234"}',
    ),
]
