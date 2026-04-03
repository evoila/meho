# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Azure Compute Operation Definitions (Phase 92).

Operations for managing Azure VMs, managed disks, availability sets,
and resource groups.
"""

from meho_app.modules.connectors.base import OperationDefinition

NAME_OF_THE_VIRTUAL_MACHINE = "Name of the virtual machine"
RESOURCE_GROUP_CONTAINING_THE_VM = "Resource group containing the VM"

COMPUTE_OPERATIONS = [
    # VM Operations
    OperationDefinition(
        operation_id="list_azure_vms",
        name="List Azure Virtual Machines",
        description=(
            "List all Azure Virtual Machines in the subscription or a specific resource group. "
            "Returns VM name, location, resource group, VM size, provisioning state, OS type, "
            "disk information, tags, and availability zones."
        ),
        category="compute",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": False,
                "description": "Resource group to list VMs from (default: all resource groups or resource_group_filter)",
            },
            {
                "name": "state_filter",
                "type": "string",
                "required": False,
                "description": "Filter by provisioning state (e.g., 'Succeeded', 'Failed')",
            },
        ],
        example="list_azure_vms(resource_group='my-rg')",
        response_entity_type="AzureVM",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_azure_vm",
        name="Get Azure VM Details",
        description=(
            "Get detailed information about a specific Azure VM including configuration, "
            "power state, VM agent status, OS information. Merges VM properties with "
            "instance view to provide both static config and runtime state."
        ),
        category="compute",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": True,
                "description": RESOURCE_GROUP_CONTAINING_THE_VM,
            },
            {
                "name": "vm_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_THE_VIRTUAL_MACHINE,
            },
        ],
        example="get_azure_vm(resource_group='my-rg', vm_name='my-vm')",
        response_entity_type="AzureVM",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_azure_vm_instance_view",
        name="Get Azure VM Instance View",
        description=(
            "Get the runtime status of a VM including power state (running, deallocated, stopped), "
            "VM agent version, OS name/version, boot diagnostics status, and maintenance state."
        ),
        category="compute",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": True,
                "description": RESOURCE_GROUP_CONTAINING_THE_VM,
            },
            {
                "name": "vm_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_THE_VIRTUAL_MACHINE,
            },
        ],
        example="get_azure_vm_instance_view(resource_group='my-rg', vm_name='my-vm')",
    ),
    OperationDefinition(
        operation_id="get_azure_vm_metrics",
        name="Get Azure VM Metrics",
        description=(
            "Get common performance metrics for an Azure VM including CPU percentage, "
            "available memory, disk read/write bytes, and network in/out totals. "
            "Automatically constructs the resource URI and queries Azure Monitor."
        ),
        category="compute",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": True,
                "description": RESOURCE_GROUP_CONTAINING_THE_VM,
            },
            {
                "name": "vm_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_THE_VIRTUAL_MACHINE,
            },
            {
                "name": "timespan",
                "type": "string",
                "required": False,
                "description": "ISO 8601 duration for the query window (default: PT1H = 1 hour)",
            },
            {
                "name": "interval",
                "type": "string",
                "required": False,
                "description": "Metric granularity (default: PT5M = 5 minutes)",
            },
        ],
        example="get_azure_vm_metrics(resource_group='my-rg', vm_name='my-vm', timespan='PT4H')",
    ),
    # Disk Operations
    OperationDefinition(
        operation_id="list_azure_disks",
        name="List Azure Managed Disks",
        description=(
            "List all managed disks in the subscription or a specific resource group. "
            "Returns disk name, size (GB), SKU, provisioning state, disk state, OS type, "
            "and creation time."
        ),
        category="compute",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": False,
                "description": "Resource group to list disks from (default: all resource groups)",
            },
        ],
        example="list_azure_disks(resource_group='my-rg')",
        response_entity_type="AzureDisk",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_azure_disk",
        name="Get Azure Disk Details",
        description=(
            "Get detailed information about a specific managed disk including size, "
            "SKU name, provisioning state, disk state, OS type, and creation time."
        ),
        category="compute",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": True,
                "description": "Resource group containing the disk",
            },
            {
                "name": "disk_name",
                "type": "string",
                "required": True,
                "description": "Name of the managed disk",
            },
        ],
        example="get_azure_disk(resource_group='my-rg', disk_name='my-disk')",
        response_entity_type="AzureDisk",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    # Availability Set Operations
    OperationDefinition(
        operation_id="list_azure_availability_sets",
        name="List Azure Availability Sets",
        description=(
            "List availability sets in a resource group. Returns name, location, "
            "fault domain count, update domain count, and SKU."
        ),
        category="compute",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": True,
                "description": "Resource group to list availability sets from",
            },
        ],
        example="list_azure_availability_sets(resource_group='my-rg')",
    ),
    # Resource Group Operations
    OperationDefinition(
        operation_id="list_azure_resource_groups",
        name="List Azure Resource Groups",
        description=(
            "List all resource groups in the subscription. Returns resource group name, "
            "location, provisioning state, and tags. Useful for discovering available "
            "resource groups before querying resources."
        ),
        category="compute",
        parameters=[],
        example="list_azure_resource_groups()",
        response_entity_type="AzureResourceGroup",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
]
