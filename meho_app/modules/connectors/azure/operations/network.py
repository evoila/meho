# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Azure Network Operation Definitions (Phase 92).

Operations for Azure networking: VNets, subnets, NSGs,
load balancers, and public IPs.
"""

from meho_app.modules.connectors.base import OperationDefinition

NETWORK_OPERATIONS = [
    # VNet Operations
    OperationDefinition(
        operation_id="list_azure_vnets",
        name="List Azure Virtual Networks",
        description=(
            "List virtual networks in the subscription or a specific resource group. "
            "Returns VNet name, location, address prefixes, DNS servers, subnet count, "
            "provisioning state, and tags."
        ),
        category="network",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": False,
                "description": "Resource group to list VNets from (default: all resource groups)",
            },
        ],
        example="list_azure_vnets(resource_group='my-rg')",
        response_entity_type="AzureVNet",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_azure_vnet",
        name="Get Azure VNet Details",
        description=(
            "Get detailed information about a virtual network including address space, "
            "DNS servers, and all subnets with their address prefixes, NSG associations, "
            "route tables, and service endpoints."
        ),
        category="network",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": True,
                "description": "Resource group containing the VNet",
            },
            {
                "name": "vnet_name",
                "type": "string",
                "required": True,
                "description": "Name of the virtual network",
            },
        ],
        example="get_azure_vnet(resource_group='my-rg', vnet_name='my-vnet')",
        response_entity_type="AzureVNet",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    # Subnet Operations
    OperationDefinition(
        operation_id="list_azure_subnets",
        name="List Azure Subnets",
        description=(
            "List subnets in a virtual network. Returns subnet name, address prefix, "
            "provisioning state, associated NSG ID, route table ID, and service endpoints."
        ),
        category="network",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": True,
                "description": "Resource group containing the VNet",
            },
            {
                "name": "vnet_name",
                "type": "string",
                "required": True,
                "description": "Name of the virtual network",
            },
        ],
        example="list_azure_subnets(resource_group='my-rg', vnet_name='my-vnet')",
        response_entity_type="AzureSubnet",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    # NSG Operations
    OperationDefinition(
        operation_id="list_azure_nsgs",
        name="List Azure Network Security Groups",
        description=(
            "List network security groups in the subscription or a specific resource group. "
            "Returns NSG name, location, custom security rules, default security rules, "
            "provisioning state, and tags."
        ),
        category="network",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": False,
                "description": "Resource group to list NSGs from (default: all resource groups)",
            },
        ],
        example="list_azure_nsgs(resource_group='my-rg')",
        response_entity_type="AzureNSG",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_azure_nsg",
        name="Get Azure NSG Details",
        description=(
            "Get detailed information about a network security group including both "
            "custom security rules and default security rules. Each rule includes "
            "priority, direction, access (Allow/Deny), protocol, source/destination "
            "address prefixes, and port ranges."
        ),
        category="network",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": True,
                "description": "Resource group containing the NSG",
            },
            {
                "name": "nsg_name",
                "type": "string",
                "required": True,
                "description": "Name of the network security group",
            },
        ],
        example="get_azure_nsg(resource_group='my-rg', nsg_name='my-nsg')",
        response_entity_type="AzureNSG",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    # Load Balancer Operations
    OperationDefinition(
        operation_id="list_azure_load_balancers",
        name="List Azure Load Balancers",
        description=(
            "List load balancers in the subscription or a specific resource group. "
            "Returns LB name, location, SKU, frontend IP count, backend pool count, "
            "inbound NAT rule count, and provisioning state."
        ),
        category="network",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": False,
                "description": "Resource group to list load balancers from (default: all resource groups)",
            },
        ],
        example="list_azure_load_balancers(resource_group='my-rg')",
        response_entity_type="AzureLoadBalancer",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_azure_load_balancer",
        name="Get Azure Load Balancer Details",
        description=(
            "Get detailed information about a specific load balancer including SKU, "
            "frontend IP configurations, backend pools, and inbound NAT rules."
        ),
        category="network",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": True,
                "description": "Resource group containing the load balancer",
            },
            {
                "name": "lb_name",
                "type": "string",
                "required": True,
                "description": "Name of the load balancer",
            },
        ],
        example="get_azure_load_balancer(resource_group='my-rg', lb_name='my-lb')",
        response_entity_type="AzureLoadBalancer",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    # Public IP Operations
    OperationDefinition(
        operation_id="list_azure_public_ips",
        name="List Azure Public IP Addresses",
        description=(
            "List public IP addresses in the subscription or a specific resource group. "
            "Returns IP name, address, allocation method (Static/Dynamic), IP version, "
            "SKU, and provisioning state."
        ),
        category="network",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": False,
                "description": "Resource group to list public IPs from (default: all resource groups)",
            },
        ],
        example="list_azure_public_ips(resource_group='my-rg')",
    ),
]
