# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
VMware Capacity Planning Operation Definitions

4 operations for cluster capacity summary, overcommitment ratios,
datastore utilization, and host load distribution.
"""

from meho_app.modules.connectors.base import OperationDefinition

CAPACITY_OPERATIONS = [
    OperationDefinition(
        operation_id="get_cluster_capacity",
        name="Get Cluster Capacity Summary",
        description=(
            "Get cluster-level capacity summary including total/effective CPU and memory, "
            "host count, core count, and VM count. Use this to understand overall cluster "
            "sizing and headroom for capacity planning."
        ),
        category="monitoring",
        parameters=[
            {
                "name": "cluster_name",
                "type": "string",
                "required": True,
                "description": "Name of the cluster",
            }
        ],
        example="get_cluster_capacity(cluster_name='Production')",
        response_entity_type="ClusterComputeResource",
        response_identifier_field="cluster_name",
        response_display_name_field="cluster_name",
    ),
    OperationDefinition(
        operation_id="get_cluster_overcommitment",
        name="Get Cluster Overcommitment Ratios",
        description=(
            "Calculate vCPU-to-pCPU and memory overcommitment ratios for a cluster. "
            "Check if cluster is overprovisioned -- high ratios indicate contention risk. "
            "Typical healthy ranges: vCPU:pCPU < 4:1, memory < 1.5:1."
        ),
        category="monitoring",
        parameters=[
            {
                "name": "cluster_name",
                "type": "string",
                "required": True,
                "description": "Name of the cluster",
            }
        ],
        example="get_cluster_overcommitment(cluster_name='Production')",
        response_entity_type="ClusterComputeResource",
        response_identifier_field="cluster_name",
        response_display_name_field="cluster_name",
    ),
    OperationDefinition(
        operation_id="get_datastore_utilization",
        name="Get All Datastore Utilization",
        description=(
            "Get capacity, free space, provisioned storage, and utilization percentage "
            "for all datastores. Identifies datastores running low on space or heavily "
            "over-provisioned with thin disks. No parameters required -- lists all datastores."
        ),
        category="monitoring",
        parameters=[],
        example="get_datastore_utilization()",
        response_entity_type="Datastore",
        response_identifier_field="name",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_host_load_distribution",
        name="Get Host Load Distribution",
        description=(
            "Get per-host CPU and memory utilization within a cluster. Shows load imbalance -- "
            "high imbalance suggests DRS is not distributing load effectively. Useful for "
            "identifying hot hosts causing VM performance degradation."
        ),
        category="monitoring",
        parameters=[
            {
                "name": "cluster_name",
                "type": "string",
                "required": True,
                "description": "Name of the cluster",
            }
        ],
        example="get_host_load_distribution(cluster_name='Production')",
        response_entity_type="ClusterComputeResource",
        response_identifier_field="cluster_name",
        response_display_name_field="cluster_name",
    ),
]
