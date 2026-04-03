# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS EC2 Operation Definitions.

Operations for EC2 instances and security groups.
"""

from meho_app.modules.connectors.base import OperationDefinition

AWS_REGION_OVERRIDE = "AWS region override"

EC2_OPERATIONS = [
    # Instance Operations
    OperationDefinition(
        operation_id="list_instances",
        name="List EC2 Instances",
        description=(
            "List all EC2 instances. Can filter by tag (key/value) and "
            "instance state (running, stopped, terminated). Returns instance "
            "details including type, IPs, VPC, security groups, and tags."
        ),
        category="compute",
        parameters=[
            {
                "name": "tag_filter",
                "type": "object",
                "required": False,
                "description": "Filter by tag: {key: 'Name', value: 'web-server'}",
            },
            {
                "name": "state",
                "type": "string",
                "required": False,
                "description": "Filter by state: running, stopped, terminated, pending, shutting-down",
            },
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": AWS_REGION_OVERRIDE,
            },
        ],
        example="list_instances state=running",
        response_entity_type="EC2Instance",
        response_identifier_field="instance_id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_instance",
        name="Get EC2 Instance Details",
        description=(
            "Get detailed information about a specific EC2 instance "
            "including configuration, network interfaces, security groups, "
            "and current state."
        ),
        category="compute",
        parameters=[
            {
                "name": "instance_id",
                "type": "string",
                "required": True,
                "description": "EC2 instance ID (e.g., i-0abc123def456)",
            },
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": AWS_REGION_OVERRIDE,
            },
        ],
        example="get_instance instance_id=i-0abc123def456",
        response_entity_type="EC2Instance",
        response_identifier_field="instance_id",
        response_display_name_field="name",
    ),
    # Security Group Operations
    OperationDefinition(
        operation_id="list_security_groups",
        name="List Security Groups",
        description=(
            "List EC2 security groups. Can filter by VPC ID. Returns "
            "group details including inbound/outbound rules with protocols, "
            "ports, and source/destination CIDRs."
        ),
        category="compute",
        parameters=[
            {
                "name": "vpc_id",
                "type": "string",
                "required": False,
                "description": "Filter security groups by VPC ID",
            },
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": AWS_REGION_OVERRIDE,
            },
        ],
        example="list_security_groups vpc_id=vpc-0abc123",
        response_entity_type="SecurityGroup",
        response_identifier_field="group_id",
        response_display_name_field="group_name",
    ),
    OperationDefinition(
        operation_id="get_security_group",
        name="Get Security Group Details",
        description=(
            "Get detailed information about a specific security group "
            "including all inbound and outbound rules."
        ),
        category="compute",
        parameters=[
            {
                "name": "group_id",
                "type": "string",
                "required": True,
                "description": "Security group ID (e.g., sg-0abc123def456)",
            },
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": AWS_REGION_OVERRIDE,
            },
        ],
        example="get_security_group group_id=sg-0abc123def456",
        response_entity_type="SecurityGroup",
        response_identifier_field="group_id",
        response_display_name_field="group_name",
    ),
]
