# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Azure Storage Operation Definitions (Phase 92).

Operations for Azure Storage: storage accounts, blob containers,
managed disks (alias), account keys, and usage.
"""

from meho_app.modules.connectors.base import OperationDefinition

DESC_RESOURCE_GROUP_CONTAINING_THE_STORAGE = "Resource group containing the storage account"
NAME_OF_THE_STORAGE_ACCOUNT = "Name of the storage account"

STORAGE_OPERATIONS = [
    # Storage Account Operations
    OperationDefinition(
        operation_id="list_azure_storage_accounts",
        name="List Azure Storage Accounts",
        description=(
            "List storage accounts in the subscription or a specific resource group. "
            "Returns account name, location, SKU, kind (StorageV2, BlobStorage, etc.), "
            "provisioning state, access tier, HNS status, creation time, primary "
            "endpoints (blob, file, queue, table), and tags."
        ),
        category="storage",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": False,
                "description": "Resource group to list storage accounts from (default: all resource groups)",
            },
        ],
        example="list_azure_storage_accounts(resource_group='my-rg')",
        response_entity_type="AzureStorageAccount",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_azure_storage_account",
        name="Get Azure Storage Account Details",
        description=(
            "Get detailed information about a specific storage account including SKU, "
            "kind, access tier, HNS status, primary endpoints, and creation time."
        ),
        category="storage",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": True,
                "description": DESC_RESOURCE_GROUP_CONTAINING_THE_STORAGE,
            },
            {
                "name": "account_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_THE_STORAGE_ACCOUNT,
            },
        ],
        example="get_azure_storage_account(resource_group='my-rg', account_name='mystorageaccount')",
        response_entity_type="AzureStorageAccount",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    # Blob Container Operations
    OperationDefinition(
        operation_id="list_azure_blob_containers",
        name="List Azure Blob Containers",
        description=(
            "List blob containers in a storage account. Returns container name, "
            "public access level, lease state, immutability policy status, "
            "legal hold status, and last modified time."
        ),
        category="storage",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": True,
                "description": DESC_RESOURCE_GROUP_CONTAINING_THE_STORAGE,
            },
            {
                "name": "account_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_THE_STORAGE_ACCOUNT,
            },
        ],
        example="list_azure_blob_containers(resource_group='my-rg', account_name='mystorageaccount')",
    ),
    # Managed Disk Alias
    OperationDefinition(
        operation_id="list_azure_managed_disks",
        name="List Azure Managed Disks (Storage View)",
        description=(
            "List managed disks (alias for list_azure_disks). Provides a storage-centric "
            "view of the same managed disk data. Returns disk name, size, SKU, state, "
            "OS type, and creation time."
        ),
        category="storage",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": False,
                "description": "Resource group to list managed disks from (default: all resource groups)",
            },
        ],
        example="list_azure_managed_disks(resource_group='my-rg')",
    ),
    # Account Keys
    OperationDefinition(
        operation_id="get_azure_storage_account_keys",
        name="Get Azure Storage Account Keys",
        description=(
            "Get storage account access key metadata. Returns key names and permissions "
            "(Full or ReadOnly). Note: actual key values are not returned for security."
        ),
        category="storage",
        parameters=[
            {
                "name": "resource_group",
                "type": "string",
                "required": True,
                "description": DESC_RESOURCE_GROUP_CONTAINING_THE_STORAGE,
            },
            {
                "name": "account_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_THE_STORAGE_ACCOUNT,
            },
        ],
        example="get_azure_storage_account_keys(resource_group='my-rg', account_name='mystorageaccount')",
    ),
    # Storage Usage
    OperationDefinition(
        operation_id="list_azure_storage_usage",
        name="List Azure Storage Usage",
        description=(
            "List storage usage statistics for a specific Azure location. Returns "
            "usage name, current value, limit, and unit. Useful for checking quota "
            "consumption."
        ),
        category="storage",
        parameters=[
            {
                "name": "location",
                "type": "string",
                "required": True,
                "description": "Azure region (e.g., 'eastus', 'westeurope')",
            },
        ],
        example="list_azure_storage_usage(location='eastus')",
    ),
]
