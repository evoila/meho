# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GCP Type Definitions (TASK-102)

Defines entity types for GCP resources.
These are registered in the connector_type table for agent discovery.
"""

from meho_app.modules.connectors.base import TypeDefinition

GCP_TYPES = [
    # Compute Engine Types
    TypeDefinition(
        type_name="Instance",
        description="A GCP Compute Engine virtual machine instance. Contains compute resources with CPU, memory, and storage. Runs in a specific zone.",
        category="compute",
        properties=[
            {"name": "id", "type": "string", "description": "Unique instance ID"},
            {"name": "name", "type": "string", "description": "Instance name"},
            {"name": "zone", "type": "string", "description": "Zone where instance runs"},
            {
                "name": "machine_type",
                "type": "string",
                "description": "Machine type (e.g., n1-standard-4)",
            },
            {
                "name": "status",
                "type": "string",
                "description": "Instance status (RUNNING, STOPPED, etc.)",
            },
            {"name": "internal_ip", "type": "string", "description": "Internal IP address"},
            {
                "name": "external_ip",
                "type": "string",
                "description": "External IP address (if any)",
            },
            {"name": "labels", "type": "object", "description": "User-defined labels"},
        ],
    ),
    TypeDefinition(
        type_name="Disk",
        description="A GCP Persistent Disk for storage. Can be attached to instances. Supports snapshots and different performance tiers.",
        category="storage",
        properties=[
            {"name": "id", "type": "string", "description": "Unique disk ID"},
            {"name": "name", "type": "string", "description": "Disk name"},
            {"name": "zone", "type": "string", "description": "Zone where disk is located"},
            {"name": "size_gb", "type": "integer", "description": "Disk size in GB"},
            {
                "name": "type",
                "type": "string",
                "description": "Disk type (pd-standard, pd-ssd, etc.)",
            },
            {"name": "status", "type": "string", "description": "Disk status"},
            {"name": "users", "type": "array", "description": "Instances using this disk"},
        ],
    ),
    TypeDefinition(
        type_name="Snapshot",
        description="A point-in-time backup of a persistent disk. Can be used to create new disks or restore data.",
        category="storage",
        properties=[
            {"name": "id", "type": "string", "description": "Unique snapshot ID"},
            {"name": "name", "type": "string", "description": "Snapshot name"},
            {"name": "source_disk", "type": "string", "description": "Source disk name"},
            {"name": "disk_size_gb", "type": "integer", "description": "Size of source disk in GB"},
            {"name": "storage_bytes", "type": "integer", "description": "Actual storage used"},
            {"name": "status", "type": "string", "description": "Snapshot status"},
        ],
    ),
    # Network Types
    TypeDefinition(
        type_name="VPCNetwork",
        description="A Virtual Private Cloud network in GCP. Contains subnetworks and firewall rules. Provides isolation and connectivity.",
        category="networking",
        properties=[
            {"name": "id", "type": "string", "description": "Unique network ID"},
            {"name": "name", "type": "string", "description": "Network name"},
            {
                "name": "auto_create_subnetworks",
                "type": "boolean",
                "description": "Whether subnets are auto-created",
            },
            {"name": "routing_mode", "type": "string", "description": "REGIONAL or GLOBAL routing"},
            {"name": "subnetworks", "type": "array", "description": "List of subnetwork names"},
            {"name": "mtu", "type": "integer", "description": "Maximum transmission unit"},
        ],
    ),
    TypeDefinition(
        type_name="Subnetwork",
        description="A regional subnetwork within a VPC. Defines IP ranges and connectivity for resources in a region.",
        category="networking",
        properties=[
            {"name": "id", "type": "string", "description": "Unique subnetwork ID"},
            {"name": "name", "type": "string", "description": "Subnetwork name"},
            {"name": "network", "type": "string", "description": "Parent VPC network"},
            {"name": "region", "type": "string", "description": "Region of the subnetwork"},
            {"name": "ip_cidr_range", "type": "string", "description": "Primary IP CIDR range"},
            {"name": "gateway_address", "type": "string", "description": "Gateway IP address"},
        ],
    ),
    TypeDefinition(
        type_name="Firewall",
        description="A firewall rule in GCP. Controls ingress/egress traffic to instances based on tags, service accounts, or IP ranges.",
        category="networking",
        properties=[
            {"name": "id", "type": "string", "description": "Unique firewall ID"},
            {"name": "name", "type": "string", "description": "Firewall rule name"},
            {"name": "network", "type": "string", "description": "Network this rule applies to"},
            {"name": "direction", "type": "string", "description": "INGRESS or EGRESS"},
            {
                "name": "priority",
                "type": "integer",
                "description": "Rule priority (lower = higher priority)",
            },
            {"name": "source_ranges", "type": "array", "description": "Source IP CIDR ranges"},
            {"name": "allowed", "type": "array", "description": "Allowed protocols and ports"},
        ],
    ),
    # GKE Types
    TypeDefinition(
        type_name="GKECluster",
        description="A Google Kubernetes Engine cluster. Managed Kubernetes control plane with node pools for running containerized workloads.",
        category="containers",
        properties=[
            {"name": "name", "type": "string", "description": "Cluster name"},
            {"name": "location", "type": "string", "description": "Zone or region of the cluster"},
            {
                "name": "status",
                "type": "string",
                "description": "Cluster status (RUNNING, PROVISIONING, etc.)",
            },
            {
                "name": "current_master_version",
                "type": "string",
                "description": "Kubernetes master version",
            },
            {"name": "current_node_count", "type": "integer", "description": "Total node count"},
            {"name": "endpoint", "type": "string", "description": "Kubernetes API endpoint"},
            {"name": "node_pools", "type": "array", "description": "Node pools in the cluster"},
        ],
    ),
    TypeDefinition(
        type_name="NodePool",
        description="A GKE node pool. A group of nodes with the same configuration (machine type, disk, etc.) within a cluster.",
        category="containers",
        properties=[
            {"name": "name", "type": "string", "description": "Node pool name"},
            {"name": "status", "type": "string", "description": "Node pool status"},
            {"name": "machine_type", "type": "string", "description": "Machine type for nodes"},
            {"name": "disk_size_gb", "type": "integer", "description": "Boot disk size in GB"},
            {
                "name": "initial_node_count",
                "type": "integer",
                "description": "Initial number of nodes",
            },
            {"name": "autoscaling", "type": "object", "description": "Autoscaling configuration"},
        ],
    ),
    # Monitoring Types
    TypeDefinition(
        type_name="MetricDescriptor",
        description="A Cloud Monitoring metric descriptor. Defines a metric type, its labels, and value type.",
        category="monitoring",
        properties=[
            {"name": "name", "type": "string", "description": "Full metric name"},
            {
                "name": "type",
                "type": "string",
                "description": "Metric type (e.g., compute.googleapis.com/instance/cpu/usage_time)",
            },
            {"name": "display_name", "type": "string", "description": "Human-readable name"},
            {"name": "description", "type": "string", "description": "Metric description"},
            {"name": "metric_kind", "type": "string", "description": "GAUGE, DELTA, or CUMULATIVE"},
            {
                "name": "value_type",
                "type": "string",
                "description": "Value type (INT64, DOUBLE, etc.)",
            },
        ],
    ),
    TypeDefinition(
        type_name="AlertPolicy",
        description="A Cloud Monitoring alert policy. Defines conditions for triggering alerts and notification channels.",
        category="monitoring",
        properties=[
            {"name": "name", "type": "string", "description": "Full alert policy name"},
            {"name": "display_name", "type": "string", "description": "Human-readable name"},
            {"name": "enabled", "type": "boolean", "description": "Whether the policy is enabled"},
            {"name": "conditions", "type": "array", "description": "Alert conditions"},
            {
                "name": "notification_channels",
                "type": "array",
                "description": "Notification channel IDs",
            },
        ],
    ),
]
