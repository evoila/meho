# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Kubernetes topology schema definition.

Defines entity types and valid relationships for Kubernetes resources.
Based on the existing KubernetesExtractor in auto_discovery/kubernetes.py.

Entity Types:
- Namespace (cluster-scoped)
- Node (cluster-scoped)
- Pod (namespace-scoped, ephemeral)
- Deployment (namespace-scoped)
- ReplicaSet (namespace-scoped)
- StatefulSet (namespace-scoped)
- DaemonSet (namespace-scoped)
- Service (namespace-scoped)
- Ingress (namespace-scoped)

Relationship Hierarchy:
- Containment: Pod → member_of → Namespace
- Ownership: Deployment → manages → ReplicaSet → manages → Pod
- Scheduling: Pod → runs_on → Node
- Networking: Ingress → routes_to → Service → routes_to → Pod
"""

from .base import (
    ConnectorTopologySchema,
    EntityTypeDefinition,
    RelationshipRule,
    SameAsEligibility,
    Volatility,
)

# =============================================================================
# Entity Type Definitions
# =============================================================================

_NAMESPACE = EntityTypeDefinition(
    name="Namespace",
    scoped=False,  # Cluster-scoped
    identity_fields=["name"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "Contains all namespaced resources (Pods, Services, Deployments)",
        "To find resources: query entities where scope.namespace = this.name",
    ],
    common_queries=[
        "What resources are in this namespace?",
        "How many pods are in this namespace?",
    ],
)

_NODE = EntityTypeDefinition(
    name="Node",
    scoped=False,  # Cluster-scoped
    identity_fields=["name"],
    volatility=Volatility.STABLE,
    # SAME_AS: K8s Node can correlate with VMs from other connectors
    # A K8s Node IS the underlying VM/Instance in most cloud deployments
    same_as=SameAsEligibility(
        can_match=["VM", "Instance", "Host"],  # VMware VM, GCP Instance, Proxmox Host
        matching_attributes=[
            "spec.providerID",  # "gce://project/zone/vm-name"
            "status.addresses[?type=='InternalIP'].address",
            "status.addresses[?type=='Hostname'].address",
            "metadata.name",
        ],
    ),
    navigation_hints=[
        "Runs Pods - find via runs_on relationship (reverse)",
        "Cluster-scoped resource",
    ],
    common_queries=[
        "What pods are running on this node?",
        "Is this node healthy?",
        "What is the capacity of this node?",
    ],
)

_POD = EntityTypeDefinition(
    name="Pod",
    scoped=True,
    scope_type="namespace",
    identity_fields=["namespace", "name"],
    volatility=Volatility.EPHEMERAL,
    # SAME_AS: Pods are ephemeral - they have no external equivalent
    # Pods restart, move, and have short lifespans - not suitable for correlation
    same_as=None,
    navigation_hints=[
        "To find deployment: follow managed_by → ReplicaSet → managed_by → Deployment",
        "To find node: follow runs_on relationship",
        "To find services routing here: find Services where selector matches labels",
    ],
    common_queries=[
        "What node is this pod running on?",
        "What deployment owns this pod?",
        "What services route to this pod?",
        "Is this pod healthy?",
    ],
)

_DEPLOYMENT = EntityTypeDefinition(
    name="Deployment",
    scoped=True,
    scope_type="namespace",
    identity_fields=["namespace", "name"],
    volatility=Volatility.MODERATE,
    same_as=SameAsEligibility(
        can_match=["Deployment"],  # ArgoCD resource tree Deployment
        matching_attributes=["namespace", "name"],
    ),
    navigation_hints=[
        "Manages ReplicaSets - follow manages relationship",
        "To find pods: Deployment → manages → ReplicaSet → manages → Pod",
    ],
    common_queries=[
        "How many replicas are running?",
        "What pods belong to this deployment?",
        "Is this deployment healthy?",
    ],
)

_REPLICASET = EntityTypeDefinition(
    name="ReplicaSet",
    scoped=True,
    scope_type="namespace",
    identity_fields=["namespace", "name"],
    volatility=Volatility.MODERATE,
    navigation_hints=[
        "Managed by Deployment - find via managed_by relationship",
        "Manages Pods - follow manages relationship",
    ],
    common_queries=[
        "What deployment owns this replicaset?",
        "What pods belong to this replicaset?",
    ],
)

_STATEFULSET = EntityTypeDefinition(
    name="StatefulSet",
    scoped=True,
    scope_type="namespace",
    identity_fields=["namespace", "name"],
    volatility=Volatility.MODERATE,
    same_as=SameAsEligibility(
        can_match=["StatefulSet"],  # ArgoCD resource tree StatefulSet
        matching_attributes=["namespace", "name"],
    ),
    navigation_hints=[
        "Manages Pods with stable identities",
        "Pods are named {statefulset}-{ordinal}",
    ],
    common_queries=[
        "What pods belong to this statefulset?",
        "Is this statefulset healthy?",
    ],
)

_DAEMONSET = EntityTypeDefinition(
    name="DaemonSet",
    scoped=True,
    scope_type="namespace",
    identity_fields=["namespace", "name"],
    volatility=Volatility.MODERATE,
    same_as=SameAsEligibility(
        can_match=["DaemonSet"],  # ArgoCD resource tree DaemonSet
        matching_attributes=["namespace", "name"],
    ),
    navigation_hints=[
        "Runs one pod per matching node",
        "Manages Pods - follow manages relationship",
    ],
    common_queries=[
        "What pods belong to this daemonset?",
        "On which nodes is this daemonset running?",
    ],
)

_SERVICE = EntityTypeDefinition(
    name="Service",
    scoped=True,
    scope_type="namespace",
    identity_fields=["namespace", "name"],
    volatility=Volatility.MODERATE,
    same_as=SameAsEligibility(
        can_match=["Service"],  # ArgoCD resource tree Service
        matching_attributes=["namespace", "name"],
    ),
    navigation_hints=[
        "To find backend pods: follow routes_to relationship",
        "To find ingress: find Ingress that routes_to this service",
        "Service selector matches pod labels",
    ],
    common_queries=[
        "What pods does this service route to?",
        "What is the cluster IP of this service?",
        "What ingresses route to this service?",
    ],
)

_INGRESS = EntityTypeDefinition(
    name="Ingress",
    scoped=True,
    scope_type="namespace",
    identity_fields=["namespace", "name"],
    volatility=Volatility.MODERATE,
    same_as=SameAsEligibility(
        can_match=["Ingress"],  # ArgoCD resource tree Ingress
        matching_attributes=["namespace", "name"],
    ),
    navigation_hints=[
        "Routes external traffic to Services",
        "To find backend services: follow routes_to relationship",
    ],
    common_queries=[
        "What services does this ingress route to?",
        "What hostnames does this ingress handle?",
        "What is the ingress class?",
    ],
)


# =============================================================================
# Relationship Rules
# =============================================================================

# Containment relationships (namespaced resources belong to namespace)
_CONTAINMENT_RULES = {
    ("Pod", "member_of", "Namespace"): RelationshipRule(
        from_type="Pod",
        relationship_type="member_of",
        to_type="Namespace",
        required=True,
    ),
    ("Deployment", "member_of", "Namespace"): RelationshipRule(
        from_type="Deployment",
        relationship_type="member_of",
        to_type="Namespace",
        required=True,
    ),
    ("ReplicaSet", "member_of", "Namespace"): RelationshipRule(
        from_type="ReplicaSet",
        relationship_type="member_of",
        to_type="Namespace",
        required=True,
    ),
    ("StatefulSet", "member_of", "Namespace"): RelationshipRule(
        from_type="StatefulSet",
        relationship_type="member_of",
        to_type="Namespace",
        required=True,
    ),
    ("DaemonSet", "member_of", "Namespace"): RelationshipRule(
        from_type="DaemonSet",
        relationship_type="member_of",
        to_type="Namespace",
        required=True,
    ),
    ("Service", "member_of", "Namespace"): RelationshipRule(
        from_type="Service",
        relationship_type="member_of",
        to_type="Namespace",
        required=True,
    ),
    ("Ingress", "member_of", "Namespace"): RelationshipRule(
        from_type="Ingress",
        relationship_type="member_of",
        to_type="Namespace",
        required=True,
    ),
}

# Ownership hierarchy (parent → manages → child)
_OWNERSHIP_RULES = {
    ("Deployment", "manages", "ReplicaSet"): RelationshipRule(
        from_type="Deployment",
        relationship_type="manages",
        to_type="ReplicaSet",
        cardinality="one_to_many",
    ),
    ("ReplicaSet", "manages", "Pod"): RelationshipRule(
        from_type="ReplicaSet",
        relationship_type="manages",
        to_type="Pod",
        cardinality="one_to_many",
    ),
    ("StatefulSet", "manages", "Pod"): RelationshipRule(
        from_type="StatefulSet",
        relationship_type="manages",
        to_type="Pod",
        cardinality="one_to_many",
    ),
    ("DaemonSet", "manages", "Pod"): RelationshipRule(
        from_type="DaemonSet",
        relationship_type="manages",
        to_type="Pod",
        cardinality="one_to_many",
    ),
}

# Scheduling relationships
_SCHEDULING_RULES = {
    ("Pod", "runs_on", "Node"): RelationshipRule(
        from_type="Pod",
        relationship_type="runs_on",
        to_type="Node",
    ),
}

# Networking relationships
_NETWORKING_RULES = {
    ("Service", "routes_to", "Pod"): RelationshipRule(
        from_type="Service",
        relationship_type="routes_to",
        to_type="Pod",
        cardinality="one_to_many",
    ),
    ("Ingress", "routes_to", "Service"): RelationshipRule(
        from_type="Ingress",
        relationship_type="routes_to",
        to_type="Service",
        cardinality="one_to_many",
    ),
}


# =============================================================================
# Complete Kubernetes Schema
# =============================================================================

KUBERNETES_TOPOLOGY_SCHEMA = ConnectorTopologySchema(
    connector_type="kubernetes",
    entity_types={
        "Namespace": _NAMESPACE,
        "Node": _NODE,
        "Pod": _POD,
        "Deployment": _DEPLOYMENT,
        "ReplicaSet": _REPLICASET,
        "StatefulSet": _STATEFULSET,
        "DaemonSet": _DAEMONSET,
        "Service": _SERVICE,
        "Ingress": _INGRESS,
    },
    relationship_rules={
        **_CONTAINMENT_RULES,
        **_OWNERSHIP_RULES,
        **_SCHEDULING_RULES,
        **_NETWORKING_RULES,
    },
)
