# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS (Amazon Web Services) topology schema definition.

Defines entity types and valid relationships for AWS resources.

Entity Types:
- EC2Instance (EC2 instance, availability-zone-scoped)
- EKSCluster (EKS cluster, region-scoped)
- ECSCluster (ECS cluster, region-scoped)
- VPC (Virtual Private Cloud, region-scoped)
- Subnet (subnet, availability-zone-scoped)
- SecurityGroup (security group, VPC-scoped)

Relationship Hierarchy:
- Networking: EC2Instance -> member_of -> VPC, EC2Instance -> member_of -> Subnet
- VPC: Subnet -> member_of -> VPC, SecurityGroup -> member_of -> VPC
- EKS: EKSCluster -> member_of -> VPC

Cross-System Matching:
- EC2Instance can match K8s Node or GCP Instance via private_ip/instance_id
- EKSCluster can match K8s Cluster via endpoint/name
"""

from .base import (
    ConnectorTopologySchema,
    EntityTypeDefinition,
    RelationshipRule,
    SameAsEligibility,
    Volatility,
)

# =============================================================================
# Entity Type Definitions
# =============================================================================

_EC2_INSTANCE = EntityTypeDefinition(
    name="EC2Instance",
    scoped=True,
    scope_type="availability_zone",
    identity_fields=["availability_zone", "instance_id"],
    volatility=Volatility.MODERATE,
    # SAME_AS: EC2 Instance is the underlying machine for K8s Node or equivalent to GCP Instance
    same_as=SameAsEligibility(
        can_match=["Node", "Instance"],
        matching_attributes=["instance_id", "private_ip"],
    ),
    navigation_hints=[
        "EC2 instance (virtual machine) in AWS",
        "Find VPC via member_of relationship",
        "Find subnet via member_of relationship",
    ],
    common_queries=[
        "What AZ is this instance in?",
        "What is the instance state?",
        "What VPC is this instance in?",
        "What security groups are attached?",
    ],
)

_EKS_CLUSTER = EntityTypeDefinition(
    name="EKSCluster",
    scoped=True,
    scope_type="region",
    identity_fields=["region", "name"],
    volatility=Volatility.STABLE,
    # SAME_AS: EKS Cluster can match K8s Cluster via endpoint
    same_as=SameAsEligibility(
        can_match=["Cluster"],
        matching_attributes=["endpoint", "name"],
    ),
    navigation_hints=[
        "EKS (Elastic Kubernetes Service) managed cluster",
        "Contains node groups with EC2 instances",
        "Cross-reference with Kubernetes connector for pod-level details",
    ],
    common_queries=[
        "What is the Kubernetes version?",
        "What VPC is this cluster in?",
        "Is the cluster healthy?",
        "How many node groups does it have?",
    ],
)

_ECS_CLUSTER = EntityTypeDefinition(
    name="ECSCluster",
    scoped=True,
    scope_type="region",
    identity_fields=["region", "cluster_name"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "ECS (Elastic Container Service) cluster",
        "Contains services and tasks",
    ],
    common_queries=[
        "How many tasks are running?",
        "How many active services?",
        "What is the cluster status?",
    ],
)

_VPC = EntityTypeDefinition(
    name="VPC",
    scoped=True,
    scope_type="region",
    identity_fields=["region", "vpc_id"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "Virtual Private Cloud (VPC) network in AWS",
        "Contains subnets, security groups, and instances",
        "Find child resources via member_of relationship (reverse)",
    ],
    common_queries=[
        "What subnets are in this VPC?",
        "What is the CIDR block?",
        "What instances are in this VPC?",
    ],
)

_SUBNET = EntityTypeDefinition(
    name="Subnet",
    scoped=True,
    scope_type="availability_zone",
    identity_fields=["availability_zone", "subnet_id"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "Subnet within a VPC",
        "Find parent VPC via member_of relationship",
    ],
    common_queries=[
        "What VPC does this subnet belong to?",
        "What is the CIDR block?",
        "What AZ is this subnet in?",
        "How many IPs are available?",
    ],
)

_SECURITY_GROUP = EntityTypeDefinition(
    name="SecurityGroup",
    scoped=True,
    scope_type="vpc",
    identity_fields=["vpc_id", "group_id"],
    volatility=Volatility.MODERATE,
    navigation_hints=[
        "Security group (virtual firewall) in AWS",
        "Find parent VPC via member_of relationship",
        "Check ingress/egress rules for connectivity issues",
    ],
    common_queries=[
        "What VPC does this security group belong to?",
        "What are the inbound rules?",
        "What are the outbound rules?",
    ],
)

# =============================================================================
# Relationship Rules
# =============================================================================

# EC2 Instance relationships
_EC2_RULES = {
    ("EC2Instance", "member_of", "VPC"): RelationshipRule(
        from_type="EC2Instance",
        relationship_type="member_of",
        to_type="VPC",
    ),
    ("EC2Instance", "member_of", "Subnet"): RelationshipRule(
        from_type="EC2Instance",
        relationship_type="member_of",
        to_type="Subnet",
    ),
}

# VPC networking relationships
_VPC_RULES = {
    ("Subnet", "member_of", "VPC"): RelationshipRule(
        from_type="Subnet",
        relationship_type="member_of",
        to_type="VPC",
        required=True,
    ),
    ("SecurityGroup", "member_of", "VPC"): RelationshipRule(
        from_type="SecurityGroup",
        relationship_type="member_of",
        to_type="VPC",
        required=True,
    ),
}

# EKS relationships
_EKS_RULES = {
    ("EKSCluster", "member_of", "VPC"): RelationshipRule(
        from_type="EKSCluster",
        relationship_type="member_of",
        to_type="VPC",
    ),
}

# =============================================================================
# Complete AWS Schema
# =============================================================================

AWS_TOPOLOGY_SCHEMA = ConnectorTopologySchema(
    connector_type="aws",
    entity_types={
        "EC2Instance": _EC2_INSTANCE,
        "EKSCluster": _EKS_CLUSTER,
        "ECSCluster": _ECS_CLUSTER,
        "VPC": _VPC,
        "Subnet": _SUBNET,
        "SecurityGroup": _SECURITY_GROUP,
    },
    relationship_rules={
        **_EC2_RULES,
        **_VPC_RULES,
        **_EKS_RULES,
    },
)
