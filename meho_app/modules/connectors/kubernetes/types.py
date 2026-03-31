# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Kubernetes Type Definitions (TASK-159)

These help the agent understand what entities exist in Kubernetes
and can be discovered via search_types.
"""

from meho_app.modules.connectors.base import TypeDefinition

KUBERNETES_TYPES = [
    # ==========================================================================
    # Core Resources
    # ==========================================================================
    TypeDefinition(
        type_name="Pod",
        description="The smallest deployable unit in Kubernetes. A pod contains one or more "
        "containers that share storage, network, and lifecycle.",
        category="core",
        properties=[
            {"name": "name", "type": "string", "description": "Pod name"},
            {"name": "namespace", "type": "string", "description": "Namespace the pod is in"},
            {
                "name": "phase",
                "type": "string",
                "description": "Pod phase: Pending, Running, Succeeded, Failed, Unknown",
            },
            {"name": "pod_ip", "type": "string", "description": "IP address assigned to the pod"},
            {
                "name": "node_name",
                "type": "string",
                "description": "Node where the pod is scheduled",
            },
            {"name": "containers", "type": "array", "description": "List of containers in the pod"},
            {"name": "labels", "type": "object", "description": "Labels attached to the pod"},
        ],
    ),
    TypeDefinition(
        type_name="Node",
        description="A worker machine in Kubernetes (physical or virtual). Nodes run pods and "
        "are managed by the control plane.",
        category="core",
        properties=[
            {"name": "name", "type": "string", "description": "Node name"},
            {
                "name": "status",
                "type": "string",
                "description": "Node status: Ready, NotReady, Unknown",
            },
            {
                "name": "unschedulable",
                "type": "boolean",
                "description": "Whether new pods can be scheduled",
            },
            {
                "name": "capacity",
                "type": "object",
                "description": "Total resources (cpu, memory, pods)",
            },
            {
                "name": "allocatable",
                "type": "object",
                "description": "Resources available for pods",
            },
            {"name": "kubelet_version", "type": "string", "description": "Version of kubelet"},
            {
                "name": "container_runtime",
                "type": "string",
                "description": "Container runtime version",
            },
        ],
    ),
    TypeDefinition(
        type_name="Namespace",
        description="A virtual cluster that provides scope for names. Namespaces isolate "
        "resources and allow multiple teams to share a cluster.",
        category="core",
        properties=[
            {"name": "name", "type": "string", "description": "Namespace name"},
            {
                "name": "phase",
                "type": "string",
                "description": "Namespace phase: Active, Terminating",
            },
            {"name": "labels", "type": "object", "description": "Labels attached to the namespace"},
        ],
    ),
    TypeDefinition(
        type_name="ConfigMap",
        description="Stores non-confidential configuration data as key-value pairs. "
        "Pods can consume ConfigMaps as environment variables or mounted files.",
        category="core",
        properties=[
            {"name": "name", "type": "string", "description": "ConfigMap name"},
            {"name": "namespace", "type": "string", "description": "Namespace the ConfigMap is in"},
            {"name": "data", "type": "object", "description": "Key-value configuration data"},
        ],
    ),
    TypeDefinition(
        type_name="Secret",
        description="Stores sensitive data like passwords, tokens, and keys. "
        "Secrets are base64 encoded and can be mounted into pods.",
        category="core",
        properties=[
            {"name": "name", "type": "string", "description": "Secret name"},
            {"name": "namespace", "type": "string", "description": "Namespace the Secret is in"},
            {
                "name": "type",
                "type": "string",
                "description": "Secret type (Opaque, kubernetes.io/tls, etc.)",
            },
        ],
    ),
    # ==========================================================================
    # Workload Resources
    # ==========================================================================
    TypeDefinition(
        type_name="Deployment",
        description="Manages a replicated application by creating and managing ReplicaSets. "
        "Provides declarative updates, rollback, and scaling.",
        category="workloads",
        properties=[
            {"name": "name", "type": "string", "description": "Deployment name"},
            {
                "name": "namespace",
                "type": "string",
                "description": "Namespace the Deployment is in",
            },
            {
                "name": "replicas",
                "type": "integer",
                "description": "Desired number of pod replicas",
            },
            {
                "name": "ready_replicas",
                "type": "integer",
                "description": "Number of ready replicas",
            },
            {
                "name": "available_replicas",
                "type": "integer",
                "description": "Number of available replicas",
            },
            {
                "name": "strategy",
                "type": "string",
                "description": "Update strategy (RollingUpdate, Recreate)",
            },
            {"name": "selector", "type": "object", "description": "Label selector for pods"},
        ],
    ),
    TypeDefinition(
        type_name="ReplicaSet",
        description="Maintains a stable set of replica pods. Usually created by Deployments "
        "and not directly by users.",
        category="workloads",
        properties=[
            {"name": "name", "type": "string", "description": "ReplicaSet name"},
            {
                "name": "namespace",
                "type": "string",
                "description": "Namespace the ReplicaSet is in",
            },
            {"name": "replicas", "type": "integer", "description": "Desired number of replicas"},
            {
                "name": "ready_replicas",
                "type": "integer",
                "description": "Number of ready replicas",
            },
        ],
    ),
    TypeDefinition(
        type_name="StatefulSet",
        description="Manages stateful applications with persistent storage and stable network "
        "identities. Pods are created in order and have predictable names.",
        category="workloads",
        properties=[
            {"name": "name", "type": "string", "description": "StatefulSet name"},
            {
                "name": "namespace",
                "type": "string",
                "description": "Namespace the StatefulSet is in",
            },
            {"name": "replicas", "type": "integer", "description": "Desired number of replicas"},
            {
                "name": "ready_replicas",
                "type": "integer",
                "description": "Number of ready replicas",
            },
            {
                "name": "service_name",
                "type": "string",
                "description": "Headless service for network identity",
            },
        ],
    ),
    TypeDefinition(
        type_name="DaemonSet",
        description="Ensures a pod runs on all (or selected) nodes. Useful for node-level "
        "agents like log collectors and monitoring agents.",
        category="workloads",
        properties=[
            {"name": "name", "type": "string", "description": "DaemonSet name"},
            {"name": "namespace", "type": "string", "description": "Namespace the DaemonSet is in"},
            {
                "name": "desired_number_scheduled",
                "type": "integer",
                "description": "Number of nodes that should run the pod",
            },
            {
                "name": "number_ready",
                "type": "integer",
                "description": "Number of nodes with ready pods",
            },
        ],
    ),
    TypeDefinition(
        type_name="Job",
        description="Creates pods that run to completion. Useful for batch processing "
        "and one-time tasks.",
        category="workloads",
        properties=[
            {"name": "name", "type": "string", "description": "Job name"},
            {"name": "namespace", "type": "string", "description": "Namespace the Job is in"},
            {
                "name": "completions",
                "type": "integer",
                "description": "Desired number of completions",
            },
            {
                "name": "succeeded",
                "type": "integer",
                "description": "Number of successful completions",
            },
            {"name": "failed", "type": "integer", "description": "Number of failed pods"},
        ],
    ),
    TypeDefinition(
        type_name="CronJob",
        description="Creates Jobs on a recurring schedule (cron format). Useful for "
        "scheduled tasks like backups.",
        category="workloads",
        properties=[
            {"name": "name", "type": "string", "description": "CronJob name"},
            {"name": "namespace", "type": "string", "description": "Namespace the CronJob is in"},
            {"name": "schedule", "type": "string", "description": "Cron schedule expression"},
            {
                "name": "last_schedule_time",
                "type": "string",
                "description": "Last time the job was scheduled",
            },
            {
                "name": "suspend",
                "type": "boolean",
                "description": "Whether the CronJob is suspended",
            },
        ],
    ),
    # ==========================================================================
    # Networking Resources
    # ==========================================================================
    TypeDefinition(
        type_name="Service",
        description="Exposes pods as a network service. Provides load balancing and "
        "service discovery via DNS or cluster IP.",
        category="networking",
        properties=[
            {"name": "name", "type": "string", "description": "Service name"},
            {"name": "namespace", "type": "string", "description": "Namespace the Service is in"},
            {
                "name": "type",
                "type": "string",
                "description": "Service type: ClusterIP, NodePort, LoadBalancer",
            },
            {"name": "cluster_ip", "type": "string", "description": "Internal cluster IP"},
            {
                "name": "external_ip",
                "type": "string",
                "description": "External IP (for LoadBalancer type)",
            },
            {"name": "ports", "type": "array", "description": "List of exposed ports"},
            {"name": "selector", "type": "object", "description": "Label selector for pods"},
        ],
    ),
    TypeDefinition(
        type_name="Ingress",
        description="Manages external access to services via HTTP/HTTPS. Provides "
        "load balancing, SSL termination, and name-based virtual hosting.",
        category="networking",
        properties=[
            {"name": "name", "type": "string", "description": "Ingress name"},
            {"name": "namespace", "type": "string", "description": "Namespace the Ingress is in"},
            {"name": "hosts", "type": "array", "description": "List of hosts this ingress handles"},
            {"name": "ingress_class", "type": "string", "description": "Ingress controller class"},
            {"name": "tls", "type": "array", "description": "TLS configuration for hosts"},
        ],
    ),
    TypeDefinition(
        type_name="Endpoints",
        description="Represents the set of IP addresses backing a Service. "
        "Shows which pods are ready to receive traffic.",
        category="networking",
        properties=[
            {"name": "name", "type": "string", "description": "Endpoints name (same as Service)"},
            {"name": "namespace", "type": "string", "description": "Namespace"},
            {"name": "addresses", "type": "array", "description": "List of ready pod IPs"},
            {
                "name": "not_ready_addresses",
                "type": "array",
                "description": "List of not-ready pod IPs",
            },
        ],
    ),
    TypeDefinition(
        type_name="NetworkPolicy",
        description="Specifies how pods can communicate with each other and external "
        "endpoints. Used for network segmentation and security.",
        category="networking",
        properties=[
            {"name": "name", "type": "string", "description": "NetworkPolicy name"},
            {
                "name": "namespace",
                "type": "string",
                "description": "Namespace the policy applies to",
            },
            {
                "name": "pod_selector",
                "type": "object",
                "description": "Label selector for target pods",
            },
            {
                "name": "policy_types",
                "type": "array",
                "description": "Policy types: Ingress, Egress",
            },
        ],
    ),
    # ==========================================================================
    # Storage Resources
    # ==========================================================================
    TypeDefinition(
        type_name="PersistentVolumeClaim",
        description="A request for storage by a user. Claims can request specific size "
        "and access modes (ReadWriteOnce, ReadOnlyMany, ReadWriteMany).",
        category="storage",
        properties=[
            {"name": "name", "type": "string", "description": "PVC name"},
            {"name": "namespace", "type": "string", "description": "Namespace the PVC is in"},
            {"name": "phase", "type": "string", "description": "PVC phase: Pending, Bound, Lost"},
            {"name": "access_modes", "type": "array", "description": "Requested access modes"},
            {"name": "storage_class", "type": "string", "description": "Storage class name"},
            {"name": "capacity", "type": "string", "description": "Actual storage capacity"},
            {"name": "volume_name", "type": "string", "description": "Bound PersistentVolume name"},
        ],
    ),
    TypeDefinition(
        type_name="PersistentVolume",
        description="A piece of storage provisioned by an administrator or dynamically. "
        "PVs have a lifecycle independent of any pod.",
        category="storage",
        properties=[
            {"name": "name", "type": "string", "description": "PV name"},
            {
                "name": "phase",
                "type": "string",
                "description": "PV phase: Available, Bound, Released, Failed",
            },
            {"name": "capacity", "type": "string", "description": "Storage capacity"},
            {"name": "access_modes", "type": "array", "description": "Supported access modes"},
            {
                "name": "reclaim_policy",
                "type": "string",
                "description": "Reclaim policy: Retain, Recycle, Delete",
            },
            {"name": "storage_class", "type": "string", "description": "Storage class name"},
            {"name": "claim_ref", "type": "object", "description": "Reference to bound PVC"},
        ],
    ),
    TypeDefinition(
        type_name="StorageClass",
        description="Defines a class of storage with provisioner, parameters, and "
        "reclaim policy. Used for dynamic volume provisioning.",
        category="storage",
        properties=[
            {"name": "name", "type": "string", "description": "StorageClass name"},
            {"name": "provisioner", "type": "string", "description": "Volume provisioner"},
            {
                "name": "reclaim_policy",
                "type": "string",
                "description": "Reclaim policy: Delete, Retain",
            },
            {
                "name": "volume_binding_mode",
                "type": "string",
                "description": "When to bind: Immediate, WaitForFirstConsumer",
            },
            {
                "name": "allow_volume_expansion",
                "type": "boolean",
                "description": "Whether volumes can be expanded",
            },
        ],
    ),
    # ==========================================================================
    # Events
    # ==========================================================================
    TypeDefinition(
        type_name="Event",
        description="A record of an event in the cluster (warning, normal, error). "
        "Events provide debugging information for pod and node issues.",
        category="events",
        properties=[
            {"name": "type", "type": "string", "description": "Event type: Normal, Warning"},
            {"name": "reason", "type": "string", "description": "Short reason for the event"},
            {"name": "message", "type": "string", "description": "Human-readable message"},
            {"name": "count", "type": "integer", "description": "Number of times event occurred"},
            {
                "name": "involved_object",
                "type": "object",
                "description": "Object this event is about",
            },
            {"name": "first_timestamp", "type": "string", "description": "First occurrence time"},
            {"name": "last_timestamp", "type": "string", "description": "Last occurrence time"},
        ],
    ),
]
