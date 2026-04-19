# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Proxmox Storage Operations

Defines operations for Proxmox storage pools.
"""

from meho_app.modules.connectors.base import OperationDefinition

NODE_NAME = "Node name"
STORAGE_POOL_NAME = "Storage pool name"

STORAGE_OPERATIONS = [
    OperationDefinition(
        operation_id="list_storage",
        name="List Storage",
        description="Get all storage pools available on a node or in the cluster. Returns type, capacity, usage, and content types for each storage.",
        category="storage",
        parameters=[
            {
                "name": "node",
                "type": "string",
                "required": False,
                "description": "Node name (optional, lists cluster-wide if not specified)",
            },
        ],
        example="list_storage()",
        response_entity_type="Storage",
        response_identifier_field="storage",
        response_display_name_field="storage",
    ),
    OperationDefinition(
        operation_id="get_storage",
        name="Get Storage Details",
        description="Get detailed information about a specific storage pool including capacity and configuration.",
        category="storage",
        parameters=[
            {
                "name": "storage",
                "type": "string",
                "required": True,
                "description": STORAGE_POOL_NAME,
            },
            {"name": "node", "type": "string", "required": False, "description": NODE_NAME},
        ],
        example="get_storage(storage='local-lvm')",
        response_entity_type="Storage",
        response_identifier_field="storage",
        response_display_name_field="storage",
    ),
    OperationDefinition(
        operation_id="get_storage_content",
        name="Get Storage Content",
        description="List contents of a storage pool (ISO images, VM disks, container templates, backups, etc.).",
        category="storage",
        parameters=[
            {
                "name": "storage",
                "type": "string",
                "required": True,
                "description": STORAGE_POOL_NAME,
            },
            {"name": "node", "type": "string", "required": True, "description": NODE_NAME},
            {
                "name": "content",
                "type": "string",
                "required": False,
                "description": "Filter by content type: images, iso, vztmpl, backup, rootdir",
            },
        ],
        example="get_storage_content(storage='local', node='pve1', content='iso')",
        response_entity_type="StorageContent",
        response_identifier_field="volid",
        response_display_name_field="volid",
    ),
    OperationDefinition(
        operation_id="get_storage_status",
        name="Get Storage Status",
        description="Get status and health information for a storage pool.",
        category="storage",
        parameters=[
            {
                "name": "storage",
                "type": "string",
                "required": True,
                "description": STORAGE_POOL_NAME,
            },
            {"name": "node", "type": "string", "required": True, "description": NODE_NAME},
        ],
        example="get_storage_status(storage='local-lvm', node='pve1')",
        response_entity_type="Storage",
        response_identifier_field="storage",
        response_display_name_field="storage",
    ),
]
