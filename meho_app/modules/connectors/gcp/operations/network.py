# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GCP Network Operation Definitions (TASK-102)

Operations for managing VPC networks, subnets, and firewall rules.
"""

from meho_app.modules.connectors.base import OperationDefinition

NETWORK_OPERATIONS = [
    # VPC Network Operations
    OperationDefinition(
        operation_id="list_networks",
        name="List VPC Networks",
        description="List all VPC networks in the project. Returns network name, routing mode, subnetworks, and peerings.",
        category="networking",
        parameters=[
            {
                "name": "filter",
                "type": "string",
                "required": False,
                "description": "Filter expression",
            },
        ],
        example="list_networks()",
        response_entity_type="Network",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_network",
        name="Get VPC Network Details",
        description="Get detailed information about a specific VPC network including routing configuration, subnetworks, and peering connections.",
        category="networking",
        parameters=[
            {
                "name": "network_name",
                "type": "string",
                "required": True,
                "description": "Name of the network",
            },
        ],
        example="get_network(network_name='default')",
        response_entity_type="Network",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    # Subnetwork Operations
    OperationDefinition(
        operation_id="list_subnetworks",
        name="List Subnetworks",
        description="List all subnetworks in the project. Returns subnetwork name, region, IP range, and parent network.",
        category="networking",
        parameters=[
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": "Region to list subnetworks from (default: all regions)",
            },
            {
                "name": "filter",
                "type": "string",
                "required": False,
                "description": "Filter expression",
            },
        ],
        example="list_subnetworks(region='us-central1')",
        response_entity_type="Subnetwork",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_subnetwork",
        name="Get Subnetwork Details",
        description="Get detailed information about a specific subnetwork including IP range, gateway, secondary ranges, and private Google access settings.",
        category="networking",
        parameters=[
            {
                "name": "subnetwork_name",
                "type": "string",
                "required": True,
                "description": "Name of the subnetwork",
            },
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": "Region of the subnetwork",
            },
        ],
        example="get_subnetwork(subnetwork_name='default', region='us-central1')",
        response_entity_type="Subnetwork",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    # Firewall Operations
    OperationDefinition(
        operation_id="list_firewalls",
        name="List Firewall Rules",
        description="List all firewall rules in the project. Returns rule name, network, direction, priority, and allowed/denied protocols.",
        category="networking",
        parameters=[
            {
                "name": "filter",
                "type": "string",
                "required": False,
                "description": "Filter expression (e.g., 'network=default')",
            },
        ],
        example="list_firewalls()",
        response_entity_type="Firewall",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_firewall",
        name="Get Firewall Rule Details",
        description="Get detailed information about a specific firewall rule including source/target specifications and allowed/denied traffic.",
        category="networking",
        parameters=[
            {
                "name": "firewall_name",
                "type": "string",
                "required": True,
                "description": "Name of the firewall rule",
            },
        ],
        example="get_firewall(firewall_name='allow-ssh')",
        response_entity_type="Firewall",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    # Route Operations
    OperationDefinition(
        operation_id="list_routes",
        name="List Routes",
        description="List all routes in the project. Returns route name, network, destination, and next hop.",
        category="networking",
        parameters=[
            {
                "name": "filter",
                "type": "string",
                "required": False,
                "description": "Filter expression",
            },
        ],
        example="list_routes()",
        response_entity_type="Route",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    # External IP Operations
    OperationDefinition(
        operation_id="list_addresses",
        name="List Static IP Addresses",
        description="List all reserved static IP addresses in the project. Returns address, type (EXTERNAL/INTERNAL), and status.",
        category="networking",
        parameters=[
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": "Region for regional addresses (omit for global)",
            },
        ],
        example="list_addresses(region='us-central1')",
        response_entity_type="Address",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
]
