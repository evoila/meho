# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS VPC Operation Definitions.

Operations for VPC and subnet management.
"""

from meho_app.modules.connectors.base import OperationDefinition

AWS_REGION_OVERRIDE = "AWS region override"

VPC_OPERATIONS = [
    OperationDefinition(
        operation_id="list_vpcs",
        name="List VPCs",
        description=(
            "List all VPCs with CIDR blocks, state, tenancy, and tags. Identifies the default VPC."
        ),
        category="networking",
        parameters=[
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": AWS_REGION_OVERRIDE,
            },
        ],
        example="list_vpcs",
        response_entity_type="VPC",
        response_identifier_field="vpc_id",
        response_display_name_field="vpc_id",
    ),
    OperationDefinition(
        operation_id="get_vpc",
        name="Get VPC Details",
        description=(
            "Get detailed information about a specific VPC including "
            "CIDR block associations, DHCP options, and tenancy."
        ),
        category="networking",
        parameters=[
            {
                "name": "vpc_id",
                "type": "string",
                "required": True,
                "description": "VPC ID (e.g., vpc-0abc123)",
            },
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": AWS_REGION_OVERRIDE,
            },
        ],
        example="get_vpc vpc_id=vpc-0abc123",
        response_entity_type="VPC",
        response_identifier_field="vpc_id",
        response_display_name_field="vpc_id",
    ),
    OperationDefinition(
        operation_id="list_subnets",
        name="List Subnets",
        description=(
            "List subnets, optionally filtered by VPC. Returns availability "
            "zone, CIDR block, available IPs, and public IP mapping."
        ),
        category="networking",
        parameters=[
            {
                "name": "vpc_id",
                "type": "string",
                "required": False,
                "description": "Filter subnets by VPC ID",
            },
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": AWS_REGION_OVERRIDE,
            },
        ],
        example="list_subnets vpc_id=vpc-0abc123",
        response_entity_type="Subnet",
        response_identifier_field="subnet_id",
    ),
]
