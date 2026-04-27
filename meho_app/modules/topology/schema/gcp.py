# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GCP (Google Cloud Platform) topology schema definition.

Defines entity types and valid relationships for GCP resources.
Based on the existing GCPExtractor in auto_discovery/gcp.py.

Entity Types:
- Network (VPC network, project-scoped)
- Subnet (subnetwork, region-scoped)
- Instance (Compute Engine VM, zone-scoped)
- Disk (persistent disk, zone-scoped)
- GKECluster (GKE cluster, location-scoped)
- NodePool (GKE node pool, cluster-scoped)
- Snapshot (disk snapshot, project-scoped)

Relationship Hierarchy:
- Networking: Instance → member_of → Network, Subnet → member_of → Network
- Storage: Instance → uses → Disk
- GKE: NodePool → member_of → GKECluster, GKECluster → member_of → Network
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

_NETWORK = EntityTypeDefinition(
    name="Network",
    scoped=True,
    scope_type="project",
    identity_fields=["project", "name"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "VPC network containing subnets and instances",
        "Find instances via member_of relationship (reverse)",
    ],
    common_queries=[
        "What subnets are in this network?",
        "What instances are connected to this network?",
    ],
)

_SUBNET = EntityTypeDefinition(
    name="Subnet",
    scoped=True,
    scope_type="region",
    identity_fields=["region", "name"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "Subnet within a VPC network",
        "Find parent network via member_of relationship",
    ],
    common_queries=[
        "What is the IP range of this subnet?",
        "What network does this subnet belong to?",
    ],
)

_INSTANCE = EntityTypeDefinition(
    name="Instance",
    scoped=True,
    scope_type="zone",
    identity_fields=["zone", "name"],
    volatility=Volatility.MODERATE,
    # SAME_AS: GCP Instance is the underlying machine for K8s Node or equivalent to VMware VM
    same_as=SameAsEligibility(
        can_match=["Node", "VM", "Host"],  # K8s Node, VMware VM, ESXi Host
        matching_attributes=[
            "name",
            "networkInterfaces[0].networkIP",
            "selfLink",  # Contains project/zone/name
        ],
    ),
    navigation_hints=[
        "Compute Engine VM instance",
        "Find disks via uses relationship",
        "Find network via member_of relationship",
    ],
    common_queries=[
        "What zone is this instance in?",
        "What disks are attached to this instance?",
        "What network is this instance on?",
        "What is the machine type?",
    ],
)

_DISK = EntityTypeDefinition(
    name="Disk",
    scoped=True,
    scope_type="zone",
    identity_fields=["zone", "name"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "Persistent disk storage",
        "Find attached instances via uses relationship (reverse)",
    ],
    common_queries=[
        "What instances are using this disk?",
        "What is the disk size?",
        "What is the disk type (SSD, standard)?",
    ],
)

_GKE_CLUSTER = EntityTypeDefinition(
    name="GKECluster",
    scoped=True,
    scope_type="location",
    identity_fields=["location", "name"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "GKE (Google Kubernetes Engine) cluster",
        "Contains NodePools",
        "Find NodePools via member_of relationship (reverse)",
    ],
    common_queries=[
        "What node pools are in this cluster?",
        "What is the Kubernetes version?",
        "How many nodes are in this cluster?",
    ],
)

_NODE_POOL = EntityTypeDefinition(
    name="NodePool",
    scoped=True,
    scope_type="cluster",
    identity_fields=["cluster", "name"],
    volatility=Volatility.MODERATE,
    navigation_hints=[
        "Node pool within a GKE cluster",
        "Find parent cluster via member_of relationship",
    ],
    common_queries=[
        "What cluster does this node pool belong to?",
        "What is the machine type?",
        "How many nodes are in this pool?",
        "Is autoscaling enabled?",
    ],
)

_SNAPSHOT = EntityTypeDefinition(
    name="Snapshot",
    scoped=True,
    scope_type="project",
    identity_fields=["project", "name"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "Disk snapshot for backup",
        "Created from a source disk",
    ],
    common_queries=[
        "What disk was this snapshot created from?",
        "What is the snapshot size?",
        "When was this snapshot created?",
    ],
)


# =============================================================================
# Relationship Rules
# =============================================================================

# Networking relationships
_NETWORKING_RULES = {
    ("Instance", "member_of", "Network"): RelationshipRule(
        from_type="Instance",
        relationship_type="member_of",
        to_type="Network",
    ),
    ("Subnet", "member_of", "Network"): RelationshipRule(
        from_type="Subnet",
        relationship_type="member_of",
        to_type="Network",
        required=True,
    ),
    ("GKECluster", "member_of", "Network"): RelationshipRule(
        from_type="GKECluster",
        relationship_type="member_of",
        to_type="Network",
    ),
}

# Storage relationships
_STORAGE_RULES = {
    ("Instance", "uses", "Disk"): RelationshipRule(
        from_type="Instance",
        relationship_type="uses",
        to_type="Disk",
        cardinality="one_to_many",
    ),
}

# GKE relationships
_GKE_RULES = {
    ("NodePool", "member_of", "GKECluster"): RelationshipRule(
        from_type="NodePool",
        relationship_type="member_of",
        to_type="GKECluster",
        required=True,
    ),
}


# =============================================================================
# Complete GCP Schema
# =============================================================================

GCP_TOPOLOGY_SCHEMA = ConnectorTopologySchema(
    connector_type="gcp",
    entity_types={
        "Network": _NETWORK,
        "Subnet": _SUBNET,
        "Instance": _INSTANCE,
        "Disk": _DISK,
        "GKECluster": _GKE_CLUSTER,
        "NodePool": _NODE_POOL,
        "Snapshot": _SNAPSHOT,
    },
    relationship_rules={
        **_NETWORKING_RULES,
        **_STORAGE_RULES,
        **_GKE_RULES,
    },
)
