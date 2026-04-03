# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Kubernetes Events Operations - Events and resource descriptions

These are registered in the generic connector_operation table
so the agent can discover them via search_operations.
"""

from meho_app.modules.connectors.base import OperationDefinition

EVENTS_OPERATIONS = [
    # ==========================================================================
    # Events
    # ==========================================================================
    OperationDefinition(
        operation_id="list_events",
        name="List Events",
        description="List events in a namespace. Useful for debugging issues like pod failures, "
        "image pull errors, and scheduling problems.",
        category="events",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": False,
                "description": "Namespace to list events from. If not specified, lists from all namespaces.",
            },
            {
                "name": "field_selector",
                "type": "string",
                "required": False,
                "description": "Filter by field (e.g., 'involvedObject.name=nginx')",
            },
            {
                "name": "limit",
                "type": "integer",
                "required": False,
                "description": "Maximum number of events to return (default: 100)",
            },
        ],
        example="list_events(namespace='default')",
        response_entity_type="Event",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_events_for_resource",
        name="Get Events for Resource",
        description="Get all events related to a specific resource (pod, deployment, node, etc.).",
        category="events",
        parameters=[
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": "Name of the resource",
            },
            {
                "name": "namespace",
                "type": "string",
                "required": False,
                "description": "Namespace the resource is in (not needed for cluster-scoped resources like nodes)",
            },
            {
                "name": "kind",
                "type": "string",
                "required": False,
                "description": "Kind of resource (Pod, Deployment, Node, etc.). Defaults to Pod.",
            },
        ],
        example="get_events_for_resource(name='nginx-abc123', namespace='default', kind='Pod')",
        response_entity_type="Event",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    # ==========================================================================
    # Resource Descriptions (combining resource + events)
    # ==========================================================================
    OperationDefinition(
        operation_id="describe_deployment",
        name="Describe Deployment",
        description="Get comprehensive deployment information including events, conditions, "
        "and replica status. Similar to 'kubectl describe deployment'.",
        category="events",
        parameters=[
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": "Name of the deployment",
            },
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Namespace the deployment is in",
            },
        ],
        example="describe_deployment(name='nginx', namespace='default')",
        response_entity_type="Deployment",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="describe_service",
        name="Describe Service",
        description="Get comprehensive service information including endpoints and events. "
        "Similar to 'kubectl describe service'.",
        category="events",
        parameters=[
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": "Name of the service",
            },
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Namespace the service is in",
            },
        ],
        example="describe_service(name='nginx', namespace='default')",
        response_entity_type="Service",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
]
