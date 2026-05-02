# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Azure AKS Operation Definitions (Phase 92).

Operations for Azure Kubernetes Service: clusters, node pools,
credentials, and upgrade profiles.
"""

from meho_app.modules.connectors.base import OperationDefinition

DESC_RESOURCE_GROUP_CONTAINING_THE_AKS = "Resource group containing the AKS cluster"
NAME_OF_THE_AKS_CLUSTER = "Name of the AKS cluster"

AKS_OPERATIONS = [
    # Cluster Operations
    OperationDefinition(
        operation_id="list_azure_aks_clusters",
        name="List Azure AKS Clusters",
        description=(
            "List AKS managed Kubernetes clusters in the subscription or a specific "
            "resource group. Returns cluster name, location, Kubernetes version, "
            "provisioning state, power state, FQDN, network configuration, and "
            "agent pool count."
        ),
        category="aks",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": False,
                "description": "Resource group to list clusters from (default: all resource groups)",
            },
        ],
        example="list_azure_aks_clusters(resource_group='my-rg')",
        response_entity_type="AzureAKSCluster",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_azure_aks_cluster",
        name="Get Azure AKS Cluster Details",
        description=(
            "Get detailed information about a specific AKS cluster including Kubernetes "
            "version, provisioning state, power state, FQDN, DNS prefix, node resource "
            "group, network plugin/policy, service CIDR, and agent pool count."
        ),
        category="aks",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": True,
                "description": DESC_RESOURCE_GROUP_CONTAINING_THE_AKS,
            },
            {
                "name": "cluster_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_THE_AKS_CLUSTER,
            },
        ],
        example="get_azure_aks_cluster(resource_group='my-rg', cluster_name='my-aks')",
        response_entity_type="AzureAKSCluster",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_azure_aks_cluster_health",
        name="Get Azure AKS Cluster Health",
        description=(
            "Get a health summary for an AKS cluster including provisioning state, "
            "power state, Kubernetes version, FQDN, and per-pool health "
            "(name, count, provisioning state, power state)."
        ),
        category="aks",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": True,
                "description": DESC_RESOURCE_GROUP_CONTAINING_THE_AKS,
            },
            {
                "name": "cluster_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_THE_AKS_CLUSTER,
            },
        ],
        example="get_azure_aks_cluster_health(resource_group='my-rg', cluster_name='my-aks')",
    ),
    # Node Pool Operations
    OperationDefinition(
        operation_id="list_azure_aks_node_pools",
        name="List Azure AKS Node Pools",
        description=(
            "List node pools for an AKS cluster. Returns pool name, VM size, node count, "
            "OS type, OS disk size, provisioning state, power state, mode (System/User), "
            "auto-scaling settings, Kubernetes version, and availability zones."
        ),
        category="aks",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": True,
                "description": DESC_RESOURCE_GROUP_CONTAINING_THE_AKS,
            },
            {
                "name": "cluster_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_THE_AKS_CLUSTER,
            },
        ],
        example="list_azure_aks_node_pools(resource_group='my-rg', cluster_name='my-aks')",
        response_entity_type="AzureNodePool",
        response_identifier_field="name",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_azure_aks_node_pool",
        name="Get Azure AKS Node Pool Details",
        description=(
            "Get detailed information about a specific node pool including VM size, "
            "node count, auto-scaling configuration, OS type, and availability zones."
        ),
        category="aks",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": True,
                "description": DESC_RESOURCE_GROUP_CONTAINING_THE_AKS,
            },
            {
                "name": "cluster_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_THE_AKS_CLUSTER,
            },
            {
                "name": "pool_name",
                "type": "string",
                "required": True,
                "description": "Name of the node pool",
            },
        ],
        example="get_azure_aks_node_pool(resource_group='my-rg', cluster_name='my-aks', pool_name='nodepool1')",
        response_entity_type="AzureNodePool",
        response_identifier_field="name",
        response_display_name_field="name",
    ),
    # Credentials & Upgrades
    OperationDefinition(
        operation_id="get_azure_aks_credentials",
        name="Get Azure AKS Credentials Info",
        description=(
            "Get kubeconfig-relevant information for an AKS cluster including FQDN, "
            "private FQDN, API server access profile (authorized IP ranges, private "
            "cluster status), AAD profile, and node resource group. Does not return "
            "secrets -- provides metadata for kubeconfig construction."
        ),
        category="aks",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": True,
                "description": DESC_RESOURCE_GROUP_CONTAINING_THE_AKS,
            },
            {
                "name": "cluster_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_THE_AKS_CLUSTER,
            },
        ],
        example="get_azure_aks_credentials(resource_group='my-rg', cluster_name='my-aks')",
    ),
    OperationDefinition(
        operation_id="list_azure_aks_upgrades",
        name="List Azure AKS Available Upgrades",
        description=(
            "List available Kubernetes version upgrades for an AKS cluster. Returns "
            "control plane upgrade profile (current version, available upgrades with "
            "preview status) and per-pool upgrade profiles."
        ),
        category="aks",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": True,
                "description": DESC_RESOURCE_GROUP_CONTAINING_THE_AKS,
            },
            {
                "name": "cluster_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_THE_AKS_CLUSTER,
            },
        ],
        example="list_azure_aks_upgrades(resource_group='my-rg', cluster_name='my-aks')",
    ),
]
