# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Kubernetes Networking Operations - Services, Ingresses, Endpoints

These are registered in the generic connector_operation table
so the agent can discover them via search_operations.
"""

from meho_app.modules.connectors.base import OperationDefinition

NETWORKING_OPERATIONS = [
    # ==========================================================================
    # Services
    # ==========================================================================
    OperationDefinition(
        operation_id="list_services",
        name="List Services",
        description="List all services in a namespace or across all namespaces. Returns type, "
        "cluster IP, external IP, and ports.",
        category="networking",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": False,
                "description": "Namespace to list from. If not specified, lists from all namespaces.",
            },
            {
                "name": "label_selector",
                "type": "string",
                "required": False,
                "description": "Filter by label selector",
            },
        ],
        example="list_services(namespace='default')",
        response_entity_type="Service",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_service",
        name="Get Service",
        description="Get details about a specific service including cluster IP, ports, and selectors.",
        category="networking",
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
        example="get_service(name='nginx', namespace='default')",
        response_entity_type="Service",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    # ==========================================================================
    # Ingresses
    # ==========================================================================
    OperationDefinition(
        operation_id="list_ingresses",
        name="List Ingresses",
        description="List all ingresses in a namespace or across all namespaces.",
        category="networking",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": False,
                "description": "Namespace to list from. If not specified, lists from all namespaces.",
            },
        ],
        example="list_ingresses(namespace='default')",
        response_entity_type="Ingress",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_ingress",
        name="Get Ingress",
        description="Get details about a specific ingress including hosts, paths, and backends.",
        category="networking",
        parameters=[
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": "Name of the ingress",
            },
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Namespace the ingress is in",
            },
        ],
        example="get_ingress(name='web-ingress', namespace='default')",
        response_entity_type="Ingress",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    # ==========================================================================
    # Endpoints
    # ==========================================================================
    OperationDefinition(
        operation_id="list_endpoints",
        name="List Endpoints",
        description="List all endpoints in a namespace. Endpoints show which pods back a service.",
        category="networking",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": False,
                "description": "Namespace to list from. Defaults to 'default'.",
            },
        ],
        example="list_endpoints(namespace='default')",
        response_entity_type="Endpoints",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_endpoints",
        name="Get Endpoints",
        description="Get endpoints for a specific service showing ready and not-ready addresses.",
        category="networking",
        parameters=[
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": "Name of the endpoints (usually same as service name)",
            },
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Namespace the endpoints are in",
            },
        ],
        example="get_endpoints(name='nginx', namespace='default')",
        response_entity_type="Endpoints",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    # ==========================================================================
    # NetworkPolicies
    # ==========================================================================
    OperationDefinition(
        operation_id="list_network_policies",
        name="List Network Policies",
        description="List all NetworkPolicies in a namespace.",
        category="networking",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": False,
                "description": "Namespace to list from. Defaults to 'default'.",
            },
        ],
        example="list_network_policies(namespace='default')",
        response_entity_type="NetworkPolicy",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_network_policy",
        name="Get Network Policy",
        description="Get details about a specific NetworkPolicy including ingress/egress rules.",
        category="networking",
        parameters=[
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": "Name of the NetworkPolicy",
            },
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Namespace the NetworkPolicy is in",
            },
        ],
        example="get_network_policy(name='deny-all', namespace='default')",
        response_entity_type="NetworkPolicy",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
]
