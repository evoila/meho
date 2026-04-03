# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
ArgoCD Diff Operations.

Operations for server-side diff (live vs desired state).
"""

from meho_app.modules.connectors.base import OperationDefinition

DIFF_OPERATIONS = [
    OperationDefinition(
        operation_id="get_server_diff",
        name="Get Server-Side Diff",
        description=(
            "View server-side diff showing differences between live state "
            "and desired state. Use this to understand what a sync would "
            "change before triggering it."
        ),
        category="diff",
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
        ],
        example='{"application": "my-app"}',
    ),
]
