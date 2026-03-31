# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS Type Definitions.

Defines entity types for topology-relevant AWS resources.
These are registered in the connector_type table for agent discovery.
"""

from meho_app.modules.connectors.base import TypeDefinition

AWS_TYPES: list[TypeDefinition] = [
    # Compute
    TypeDefinition(
        type_name="EC2Instance",
        description=(
            "An AWS EC2 virtual machine instance. Provides scalable compute capacity "
            "in the cloud. Runs in a specific availability zone within a VPC."
        ),
        category="compute",
        properties=[
            {"name": "instance_id", "type": "string", "description": "Unique instance ID (i-...)"},
            {"name": "name", "type": "string", "description": "Instance name from tags"},
            {
                "name": "instance_type",
                "type": "string",
                "description": "Instance type (e.g., t3.micro, m5.xlarge)",
            },
            {
                "name": "state",
                "type": "string",
                "description": "Instance state (running, stopped, terminated, etc.)",
            },
            {
                "name": "availability_zone",
                "type": "string",
                "description": "Availability zone (e.g., us-east-1a)",
            },
            {"name": "private_ip", "type": "string", "description": "Private IP address"},
            {
                "name": "public_ip",
                "type": "string",
                "description": "Public IP address (if assigned)",
            },
            {"name": "vpc_id", "type": "string", "description": "VPC the instance runs in"},
            {"name": "tags", "type": "object", "description": "User-defined tags"},
        ],
    ),
    # Container - EKS
    TypeDefinition(
        type_name="EKSCluster",
        description=(
            "An AWS EKS (Elastic Kubernetes Service) managed Kubernetes cluster. "
            "Provides a managed control plane for running containerized workloads."
        ),
        category="container",
        properties=[
            {"name": "name", "type": "string", "description": "Cluster name"},
            {"name": "arn", "type": "string", "description": "Cluster ARN"},
            {
                "name": "status",
                "type": "string",
                "description": "Cluster status (ACTIVE, CREATING, etc.)",
            },
            {"name": "version", "type": "string", "description": "Kubernetes version"},
            {"name": "endpoint", "type": "string", "description": "Kubernetes API endpoint"},
            {"name": "vpc_id", "type": "string", "description": "VPC ID from resources config"},
            {"name": "tags", "type": "object", "description": "User-defined tags"},
        ],
    ),
    # Container - ECS
    TypeDefinition(
        type_name="ECSCluster",
        description=(
            "An AWS ECS (Elastic Container Service) cluster. "
            "Runs containerized applications using tasks and services."
        ),
        category="container",
        properties=[
            {"name": "cluster_name", "type": "string", "description": "Cluster name"},
            {"name": "cluster_arn", "type": "string", "description": "Cluster ARN"},
            {
                "name": "status",
                "type": "string",
                "description": "Cluster status (ACTIVE, INACTIVE, etc.)",
            },
            {
                "name": "running_tasks_count",
                "type": "integer",
                "description": "Number of running tasks",
            },
            {
                "name": "active_services_count",
                "type": "integer",
                "description": "Number of active services",
            },
        ],
    ),
    # Networking - VPC
    TypeDefinition(
        type_name="VPC",
        description=(
            "An AWS Virtual Private Cloud. Provides isolated network environments "
            "for running AWS resources with custom IP ranges, subnets, and routing."
        ),
        category="networking",
        properties=[
            {"name": "vpc_id", "type": "string", "description": "Unique VPC ID (vpc-...)"},
            {
                "name": "state",
                "type": "string",
                "description": "VPC state (available, pending)",
            },
            {"name": "cidr_block", "type": "string", "description": "Primary CIDR block"},
            {"name": "is_default", "type": "boolean", "description": "Whether this is the default VPC"},
            {"name": "tags", "type": "object", "description": "User-defined tags"},
        ],
    ),
    # Networking - Subnet
    TypeDefinition(
        type_name="Subnet",
        description=(
            "An AWS VPC subnet. A range of IP addresses within a VPC, "
            "scoped to a single availability zone."
        ),
        category="networking",
        properties=[
            {"name": "subnet_id", "type": "string", "description": "Unique subnet ID (subnet-...)"},
            {"name": "vpc_id", "type": "string", "description": "Parent VPC ID"},
            {
                "name": "availability_zone",
                "type": "string",
                "description": "Availability zone",
            },
            {"name": "cidr_block", "type": "string", "description": "Subnet CIDR block"},
            {"name": "tags", "type": "object", "description": "User-defined tags"},
        ],
    ),
    # Networking - Security Group
    TypeDefinition(
        type_name="SecurityGroup",
        description=(
            "An AWS EC2 security group. A virtual firewall that controls "
            "inbound and outbound traffic for associated resources."
        ),
        category="networking",
        properties=[
            {"name": "group_id", "type": "string", "description": "Unique security group ID (sg-...)"},
            {"name": "group_name", "type": "string", "description": "Security group name"},
            {"name": "vpc_id", "type": "string", "description": "VPC the group belongs to"},
            {"name": "description", "type": "string", "description": "Group description"},
        ],
    ),
]
