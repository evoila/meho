# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
ArgoCD Resource Operations.

Operations for inspecting resource trees, managed resources, and events.
"""

from meho_app.modules.connectors.base import OperationDefinition

APPLICATION_NAME = "Application name"
DESC_APPLICATION_NAMESPACE_FOR_APPS_IN = "Application namespace (for apps in non-default namespace)"

RESOURCE_OPERATIONS = [
    OperationDefinition(
        operation_id="get_resource_tree",
        name="Get Resource Tree",
        description=(
            "View the resource tree of an ArgoCD application showing managed "
            "K8s resources (Deployments, ReplicaSets, Pods, Services) with "
            "health status. Returns top-level resources plus one level of children."
        ),
        category="resources",
        parameters=[
            {
                "name": "application",
                "type": "string",
                "required": True,
                "description": APPLICATION_NAME,
            },
            {
                "name": "app_namespace",
                "type": "string",
                "required": False,
                "description": DESC_APPLICATION_NAMESPACE_FOR_APPS_IN,
            },
        ],
        example='{"application": "my-app"}',
    ),
    OperationDefinition(
        operation_id="get_managed_resources",
        name="Get Managed Resources",
        description=(
            "View managed resources with live vs desired state diff status for drift detection."
        ),
        category="resources",
        parameters=[
            {
                "name": "application",
                "type": "string",
                "required": True,
                "description": APPLICATION_NAME,
            },
            {
                "name": "app_namespace",
                "type": "string",
                "required": False,
                "description": DESC_APPLICATION_NAMESPACE_FOR_APPS_IN,
            },
            {
                "name": "group",
                "type": "string",
                "required": False,
                "description": "Filter by API group (e.g. 'apps', 'networking.k8s.io')",
            },
            {
                "name": "kind",
                "type": "string",
                "required": False,
                "description": "Filter by resource kind (e.g. 'Deployment', 'Service')",
            },
        ],
        example='{"application": "my-app", "kind": "Deployment"}',
    ),
    OperationDefinition(
        operation_id="get_application_events",
        name="Get Application Events",
        description=(
            "View K8s events for resources managed by an ArgoCD application. "
            "Shows warnings, errors, and lifecycle events."
        ),
        category="resources",
        parameters=[
            {
                "name": "application",
                "type": "string",
                "required": True,
                "description": APPLICATION_NAME,
            },
            {
                "name": "app_namespace",
                "type": "string",
                "required": False,
                "description": DESC_APPLICATION_NAMESPACE_FOR_APPS_IN,
            },
        ],
        example='{"application": "my-app"}',
    ),
]
