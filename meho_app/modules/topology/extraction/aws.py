# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS extraction schema for topology auto-discovery.

Defines declarative extraction rules for Amazon Web Services resources.
These rules specify how to extract entities and relationships from
AWS connector operation results using JMESPath expressions.

Supported Entity Types:
    - EC2Instance: EC2 instances with member_of VPC/Subnet relationships
    - EKSCluster: EKS clusters with member_of VPC relationship
    - ECSCluster: ECS clusters (standalone, no outgoing relationships)
    - VPC: Virtual Private Clouds (top-level)
    - Subnet: Subnets with member_of VPC relationship
    - SecurityGroup: Security groups with member_of VPC relationship

Relationship Types:
    - member_of: EC2Instance -> VPC, EC2Instance -> Subnet,
                 Subnet -> VPC, SecurityGroup -> VPC, EKSCluster -> VPC

Cross-System Matching:
    - EC2Instance matches K8s Node via private_ip or instance_id in providerID
    - EKSCluster matches K8s Cluster via endpoint

Data Formats:
    AWS connector serializers return flat dictionaries with pre-extracted names.
    Field paths match the serializer output format from:
    meho_app/modules/connectors/aws/serializers.py
"""

from .rules import (
    AttributeExtraction,
    ConnectorExtractionSchema,
    DescriptionTemplate,
    EntityExtractionRule,
    RelationshipExtraction,
)

# =============================================================================
# AWS Extraction Schema
# =============================================================================

AWS_EXTRACTION_SCHEMA = ConnectorExtractionSchema(
    connector_type="aws",
    entity_rules=[
        # =====================================================================
        # EC2Instance Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="EC2Instance",
            source_operations=["list_instances", "get_instance"],
            items_path=None,
            name_path="name",
            scope_paths={"availability_zone": "availability_zone"},
            description=DescriptionTemplate(
                template="AWS EC2 Instance {name}, AZ {availability_zone}, {instance_type}, {state}",
                fallback="AWS EC2 Instance",
            ),
            attributes=[
                AttributeExtraction(name="instance_id", path="instance_id"),
                AttributeExtraction(name="instance_type", path="instance_type"),
                AttributeExtraction(name="state", path="state"),
                AttributeExtraction(name="availability_zone", path="availability_zone"),
                AttributeExtraction(name="private_ip", path="private_ip"),
                AttributeExtraction(name="public_ip", path="public_ip"),
                AttributeExtraction(name="vpc_id", path="vpc_id"),
                AttributeExtraction(name="subnet_id", path="subnet_id"),
                AttributeExtraction(name="tags", path="tags", default={}),
                AttributeExtraction(name="security_groups", path="security_groups", default=[]),
                AttributeExtraction(name="launch_time", path="launch_time"),
                AttributeExtraction(name="platform", path="platform"),
                AttributeExtraction(name="architecture", path="architecture"),
            ],
            relationships=[
                # EC2Instance is member of VPC
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="VPC",
                    target_path="vpc_id",
                    optional=True,
                ),
                # EC2Instance is member of Subnet
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="Subnet",
                    target_path="subnet_id",
                    optional=True,
                ),
            ],
        ),
        # =====================================================================
        # EKSCluster Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="EKSCluster",
            source_operations=["list_eks_clusters", "get_eks_cluster"],
            items_path=None,
            name_path="name",
            scope_paths={"region": "arn"},  # Region extracted from ARN
            description=DescriptionTemplate(
                template="AWS EKS Cluster {name}, {status}, v{version}",
                fallback="AWS EKS Cluster",
            ),
            attributes=[
                AttributeExtraction(name="name", path="name"),
                AttributeExtraction(name="arn", path="arn"),
                AttributeExtraction(name="status", path="status"),
                AttributeExtraction(name="version", path="version"),
                AttributeExtraction(name="endpoint", path="endpoint"),
                AttributeExtraction(name="vpc_id", path="vpc_config.vpc_id"),
                AttributeExtraction(name="tags", path="tags", default={}),
                AttributeExtraction(name="created_at", path="created_at"),
                AttributeExtraction(name="platform_version", path="platform_version"),
            ],
            relationships=[
                # EKSCluster is member of VPC
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="VPC",
                    target_path="vpc_config.vpc_id",
                    optional=True,
                ),
            ],
        ),
        # =====================================================================
        # ECSCluster Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="ECSCluster",
            source_operations=["list_ecs_clusters"],
            items_path=None,
            name_path="cluster_name",
            scope_paths={},
            description=DescriptionTemplate(
                template="AWS ECS Cluster {cluster_name}, {status}, {running_tasks_count} running tasks",
                fallback="AWS ECS Cluster",
            ),
            attributes=[
                AttributeExtraction(name="cluster_name", path="cluster_name"),
                AttributeExtraction(name="cluster_arn", path="cluster_arn"),
                AttributeExtraction(name="status", path="status"),
                AttributeExtraction(name="running_tasks_count", path="running_tasks_count"),
                AttributeExtraction(name="active_services_count", path="active_services_count"),
                AttributeExtraction(name="pending_tasks_count", path="pending_tasks_count"),
                AttributeExtraction(name="registered_container_instances_count", path="registered_container_instances_count"),
            ],
            relationships=[],  # ECS clusters are standalone
        ),
        # =====================================================================
        # VPC Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="VPC",
            source_operations=["list_vpcs", "get_vpc"],
            items_path=None,
            name_path="vpc_id",
            scope_paths={},
            description=DescriptionTemplate(
                template="AWS VPC {vpc_id}, {state}, CIDR {cidr_block}",
                fallback="AWS VPC",
            ),
            attributes=[
                AttributeExtraction(name="vpc_id", path="vpc_id"),
                AttributeExtraction(name="state", path="state"),
                AttributeExtraction(name="cidr_block", path="cidr_block"),
                AttributeExtraction(name="is_default", path="is_default"),
                AttributeExtraction(name="tags", path="tags", default={}),
                AttributeExtraction(name="owner_id", path="owner_id"),
                AttributeExtraction(name="dhcp_options_id", path="dhcp_options_id"),
            ],
            relationships=[],  # VPCs are top-level
        ),
        # =====================================================================
        # Subnet Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="Subnet",
            source_operations=["list_subnets"],
            items_path=None,
            name_path="subnet_id",
            scope_paths={"availability_zone": "availability_zone"},
            description=DescriptionTemplate(
                template="AWS Subnet {subnet_id}, AZ {availability_zone}, CIDR {cidr_block}",
                fallback="AWS Subnet",
            ),
            attributes=[
                AttributeExtraction(name="subnet_id", path="subnet_id"),
                AttributeExtraction(name="vpc_id", path="vpc_id"),
                AttributeExtraction(name="availability_zone", path="availability_zone"),
                AttributeExtraction(name="cidr_block", path="cidr_block"),
                AttributeExtraction(name="tags", path="tags", default={}),
                AttributeExtraction(name="available_ip_address_count", path="available_ip_address_count"),
                AttributeExtraction(name="map_public_ip_on_launch", path="map_public_ip_on_launch"),
            ],
            relationships=[
                # Subnet is member of VPC
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="VPC",
                    target_path="vpc_id",
                    optional=False,
                ),
            ],
        ),
        # =====================================================================
        # SecurityGroup Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="SecurityGroup",
            source_operations=["list_security_groups", "get_security_group"],
            items_path=None,
            name_path="group_name",
            scope_paths={},
            description=DescriptionTemplate(
                template="AWS Security Group {group_name} ({group_id}), VPC {vpc_id}",
                fallback="AWS Security Group",
            ),
            attributes=[
                AttributeExtraction(name="group_id", path="group_id"),
                AttributeExtraction(name="group_name", path="group_name"),
                AttributeExtraction(name="vpc_id", path="vpc_id"),
                AttributeExtraction(name="description", path="description"),
                AttributeExtraction(name="ingress_rules", path="ingress_rules", default=[]),
                AttributeExtraction(name="egress_rules", path="egress_rules", default=[]),
                AttributeExtraction(name="tags", path="tags", default={}),
            ],
            relationships=[
                # SecurityGroup is member of VPC
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="VPC",
                    target_path="vpc_id",
                    optional=False,
                ),
            ],
        ),
    ],
)
