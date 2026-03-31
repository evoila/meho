# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Kubernetes Core Operations - Pods, Nodes, Namespaces, ConfigMaps, Secrets

These are registered in the generic connector_operation table
so the agent can discover them via search_operations.
"""

from meho_app.modules.connectors.base import OperationDefinition

CORE_OPERATIONS = [
    # ==========================================================================
    # Pods
    # ==========================================================================
    OperationDefinition(
        operation_id="list_pods",
        name="List Pods",
        description="List all pods in a namespace or across all namespaces. Returns pod status, "
        "containers, resource usage, IP addresses, and node placement.",
        category="core",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": False,
                "description": "Namespace to list pods from. If not specified, lists from all namespaces.",
            },
            {
                "name": "label_selector",
                "type": "string",
                "required": False,
                "description": "Filter by label selector (e.g., 'app=nginx,tier=frontend')",
            },
            {
                "name": "field_selector",
                "type": "string",
                "required": False,
                "description": "Filter by field selector (e.g., 'status.phase=Running')",
            },
        ],
        example="list_pods(namespace='default', label_selector='app=web')",
        response_entity_type="Pod",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_pod",
        name="Get Pod",
        description="Get detailed information about a specific pod including status, containers, "
        "volumes, conditions, and events.",
        category="core",
        parameters=[
            {"name": "name", "type": "string", "required": True, "description": "Name of the pod"},
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Namespace the pod is in",
            },
        ],
        example="get_pod(name='nginx-abc123', namespace='default')",
        response_entity_type="Pod",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_pod_logs",
        name="Get Pod Logs",
        description="Retrieve logs from a pod container. Supports tail lines, since time, and "
        "previous container logs.",
        category="core",
        parameters=[
            {"name": "name", "type": "string", "required": True, "description": "Name of the pod"},
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Namespace the pod is in",
            },
            {
                "name": "container",
                "type": "string",
                "required": False,
                "description": "Container name (required if pod has multiple containers)",
            },
            {
                "name": "tail_lines",
                "type": "integer",
                "required": False,
                "description": "Number of lines from end of logs to return (default: 100)",
            },
            {
                "name": "since_seconds",
                "type": "integer",
                "required": False,
                "description": "Only return logs newer than this many seconds",
            },
            {
                "name": "previous",
                "type": "boolean",
                "required": False,
                "description": "Return logs from previous container instance (for crash debugging)",
            },
        ],
        example="get_pod_logs(name='nginx-abc123', namespace='default', tail_lines=100)",
        # Logs don't have entity type - it's raw text
    ),
    OperationDefinition(
        operation_id="describe_pod",
        name="Describe Pod",
        description="Get comprehensive pod information including events, conditions, volumes, "
        "and container details. Similar to 'kubectl describe pod'.",
        category="core",
        parameters=[
            {"name": "name", "type": "string", "required": True, "description": "Name of the pod"},
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Namespace the pod is in",
            },
        ],
        example="describe_pod(name='nginx-abc123', namespace='default')",
        response_entity_type="Pod",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="delete_pod",
        name="Delete Pod",
        description="Delete a pod. The pod will be recreated if managed by a controller (Deployment, etc.).",
        category="core",
        parameters=[
            {"name": "name", "type": "string", "required": True, "description": "Name of the pod"},
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Namespace the pod is in",
            },
            {
                "name": "grace_period_seconds",
                "type": "integer",
                "required": False,
                "description": "Grace period for termination (default: 30)",
            },
        ],
        example="delete_pod(name='nginx-abc123', namespace='default')",
        # Delete operations return status, not entity
    ),
    # ==========================================================================
    # Nodes
    # ==========================================================================
    OperationDefinition(
        operation_id="list_nodes",
        name="List Nodes",
        description="List all nodes in the cluster with status, capacity, and allocatable resources.",
        category="core",
        parameters=[
            {
                "name": "label_selector",
                "type": "string",
                "required": False,
                "description": "Filter by label selector (e.g., 'node-role.kubernetes.io/worker=')",
            },
        ],
        example="list_nodes()",
        response_entity_type="Node",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_node",
        name="Get Node",
        description="Get detailed information about a specific node including conditions, "
        "capacity, allocatable resources, and system info.",
        category="core",
        parameters=[
            {"name": "name", "type": "string", "required": True, "description": "Name of the node"},
        ],
        example="get_node(name='worker-01')",
        response_entity_type="Node",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="describe_node",
        name="Describe Node",
        description="Get comprehensive node information including conditions, capacity, "
        "allocatable resources, running pods, and events.",
        category="core",
        parameters=[
            {"name": "name", "type": "string", "required": True, "description": "Name of the node"},
        ],
        example="describe_node(name='worker-01')",
        response_entity_type="Node",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="cordon_node",
        name="Cordon Node",
        description="Mark a node as unschedulable. Existing pods are not affected but no new pods "
        "will be scheduled to this node.",
        category="core",
        parameters=[
            {"name": "name", "type": "string", "required": True, "description": "Name of the node"},
        ],
        example="cordon_node(name='worker-01')",
        # Action operations return status
    ),
    OperationDefinition(
        operation_id="uncordon_node",
        name="Uncordon Node",
        description="Mark a node as schedulable again. New pods can be scheduled to this node.",
        category="core",
        parameters=[
            {"name": "name", "type": "string", "required": True, "description": "Name of the node"},
        ],
        example="uncordon_node(name='worker-01')",
        # Action operations return status
    ),
    # ==========================================================================
    # Namespaces
    # ==========================================================================
    OperationDefinition(
        operation_id="list_namespaces",
        name="List Namespaces",
        description="List all namespaces in the cluster with status and labels.",
        category="core",
        parameters=[],
        example="list_namespaces()",
        response_entity_type="Namespace",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_namespace",
        name="Get Namespace",
        description="Get details about a specific namespace including status, labels, and annotations.",
        category="core",
        parameters=[
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": "Name of the namespace",
            },
        ],
        example="get_namespace(name='production')",
        response_entity_type="Namespace",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    # ==========================================================================
    # ConfigMaps
    # ==========================================================================
    OperationDefinition(
        operation_id="list_configmaps",
        name="List ConfigMaps",
        description="List all ConfigMaps in a namespace.",
        category="core",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": False,
                "description": "Namespace to list ConfigMaps from. Defaults to 'default'.",
            },
        ],
        example="list_configmaps(namespace='default')",
        response_entity_type="ConfigMap",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_configmap",
        name="Get ConfigMap",
        description="Get a specific ConfigMap including its data keys and values.",
        category="core",
        parameters=[
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": "Name of the ConfigMap",
            },
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Namespace the ConfigMap is in",
            },
        ],
        example="get_configmap(name='app-config', namespace='default')",
        response_entity_type="ConfigMap",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    # ==========================================================================
    # Secrets
    # ==========================================================================
    OperationDefinition(
        operation_id="list_secrets",
        name="List Secrets",
        description="List all Secrets in a namespace (data values are NOT returned for security).",
        category="core",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": False,
                "description": "Namespace to list Secrets from. Defaults to 'default'.",
            },
        ],
        example="list_secrets(namespace='default')",
        response_entity_type="Secret",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_secret",
        name="Get Secret",
        description="Get a specific Secret metadata (data values are base64 encoded for security).",
        category="core",
        parameters=[
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": "Name of the Secret",
            },
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Namespace the Secret is in",
            },
            {
                "name": "decode",
                "type": "boolean",
                "required": False,
                "description": "Whether to decode base64 values (default: False)",
            },
        ],
        example="get_secret(name='db-credentials', namespace='default')",
        response_entity_type="Secret",
        response_identifier_field="uid",
        response_display_name_field="name",
    ),
]
