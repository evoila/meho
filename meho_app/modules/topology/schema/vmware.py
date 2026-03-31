# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
VMware vSphere topology schema definition.

Defines entity types and valid relationships for VMware vSphere resources.
Based on the existing VMwareExtractor in auto_discovery/vmware.py.

Entity Types:
- Datacenter (top-level container)
- Cluster (compute cluster within datacenter)
- Host (ESXi host within cluster)
- VM (virtual machine)
- Datastore (storage)
- Network (virtual network)

Relationship Hierarchy:
- Hierarchy: Cluster → member_of → Datacenter, Host → member_of → Cluster
- Compute: VM → runs_on → Host
- Storage: VM → uses_storage → Datastore, Host → uses_storage → Datastore
- Networking: VM → connected_to → Network, Host → connected_to → Network
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

_DATACENTER = EntityTypeDefinition(
    name="Datacenter",
    scoped=False,  # Top-level container
    identity_fields=["name"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "Top-level container for vSphere inventory",
        "Contains Clusters, Datastores, and Networks",
    ],
    common_queries=[
        "What clusters are in this datacenter?",
        "What datastores are available in this datacenter?",
    ],
)

_CLUSTER = EntityTypeDefinition(
    name="Cluster",
    scoped=True,
    scope_type="datacenter",
    identity_fields=["datacenter", "name"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "Contains ESXi Hosts",
        "DRS and HA are configured at cluster level",
    ],
    common_queries=[
        "What hosts are in this cluster?",
        "Is DRS enabled on this cluster?",
        "Is HA enabled on this cluster?",
    ],
)

_HOST = EntityTypeDefinition(
    name="Host",
    scoped=True,
    scope_type="cluster",
    identity_fields=["cluster", "name"],
    volatility=Volatility.STABLE,
    # SAME_AS: ESXi Host could be a bare-metal K8s node
    same_as=SameAsEligibility(
        can_match=["Node"],  # K8s Node running directly on ESXi host
        matching_attributes=[
            "name",
            "config.network.dnsConfig.hostName",
        ],
    ),
    navigation_hints=[
        "Runs VMs - find via runs_on relationship (reverse)",
        "Member of a Cluster",
        "Uses Datastores",
    ],
    common_queries=[
        "What VMs are running on this host?",
        "Is this host in maintenance mode?",
        "What is the CPU and memory usage?",
    ],
)

_VM = EntityTypeDefinition(
    name="VM",
    scoped=False,  # moref is globally unique
    identity_fields=["moref"],  # VMware moref is globally unique within vCenter
    volatility=Volatility.MODERATE,
    # SAME_AS: VMware VM is often the underlying machine for K8s Node or GCP Instance
    same_as=SameAsEligibility(
        can_match=["Node", "Instance"],  # K8s Node, GCP Instance
        matching_attributes=[
            "guest.hostName",
            "guest.ipAddress",
            "name",
            "config.uuid",
        ],
    ),
    navigation_hints=[
        "To find host: follow runs_on relationship",
        "To find datastores: follow uses_storage relationship",
        "VMs can move between hosts (vMotion)",
    ],
    common_queries=[
        "What host is this VM running on?",
        "What datastores is this VM using?",
        "What is the power state of this VM?",
        "What networks is this VM connected to?",
    ],
)

_DATASTORE = EntityTypeDefinition(
    name="Datastore",
    scoped=True,
    scope_type="datacenter",
    identity_fields=["datacenter", "name"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "Shared storage for VMs",
        "Find VMs using this datastore via uses_storage relationship (reverse)",
    ],
    common_queries=[
        "What VMs are stored on this datastore?",
        "How much free space is available?",
        "What is the datastore type (VMFS, NFS, vSAN)?",
    ],
)

_NETWORK = EntityTypeDefinition(
    name="Network",
    scoped=True,
    scope_type="datacenter",
    identity_fields=["datacenter", "name"],
    volatility=Volatility.STABLE,
    navigation_hints=[
        "Virtual network for VMs and Hosts",
        "Find VMs connected via connected_to relationship (reverse)",
    ],
    common_queries=[
        "What VMs are connected to this network?",
        "What is the VLAN ID?",
    ],
)


# =============================================================================
# Relationship Rules
# =============================================================================

# Hierarchy relationships
_HIERARCHY_RULES = {
    ("Cluster", "member_of", "Datacenter"): RelationshipRule(
        from_type="Cluster",
        relationship_type="member_of",
        to_type="Datacenter",
    ),
    ("Host", "member_of", "Cluster"): RelationshipRule(
        from_type="Host",
        relationship_type="member_of",
        to_type="Cluster",
        required=True,
    ),
}

# Compute relationships
_COMPUTE_RULES = {
    ("VM", "runs_on", "Host"): RelationshipRule(
        from_type="VM",
        relationship_type="runs_on",
        to_type="Host",
    ),
}

# Storage relationships
_STORAGE_RULES = {
    ("VM", "uses_storage", "Datastore"): RelationshipRule(
        from_type="VM",
        relationship_type="uses_storage",
        to_type="Datastore",
        cardinality="many_to_many",
    ),
    ("Host", "uses_storage", "Datastore"): RelationshipRule(
        from_type="Host",
        relationship_type="uses_storage",
        to_type="Datastore",
        cardinality="many_to_many",
    ),
}

# Network relationships
_NETWORK_RULES = {
    ("VM", "connected_to", "Network"): RelationshipRule(
        from_type="VM",
        relationship_type="connected_to",
        to_type="Network",
        cardinality="many_to_many",
    ),
    ("Host", "connected_to", "Network"): RelationshipRule(
        from_type="Host",
        relationship_type="connected_to",
        to_type="Network",
        cardinality="many_to_many",
    ),
}

# Also allow "uses" relationship (used by some extractors)
_USES_RULES = {
    ("VM", "uses", "Datastore"): RelationshipRule(
        from_type="VM",
        relationship_type="uses",
        to_type="Datastore",
        cardinality="many_to_many",
    ),
}


# =============================================================================
# Complete VMware Schema
# =============================================================================

VMWARE_TOPOLOGY_SCHEMA = ConnectorTopologySchema(
    connector_type="vmware",
    entity_types={
        "Datacenter": _DATACENTER,
        "Cluster": _CLUSTER,
        "Host": _HOST,
        "VM": _VM,
        "Datastore": _DATASTORE,
        "Network": _NETWORK,
    },
    relationship_rules={
        **_HIERARCHY_RULES,
        **_COMPUTE_RULES,
        **_STORAGE_RULES,
        **_NETWORK_RULES,
        **_USES_RULES,
    },
)
