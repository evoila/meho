# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Azure Type Definitions (Phase 92).

Defines entity types for Azure resources.
These are registered in the connector_type table for agent discovery.
"""

from meho_app.modules.connectors.base import TypeDefinition

AZURE_TYPES = [
    # Compute Types
    TypeDefinition(
        type_name="AzureVM",
        description="An Azure Virtual Machine. Contains compute resources with CPU, memory, and managed disks. Runs in a specific region and availability zone.",
        category="compute",
        properties=[
            {"name": "id", "type": "string", "description": "ARM resource ID"},
            {"name": "name", "type": "string", "description": "VM name"},
            {"name": "location", "type": "string", "description": "Azure region"},
            {"name": "resource_group", "type": "string", "description": "Resource group name"},
            {"name": "vm_size", "type": "string", "description": "VM size (e.g., Standard_D4s_v3)"},
            {"name": "provisioning_state", "type": "string", "description": "Provisioning state (Succeeded, Failed, etc.)"},
            {"name": "os_type", "type": "string", "description": "Operating system type (Linux, Windows)"},
            {"name": "tags", "type": "object", "description": "User-defined tags"},
        ],
    ),
    TypeDefinition(
        type_name="AzureDisk",
        description="An Azure Managed Disk. Persistent block storage for VMs with SSD/HDD tiers and snapshot support.",
        category="compute",
        properties=[
            {"name": "id", "type": "string", "description": "ARM resource ID"},
            {"name": "name", "type": "string", "description": "Disk name"},
            {"name": "location", "type": "string", "description": "Azure region"},
            {"name": "resource_group", "type": "string", "description": "Resource group name"},
            {"name": "size_gb", "type": "integer", "description": "Disk size in GB"},
            {"name": "sku_name", "type": "string", "description": "SKU (Premium_LRS, Standard_LRS, etc.)"},
            {"name": "disk_state", "type": "string", "description": "Disk state (Attached, Unattached, etc.)"},
        ],
    ),
    # Container Types
    TypeDefinition(
        type_name="AzureAKSCluster",
        description="An Azure Kubernetes Service cluster. Managed Kubernetes control plane with agent pools for running containerized workloads.",
        category="containers",
        properties=[
            {"name": "id", "type": "string", "description": "ARM resource ID"},
            {"name": "name", "type": "string", "description": "Cluster name"},
            {"name": "location", "type": "string", "description": "Azure region"},
            {"name": "resource_group", "type": "string", "description": "Resource group name"},
            {"name": "kubernetes_version", "type": "string", "description": "Kubernetes version"},
            {"name": "provisioning_state", "type": "string", "description": "Provisioning state"},
            {"name": "fqdn", "type": "string", "description": "FQDN of the API server"},
            {"name": "agent_pool_count", "type": "integer", "description": "Number of agent pools"},
        ],
    ),
    TypeDefinition(
        type_name="AzureNodePool",
        description="An AKS agent pool (node pool). A group of nodes with the same VM size and configuration within a cluster.",
        category="containers",
        properties=[
            {"name": "name", "type": "string", "description": "Node pool name"},
            {"name": "vm_size", "type": "string", "description": "VM size for pool nodes"},
            {"name": "count", "type": "integer", "description": "Current node count"},
            {"name": "os_type", "type": "string", "description": "Operating system type"},
            {"name": "mode", "type": "string", "description": "Pool mode (System, User)"},
            {"name": "enable_auto_scaling", "type": "boolean", "description": "Whether autoscaling is enabled"},
        ],
    ),
    # Networking Types
    TypeDefinition(
        type_name="AzureVNet",
        description="An Azure Virtual Network. Provides network isolation with address spaces, subnets, and DNS configuration.",
        category="networking",
        properties=[
            {"name": "id", "type": "string", "description": "ARM resource ID"},
            {"name": "name", "type": "string", "description": "VNet name"},
            {"name": "location", "type": "string", "description": "Azure region"},
            {"name": "resource_group", "type": "string", "description": "Resource group name"},
            {"name": "address_prefixes", "type": "array", "description": "Address space CIDR ranges"},
            {"name": "subnet_count", "type": "integer", "description": "Number of subnets"},
            {"name": "provisioning_state", "type": "string", "description": "Provisioning state"},
        ],
    ),
    TypeDefinition(
        type_name="AzureSubnet",
        description="A subnet within an Azure Virtual Network. Defines an IP address range and can have NSGs and route tables attached.",
        category="networking",
        properties=[
            {"name": "id", "type": "string", "description": "ARM resource ID"},
            {"name": "name", "type": "string", "description": "Subnet name"},
            {"name": "address_prefix", "type": "string", "description": "Subnet CIDR range"},
            {"name": "nsg_id", "type": "string", "description": "Associated NSG resource ID"},
            {"name": "route_table_id", "type": "string", "description": "Associated route table ID"},
            {"name": "service_endpoints", "type": "array", "description": "Enabled service endpoints"},
        ],
    ),
    TypeDefinition(
        type_name="AzureNSG",
        description="An Azure Network Security Group. Contains security rules to filter network traffic to and from resources in a VNet.",
        category="networking",
        properties=[
            {"name": "id", "type": "string", "description": "ARM resource ID"},
            {"name": "name", "type": "string", "description": "NSG name"},
            {"name": "location", "type": "string", "description": "Azure region"},
            {"name": "resource_group", "type": "string", "description": "Resource group name"},
            {"name": "security_rules", "type": "array", "description": "Custom security rules"},
            {"name": "default_security_rules", "type": "array", "description": "Default security rules"},
        ],
    ),
    TypeDefinition(
        type_name="AzureLoadBalancer",
        description="An Azure Load Balancer. Distributes inbound traffic across backend pool instances with health probes and NAT rules.",
        category="networking",
        properties=[
            {"name": "id", "type": "string", "description": "ARM resource ID"},
            {"name": "name", "type": "string", "description": "Load balancer name"},
            {"name": "location", "type": "string", "description": "Azure region"},
            {"name": "resource_group", "type": "string", "description": "Resource group name"},
            {"name": "sku_name", "type": "string", "description": "SKU (Basic, Standard)"},
            {"name": "frontend_ip_count", "type": "integer", "description": "Number of frontend IP configurations"},
            {"name": "backend_pool_count", "type": "integer", "description": "Number of backend pools"},
        ],
    ),
    # Storage Types
    TypeDefinition(
        type_name="AzureStorageAccount",
        description="An Azure Storage Account. Provides blob, file, queue, and table storage with different performance tiers and access levels.",
        category="storage",
        properties=[
            {"name": "id", "type": "string", "description": "ARM resource ID"},
            {"name": "name", "type": "string", "description": "Storage account name"},
            {"name": "location", "type": "string", "description": "Azure region"},
            {"name": "resource_group", "type": "string", "description": "Resource group name"},
            {"name": "sku_name", "type": "string", "description": "SKU (Standard_LRS, Premium_LRS, etc.)"},
            {"name": "kind", "type": "string", "description": "Account kind (StorageV2, BlobStorage, etc.)"},
            {"name": "access_tier", "type": "string", "description": "Access tier (Hot, Cool, Archive)"},
        ],
    ),
    # Web Types
    TypeDefinition(
        type_name="AzureAppService",
        description="An Azure App Service web app. PaaS hosting for web applications with auto-scaling, deployment slots, and managed runtime.",
        category="web",
        properties=[
            {"name": "id", "type": "string", "description": "ARM resource ID"},
            {"name": "name", "type": "string", "description": "App name"},
            {"name": "location", "type": "string", "description": "Azure region"},
            {"name": "resource_group", "type": "string", "description": "Resource group name"},
            {"name": "state", "type": "string", "description": "App state (Running, Stopped)"},
            {"name": "default_host_name", "type": "string", "description": "Default hostname"},
            {"name": "runtime_stack", "type": "string", "description": "Runtime stack (e.g., PYTHON|3.11)"},
        ],
    ),
    TypeDefinition(
        type_name="AzureFunctionApp",
        description="An Azure Function App. Serverless compute for event-driven code execution with consumption-based billing.",
        category="web",
        properties=[
            {"name": "id", "type": "string", "description": "ARM resource ID"},
            {"name": "name", "type": "string", "description": "Function app name"},
            {"name": "location", "type": "string", "description": "Azure region"},
            {"name": "resource_group", "type": "string", "description": "Resource group name"},
            {"name": "state", "type": "string", "description": "App state (Running, Stopped)"},
            {"name": "default_host_name", "type": "string", "description": "Default hostname"},
            {"name": "runtime_stack", "type": "string", "description": "Runtime stack"},
        ],
    ),
    # Resource Group Type
    TypeDefinition(
        type_name="AzureResourceGroup",
        description="An Azure Resource Group. Logical container for Azure resources that share the same lifecycle, permissions, and policies.",
        category="management",
        properties=[
            {"name": "id", "type": "string", "description": "ARM resource ID"},
            {"name": "name", "type": "string", "description": "Resource group name"},
            {"name": "location", "type": "string", "description": "Azure region"},
            {"name": "provisioning_state", "type": "string", "description": "Provisioning state"},
            {"name": "tags", "type": "object", "description": "User-defined tags"},
        ],
    ),
]
