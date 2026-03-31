# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS ECS Operation Definitions.

Operations for ECS clusters, services, and tasks.
"""

from meho_app.modules.connectors.base import OperationDefinition

ECS_OPERATIONS = [
    OperationDefinition(
        operation_id="list_ecs_clusters",
        name="List ECS Clusters",
        description=(
            "List all ECS clusters with full details including running/pending "
            "task counts, active services, and capacity providers."
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
        example="list_ecs_clusters",
        response_entity_type="ECSCluster",
        response_identifier_field="cluster_arn",
        response_display_name_field="cluster_name",
    ),
    OperationDefinition(
        operation_id="list_ecs_services",
        name="List ECS Services",
        description=(
            "List all services in an ECS cluster with deployment details, "
            "desired/running/pending counts, and load balancer configuration."
        ),
        category="container",
        parameters=[
            {
                "name": "cluster",
                "type": "string",
                "required": True,
                "description": "Cluster name or ARN",
            },
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": "AWS region override",
            },
        ],
        example="list_ecs_services cluster=my-cluster",
    ),
    OperationDefinition(
        operation_id="list_ecs_tasks",
        name="List ECS Tasks",
        description=(
            "List tasks in an ECS cluster, optionally filtered by service. "
            "Returns task status, containers, CPU/memory, and launch type."
        ),
        category="container",
        parameters=[
            {
                "name": "cluster",
                "type": "string",
                "required": True,
                "description": "Cluster name or ARN",
            },
            {
                "name": "service_name",
                "type": "string",
                "required": False,
                "description": "Filter tasks by service name",
            },
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": "AWS region override",
            },
        ],
        example="list_ecs_tasks cluster=my-cluster service_name=my-service",
    ),
    OperationDefinition(
        operation_id="get_ecs_service",
        name="Get ECS Service Details",
        description=(
            "Get detailed information about a specific ECS service including "
            "deployments, load balancers, and task definition."
        ),
        category="container",
        parameters=[
            {
                "name": "cluster",
                "type": "string",
                "required": True,
                "description": "Cluster name or ARN",
            },
            {
                "name": "service_name",
                "type": "string",
                "required": True,
                "description": "Service name",
            },
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": "AWS region override",
            },
        ],
        example="get_ecs_service cluster=my-cluster service_name=my-service",
    ),
    OperationDefinition(
        operation_id="get_ecs_task",
        name="Get ECS Task Details",
        description=(
            "Get detailed information about a specific ECS task including "
            "containers, status, and resource allocation."
        ),
        category="container",
        parameters=[
            {
                "name": "cluster",
                "type": "string",
                "required": True,
                "description": "Cluster name or ARN",
            },
            {
                "name": "task_arn",
                "type": "string",
                "required": True,
                "description": "Task ARN",
            },
            {
                "name": "region",
                "type": "string",
                "required": False,
                "description": "AWS region override",
            },
        ],
        example="get_ecs_task cluster=my-cluster task_arn=arn:aws:ecs:...",
    ),
]
