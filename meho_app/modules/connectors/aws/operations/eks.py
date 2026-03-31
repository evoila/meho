# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS EKS Operation Definitions.

Operations for EKS clusters and node groups.
"""

from meho_app.modules.connectors.base import OperationDefinition

EKS_OPERATIONS = [
    OperationDefinition(
        operation_id="list_eks_clusters",
        name="List EKS Clusters",
        description=(
            "List all EKS (Elastic Kubernetes Service) clusters with full "
            "details including version, endpoint, VPC config, and status."
        ),
        category="container",
        parameters=[
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": "AWS region override",
            },
        ],
        example="list_eks_clusters",
        response_entity_type="EKSCluster",
        response_identifier_field="arn",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_eks_cluster",
        name="Get EKS Cluster Details",
        description=(
            "Get detailed information about a specific EKS cluster including "
            "Kubernetes version, endpoint, VPC configuration, and network settings."
        ),
        category="container",
        parameters=[
            {
                "name": "cluster_name",
                "type": "string",
                "required": True,
                "description": "Name of the EKS cluster",
            },
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": "AWS region override",
            },
        ],
        example="get_eks_cluster cluster_name=my-cluster",
        response_entity_type="EKSCluster",
        response_identifier_field="arn",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="list_eks_node_groups",
        name="List EKS Node Groups",
        description=(
            "List all node groups for an EKS cluster with scaling config, "
            "instance types, health status, and labels."
        ),
        category="container",
        parameters=[
            {
                "name": "cluster_name",
                "type": "string",
                "required": True,
                "description": "Name of the EKS cluster",
            },
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": "AWS region override",
            },
        ],
        example="list_eks_node_groups cluster_name=my-cluster",
    ),
    OperationDefinition(
        operation_id="get_eks_node_group",
        name="Get EKS Node Group Details",
        description=(
            "Get detailed information about a specific EKS node group "
            "including scaling configuration, instance types, and health."
        ),
        category="container",
        parameters=[
            {
                "name": "cluster_name",
                "type": "string",
                "required": True,
                "description": "Name of the EKS cluster",
            },
            {
                "name": "node_group_name",
                "type": "string",
                "required": True,
                "description": "Name of the node group",
            },
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": "AWS region override",
            },
        ],
        example="get_eks_node_group cluster_name=my-cluster node_group_name=my-nodes",
    ),
]
