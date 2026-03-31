# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Proxmox VE topology schema definition.

Defines entity types and valid relationships for Proxmox VE resources.
Based on the existing ProxmoxExtractor in auto_discovery/proxmox.py.

Entity Types:
- Node (Proxmox VE cluster node)
- VM (KVM virtual machine)
- Container (LXC container)
- Storage (storage pool)

Relationship Hierarchy:
- Compute: VM → runs_on → Node, Container → runs_on → Node
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

_NODE = EntityTypeDefinition(
    name="Node",
    scoped=False,  # Cluster-scoped
    identity_fields=["name"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "Proxmox VE cluster node",
        "Runs VMs and Containers - find via runs_on relationship (reverse)",
    ],
    common_queries=[
        "What VMs are running on this node?",
        "What containers are running on this node?",
        "What is the CPU and memory usage?",
        "Is this node online?",
    ],
)

_VM = EntityTypeDefinition(
    name="VM",
    scoped=True,
    scope_type="node",
    identity_fields=["node", "vmid"],  # Proxmox uses vmid as unique identifier
    volatility=Volatility.MODERATE,
    # SAME_AS: Proxmox VM can host a K8s Node or be equivalent to GCP Instance
    same_as=SameAsEligibility(
        can_match=["Node", "Instance"],  # K8s Node, GCP Instance
        matching_attributes=[
            "name",
            "vmid",
            # IP would come from QEMU guest agent or manual configuration
        ],
    ),
    navigation_hints=[
        "KVM virtual machine",
        "To find node: follow runs_on relationship",
    ],
    common_queries=[
        "What node is this VM running on?",
        "What is the power state?",
        "What is the CPU and memory configuration?",
    ],
)

_CONTAINER = EntityTypeDefinition(
    name="Container",
    scoped=True,
    scope_type="node",
    identity_fields=["node", "vmid"],  # LXC containers also use vmid
    volatility=Volatility.MODERATE,
    # SAME_AS: LXC container could be running a K8s Node
    same_as=SameAsEligibility(
        can_match=["Node"],  # K8s Node running inside LXC
        matching_attributes=[
            "name",
            "vmid",
        ],
    ),
    navigation_hints=[
        "LXC container",
        "To find node: follow runs_on relationship",
    ],
    common_queries=[
        "What node is this container running on?",
        "What is the status?",
        "What is the resource allocation?",
    ],
)

_STORAGE = EntityTypeDefinition(
    name="Storage",
    scoped=False,  # Storage is cluster-wide
    identity_fields=["name"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "Storage pool for VMs and containers",
        "Can be shared across nodes",
    ],
    common_queries=[
        "How much space is available?",
        "What is the storage type?",
        "What content types are stored here?",
    ],
)


# =============================================================================
# Relationship Rules
# =============================================================================

# Compute relationships
_COMPUTE_RULES = {
    ("VM", "runs_on", "Node"): RelationshipRule(
        from_type="VM",
        relationship_type="runs_on",
        to_type="Node",
    ),
    ("Container", "runs_on", "Node"): RelationshipRule(
        from_type="Container",
        relationship_type="runs_on",
        to_type="Node",
    ),
}


# =============================================================================
# Complete Proxmox Schema
# =============================================================================

PROXMOX_TOPOLOGY_SCHEMA = ConnectorTopologySchema(
    connector_type="proxmox",
    entity_types={
        "Node": _NODE,
        "VM": _VM,
        "Container": _CONTAINER,
        "Storage": _STORAGE,
    },
    relationship_rules={
        **_COMPUTE_RULES,
    },
)
