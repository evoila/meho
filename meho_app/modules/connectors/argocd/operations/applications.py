# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
ArgoCD Application Operations.

Operations for listing and inspecting ArgoCD applications.
"""

from meho_app.modules.connectors.base import OperationDefinition

APPLICATION_OPERATIONS = [
    OperationDefinition(
        operation_id="list_applications",
        name="List Applications",
        description=(
            "List ArgoCD applications with sync status, health status, source "
            "repo, and destination cluster/namespace."
        ),
        category="applications",
        parameters=[
            {
                "name": "project",
                "type": "string",
                "required": False,
                "description": "Filter by ArgoCD project name",
            },
            {
                "name": "selector",
                "type": "string",
                "required": False,
                "description": "Label selector to filter applications (e.g. 'team=platform')",
            },
        ],
        example='{"project": "default"}',
        response_entity_type="Application",
        response_identifier_field="name",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_application",
        name="Get Application",
        description=(
            "Get detailed ArgoCD application status including composite "
            "sync+health state, source revision, conditions, and summary "
            "of managed resources."
        ),
        category="applications",
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
        response_entity_type="Application",
        response_identifier_field="name",
        response_display_name_field="name",
    ),
]
