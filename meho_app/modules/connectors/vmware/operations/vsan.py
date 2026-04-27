# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
VMware vSAN Operation Definitions

6 operations for vSAN health diagnostics, disk groups, capacity,
resync status, storage policies, and object health.
"""

from meho_app.modules.connectors.base import OperationDefinition

NAME_OF_THE_CLUSTER = "Name of the cluster"

VSAN_OPERATIONS = [
    OperationDefinition(
        operation_id="get_vsan_cluster_health",
        name="Get vSAN Cluster Health",
        description=(
            "Check vSAN configuration and health status for a cluster. "
            "Returns whether vSAN is enabled, default storage policy, and auto-claim settings. "
            "Use this first to verify vSAN is active before running other vSAN operations."
        ),
        category="storage",
        parameters=[
            {
                "name": "cluster_name",
                "type": "string",
                "required": True,
                "description": "Name of the cluster to check",
            }
        ],
        example="get_vsan_cluster_health(cluster_name='Production')",
        response_entity_type="ClusterComputeResource",
        response_identifier_field="cluster_name",
        response_display_name_field="cluster_name",
    ),
    OperationDefinition(
        operation_id="get_vsan_disk_groups",
        name="Get vSAN Disk Groups",
        description=(
            "List vSAN disk groups per host showing cache SSDs and capacity disks. "
            "Useful for diagnosing vSAN storage performance issues -- identifies "
            "which hosts contribute storage and their disk composition."
        ),
        category="storage",
        parameters=[
            {
                "name": "cluster_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_THE_CLUSTER,
            }
        ],
        example="get_vsan_disk_groups(cluster_name='Production')",
        response_entity_type="ClusterComputeResource",
        response_identifier_field="cluster_name",
        response_display_name_field="cluster_name",
    ),
    OperationDefinition(
        operation_id="get_vsan_capacity",
        name="Get vSAN Capacity",
        description=(
            "Get vSAN datastore capacity, free space, and provisioned storage. "
            "Check if vSAN storage is causing VM performance issues due to low free space "
            "or over-provisioning."
        ),
        category="storage",
        parameters=[
            {
                "name": "cluster_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_THE_CLUSTER,
            }
        ],
        example="get_vsan_capacity(cluster_name='Production')",
        response_entity_type="ClusterComputeResource",
        response_identifier_field="cluster_name",
        response_display_name_field="cluster_name",
    ),
    OperationDefinition(
        operation_id="get_vsan_resync_status",
        name="Get vSAN Resync Status",
        description=(
            "Check if vSAN objects are resyncing after host/disk failures. "
            "Active resyncs consume I/O bandwidth and can degrade VM performance. "
            "Requires vSAN SDK stubs; falls back to basic health if unavailable."
        ),
        category="storage",
        parameters=[
            {
                "name": "cluster_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_THE_CLUSTER,
            }
        ],
        example="get_vsan_resync_status(cluster_name='Production')",
        response_entity_type="ClusterComputeResource",
        response_identifier_field="cluster_name",
        response_display_name_field="cluster_name",
    ),
    OperationDefinition(
        operation_id="get_vsan_storage_policies",
        name="Get vSAN Storage Policies",
        description=(
            "Get vSAN default storage policy configuration including failures to tolerate "
            "and stripe width. Helps diagnose storage policy misconfigurations that affect "
            "capacity utilization and data protection."
        ),
        category="storage",
        parameters=[
            {
                "name": "cluster_name",
                "type": "string",
                "required": False,
                "description": "Name of the cluster (used for context)",
            }
        ],
        example="get_vsan_storage_policies(cluster_name='Production')",
        response_entity_type="ClusterComputeResource",
        response_identifier_field="cluster_name",
        response_display_name_field="cluster_name",
    ),
    OperationDefinition(
        operation_id="get_vsan_objects_health",
        name="Get vSAN Objects Health",
        description=(
            "Check vSAN object health and disk status per host. "
            "Identifies hosts with degraded disk groups or missing disks "
            "that could affect VM availability and performance."
        ),
        category="storage",
        parameters=[
            {
                "name": "cluster_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_THE_CLUSTER,
            }
        ],
        example="get_vsan_objects_health(cluster_name='Production')",
        response_entity_type="ClusterComputeResource",
        response_identifier_field="cluster_name",
        response_display_name_field="cluster_name",
    ),
]
