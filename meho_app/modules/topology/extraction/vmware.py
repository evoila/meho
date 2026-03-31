# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
VMware extraction schema for topology auto-discovery.

Defines declarative extraction rules for VMware vSphere resources.
These rules specify how to extract entities and relationships from
VMware connector operation results using JMESPath expressions.

Supported Entity Types:
    - VM: Virtual machines with runs_on, uses relationships
    - Host: ESXi hosts with member_of relationship
    - Cluster: vSphere clusters (no outgoing relationships)
    - Datastore: Storage datastores (no outgoing relationships)

Relationship Types:
    - runs_on: VM → Host (where VM is running)
    - uses: VM → Datastore (storage consumption)
    - member_of: Host → Cluster (cluster membership)

Data Formats:
    VMware connector returns serialized data from pyvmomi objects.
    Field paths match the VMware serializer output format:
    - config.num_cpu, config.memory_mb, config.guest_os
    - runtime.power_state, runtime.host
    - summary.hardware.*, summary.quickStats.*
"""

from .rules import (
    AttributeExtraction,
    ConnectorExtractionSchema,
    DescriptionTemplate,
    EntityExtractionRule,
    RelationshipExtraction,
)

# =============================================================================
# VMware Extraction Schema
# =============================================================================

VMWARE_EXTRACTION_SCHEMA = ConnectorExtractionSchema(
    connector_type="vmware",
    entity_rules=[
        # =====================================================================
        # VM (Virtual Machine) Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="VM",
            source_operations=["list_virtual_machines", "get_virtual_machine"],
            # VMware list operations return arrays directly
            items_path=None,
            name_path="name",
            scope_paths={},  # VMs use name for identity
            description=DescriptionTemplate(
                template="VMware VM {name}, {config.num_cpu} vCPU, {config.memory_mb}MB RAM, {runtime.power_state}",
                fallback="VMware VM",
            ),
            attributes=[
                AttributeExtraction(name="moref", path="moref"),
                AttributeExtraction(name="power_state", path="runtime.power_state"),
                AttributeExtraction(name="num_cpu", path="config.num_cpu"),
                AttributeExtraction(name="memory_mb", path="config.memory_mb"),
                AttributeExtraction(name="guest_os", path="config.guest_os"),
                AttributeExtraction(name="guest_full_name", path="config.guest_full_name"),
                AttributeExtraction(name="ip_address", path="guest.ip_address"),
                AttributeExtraction(name="hostname", path="guest.hostname"),
                AttributeExtraction(name="tools_status", path="guest.tools_status"),
                AttributeExtraction(name="datastores", path="datastores", default=[]),
            ],
            relationships=[
                # VM runs on Host
                RelationshipExtraction(
                    relationship_type="runs_on",
                    target_type="Host",
                    target_path="runtime.host",
                    optional=True,
                ),
                # VM uses Datastores
                RelationshipExtraction(
                    relationship_type="uses",
                    target_type="Datastore",
                    target_path="datastores",
                    multiple=True,
                    optional=True,
                ),
            ],
        ),
        # =====================================================================
        # Host (ESXi Host) Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="Host",
            source_operations=["list_hosts", "get_host"],
            items_path=None,
            name_path="name",
            scope_paths={"cluster": "cluster"},
            description=DescriptionTemplate(
                template="VMware ESXi Host {name}, {summary.hardware.num_cpu_cores} cores, {runtime.connection_state}",
                fallback="VMware ESXi Host",
            ),
            attributes=[
                AttributeExtraction(name="moref", path="moref"),
                AttributeExtraction(name="connection_state", path="runtime.connection_state"),
                AttributeExtraction(name="power_state", path="runtime.power_state"),
                AttributeExtraction(name="in_maintenance_mode", path="runtime.in_maintenance_mode"),
                AttributeExtraction(name="vendor", path="summary.hardware.vendor"),
                AttributeExtraction(name="model", path="summary.hardware.model"),
                AttributeExtraction(name="num_cpu_cores", path="summary.hardware.num_cpu_cores"),
                AttributeExtraction(name="memory_size", path="summary.hardware.memory_size"),
                AttributeExtraction(name="cpu_usage", path="summary.quickStats.overall_cpu_usage"),
                AttributeExtraction(
                    name="memory_usage", path="summary.quickStats.overall_memory_usage"
                ),
            ],
            relationships=[
                # Host is member of Cluster
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="Cluster",
                    target_path="cluster",
                    optional=True,
                ),
                # Also try parent field for cluster membership
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="Cluster",
                    target_path="parent",
                    optional=True,
                ),
            ],
        ),
        # =====================================================================
        # Cluster Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="Cluster",
            source_operations=["list_clusters", "get_cluster"],
            items_path=None,
            name_path="name",
            scope_paths={"datacenter": "datacenter"},
            description=DescriptionTemplate(
                template="VMware Cluster {name}, HA: {ha_enabled}, DRS: {drs_enabled}, {host_count} hosts",
                fallback="VMware Cluster",
            ),
            attributes=[
                AttributeExtraction(name="moref", path="moref"),
                AttributeExtraction(name="ha_enabled", path="ha_enabled"),
                AttributeExtraction(name="drs_enabled", path="drs_enabled"),
                AttributeExtraction(name="host_count", path="host_count"),
                AttributeExtraction(name="overall_status", path="overall_status"),
                # Alternative paths for nested configuration
                AttributeExtraction(
                    name="das_enabled",
                    path="configuration.das_config.enabled",
                ),
                AttributeExtraction(
                    name="drs_behavior",
                    path="configuration.drs_config.default_vm_behavior",
                ),
            ],
            relationships=[],  # Clusters don't have outgoing relationships in this schema
        ),
        # =====================================================================
        # Datastore Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="Datastore",
            source_operations=["list_datastores", "get_datastore"],
            items_path=None,
            name_path="name",
            scope_paths={"datacenter": "datacenter"},
            description=DescriptionTemplate(
                template="VMware Datastore {name}, type: {type}, {capacity_gb}GB capacity, {free_space_gb}GB free",
                fallback="VMware Datastore",
            ),
            attributes=[
                AttributeExtraction(name="moref", path="moref"),
                AttributeExtraction(name="type", path="type"),
                AttributeExtraction(name="capacity_gb", path="capacity_gb"),
                AttributeExtraction(name="free_space_gb", path="free_space_gb"),
                AttributeExtraction(name="accessible", path="accessible", default=True),
                # Alternative paths for summary data
                AttributeExtraction(name="capacity", path="summary.capacity"),
                AttributeExtraction(name="free_space", path="summary.free_space"),
                AttributeExtraction(name="summary_type", path="summary.type"),
            ],
            relationships=[],  # Datastores don't have outgoing relationships
        ),
    ],
)
