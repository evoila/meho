# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Azure (Microsoft Azure) topology schema definition.

Defines entity types and valid relationships for Azure resources.

Entity Types:
- AzureResourceGroup (subscription-scoped)
- AzureVM (resource_group-scoped, same_as eligible)
- AzureDisk (resource_group-scoped)
- AzureAKSCluster (resource_group-scoped, same_as eligible)
- AzureNodePool (cluster-scoped)
- AzureVNet (resource_group-scoped)
- AzureSubnet (vnet-scoped)
- AzureNSG (resource_group-scoped)
- AzureLoadBalancer (resource_group-scoped)
- AzureStorageAccount (resource_group-scoped)
- AzureAppService (resource_group-scoped)
- AzureFunctionApp (resource_group-scoped)

Relationship Hierarchy:
- Containment: VM -> member_of -> ResourceGroup, AKS -> member_of -> ResourceGroup,
               VNet -> member_of -> ResourceGroup, Subnet -> member_of -> VNet,
               NodePool -> member_of -> AKSCluster
- Storage: VM -> uses -> Disk (via data_disks)
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

_RESOURCE_GROUP = EntityTypeDefinition(
    name="AzureResourceGroup",
    scoped=True,
    scope_type="subscription",
    identity_fields=["subscription", "name"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "Azure Resource Group containing related resources",
        "Find VMs, AKS clusters, VNets via member_of relationship (reverse)",
    ],
    common_queries=[
        "What resources are in this resource group?",
        "What is the location of this resource group?",
    ],
)

_VM = EntityTypeDefinition(
    name="AzureVM",
    scoped=True,
    scope_type="resource_group",
    identity_fields=["resource_group", "name"],
    volatility=Volatility.MODERATE,
    # SAME_AS: Azure VM can match K8s Node, GCP Instance, or VMware VM
    same_as=SameAsEligibility(
        can_match=["Node", "Instance", "VM", "Host"],
        matching_attributes=[
            "name",
            "private_ips",
            "public_ips",
        ],
    ),
    navigation_hints=[
        "Azure Virtual Machine",
        "Find disks via uses relationship",
        "Find resource group via member_of relationship",
    ],
    common_queries=[
        "What resource group is this VM in?",
        "What is the VM size?",
        "What is the power state?",
        "What disks are attached?",
    ],
)

_DISK = EntityTypeDefinition(
    name="AzureDisk",
    scoped=True,
    scope_type="resource_group",
    identity_fields=["resource_group", "name"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "Azure Managed Disk",
        "Find attached VMs via uses relationship (reverse)",
    ],
    common_queries=[
        "What VM is this disk attached to?",
        "What is the disk size?",
        "What is the disk SKU (Standard/Premium)?",
    ],
)

_AKS_CLUSTER = EntityTypeDefinition(
    name="AzureAKSCluster",
    scoped=True,
    scope_type="resource_group",
    identity_fields=["resource_group", "name"],
    volatility=Volatility.MODERATE,
    # SAME_AS: AKS cluster can correlate with GKE cluster
    same_as=SameAsEligibility(
        can_match=["GKECluster"],
        matching_attributes=["name", "kubernetes_version"],
    ),
    navigation_hints=[
        "Azure Kubernetes Service (AKS) cluster",
        "Contains Node Pools",
        "Find Node Pools via member_of relationship (reverse)",
    ],
    common_queries=[
        "What node pools are in this cluster?",
        "What Kubernetes version is running?",
        "What is the cluster power state?",
    ],
)

_NODE_POOL = EntityTypeDefinition(
    name="AzureNodePool",
    scoped=True,
    scope_type="cluster",
    identity_fields=["cluster", "name"],
    volatility=Volatility.MODERATE,
    navigation_hints=[
        "Node pool within an AKS cluster",
        "Find parent cluster via member_of relationship",
    ],
    common_queries=[
        "What cluster does this node pool belong to?",
        "What is the VM size?",
        "How many nodes are in this pool?",
        "Is autoscaling enabled?",
    ],
)

_VNET = EntityTypeDefinition(
    name="AzureVNet",
    scoped=True,
    scope_type="resource_group",
    identity_fields=["resource_group", "name"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "Azure Virtual Network",
        "Contains subnets",
        "Find subnets via member_of relationship (reverse)",
    ],
    common_queries=[
        "What subnets are in this VNet?",
        "What is the address space?",
        "What resource group is this VNet in?",
    ],
)

_SUBNET = EntityTypeDefinition(
    name="AzureSubnet",
    scoped=True,
    scope_type="vnet",
    identity_fields=["vnet", "name"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "Subnet within an Azure VNet",
        "Find parent VNet via member_of relationship",
    ],
    common_queries=[
        "What VNet does this subnet belong to?",
        "What is the address prefix?",
        "What NSG is associated with this subnet?",
    ],
)

_NSG = EntityTypeDefinition(
    name="AzureNSG",
    scoped=True,
    scope_type="resource_group",
    identity_fields=["resource_group", "name"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "Azure Network Security Group",
        "Contains inbound and outbound security rules",
    ],
    common_queries=[
        "What security rules are in this NSG?",
        "Is inbound traffic on port 443 allowed?",
        "What subnets or NICs is this NSG associated with?",
    ],
)

_LOAD_BALANCER = EntityTypeDefinition(
    name="AzureLoadBalancer",
    scoped=True,
    scope_type="resource_group",
    identity_fields=["resource_group", "name"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "Azure Load Balancer",
        "Has frontend IPs, backend pools, and health probes",
    ],
    common_queries=[
        "What backend pools are configured?",
        "What is the frontend IP?",
        "What health probes are configured?",
    ],
)

_STORAGE_ACCOUNT = EntityTypeDefinition(
    name="AzureStorageAccount",
    scoped=True,
    scope_type="resource_group",
    identity_fields=["resource_group", "name"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "Azure Storage Account",
        "Contains blob containers, file shares, queues, tables",
    ],
    common_queries=[
        "What is the access tier?",
        "What redundancy type is configured?",
        "What containers are in this storage account?",
    ],
)

_APP_SERVICE = EntityTypeDefinition(
    name="AzureAppService",
    scoped=True,
    scope_type="resource_group",
    identity_fields=["resource_group", "name"],
    volatility=Volatility.MODERATE,
    navigation_hints=[
        "Azure App Service (Web App)",
        "Runs on an App Service Plan",
    ],
    common_queries=[
        "What is the app state (Running/Stopped)?",
        "What is the default hostname?",
        "What App Service Plan is this running on?",
    ],
)

_FUNCTION_APP = EntityTypeDefinition(
    name="AzureFunctionApp",
    scoped=True,
    scope_type="resource_group",
    identity_fields=["resource_group", "name"],
    volatility=Volatility.MODERATE,
    navigation_hints=[
        "Azure Function App (serverless)",
        "Runs on a consumption or App Service Plan",
    ],
    common_queries=[
        "What is the function app state?",
        "What runtime is configured?",
        "What is the default hostname?",
    ],
)


# =============================================================================
# Relationship Rules
# =============================================================================

# Containment relationships (member_of)
_CONTAINMENT_RULES = {
    ("AzureVM", "member_of", "AzureResourceGroup"): RelationshipRule(
        from_type="AzureVM",
        relationship_type="member_of",
        to_type="AzureResourceGroup",
    ),
    ("AzureAKSCluster", "member_of", "AzureResourceGroup"): RelationshipRule(
        from_type="AzureAKSCluster",
        relationship_type="member_of",
        to_type="AzureResourceGroup",
    ),
    ("AzureVNet", "member_of", "AzureResourceGroup"): RelationshipRule(
        from_type="AzureVNet",
        relationship_type="member_of",
        to_type="AzureResourceGroup",
    ),
    ("AzureSubnet", "member_of", "AzureVNet"): RelationshipRule(
        from_type="AzureSubnet",
        relationship_type="member_of",
        to_type="AzureVNet",
        required=True,
    ),
    ("AzureNodePool", "member_of", "AzureAKSCluster"): RelationshipRule(
        from_type="AzureNodePool",
        relationship_type="member_of",
        to_type="AzureAKSCluster",
        required=True,
    ),
}

# Storage relationships (uses)
_STORAGE_RULES = {
    ("AzureVM", "uses", "AzureDisk"): RelationshipRule(
        from_type="AzureVM",
        relationship_type="uses",
        to_type="AzureDisk",
        cardinality="one_to_many",
    ),
}


# =============================================================================
# Complete Azure Schema
# =============================================================================

AZURE_TOPOLOGY_SCHEMA = ConnectorTopologySchema(
    connector_type="azure",
    entity_types={
        "AzureResourceGroup": _RESOURCE_GROUP,
        "AzureVM": _VM,
        "AzureDisk": _DISK,
        "AzureAKSCluster": _AKS_CLUSTER,
        "AzureNodePool": _NODE_POOL,
        "AzureVNet": _VNET,
        "AzureSubnet": _SUBNET,
        "AzureNSG": _NSG,
        "AzureLoadBalancer": _LOAD_BALANCER,
        "AzureStorageAccount": _STORAGE_ACCOUNT,
        "AzureAppService": _APP_SERVICE,
        "AzureFunctionApp": _FUNCTION_APP,
    },
    relationship_rules={
        **_CONTAINMENT_RULES,
        **_STORAGE_RULES,
    },
)
