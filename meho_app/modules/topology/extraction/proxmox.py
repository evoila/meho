# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Proxmox extraction schema for topology auto-discovery.

Defines declarative extraction rules for Proxmox VE resources.
These rules specify how to extract entities and relationships from
Proxmox connector operation results using JMESPath expressions.

Supported Entity Types:
    - VM: QEMU Virtual Machines with runs_on Node relationship
    - Container: LXC Containers with runs_on Node relationship
    - Node: Proxmox hosts (top-level, no outgoing relationships)
    - Storage: Storage pools with optional hosted_on Node relationship

Relationship Types:
    - runs_on: VM/Container → Node (which host runs this workload)
    - hosted_on: Storage → Node (optional, shared storage has no node)

Data Formats:
    Proxmox connector serializers return flat dictionaries with pre-computed values.
    Field paths match the serializer output format from:
    meho_app/modules/connectors/proxmox/serializers.py
"""

from .rules import (
    AttributeExtraction,
    ConnectorExtractionSchema,
    DescriptionTemplate,
    EntityExtractionRule,
    RelationshipExtraction,
)

# =============================================================================
# Proxmox Extraction Schema
# =============================================================================

PROXMOX_EXTRACTION_SCHEMA = ConnectorExtractionSchema(
    connector_type="proxmox",
    entity_rules=[
        # =====================================================================
        # VM (QEMU Virtual Machine) Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="VM",
            source_operations=["list_vms", "get_vm", "get_vm_status"],
            # Proxmox list operations return arrays directly (serialized)
            items_path=None,
            name_path="name",
            scope_paths={"node": "node"},
            description=DescriptionTemplate(
                template="Proxmox VM {name} (VMID: {vmid}), node {node}, {status}, {cpu_count} vCPU, {memory_mb}MB RAM",
                fallback="Proxmox VM",
            ),
            attributes=[
                AttributeExtraction(name="vmid", path="vmid"),
                AttributeExtraction(name="status", path="status"),
                AttributeExtraction(name="node", path="node"),
                AttributeExtraction(name="cpu_count", path="cpu_count", default=0),
                AttributeExtraction(name="cpu_usage_percent", path="cpu_usage_percent", default=0),
                AttributeExtraction(name="memory_mb", path="memory_mb", default=0),
                AttributeExtraction(name="memory_used_mb", path="memory_used_mb", default=0),
                AttributeExtraction(
                    name="memory_usage_percent", path="memory_usage_percent", default=0
                ),
                AttributeExtraction(name="disk_size_gb", path="disk_size_gb", default=0),
                AttributeExtraction(name="disk_used_gb", path="disk_used_gb", default=0),
                AttributeExtraction(name="uptime", path="uptime"),
                AttributeExtraction(name="uptime_seconds", path="uptime_seconds", default=0),
                AttributeExtraction(name="template", path="template", default=False),
                AttributeExtraction(name="tags", path="tags", default=[]),
                AttributeExtraction(name="network_in_bytes", path="network_in_bytes", default=0),
                AttributeExtraction(name="network_out_bytes", path="network_out_bytes", default=0),
            ],
            relationships=[
                # VM runs on a Node
                RelationshipExtraction(
                    relationship_type="runs_on",
                    target_type="Node",
                    target_path="node",
                    optional=False,
                ),
            ],
        ),
        # =====================================================================
        # Container (LXC) Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="Container",
            source_operations=["list_containers", "get_container", "get_container_status"],
            items_path=None,
            name_path="name",
            scope_paths={"node": "node"},
            description=DescriptionTemplate(
                template="Proxmox Container {name} (VMID: {vmid}), node {node}, {status}, {cpu_count} vCPU, {memory_mb}MB RAM",
                fallback="Proxmox Container",
            ),
            attributes=[
                AttributeExtraction(name="vmid", path="vmid"),
                AttributeExtraction(name="status", path="status"),
                AttributeExtraction(name="node", path="node"),
                AttributeExtraction(name="type", path="type", default="lxc"),
                AttributeExtraction(name="cpu_count", path="cpu_count", default=0),
                AttributeExtraction(name="cpu_usage_percent", path="cpu_usage_percent", default=0),
                AttributeExtraction(name="memory_mb", path="memory_mb", default=0),
                AttributeExtraction(name="memory_used_mb", path="memory_used_mb", default=0),
                AttributeExtraction(
                    name="memory_usage_percent", path="memory_usage_percent", default=0
                ),
                AttributeExtraction(name="disk_size_gb", path="disk_size_gb", default=0),
                AttributeExtraction(name="disk_used_gb", path="disk_used_gb", default=0),
                AttributeExtraction(name="swap_mb", path="swap_mb", default=0),
                AttributeExtraction(name="swap_used_mb", path="swap_used_mb", default=0),
                AttributeExtraction(name="uptime", path="uptime"),
                AttributeExtraction(name="uptime_seconds", path="uptime_seconds", default=0),
                AttributeExtraction(name="template", path="template", default=False),
                AttributeExtraction(name="tags", path="tags", default=[]),
                AttributeExtraction(name="network_in_bytes", path="network_in_bytes", default=0),
                AttributeExtraction(name="network_out_bytes", path="network_out_bytes", default=0),
            ],
            relationships=[
                # Container runs on a Node
                RelationshipExtraction(
                    relationship_type="runs_on",
                    target_type="Node",
                    target_path="node",
                    optional=False,
                ),
            ],
        ),
        # =====================================================================
        # Node (Proxmox Host) Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="Node",
            source_operations=["list_nodes", "get_node", "get_node_status"],
            items_path=None,
            name_path="name",
            scope_paths={},  # Nodes are top-level (cluster-scoped)
            description=DescriptionTemplate(
                template="Proxmox Node {name}, {status}, CPU: {cpu_usage_percent}%, MEM: {memory_usage_percent}%",
                fallback="Proxmox Node",
            ),
            attributes=[
                AttributeExtraction(name="status", path="status"),
                AttributeExtraction(name="uptime", path="uptime"),
                AttributeExtraction(name="uptime_seconds", path="uptime_seconds", default=0),
                AttributeExtraction(name="cpu_usage_percent", path="cpu_usage_percent", default=0),
                AttributeExtraction(name="memory_used_mb", path="memory_used_mb", default=0),
                AttributeExtraction(name="memory_total_mb", path="memory_total_mb", default=0),
                AttributeExtraction(
                    name="memory_usage_percent", path="memory_usage_percent", default=0
                ),
                AttributeExtraction(name="disk_used_gb", path="disk_used_gb", default=0),
                AttributeExtraction(name="disk_total_gb", path="disk_total_gb", default=0),
                AttributeExtraction(
                    name="disk_usage_percent", path="disk_usage_percent", default=0
                ),
                AttributeExtraction(name="kernel_version", path="kernel_version"),
                AttributeExtraction(name="pve_version", path="pve_version"),
            ],
            relationships=[],  # Nodes are top-level, no outgoing relationships
        ),
        # =====================================================================
        # Storage (Storage Pool) Extraction
        # =====================================================================
        EntityExtractionRule(
            entity_type="Storage",
            source_operations=["list_storage", "get_storage", "get_storage_status"],
            items_path=None,
            # Storage uses "storage" field as name, not "name"
            name_path="storage",
            scope_paths={},  # Storage can be shared (no node) or node-specific
            description=DescriptionTemplate(
                template="Proxmox Storage {storage}, type: {type}, {used_gb}/{total_gb}GB used ({usage_percent}%)",
                fallback="Proxmox Storage",
            ),
            attributes=[
                AttributeExtraction(name="type", path="type"),
                AttributeExtraction(name="content", path="content", default=[]),
                AttributeExtraction(name="total_gb", path="total_gb", default=0),
                AttributeExtraction(name="used_gb", path="used_gb", default=0),
                AttributeExtraction(name="available_gb", path="available_gb", default=0),
                AttributeExtraction(name="usage_percent", path="usage_percent", default=0),
                AttributeExtraction(name="enabled", path="enabled", default=True),
                AttributeExtraction(name="active", path="active", default=False),
                AttributeExtraction(name="shared", path="shared", default=False),
            ],
            relationships=[],  # Storage relationships to nodes are complex (shared vs local)
            # Note: hosted_on Node relationship omitted because:
            # 1. Shared storage doesn't have a single host node
            # 2. list_storage without node param returns cluster-wide config without node info
            # 3. Storage-to-Node relationship would require additional context
        ),
    ],
)
