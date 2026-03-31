# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Kubernetes Storage Operations - PVCs, PVs, StorageClasses

These are registered in the generic connector_operation table
so the agent can discover them via search_operations.
"""

from meho_app.modules.connectors.base import OperationDefinition

STORAGE_OPERATIONS = [
    # ==========================================================================
    # PersistentVolumeClaims
    # ==========================================================================
    OperationDefinition(
        operation_id="list_pvcs",
        name="List Persistent Volume Claims",
        description="List all PersistentVolumeClaims in a namespace or across all namespaces.",
        category="storage",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": False,
                "description": "Namespace to list from. If not specified, lists from all namespaces.",
            },
            {
                "name": "label_selector",
                "type": "string",
                "required": False,
                "description": "Filter by label selector",
            },
        ],
        example="list_pvcs(namespace='default')",
        response_entity_type="PersistentVolumeClaim",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_pvc",
        name="Get Persistent Volume Claim",
        description="Get details about a specific PVC including status, capacity, and bound PV.",
        category="storage",
        parameters=[
            {"name": "name", "type": "string", "required": True, "description": "Name of the PVC"},
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Namespace the PVC is in",
            },
        ],
        example="get_pvc(name='data-pvc', namespace='default')",
        response_entity_type="PersistentVolumeClaim",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    # ==========================================================================
    # PersistentVolumes
    # ==========================================================================
    OperationDefinition(
        operation_id="list_pvs",
        name="List Persistent Volumes",
        description="List all PersistentVolumes in the cluster (cluster-scoped resource).",
        category="storage",
        parameters=[
            {
                "name": "label_selector",
                "type": "string",
                "required": False,
                "description": "Filter by label selector",
            },
        ],
        example="list_pvs()",
        response_entity_type="PersistentVolume",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_pv",
        name="Get Persistent Volume",
        description="Get details about a specific PersistentVolume including capacity, "
        "access modes, and claim reference.",
        category="storage",
        parameters=[
            {"name": "name", "type": "string", "required": True, "description": "Name of the PV"},
        ],
        example="get_pv(name='pv-nfs-01')",
        response_entity_type="PersistentVolume",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    # ==========================================================================
    # StorageClasses
    # ==========================================================================
    OperationDefinition(
        operation_id="list_storageclasses",
        name="List Storage Classes",
        description="List all StorageClasses in the cluster (cluster-scoped resource).",
        category="storage",
        parameters=[],
        example="list_storageclasses()",
        response_entity_type="StorageClass",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_storageclass",
        name="Get Storage Class",
        description="Get details about a specific StorageClass including provisioner and parameters.",
        category="storage",
        parameters=[
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": "Name of the StorageClass",
            },
        ],
        example="get_storageclass(name='standard')",
        response_entity_type="StorageClass",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
]
