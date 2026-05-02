# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
ArgoCD Sync Operations.

Operations for triggering sync and rollback (WRITE and DESTRUCTIVE).
"""

from meho_app.modules.connectors.base import OperationDefinition

SYNC_OPERATIONS = [
    OperationDefinition(
        operation_id="sync_application",
        name="Sync Application",
        description=(
            "Trigger an ArgoCD sync to apply desired state from git to the "
            "cluster. Use dry_run=true to preview changes. prune=true removes "
            "resources not in git (destructive). Always review server-side "
            "diff before syncing."
        ),
        category="sync",
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
                "required": False,
                "description": "Target revision (git SHA or tag) to sync to",
            },
            {
                "name": "prune",
                "type": "boolean",
                "required": False,
                "description": (
                    "Delete resources not in git (default: false). "
                    "WARNING: prune=true is destructive"
                ),
            },
            {
                "name": "dry_run",
                "type": "boolean",
                "required": False,
                "description": "Preview sync without applying changes (default: false)",
            },
            {
                "name": "app_namespace",
                "type": "string",
                "required": False,
                "description": "Application namespace (for apps in non-default namespace)",
            },
        ],
        example='{"application": "my-app", "dry_run": true}',
    ),
    OperationDefinition(
        operation_id="rollback_application",
        name="Rollback Application",
        description=(
            "Roll back an ArgoCD application to a previous deployment by "
            "deployment ID (from sync history, NOT a git revision SHA). "
            "This is a destructive operation that reverts the application "
            "to an earlier state."
        ),
        category="sync",
        parameters=[
            {
                "name": "application",
                "type": "string",
                "required": True,
                "description": "Application name",
            },
            {
                "name": "deployment_id",
                "type": "integer",
                "required": True,
                "description": (
                    "Deployment ID from sync history (integer, NOT a git revision SHA)"
                ),
            },
            {
                "name": "app_namespace",
                "type": "string",
                "required": False,
                "description": "Application namespace (for apps in non-default namespace)",
            },
        ],
        example='{"application": "my-app", "deployment_id": 3}',
    ),
]
