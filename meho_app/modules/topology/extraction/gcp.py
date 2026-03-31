# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GCP extraction schema for topology auto-discovery.

Defines declarative extraction rules for Google Cloud Platform resources.
These rules specify how to extract entities and relationships from
GCP connector operation results using JMESPath expressions.

Supported Entity Types:
    - Instance: Compute Engine VMs with runs_in, uses, member_of relationships
    - Disk: Persistent Disks with runs_in relationship
    - Network: VPC Networks (top-level, no outgoing relationships)
    - Subnet: Subnetworks with member_of relationship
    - Firewall: Firewall Rules with applies_to relationship
    - GKECluster: GKE Clusters with member_of relationship

Relationship Types:
    - runs_in: Instance/Disk → Zone (location)
    - uses: Instance → Disk (storage attachment)
    - member_of: Instance → Network, Subnet → Network, GKECluster → Network
    - applies_to: Firewall → Network

Data Formats:
    GCP connector serializers return flat dictionaries with pre-extracted names.
    Field paths match the serializer output format from:
    meho_app/modules/connectors/gcp/serializers.py
"""

from .rules import (
    AttributeExtraction,
    ConnectorExtractionSchema,
    DescriptionTemplate,
    EntityExtractionRule,
    RelationshipExtraction,
)

# =============================================================================
# GCP Extraction Schema
# =============================================================================

GCP_EXTRACTION_SCHEMA = ConnectorExtractionSchema(
    connector_type="gcp",
    entity_rules=[
        # =====================================================================
        # Instance (Compute Engine VM) Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="Instance",
            source_operations=["list_instances", "get_instance"],
            # GCP list operations return arrays directly
            items_path=None,
            name_path="name",
            scope_paths={"zone": "zone"},
            description=DescriptionTemplate(
                template="GCP Instance {name}, zone {zone}, {machine_type}, {status}",
                fallback="GCP Instance",
            ),
            attributes=[
                AttributeExtraction(name="id", path="id"),
                AttributeExtraction(name="machine_type", path="machine_type"),
                AttributeExtraction(name="status", path="status"),
                AttributeExtraction(name="status_message", path="status_message"),
                AttributeExtraction(name="zone", path="zone"),
                AttributeExtraction(name="cpu_platform", path="cpu_platform"),
                AttributeExtraction(name="creation_timestamp", path="creation_timestamp"),
                AttributeExtraction(name="labels", path="labels", default={}),
                AttributeExtraction(
                    name="network_interfaces", path="network_interfaces", default=[]
                ),
                AttributeExtraction(name="disks", path="disks", default=[]),
                AttributeExtraction(name="can_ip_forward", path="can_ip_forward"),
                AttributeExtraction(name="deletion_protection", path="deletion_protection"),
                AttributeExtraction(name="self_link", path="self_link"),
            ],
            relationships=[
                # Instance uses Disks (storage attachment)
                RelationshipExtraction(
                    relationship_type="uses",
                    target_type="Disk",
                    target_path="disks[*].name",
                    multiple=True,
                    optional=True,
                ),
                # Instance is member of Network (via first network interface)
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="Network",
                    target_path="network_interfaces[0].network",
                    optional=True,
                ),
            ],
        ),
        # =====================================================================
        # Disk (Persistent Disk) Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="Disk",
            source_operations=["list_disks", "get_disk"],
            items_path=None,
            name_path="name",
            scope_paths={"zone": "zone"},
            description=DescriptionTemplate(
                template="GCP Disk {name}, zone {zone}, {type}, {size_gb}GB, {status}",
                fallback="GCP Disk",
            ),
            attributes=[
                AttributeExtraction(name="id", path="id"),
                AttributeExtraction(name="size_gb", path="size_gb"),
                AttributeExtraction(name="size_formatted", path="size_formatted"),
                AttributeExtraction(name="type", path="type"),
                AttributeExtraction(name="status", path="status"),
                AttributeExtraction(name="zone", path="zone"),
                AttributeExtraction(name="source_image", path="source_image"),
                AttributeExtraction(name="source_snapshot", path="source_snapshot"),
                AttributeExtraction(name="users", path="users", default=[]),
                AttributeExtraction(name="labels", path="labels", default={}),
                AttributeExtraction(name="creation_timestamp", path="creation_timestamp"),
                AttributeExtraction(name="self_link", path="self_link"),
            ],
            relationships=[],  # Disks don't have outgoing relationships
        ),
        # =====================================================================
        # Network (VPC Network) Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="Network",
            source_operations=["list_networks", "get_network"],
            items_path=None,
            name_path="name",
            scope_paths={},  # Networks are global (project-scoped)
            description=DescriptionTemplate(
                template="GCP Network {name}, routing: {routing_mode}, auto-create: {auto_create_subnetworks}",
                fallback="GCP Network",
            ),
            attributes=[
                AttributeExtraction(name="id", path="id"),
                AttributeExtraction(name="auto_create_subnetworks", path="auto_create_subnetworks"),
                AttributeExtraction(name="routing_mode", path="routing_mode"),
                AttributeExtraction(name="mtu", path="mtu"),
                AttributeExtraction(name="subnetworks", path="subnetworks", default=[]),
                AttributeExtraction(name="peerings", path="peerings", default=[]),
                AttributeExtraction(name="creation_timestamp", path="creation_timestamp"),
                AttributeExtraction(name="self_link", path="self_link"),
            ],
            relationships=[],  # Networks are top-level, no outgoing relationships
        ),
        # =====================================================================
        # Subnet (Subnetwork) Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="Subnet",
            source_operations=["list_subnetworks", "get_subnetwork"],
            items_path=None,
            name_path="name",
            scope_paths={"region": "region"},
            description=DescriptionTemplate(
                template="GCP Subnet {name}, region {region}, {ip_cidr_range}, network: {network}",
                fallback="GCP Subnet",
            ),
            attributes=[
                AttributeExtraction(name="id", path="id"),
                AttributeExtraction(name="network", path="network"),
                AttributeExtraction(name="region", path="region"),
                AttributeExtraction(name="ip_cidr_range", path="ip_cidr_range"),
                AttributeExtraction(name="gateway_address", path="gateway_address"),
                AttributeExtraction(
                    name="private_ip_google_access", path="private_ip_google_access"
                ),
                AttributeExtraction(name="purpose", path="purpose"),
                AttributeExtraction(name="state", path="state"),
                AttributeExtraction(
                    name="secondary_ip_ranges", path="secondary_ip_ranges", default=[]
                ),
                AttributeExtraction(name="creation_timestamp", path="creation_timestamp"),
                AttributeExtraction(name="self_link", path="self_link"),
            ],
            relationships=[
                # Subnet is member of Network
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="Network",
                    target_path="network",
                    optional=False,
                ),
            ],
        ),
        # =====================================================================
        # Firewall (Firewall Rule) Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="Firewall",
            source_operations=["list_firewalls", "get_firewall"],
            items_path=None,
            name_path="name",
            scope_paths={},  # Firewalls are global (project-scoped)
            description=DescriptionTemplate(
                template="GCP Firewall {name}, {direction}, priority {priority}, network: {network}",
                fallback="GCP Firewall",
            ),
            attributes=[
                AttributeExtraction(name="id", path="id"),
                AttributeExtraction(name="network", path="network"),
                AttributeExtraction(name="priority", path="priority"),
                AttributeExtraction(name="direction", path="direction"),
                AttributeExtraction(name="disabled", path="disabled"),
                AttributeExtraction(name="source_ranges", path="source_ranges", default=[]),
                AttributeExtraction(
                    name="destination_ranges", path="destination_ranges", default=[]
                ),
                AttributeExtraction(name="source_tags", path="source_tags", default=[]),
                AttributeExtraction(name="target_tags", path="target_tags", default=[]),
                AttributeExtraction(name="allowed", path="allowed", default=[]),
                AttributeExtraction(name="denied", path="denied", default=[]),
                AttributeExtraction(name="log_config", path="log_config"),
                AttributeExtraction(name="creation_timestamp", path="creation_timestamp"),
                AttributeExtraction(name="self_link", path="self_link"),
            ],
            relationships=[
                # Firewall applies to Network
                RelationshipExtraction(
                    relationship_type="applies_to",
                    target_type="Network",
                    target_path="network",
                    optional=False,
                ),
            ],
        ),
        # =====================================================================
        # GKECluster (GKE Cluster) Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="GKECluster",
            source_operations=["list_clusters", "get_cluster"],
            items_path=None,
            name_path="name",
            scope_paths={"location": "location"},
            description=DescriptionTemplate(
                template="GKE Cluster {name}, location {location}, {status}, {current_node_count} nodes",
                fallback="GKE Cluster",
            ),
            attributes=[
                AttributeExtraction(name="location", path="location"),
                AttributeExtraction(name="status", path="status"),
                AttributeExtraction(name="status_message", path="status_message"),
                AttributeExtraction(name="current_master_version", path="current_master_version"),
                AttributeExtraction(name="current_node_version", path="current_node_version"),
                AttributeExtraction(name="current_node_count", path="current_node_count"),
                AttributeExtraction(name="endpoint", path="endpoint"),
                AttributeExtraction(name="initial_cluster_version", path="initial_cluster_version"),
                AttributeExtraction(name="node_pools", path="node_pools", default=[]),
                AttributeExtraction(name="network", path="network"),
                AttributeExtraction(name="subnetwork", path="subnetwork"),
                AttributeExtraction(name="cluster_ipv4_cidr", path="cluster_ipv4_cidr"),
                AttributeExtraction(name="services_ipv4_cidr", path="services_ipv4_cidr"),
                AttributeExtraction(name="labels", path="labels", default={}),
                AttributeExtraction(name="create_time", path="create_time"),
                AttributeExtraction(name="self_link", path="self_link"),
            ],
            relationships=[
                # GKECluster is member of Network
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="Network",
                    target_path="network",
                    optional=True,
                ),
            ],
        ),
    ],
)
