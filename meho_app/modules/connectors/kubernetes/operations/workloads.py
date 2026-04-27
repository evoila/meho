# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Kubernetes Workloads Operations - Deployments, StatefulSets, DaemonSets, Jobs, CronJobs

These are registered in the generic connector_operation table
so the agent can discover them via search_operations.
"""

from meho_app.modules.connectors.base import OperationDefinition

DESC_NAMESPACE_TO_LIST_FROM_IF = (
    "Namespace to list from. If not specified, lists from all namespaces."
)
FILTER_BY_LABEL_SELECTOR = "Filter by label selector"
NAMESPACE_THE_DEPLOYMENT_IS_IN = "Namespace the deployment is in"
NAME_OF_THE_DEPLOYMENT = "Name of the deployment"

WORKLOADS_OPERATIONS = [
    # ==========================================================================
    # Deployments
    # ==========================================================================
    OperationDefinition(
        operation_id="list_deployments",
        name="List Deployments",
        description="List all deployments in a namespace or across all namespaces. Returns "
        "replica counts, conditions, and available/unavailable replicas.",
        category="workloads",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": False,
                "description": DESC_NAMESPACE_TO_LIST_FROM_IF,
            },
            {
                "name": "label_selector",
                "type": "string",
                "required": False,
                "description": FILTER_BY_LABEL_SELECTOR,
            },
        ],
        example="list_deployments(namespace='default')",
        response_entity_type="Deployment",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_deployment",
        name="Get Deployment",
        description="Get details about a specific deployment including replicas, strategy, "
        "conditions, and pod template.",
        category="workloads",
        parameters=[
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": NAME_OF_THE_DEPLOYMENT,
            },
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": NAMESPACE_THE_DEPLOYMENT_IS_IN,
            },
        ],
        example="get_deployment(name='nginx', namespace='default')",
        response_entity_type="Deployment",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="scale_deployment",
        name="Scale Deployment",
        description="Scale a deployment to a specific number of replicas.",
        category="workloads",
        parameters=[
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": NAME_OF_THE_DEPLOYMENT,
            },
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": NAMESPACE_THE_DEPLOYMENT_IS_IN,
            },
            {
                "name": "replicas",
                "type": "integer",
                "required": True,
                "description": "Desired number of replicas",
            },
        ],
        example="scale_deployment(name='nginx', namespace='default', replicas=3)",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="restart_deployment",
        name="Restart Deployment",
        description="Trigger a rolling restart of all pods in a deployment. Useful for picking "
        "up ConfigMap changes or forcing a fresh start.",
        category="workloads",
        parameters=[
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": NAME_OF_THE_DEPLOYMENT,
            },
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": NAMESPACE_THE_DEPLOYMENT_IS_IN,
            },
        ],
        example="restart_deployment(name='nginx', namespace='default')",
        # Action operations return status
    ),
    # ==========================================================================
    # ReplicaSets
    # ==========================================================================
    OperationDefinition(
        operation_id="list_replicasets",
        name="List ReplicaSets",
        description="List all ReplicaSets in a namespace. Useful for seeing deployment history.",
        category="workloads",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": False,
                "description": DESC_NAMESPACE_TO_LIST_FROM_IF,
            },
            {
                "name": "label_selector",
                "type": "string",
                "required": False,
                "description": FILTER_BY_LABEL_SELECTOR,
            },
        ],
        example="list_replicasets(namespace='default')",
        response_entity_type="ReplicaSet",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_replicaset",
        name="Get ReplicaSet",
        description="Get details about a specific ReplicaSet.",
        category="workloads",
        parameters=[
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": "Name of the ReplicaSet",
            },
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Namespace the ReplicaSet is in",
            },
        ],
        example="get_replicaset(name='nginx-7ff4bf5d9', namespace='default')",
        response_entity_type="ReplicaSet",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    # ==========================================================================
    # StatefulSets
    # ==========================================================================
    OperationDefinition(
        operation_id="list_statefulsets",
        name="List StatefulSets",
        description="List all StatefulSets in a namespace or across all namespaces.",
        category="workloads",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": False,
                "description": DESC_NAMESPACE_TO_LIST_FROM_IF,
            },
            {
                "name": "label_selector",
                "type": "string",
                "required": False,
                "description": FILTER_BY_LABEL_SELECTOR,
            },
        ],
        example="list_statefulsets(namespace='default')",
        response_entity_type="StatefulSet",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_statefulset",
        name="Get StatefulSet",
        description="Get details about a specific StatefulSet including replicas and volume claims.",
        category="workloads",
        parameters=[
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": "Name of the StatefulSet",
            },
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Namespace the StatefulSet is in",
            },
        ],
        example="get_statefulset(name='postgres', namespace='default')",
        response_entity_type="StatefulSet",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="scale_statefulset",
        name="Scale StatefulSet",
        description="Scale a StatefulSet to a specific number of replicas.",
        category="workloads",
        parameters=[
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": "Name of the StatefulSet",
            },
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Namespace the StatefulSet is in",
            },
            {
                "name": "replicas",
                "type": "integer",
                "required": True,
                "description": "Desired number of replicas",
            },
        ],
        example="scale_statefulset(name='postgres', namespace='default', replicas=3)",
        # Action operations return status
    ),
    # ==========================================================================
    # DaemonSets
    # ==========================================================================
    OperationDefinition(
        operation_id="list_daemonsets",
        name="List DaemonSets",
        description="List all DaemonSets in a namespace or across all namespaces.",
        category="workloads",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": False,
                "description": DESC_NAMESPACE_TO_LIST_FROM_IF,
            },
            {
                "name": "label_selector",
                "type": "string",
                "required": False,
                "description": FILTER_BY_LABEL_SELECTOR,
            },
        ],
        example="list_daemonsets(namespace='kube-system')",
        response_entity_type="DaemonSet",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_daemonset",
        name="Get DaemonSet",
        description="Get details about a specific DaemonSet including desired/ready counts.",
        category="workloads",
        parameters=[
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": "Name of the DaemonSet",
            },
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Namespace the DaemonSet is in",
            },
        ],
        example="get_daemonset(name='fluentd', namespace='kube-system')",
        response_entity_type="DaemonSet",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    # ==========================================================================
    # Jobs
    # ==========================================================================
    OperationDefinition(
        operation_id="list_jobs",
        name="List Jobs",
        description="List all Jobs in a namespace or across all namespaces.",
        category="workloads",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": False,
                "description": DESC_NAMESPACE_TO_LIST_FROM_IF,
            },
            {
                "name": "label_selector",
                "type": "string",
                "required": False,
                "description": FILTER_BY_LABEL_SELECTOR,
            },
        ],
        example="list_jobs(namespace='default')",
        response_entity_type="Job",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_job",
        name="Get Job",
        description="Get details about a specific Job including completion status and conditions.",
        category="workloads",
        parameters=[
            {"name": "name", "type": "string", "required": True, "description": "Name of the Job"},
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Namespace the Job is in",
            },
        ],
        example="get_job(name='backup-job', namespace='default')",
        response_entity_type="Job",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    # ==========================================================================
    # CronJobs
    # ==========================================================================
    OperationDefinition(
        operation_id="list_cronjobs",
        name="List CronJobs",
        description="List all CronJobs in a namespace or across all namespaces.",
        category="workloads",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": False,
                "description": DESC_NAMESPACE_TO_LIST_FROM_IF,
            },
            {
                "name": "label_selector",
                "type": "string",
                "required": False,
                "description": FILTER_BY_LABEL_SELECTOR,
            },
        ],
        example="list_cronjobs(namespace='default')",
        response_entity_type="CronJob",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_cronjob",
        name="Get CronJob",
        description="Get details about a specific CronJob including schedule and last run time.",
        category="workloads",
        parameters=[
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": "Name of the CronJob",
            },
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Namespace the CronJob is in",
            },
        ],
        example="get_cronjob(name='daily-backup', namespace='default')",
        response_entity_type="CronJob",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
]
