# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Proxmox Node Operations

Defines operations for Proxmox nodes (hosts) and cluster.
"""

from meho_app.modules.connectors.base import OperationDefinition

NODE_OPERATIONS = [
    OperationDefinition(
        operation_id="list_nodes",
        name="List Nodes",
        description="Get all nodes in the Proxmox cluster. Returns name, status, CPU/memory/disk usage, uptime for each node.",
        category="nodes",
        parameters=[],
        example="list_nodes()",
        response_entity_type="Node",
        response_identifier_field="node",
        response_display_name_field="node",
    ),
    OperationDefinition(
        operation_id="get_node",
        name="Get Node Details",
        description="Get detailed information about a specific Proxmox node including hardware info and resource usage.",
        category="nodes",
        parameters=[
            {"name": "node", "type": "string", "required": True, "description": "Node name"},
        ],
        example="get_node(node='pve1')",
        response_entity_type="Node",
        response_identifier_field="node",
        response_display_name_field="node",
    ),
    OperationDefinition(
        operation_id="get_node_status",
        name="Get Node Status",
        description="Get current status and system information for a node including kernel version, PVE version, and boot info.",
        category="nodes",
        parameters=[
            {"name": "node", "type": "string", "required": True, "description": "Node name"},
        ],
        example="get_node_status(node='pve1')",
        response_entity_type="Node",
        response_identifier_field="node",
        response_display_name_field="node",
    ),
    OperationDefinition(
        operation_id="get_node_resources",
        name="Get Node Resources",
        description="Get CPU, memory, and disk resource usage for a specific node with detailed breakdown.",
        category="nodes",
        parameters=[
            {"name": "node", "type": "string", "required": True, "description": "Node name"},
        ],
        example="get_node_resources(node='pve1')",
        response_entity_type="Node",
        response_identifier_field="node",
        response_display_name_field="node",
    ),
    OperationDefinition(
        operation_id="get_cluster_status",
        name="Get Cluster Status",
        description="Get the overall status of the Proxmox cluster including quorum state and node membership.",
        category="nodes",
        parameters=[],
        example="get_cluster_status()",
        response_entity_type="Cluster",
        response_identifier_field="name",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_cluster_resources",
        name="Get Cluster Resources",
        description="Get all resources in the cluster including VMs, containers, storage, and nodes with their status.",
        category="nodes",
        parameters=[
            {
                "name": "type",
                "type": "string",
                "required": False,
                "description": "Filter by type: vm, node, storage, pool (optional)",
            },
        ],
        example="get_cluster_resources(type='vm')",
        response_entity_type="Resource",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
]
