# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Kubernetes extraction schema for topology auto-discovery.

Defines declarative extraction rules for Kubernetes resources.
These rules specify how to extract entities and relationships from
Kubernetes API responses using JMESPath expressions.

Supported Entity Types:
    - Pod: Workload pods with runs_on, member_of, managed_by relationships
    - Node: Cluster nodes (no outgoing relationships)
    - Namespace: Cluster namespaces (created as stubs for other resources)
    - Deployment: Deployment controllers with member_of relationship
    - ReplicaSet: ReplicaSet controllers with member_of, managed_by relationships
    - Service: Services with member_of relationship (routes_to is deferred)
    - Ingress: Ingress resources with routes_to, member_of relationships
    - StatefulSet: StatefulSet controllers with member_of relationship
    - DaemonSet: DaemonSet controllers with member_of relationship

Relationship Types:
    - runs_on: Pod → Node (where pod is scheduled)
    - member_of: Resource → Namespace (namespace containment)
    - managed_by: Pod → ReplicaSet, ReplicaSet → Deployment (ownership)
    - routes_to: Ingress → Service (traffic routing)

Note: Service → Pod routing is handled by deferred correlation in BatchProcessor
since pods may not exist when services are extracted.
"""

from .rules import (
    AttributeExtraction,
    ConnectorExtractionSchema,
    DescriptionTemplate,
    EntityExtractionRule,
    RelationshipExtraction,
)

PROP_METADATA_LABELS = "metadata.labels"
PROP_METADATA_NAME = "metadata.name"
PROP_METADATA_NAMESPACE = "metadata.namespace"
PROP_SPEC_REPLICAS = "spec.replicas"
PROP_STATUS_READYREPLICAS = "status.readyReplicas"

# =============================================================================
# Kubernetes Extraction Schema
# =============================================================================

KUBERNETES_EXTRACTION_SCHEMA = ConnectorExtractionSchema(
    connector_type="kubernetes",
    entity_rules=[
        # =====================================================================
        # Pod Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="Pod",
            source_kinds=["Pod", "PodList"],
            source_operations=["list_pods", "get_pod", "describe_pod"],  # Typed K8s connector ops
            items_path="items",
            name_path=PROP_METADATA_NAME,
            scope_paths={"namespace": PROP_METADATA_NAMESPACE},
            description=DescriptionTemplate(
                template="K8s Pod {metadata.name}, namespace {metadata.namespace}, {status.phase}",
                fallback="K8s Pod",
            ),
            attributes=[
                AttributeExtraction(name="phase", path="status.phase"),
                AttributeExtraction(name="pod_ip", path="status.podIP"),
                AttributeExtraction(name="node_name", path="spec.nodeName"),
                AttributeExtraction(name="labels", path=PROP_METADATA_LABELS, default={}),
                AttributeExtraction(
                    name="owner_references",
                    path="metadata.ownerReferences",
                    default=[],
                ),
                AttributeExtraction(name="host_ip", path="status.hostIP"),
                AttributeExtraction(name="start_time", path="status.startTime"),
            ],
            relationships=[
                # Pod is member of Namespace
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="Namespace",
                    target_path=PROP_METADATA_NAMESPACE,
                    optional=False,
                ),
                # Pod runs on Node
                RelationshipExtraction(
                    relationship_type="runs_on",
                    target_type="Node",
                    target_path="spec.nodeName",
                    optional=True,
                ),
                # Pod managed by ReplicaSet (if owned)
                RelationshipExtraction(
                    relationship_type="managed_by",
                    target_type="ReplicaSet",
                    target_path="metadata.ownerReferences[?kind=='ReplicaSet'].name | [0]",
                    optional=True,
                ),
            ],
        ),
        # =====================================================================
        # Node Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="Node",
            source_kinds=["Node", "NodeList"],
            source_operations=[
                "list_nodes",
                "get_node",
                "describe_node",
            ],  # Typed K8s connector ops
            items_path="items",
            name_path=PROP_METADATA_NAME,
            scope_paths={},  # Nodes are cluster-scoped
            description=DescriptionTemplate(
                template="K8s Node {metadata.name}, {status.conditions[?type=='Ready'].status | [0]} ready",
                fallback="K8s Node",
            ),
            attributes=[
                # Identity attributes for cross-connector resolution (SAME_AS matching)
                AttributeExtraction(name="provider_id", path="spec.providerID"),
                AttributeExtraction(name="addresses", path="status.addresses", default=[]),
                # Capacity and runtime attributes
                AttributeExtraction(name="cpu", path="status.capacity.cpu"),
                AttributeExtraction(name="memory", path="status.capacity.memory"),
                AttributeExtraction(
                    name="kubelet_version",
                    path="status.nodeInfo.kubeletVersion",
                ),
                AttributeExtraction(
                    name="container_runtime",
                    path="status.nodeInfo.containerRuntimeVersion",
                ),
                AttributeExtraction(name="os_image", path="status.nodeInfo.osImage"),
                AttributeExtraction(name="conditions", path="status.conditions", default=[]),
                AttributeExtraction(name="labels", path=PROP_METADATA_LABELS, default={}),
            ],
            relationships=[],  # Nodes don't have outgoing relationships
        ),
        # =====================================================================
        # Namespace Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="Namespace",
            source_kinds=["Namespace", "NamespaceList"],
            source_operations=["list_namespaces", "get_namespace"],  # Typed K8s connector ops
            items_path="items",
            name_path=PROP_METADATA_NAME,
            scope_paths={},  # Namespaces are cluster-scoped
            description=DescriptionTemplate(
                template="K8s Namespace {metadata.name}, {status.phase}",
                fallback="K8s Namespace",
            ),
            attributes=[
                AttributeExtraction(name="phase", path="status.phase", default="Active"),
                AttributeExtraction(name="labels", path=PROP_METADATA_LABELS, default={}),
                AttributeExtraction(name="annotations", path="metadata.annotations", default={}),
            ],
            relationships=[],  # Namespaces don't have outgoing relationships
        ),
        # =====================================================================
        # Deployment Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="Deployment",
            source_kinds=["Deployment", "DeploymentList"],
            source_operations=[
                "list_deployments",
                "get_deployment",
                "describe_deployment",
            ],  # Typed K8s connector ops
            items_path="items",
            name_path=PROP_METADATA_NAME,
            scope_paths={"namespace": PROP_METADATA_NAMESPACE},
            description=DescriptionTemplate(
                template="K8s Deployment {metadata.name}, namespace {metadata.namespace}, {status.readyReplicas}/{spec.replicas} replicas",
                fallback="K8s Deployment",
            ),
            attributes=[
                AttributeExtraction(name="replicas", path=PROP_SPEC_REPLICAS, default=0),
                AttributeExtraction(
                    name="ready_replicas", path=PROP_STATUS_READYREPLICAS, default=0
                ),
                AttributeExtraction(
                    name="available_replicas", path="status.availableReplicas", default=0
                ),
                AttributeExtraction(name="strategy", path="spec.strategy.type"),
                AttributeExtraction(name="labels", path=PROP_METADATA_LABELS, default={}),
                AttributeExtraction(name="selector", path="spec.selector.matchLabels", default={}),
            ],
            relationships=[
                # Deployment is member of Namespace
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="Namespace",
                    target_path=PROP_METADATA_NAMESPACE,
                    optional=False,
                ),
            ],
        ),
        # =====================================================================
        # ReplicaSet Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="ReplicaSet",
            source_kinds=["ReplicaSet", "ReplicaSetList"],
            source_operations=["list_replicasets", "get_replicaset"],  # Typed K8s connector ops
            items_path="items",
            name_path=PROP_METADATA_NAME,
            scope_paths={"namespace": PROP_METADATA_NAMESPACE},
            description=DescriptionTemplate(
                template="K8s ReplicaSet {metadata.name}, namespace {metadata.namespace}, {status.readyReplicas}/{spec.replicas} replicas",
                fallback="K8s ReplicaSet",
            ),
            attributes=[
                AttributeExtraction(name="replicas", path=PROP_SPEC_REPLICAS, default=0),
                AttributeExtraction(
                    name="ready_replicas", path=PROP_STATUS_READYREPLICAS, default=0
                ),
                AttributeExtraction(name="labels", path=PROP_METADATA_LABELS, default={}),
                AttributeExtraction(
                    name="owner_references",
                    path="metadata.ownerReferences",
                    default=[],
                ),
            ],
            relationships=[
                # ReplicaSet is member of Namespace
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="Namespace",
                    target_path=PROP_METADATA_NAMESPACE,
                    optional=False,
                ),
                # ReplicaSet managed by Deployment (if owned)
                RelationshipExtraction(
                    relationship_type="managed_by",
                    target_type="Deployment",
                    target_path="metadata.ownerReferences[?kind=='Deployment'].name | [0]",
                    optional=True,
                ),
            ],
        ),
        # =====================================================================
        # Service Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="Service",
            source_kinds=["Service", "ServiceList"],
            source_operations=[
                "list_services",
                "get_service",
                "describe_service",
            ],  # Typed K8s connector ops
            items_path="items",
            name_path=PROP_METADATA_NAME,
            scope_paths={"namespace": PROP_METADATA_NAMESPACE},
            description=DescriptionTemplate(
                template="K8s Service {metadata.name}, namespace {metadata.namespace}, {spec.type}, IP: {spec.clusterIP}",
                fallback="K8s Service",
            ),
            attributes=[
                AttributeExtraction(name="type", path="spec.type", default="ClusterIP"),
                AttributeExtraction(name="cluster_ip", path="spec.clusterIP"),
                AttributeExtraction(name="selector", path="spec.selector", default={}),
                AttributeExtraction(name="ports", path="spec.ports", default=[]),
                AttributeExtraction(name="labels", path=PROP_METADATA_LABELS, default={}),
                AttributeExtraction(name="external_ips", path="spec.externalIPs", default=[]),
            ],
            relationships=[
                # Service is member of Namespace
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="Namespace",
                    target_path=PROP_METADATA_NAMESPACE,
                    optional=False,
                ),
                # Note: Service → Pod routes_to is handled by deferred correlation
                # since pods may not exist when services are extracted
            ],
        ),
        # =====================================================================
        # Ingress Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="Ingress",
            source_kinds=["Ingress", "IngressList"],
            source_operations=["list_ingresses", "get_ingress"],  # Typed K8s connector ops
            items_path="items",
            name_path=PROP_METADATA_NAME,
            scope_paths={"namespace": PROP_METADATA_NAMESPACE},
            description=DescriptionTemplate(
                template="K8s Ingress {metadata.name}, namespace {metadata.namespace}",
                fallback="K8s Ingress",
            ),
            attributes=[
                AttributeExtraction(
                    name="hosts",
                    path="spec.rules[*].host",
                    default=[],
                ),
                AttributeExtraction(name="ingress_class", path="spec.ingressClassName"),
                AttributeExtraction(name="tls", path="spec.tls", default=[]),
                AttributeExtraction(name="labels", path=PROP_METADATA_LABELS, default={}),
            ],
            relationships=[
                # Ingress is member of Namespace
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="Namespace",
                    target_path=PROP_METADATA_NAMESPACE,
                    optional=False,
                ),
                # Ingress routes to Services (extracted from all backend services)
                RelationshipExtraction(
                    relationship_type="routes_to",
                    target_type="Service",
                    target_path="spec.rules[*].http.paths[*].backend.service.name",
                    multiple=True,
                    optional=True,
                ),
            ],
        ),
        # =====================================================================
        # StatefulSet Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="StatefulSet",
            source_kinds=["StatefulSet", "StatefulSetList"],
            source_operations=["list_statefulsets", "get_statefulset"],  # Typed K8s connector ops
            items_path="items",
            name_path=PROP_METADATA_NAME,
            scope_paths={"namespace": PROP_METADATA_NAMESPACE},
            description=DescriptionTemplate(
                template="K8s StatefulSet {metadata.name}, namespace {metadata.namespace}, {status.readyReplicas}/{spec.replicas} replicas",
                fallback="K8s StatefulSet",
            ),
            attributes=[
                AttributeExtraction(name="replicas", path=PROP_SPEC_REPLICAS, default=0),
                AttributeExtraction(
                    name="ready_replicas", path=PROP_STATUS_READYREPLICAS, default=0
                ),
                AttributeExtraction(name="service_name", path="spec.serviceName"),
                AttributeExtraction(name="labels", path=PROP_METADATA_LABELS, default={}),
                AttributeExtraction(name="update_strategy", path="spec.updateStrategy.type"),
            ],
            relationships=[
                # StatefulSet is member of Namespace
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="Namespace",
                    target_path=PROP_METADATA_NAMESPACE,
                    optional=False,
                ),
            ],
        ),
        # =====================================================================
        # DaemonSet Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="DaemonSet",
            source_kinds=["DaemonSet", "DaemonSetList"],
            source_operations=["list_daemonsets", "get_daemonset"],  # Typed K8s connector ops
            items_path="items",
            name_path=PROP_METADATA_NAME,
            scope_paths={"namespace": PROP_METADATA_NAMESPACE},
            description=DescriptionTemplate(
                template="K8s DaemonSet {metadata.name}, namespace {metadata.namespace}, {status.numberReady}/{status.desiredNumberScheduled} ready",
                fallback="K8s DaemonSet",
            ),
            attributes=[
                AttributeExtraction(
                    name="desired_number_scheduled",
                    path="status.desiredNumberScheduled",
                    default=0,
                ),
                AttributeExtraction(name="number_ready", path="status.numberReady", default=0),
                AttributeExtraction(
                    name="number_available",
                    path="status.numberAvailable",
                    default=0,
                ),
                AttributeExtraction(name="labels", path=PROP_METADATA_LABELS, default={}),
                AttributeExtraction(name="update_strategy", path="spec.updateStrategy.type"),
            ],
            relationships=[
                # DaemonSet is member of Namespace
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="Namespace",
                    target_path=PROP_METADATA_NAMESPACE,
                    optional=False,
                ),
            ],
        ),
    ],
)
