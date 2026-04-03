# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Prometheus Infrastructure Operations.

CPU, memory, disk, and network metric operations for pods, namespaces, and nodes.
All return summary statistics (min/max/avg/current/p95/trend), not raw time-series.
"""

from meho_app.modules.connectors.base import OperationDefinition

DESC_TIME_RANGE_TO_QUERY_EG = (
    "Time range to query (e.g., '1h', '30m', '6h', '24h', '7d'). Default: '1h'"
)

INFRASTRUCTURE_OPERATIONS = [
    # ==========================================================================
    # CPU Metrics
    # ==========================================================================
    OperationDefinition(
        operation_id="get_pod_cpu",
        name="Get Pod CPU Usage",
        description="Get CPU usage for all pods in a namespace. Returns summary statistics "
        "(min/max/avg/current/p95/trend) per pod, sorted by highest usage, top 10. "
        "Uses rate of container_cpu_usage_seconds_total over 5m window.",
        category="infrastructure",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Kubernetes namespace to query pods from",
            },
            {
                "name": "time_range",
                "type": "string",
                "required": False,
                "description": DESC_TIME_RANGE_TO_QUERY_EG,
            },
        ],
        example="get_pod_cpu(namespace='production')",
    ),
    OperationDefinition(
        operation_id="get_namespace_cpu",
        name="Get Namespace CPU Usage",
        description="Get total CPU usage for a namespace. Returns summary statistics "
        "(min/max/avg/current/p95/trend) as a single aggregate value. "
        "Uses rate of container_cpu_usage_seconds_total over 5m window.",
        category="infrastructure",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Kubernetes namespace to query",
            },
            {
                "name": "time_range",
                "type": "string",
                "required": False,
                "description": DESC_TIME_RANGE_TO_QUERY_EG,
            },
        ],
        example="get_namespace_cpu(namespace='production')",
    ),
    OperationDefinition(
        operation_id="get_node_cpu",
        name="Get Node CPU Usage",
        description="Get CPU usage for all cluster nodes. Returns summary statistics "
        "(min/max/avg/current/p95/trend) per node, sorted by highest usage, top 10. "
        "Uses 1 - idle CPU ratio from node_cpu_seconds_total.",
        category="infrastructure",
        parameters=[
            {
                "name": "time_range",
                "type": "string",
                "required": False,
                "description": DESC_TIME_RANGE_TO_QUERY_EG,
            },
        ],
        example="get_node_cpu()",
    ),
    # ==========================================================================
    # Memory Metrics
    # ==========================================================================
    OperationDefinition(
        operation_id="get_pod_memory",
        name="Get Pod Memory Usage",
        description="Get memory usage (working set) for all pods in a namespace. Returns summary statistics "
        "(min/max/avg/current/p95/trend) per pod in bytes, sorted by highest usage, top 10. "
        "Uses container_memory_working_set_bytes (not usage_bytes, which includes cache).",
        category="infrastructure",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Kubernetes namespace to query pods from",
            },
            {
                "name": "time_range",
                "type": "string",
                "required": False,
                "description": DESC_TIME_RANGE_TO_QUERY_EG,
            },
        ],
        example="get_pod_memory(namespace='production')",
    ),
    OperationDefinition(
        operation_id="get_namespace_memory",
        name="Get Namespace Memory Usage",
        description="Get total memory usage (working set) for a namespace. Returns summary statistics "
        "(min/max/avg/current/p95/trend) as a single aggregate value in bytes. "
        "Uses container_memory_working_set_bytes.",
        category="infrastructure",
        parameters=[
            {
                "name": "namespace",
                "type": "string",
                "required": True,
                "description": "Kubernetes namespace to query",
            },
            {
                "name": "time_range",
                "type": "string",
                "required": False,
                "description": DESC_TIME_RANGE_TO_QUERY_EG,
            },
        ],
        example="get_namespace_memory(namespace='production')",
    ),
    OperationDefinition(
        operation_id="get_node_memory",
        name="Get Node Memory Usage",
        description="Get memory usage for all cluster nodes. Returns summary statistics "
        "(min/max/avg/current/p95/trend) per node in bytes, sorted by highest usage, top 10. "
        "Computes used memory as MemTotal - MemAvailable.",
        category="infrastructure",
        parameters=[
            {
                "name": "time_range",
                "type": "string",
                "required": False,
                "description": DESC_TIME_RANGE_TO_QUERY_EG,
            },
        ],
        example="get_node_memory()",
    ),
    # ==========================================================================
    # Disk & Network Metrics
    # ==========================================================================
    OperationDefinition(
        operation_id="get_disk_usage",
        name="Get Node Disk Usage",
        description="Get root filesystem disk usage for all cluster nodes. Returns summary statistics "
        "(min/max/avg/current/p95/trend) per node as usage ratio (0-1), sorted by highest, top 10. "
        "Queries root mountpoint, excludes tmpfs filesystems.",
        category="infrastructure",
        parameters=[
            {
                "name": "time_range",
                "type": "string",
                "required": False,
                "description": DESC_TIME_RANGE_TO_QUERY_EG,
            },
        ],
        example="get_disk_usage()",
    ),
    OperationDefinition(
        operation_id="get_network_io",
        name="Get Node Network I/O",
        description="Get network receive and transmit rates for all cluster nodes. Returns summary statistics "
        "(min/max/avg/current/p95/trend) per node in bytes/sec for both rx and tx, sorted by "
        "highest receive rate, top 10. Excludes loopback and virtual interfaces.",
        category="infrastructure",
        parameters=[
            {
                "name": "time_range",
                "type": "string",
                "required": False,
                "description": DESC_TIME_RANGE_TO_QUERY_EG,
            },
        ],
        example="get_network_io()",
    ),
]
