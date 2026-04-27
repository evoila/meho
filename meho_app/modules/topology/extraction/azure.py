# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Azure extraction schema for topology auto-discovery.

Defines declarative extraction rules for Microsoft Azure resources.
These rules specify how to extract entities and relationships from
Azure connector operation results using JMESPath expressions.

Supported Entity Types:
    - AzureVM: Virtual Machines with member_of relationships
    - AzureDisk: Managed Disks
    - AzureAKSCluster: AKS Clusters with member_of relationships
    - AzureNodePool: AKS Node Pools with member_of relationships
    - AzureVNet: Virtual Networks with member_of relationships
    - AzureSubnet: Subnets with member_of relationships
    - AzureNSG: Network Security Groups
    - AzureLoadBalancer: Load Balancers
    - AzureStorageAccount: Storage Accounts
    - AzureAppService: App Service Web Apps
    - AzureFunctionApp: Function Apps
    - AzureResourceGroup: Resource Groups

Relationship Types:
    - member_of: VM -> ResourceGroup, AKSCluster -> ResourceGroup,
                 VNet -> ResourceGroup, Subnet -> VNet,
                 NodePool -> AKSCluster

Data Formats:
    Azure connector serializers return flat dictionaries with pre-extracted names.
    Field paths match the serializer output format from:
    meho_app/modules/connectors/azure/serializers.py
"""

from .rules import (
    AttributeExtraction,
    ConnectorExtractionSchema,
    DescriptionTemplate,
    EntityExtractionRule,
    RelationshipExtraction,
)

# =============================================================================
# Azure Extraction Schema
# =============================================================================

AZURE_EXTRACTION_SCHEMA = ConnectorExtractionSchema(
    connector_type="azure",
    entity_rules=[
        # =====================================================================
        # AzureVM (Virtual Machine) Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="AzureVM",
            source_operations=["list_azure_vms", "get_azure_vm"],
            items_path=None,
            name_path="name",
            scope_paths={"resource_group": "resource_group", "location": "location"},
            description=DescriptionTemplate(
                template="Azure VM {name}, RG {resource_group}, {vm_size}, {provisioning_state}",
                fallback="Azure VM",
            ),
            attributes=[
                AttributeExtraction(name="id", path="id"),
                AttributeExtraction(name="vm_size", path="vm_size"),
                AttributeExtraction(name="provisioning_state", path="provisioning_state"),
                AttributeExtraction(name="power_state", path="power_state"),
                AttributeExtraction(name="os_type", path="os_type"),
                AttributeExtraction(name="os_name", path="os_name"),
                AttributeExtraction(name="location", path="location"),
                AttributeExtraction(name="resource_group", path="resource_group"),
                AttributeExtraction(name="tags", path="tags", default={}),
                AttributeExtraction(name="private_ips", path="private_ips", default=[]),
                AttributeExtraction(name="public_ips", path="public_ips", default=[]),
                AttributeExtraction(name="data_disks", path="data_disks", default=[]),
            ],
            relationships=[
                # VM -> member_of -> ResourceGroup
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="AzureResourceGroup",
                    target_path="resource_group",
                    optional=False,
                ),
            ],
        ),
        # =====================================================================
        # AzureDisk (Managed Disk) Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="AzureDisk",
            source_operations=["list_azure_disks", "get_azure_disk"],
            items_path=None,
            name_path="name",
            scope_paths={"resource_group": "resource_group", "location": "location"},
            description=DescriptionTemplate(
                template="Azure Disk {name}, {size_gb}GB, {sku_name}, {disk_state}",
                fallback="Azure Disk",
            ),
            attributes=[
                AttributeExtraction(name="id", path="id"),
                AttributeExtraction(name="size_gb", path="size_gb"),
                AttributeExtraction(name="sku_name", path="sku_name"),
                AttributeExtraction(name="disk_state", path="disk_state"),
                AttributeExtraction(name="os_type", path="os_type"),
                AttributeExtraction(name="location", path="location"),
                AttributeExtraction(name="resource_group", path="resource_group"),
                AttributeExtraction(name="provisioning_state", path="provisioning_state"),
                AttributeExtraction(name="tags", path="tags", default={}),
            ],
            relationships=[],
        ),
        # =====================================================================
        # AzureAKSCluster (AKS Cluster) Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="AzureAKSCluster",
            source_operations=["list_azure_aks_clusters", "get_azure_aks_cluster"],
            items_path=None,
            name_path="name",
            scope_paths={"resource_group": "resource_group", "location": "location"},
            description=DescriptionTemplate(
                template="AKS Cluster {name}, K8s {kubernetes_version}, {provisioning_state}, {agent_pool_count} pools",
                fallback="AKS Cluster",
            ),
            attributes=[
                AttributeExtraction(name="id", path="id"),
                AttributeExtraction(name="kubernetes_version", path="kubernetes_version"),
                AttributeExtraction(name="provisioning_state", path="provisioning_state"),
                AttributeExtraction(name="power_state", path="power_state"),
                AttributeExtraction(name="agent_pool_count", path="agent_pool_count"),
                AttributeExtraction(name="dns_prefix", path="dns_prefix"),
                AttributeExtraction(name="fqdn", path="fqdn"),
                AttributeExtraction(name="location", path="location"),
                AttributeExtraction(name="resource_group", path="resource_group"),
                AttributeExtraction(name="tags", path="tags", default={}),
            ],
            relationships=[
                # AKSCluster -> member_of -> ResourceGroup
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="AzureResourceGroup",
                    target_path="resource_group",
                    optional=False,
                ),
            ],
        ),
        # =====================================================================
        # AzureNodePool (AKS Node Pool) Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="AzureNodePool",
            source_operations=["list_azure_aks_node_pools", "get_azure_aks_node_pool"],
            items_path=None,
            name_path="name",
            scope_paths={"cluster": "cluster_name"},
            description=DescriptionTemplate(
                template="AKS Node Pool {name}, {vm_size}, {count} nodes, {provisioning_state}",
                fallback="AKS Node Pool",
            ),
            attributes=[
                AttributeExtraction(name="vm_size", path="vm_size"),
                AttributeExtraction(name="count", path="count"),
                AttributeExtraction(name="provisioning_state", path="provisioning_state"),
                AttributeExtraction(name="power_state", path="power_state"),
                AttributeExtraction(name="os_type", path="os_type"),
                AttributeExtraction(name="mode", path="mode"),
                AttributeExtraction(name="min_count", path="min_count"),
                AttributeExtraction(name="max_count", path="max_count"),
                AttributeExtraction(name="enable_auto_scaling", path="enable_auto_scaling"),
                AttributeExtraction(name="cluster_name", path="cluster_name"),
            ],
            relationships=[
                # NodePool -> member_of -> AKSCluster
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="AzureAKSCluster",
                    target_path="cluster_name",
                    optional=False,
                ),
            ],
        ),
        # =====================================================================
        # AzureVNet (Virtual Network) Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="AzureVNet",
            source_operations=["list_azure_vnets", "get_azure_vnet"],
            items_path=None,
            name_path="name",
            scope_paths={"resource_group": "resource_group", "location": "location"},
            description=DescriptionTemplate(
                template="Azure VNet {name}, {address_prefixes}, {subnet_count} subnets",
                fallback="Azure VNet",
            ),
            attributes=[
                AttributeExtraction(name="id", path="id"),
                AttributeExtraction(name="address_prefixes", path="address_prefixes", default=[]),
                AttributeExtraction(name="subnet_count", path="subnet_count"),
                AttributeExtraction(name="location", path="location"),
                AttributeExtraction(name="resource_group", path="resource_group"),
                AttributeExtraction(name="provisioning_state", path="provisioning_state"),
                AttributeExtraction(name="tags", path="tags", default={}),
            ],
            relationships=[
                # VNet -> member_of -> ResourceGroup
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="AzureResourceGroup",
                    target_path="resource_group",
                    optional=False,
                ),
            ],
        ),
        # =====================================================================
        # AzureSubnet Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="AzureSubnet",
            source_operations=["list_azure_subnets"],
            items_path=None,
            name_path="name",
            scope_paths={"vnet": "vnet_name"},
            description=DescriptionTemplate(
                template="Azure Subnet {name}, {address_prefix}",
                fallback="Azure Subnet",
            ),
            attributes=[
                AttributeExtraction(name="id", path="id"),
                AttributeExtraction(name="address_prefix", path="address_prefix"),
                AttributeExtraction(name="provisioning_state", path="provisioning_state"),
                AttributeExtraction(name="vnet_name", path="vnet_name"),
                AttributeExtraction(name="nsg_id", path="nsg_id"),
            ],
            relationships=[
                # Subnet -> member_of -> VNet
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="AzureVNet",
                    target_path="vnet_name",
                    optional=False,
                ),
            ],
        ),
        # =====================================================================
        # AzureNSG (Network Security Group) Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="AzureNSG",
            source_operations=["list_azure_nsgs", "get_azure_nsg"],
            items_path=None,
            name_path="name",
            scope_paths={"resource_group": "resource_group", "location": "location"},
            description=DescriptionTemplate(
                template="Azure NSG {name}, {security_rules_count} rules",
                fallback="Azure NSG",
            ),
            attributes=[
                AttributeExtraction(name="id", path="id"),
                AttributeExtraction(name="security_rules_count", path="security_rules_count"),
                AttributeExtraction(name="location", path="location"),
                AttributeExtraction(name="resource_group", path="resource_group"),
                AttributeExtraction(name="provisioning_state", path="provisioning_state"),
                AttributeExtraction(name="tags", path="tags", default={}),
            ],
            relationships=[],
        ),
        # =====================================================================
        # AzureLoadBalancer Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="AzureLoadBalancer",
            source_operations=["list_azure_load_balancers", "get_azure_load_balancer"],
            items_path=None,
            name_path="name",
            scope_paths={"resource_group": "resource_group", "location": "location"},
            description=DescriptionTemplate(
                template="Azure Load Balancer {name}",
                fallback="Azure Load Balancer",
            ),
            attributes=[
                AttributeExtraction(name="id", path="id"),
                AttributeExtraction(name="sku", path="sku"),
                AttributeExtraction(name="location", path="location"),
                AttributeExtraction(name="resource_group", path="resource_group"),
                AttributeExtraction(name="provisioning_state", path="provisioning_state"),
                AttributeExtraction(name="frontend_ip_count", path="frontend_ip_count"),
                AttributeExtraction(name="backend_pool_count", path="backend_pool_count"),
                AttributeExtraction(name="tags", path="tags", default={}),
            ],
            relationships=[],
        ),
        # =====================================================================
        # AzureStorageAccount Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="AzureStorageAccount",
            source_operations=["list_azure_storage_accounts", "get_azure_storage_account"],
            items_path=None,
            name_path="name",
            scope_paths={"resource_group": "resource_group", "location": "location"},
            description=DescriptionTemplate(
                template="Azure Storage {name}, {sku_name}, {kind}, {access_tier}",
                fallback="Azure Storage Account",
            ),
            attributes=[
                AttributeExtraction(name="id", path="id"),
                AttributeExtraction(name="sku_name", path="sku_name"),
                AttributeExtraction(name="kind", path="kind"),
                AttributeExtraction(name="access_tier", path="access_tier"),
                AttributeExtraction(name="location", path="location"),
                AttributeExtraction(name="resource_group", path="resource_group"),
                AttributeExtraction(name="provisioning_state", path="provisioning_state"),
                AttributeExtraction(name="tags", path="tags", default={}),
            ],
            relationships=[],
        ),
        # =====================================================================
        # AzureAppService (Web App) Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="AzureAppService",
            source_operations=["list_azure_web_apps", "get_azure_web_app"],
            items_path=None,
            name_path="name",
            scope_paths={"resource_group": "resource_group", "location": "location"},
            description=DescriptionTemplate(
                template="Azure App Service {name}, {state}, {default_host_name}",
                fallback="Azure App Service",
            ),
            attributes=[
                AttributeExtraction(name="id", path="id"),
                AttributeExtraction(name="state", path="state"),
                AttributeExtraction(name="default_host_name", path="default_host_name"),
                AttributeExtraction(name="kind", path="kind"),
                AttributeExtraction(name="location", path="location"),
                AttributeExtraction(name="resource_group", path="resource_group"),
                AttributeExtraction(name="tags", path="tags", default={}),
            ],
            relationships=[],
        ),
        # =====================================================================
        # AzureFunctionApp Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="AzureFunctionApp",
            source_operations=["list_azure_function_apps", "get_azure_function_app"],
            items_path=None,
            name_path="name",
            scope_paths={"resource_group": "resource_group", "location": "location"},
            description=DescriptionTemplate(
                template="Azure Function App {name}",
                fallback="Azure Function App",
            ),
            attributes=[
                AttributeExtraction(name="id", path="id"),
                AttributeExtraction(name="state", path="state"),
                AttributeExtraction(name="default_host_name", path="default_host_name"),
                AttributeExtraction(name="kind", path="kind"),
                AttributeExtraction(name="location", path="location"),
                AttributeExtraction(name="resource_group", path="resource_group"),
                AttributeExtraction(name="tags", path="tags", default={}),
            ],
            relationships=[],
        ),
        # =====================================================================
        # AzureResourceGroup Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="AzureResourceGroup",
            source_operations=["list_azure_resource_groups"],
            items_path=None,
            name_path="name",
            scope_paths={},
            description=DescriptionTemplate(
                template="Azure Resource Group {name}, {location}",
                fallback="Azure Resource Group",
            ),
            attributes=[
                AttributeExtraction(name="id", path="id"),
                AttributeExtraction(name="location", path="location"),
                AttributeExtraction(name="provisioning_state", path="provisioning_state"),
                AttributeExtraction(name="tags", path="tags", default={}),
            ],
            relationships=[],
        ),
    ],
)
