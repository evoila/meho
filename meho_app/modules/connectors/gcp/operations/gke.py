# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GCP GKE Operation Definitions (TASK-102)

Operations for managing Google Kubernetes Engine clusters and node pools.
"""

from meho_app.modules.connectors.base import OperationDefinition

GKE_OPERATIONS = [
    # Cluster Operations
    OperationDefinition(
        operation_id="list_clusters",
        name="List GKE Clusters",
        description="List all GKE clusters in the project. Returns cluster name, location, status, version, node count, and endpoint.",
        category="containers",
        parameters=[
            {
                "name": "location",
                "type": "string",
                "required": False,
                "description": "Zone or region (default: all locations, use '-' for all)",
            },
        ],
        example="list_clusters()",
        response_entity_type="Cluster",
        response_identifier_field="self_link",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_cluster",
        name="Get GKE Cluster Details",
        description="Get detailed information about a specific GKE cluster including master version, node pools, networking, and addons.",
        category="containers",
        parameters=[
            {
                "name": "cluster_name",
                "type": "string",
                "required": True,
                "description": "Name of the cluster",
            },
            {
                "name": "location",
                "type": "string",
                "required": False,
                "description": "Zone or region of the cluster",
            },
        ],
        example="get_cluster(cluster_name='my-cluster', location='us-central1')",
        response_entity_type="Cluster",
        response_identifier_field="self_link",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_cluster_health",
        name="Get Cluster Health",
        description="Get the health status of a GKE cluster including master health, node health, and recent conditions.",
        category="containers",
        parameters=[
            {
                "name": "cluster_name",
                "type": "string",
                "required": True,
                "description": "Name of the cluster",
            },
            {
                "name": "location",
                "type": "string",
                "required": False,
                "description": "Zone or region of the cluster",
            },
        ],
        example="get_cluster_health(cluster_name='my-cluster')",
        response_entity_type="Cluster",
        response_identifier_field="self_link",
        response_display_name_field="name",
    ),
    # Node Pool Operations
    OperationDefinition(
        operation_id="list_node_pools",
        name="List Node Pools",
        description="List all node pools in a GKE cluster. Returns node pool name, machine type, node count, and autoscaling configuration.",
        category="containers",
        parameters=[
            {
                "name": "cluster_name",
                "type": "string",
                "required": True,
                "description": "Name of the cluster",
            },
            {
                "name": "location",
                "type": "string",
                "required": False,
                "description": "Zone or region of the cluster",
            },
        ],
        example="list_node_pools(cluster_name='my-cluster')",
        response_entity_type="NodePool",
        response_identifier_field="self_link",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_node_pool",
        name="Get Node Pool Details",
        description="Get detailed information about a specific node pool including machine type, disk configuration, autoscaling, and management settings.",
        category="containers",
        parameters=[
            {
                "name": "cluster_name",
                "type": "string",
                "required": True,
                "description": "Name of the cluster",
            },
            {
                "name": "node_pool_name",
                "type": "string",
                "required": True,
                "description": "Name of the node pool",
            },
            {
                "name": "location",
                "type": "string",
                "required": False,
                "description": "Zone or region of the cluster",
            },
        ],
        example="get_node_pool(cluster_name='my-cluster', node_pool_name='default-pool')",
        response_entity_type="NodePool",
        response_identifier_field="self_link",
        response_display_name_field="name",
    ),
    # Cluster Operations (management)
    OperationDefinition(
        operation_id="get_cluster_credentials",
        name="Get Cluster Credentials Info",
        description="Get information needed to configure kubectl for a GKE cluster including endpoint and CA certificate.",
        category="containers",
        parameters=[
            {
                "name": "cluster_name",
                "type": "string",
                "required": True,
                "description": "Name of the cluster",
            },
            {
                "name": "location",
                "type": "string",
                "required": False,
                "description": "Zone or region of the cluster",
            },
        ],
        example="get_cluster_credentials(cluster_name='my-cluster')",
        response_entity_type="Cluster",
        response_identifier_field="self_link",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="list_cluster_operations",
        name="List Cluster Operations",
        description="List recent operations (create, update, delete) on GKE clusters. Useful for tracking ongoing or past changes.",
        category="containers",
        parameters=[
            {
                "name": "location",
                "type": "string",
                "required": False,
                "description": "Zone or region (default: all locations)",
            },
        ],
        example="list_cluster_operations()",
        response_entity_type="Operation",
        response_identifier_field="self_link",
        response_display_name_field="name",
    ),
]
