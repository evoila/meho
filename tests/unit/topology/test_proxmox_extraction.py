# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for Proxmox topology extraction schema.

Tests the declarative extraction rules for Proxmox VE resources including:
- Schema structure and entity types
- Operation ID matching
- Relationship extraction paths
- Description template rendering
- Scope extraction (node)
"""

from meho_app.modules.topology.extraction import (
    PROXMOX_EXTRACTION_SCHEMA,
    get_extraction_schema,
    is_extraction_available,
)

# =============================================================================
# Proxmox Extraction Schema Structure Tests
# =============================================================================


class TestProxmoxExtractionSchemaStructure:
    """Tests for Proxmox extraction schema structure."""

    def test_schema_connector_type(self):
        """Test schema has correct connector type."""
        assert PROXMOX_EXTRACTION_SCHEMA.connector_type == "proxmox"

    def test_schema_registered(self):
        """Test Proxmox schema is registered in the registry."""
        assert is_extraction_available("proxmox") is True
        schema = get_extraction_schema("proxmox")
        assert schema is not None
        assert schema.connector_type == "proxmox"

    def test_all_entity_types_defined(self):
        """Test all expected entity types are defined."""
        expected_types = {"VM", "Container", "Node", "Storage"}
        actual_types = set(PROXMOX_EXTRACTION_SCHEMA.get_all_entity_types())
        assert expected_types == actual_types

    def test_all_operations_defined(self):
        """Test all expected operations are defined."""
        expected_ops = {
            "list_vms",
            "get_vm",
            "get_vm_status",
            "list_containers",
            "get_container",
            "get_container_status",
            "list_nodes",
            "get_node",
            "get_node_status",
            "list_storage",
            "get_storage",
            "get_storage_status",
        }
        actual_ops = set(PROXMOX_EXTRACTION_SCHEMA.get_all_operations())
        assert expected_ops == actual_ops


# =============================================================================
# VM Extraction Tests
# =============================================================================


class TestProxmoxVMExtraction:
    """Tests for Proxmox VM extraction rule."""

    def test_vm_rule_exists(self):
        """Test VM rule exists."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("VM")
        assert rule is not None
        assert rule.entity_type == "VM"

    def test_vm_matches_list_vms(self):
        """Test VM rule matches list_vms operation."""
        rules = PROXMOX_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="list_vms",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "VM"

    def test_vm_matches_get_vm(self):
        """Test VM rule matches get_vm operation."""
        rules = PROXMOX_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="get_vm",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "VM"

    def test_vm_matches_get_vm_status(self):
        """Test VM rule matches get_vm_status operation."""
        rules = PROXMOX_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="get_vm_status",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "VM"

    def test_vm_has_node_scope(self):
        """Test VM rule has node scope."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("VM")
        assert "node" in rule.scope_paths
        assert rule.scope_paths["node"] == "node"

    def test_vm_has_runs_on_relationship(self):
        """Test VM rule has runs_on Node relationship."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("VM")
        runs_on = [
            r
            for r in rule.relationships
            if r.relationship_type == "runs_on" and r.target_type == "Node"
        ]
        assert len(runs_on) == 1
        assert runs_on[0].optional is False

    def test_vm_description_rendering(self):
        """Test VM description template rendering."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("VM")

        vm_data = {
            "name": "web-server",
            "vmid": 100,
            "node": "pve1",
            "status": "running",
            "cpu_count": 4,
            "memory_mb": 8192,
        }

        description = rule.description.render(vm_data)
        assert "web-server" in description
        assert "100" in description
        assert "pve1" in description
        assert "running" in description

    def test_vm_attribute_extraction(self):
        """Test VM attribute extraction."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("VM")

        vm_data = {
            "vmid": 100,
            "name": "test-vm",
            "node": "pve1",
            "status": "running",
            "cpu_count": 2,
            "memory_mb": 4096,
            "template": False,
            "tags": ["prod", "web"],
        }

        for attr in rule.attributes:
            if attr.name == "vmid":
                assert attr.extract(vm_data) == 100
            elif attr.name == "status":
                assert attr.extract(vm_data) == "running"
            elif attr.name == "cpu_count":
                assert attr.extract(vm_data) == 2
            elif attr.name == "template":
                assert attr.extract(vm_data) is False
            elif attr.name == "tags":
                assert attr.extract(vm_data) == ["prod", "web"]

    def test_vm_relationship_extraction(self):
        """Test VM relationship target extraction."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("VM")

        vm_data = {
            "name": "test-vm",
            "node": "pve1",
        }

        for rel in rule.relationships:
            if rel.relationship_type == "runs_on":
                targets = rel.extract_targets(vm_data)
                assert targets == ["pve1"]


# =============================================================================
# Container Extraction Tests
# =============================================================================


class TestProxmoxContainerExtraction:
    """Tests for Proxmox Container extraction rule."""

    def test_container_rule_exists(self):
        """Test Container rule exists."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("Container")
        assert rule is not None
        assert rule.entity_type == "Container"

    def test_container_matches_list_containers(self):
        """Test Container rule matches list_containers operation."""
        rules = PROXMOX_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="list_containers",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "Container"

    def test_container_matches_get_container(self):
        """Test Container rule matches get_container operation."""
        rules = PROXMOX_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="get_container",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "Container"

    def test_container_matches_get_container_status(self):
        """Test Container rule matches get_container_status operation."""
        rules = PROXMOX_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="get_container_status",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "Container"

    def test_container_has_node_scope(self):
        """Test Container rule has node scope."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("Container")
        assert "node" in rule.scope_paths
        assert rule.scope_paths["node"] == "node"

    def test_container_has_runs_on_relationship(self):
        """Test Container rule has runs_on Node relationship."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("Container")
        runs_on = [
            r
            for r in rule.relationships
            if r.relationship_type == "runs_on" and r.target_type == "Node"
        ]
        assert len(runs_on) == 1
        assert runs_on[0].optional is False

    def test_container_description_rendering(self):
        """Test Container description template rendering."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("Container")

        ct_data = {
            "name": "nginx-ct",
            "vmid": 200,
            "node": "pve2",
            "status": "running",
            "cpu_count": 2,
            "memory_mb": 1024,
        }

        description = rule.description.render(ct_data)
        assert "nginx-ct" in description
        assert "200" in description
        assert "pve2" in description
        assert "running" in description

    def test_container_has_type_attribute(self):
        """Test Container has type attribute with lxc default."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("Container")

        type_attr = next((a for a in rule.attributes if a.name == "type"), None)
        assert type_attr is not None
        assert type_attr.default == "lxc"

    def test_container_has_swap_attributes(self):
        """Test Container has swap-specific attributes."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("Container")

        attr_names = {a.name for a in rule.attributes}
        assert "swap_mb" in attr_names
        assert "swap_used_mb" in attr_names

    def test_container_relationship_extraction(self):
        """Test Container relationship target extraction."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("Container")

        ct_data = {
            "name": "test-ct",
            "node": "pve2",
        }

        for rel in rule.relationships:
            if rel.relationship_type == "runs_on":
                targets = rel.extract_targets(ct_data)
                assert targets == ["pve2"]


# =============================================================================
# Node Extraction Tests
# =============================================================================


class TestProxmoxNodeExtraction:
    """Tests for Proxmox Node extraction rule."""

    def test_node_rule_exists(self):
        """Test Node rule exists."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("Node")
        assert rule is not None
        assert rule.entity_type == "Node"

    def test_node_matches_list_nodes(self):
        """Test Node rule matches list_nodes operation."""
        rules = PROXMOX_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="list_nodes",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "Node"

    def test_node_matches_get_node(self):
        """Test Node rule matches get_node operation."""
        rules = PROXMOX_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="get_node",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "Node"

    def test_node_matches_get_node_status(self):
        """Test Node rule matches get_node_status operation."""
        rules = PROXMOX_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="get_node_status",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "Node"

    def test_node_no_scope(self):
        """Test Node rule has no scope (cluster-scoped)."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("Node")
        assert rule.scope_paths == {}

    def test_node_no_outgoing_relationships(self):
        """Test Node has no outgoing relationships (top-level)."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("Node")
        assert len(rule.relationships) == 0

    def test_node_description_rendering(self):
        """Test Node description template rendering."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("Node")

        node_data = {
            "name": "pve1",
            "status": "online",
            "cpu_usage_percent": 45.5,
            "memory_usage_percent": 72.3,
        }

        description = rule.description.render(node_data)
        assert "pve1" in description
        assert "online" in description
        assert "45.5" in description or "CPU" in description

    def test_node_attribute_extraction(self):
        """Test Node attribute extraction."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("Node")

        node_data = {
            "name": "pve1",
            "status": "online",
            "uptime": "5 days",
            "cpu_usage_percent": 25.0,
            "memory_total_mb": 65536,
            "memory_used_mb": 32768,
            "pve_version": "8.0.3",
        }

        for attr in rule.attributes:
            if attr.name == "status":
                assert attr.extract(node_data) == "online"
            elif attr.name == "cpu_usage_percent":
                assert attr.extract(node_data) == 25.0
            elif attr.name == "memory_total_mb":
                assert attr.extract(node_data) == 65536
            elif attr.name == "pve_version":
                assert attr.extract(node_data) == "8.0.3"


# =============================================================================
# Storage Extraction Tests
# =============================================================================


class TestProxmoxStorageExtraction:
    """Tests for Proxmox Storage extraction rule."""

    def test_storage_rule_exists(self):
        """Test Storage rule exists."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("Storage")
        assert rule is not None
        assert rule.entity_type == "Storage"

    def test_storage_matches_list_storage(self):
        """Test Storage rule matches list_storage operation."""
        rules = PROXMOX_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="list_storage",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "Storage"

    def test_storage_matches_get_storage(self):
        """Test Storage rule matches get_storage operation."""
        rules = PROXMOX_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="get_storage",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "Storage"

    def test_storage_matches_get_storage_status(self):
        """Test Storage rule matches get_storage_status operation."""
        rules = PROXMOX_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="get_storage_status",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "Storage"

    def test_storage_uses_storage_as_name(self):
        """Test Storage uses 'storage' field as name, not 'name'."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("Storage")
        assert rule.name_path == "storage"

    def test_storage_no_scope(self):
        """Test Storage rule has no scope (can be shared or local)."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("Storage")
        assert rule.scope_paths == {}

    def test_storage_no_outgoing_relationships(self):
        """Test Storage has no outgoing relationships in this schema."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("Storage")
        assert len(rule.relationships) == 0

    def test_storage_description_rendering(self):
        """Test Storage description template rendering."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("Storage")

        storage_data = {
            "storage": "local-lvm",
            "type": "lvmthin",
            "total_gb": 500,
            "used_gb": 200,
            "usage_percent": 40.0,
        }

        description = rule.description.render(storage_data)
        assert "local-lvm" in description
        assert "lvmthin" in description
        assert "500" in description or "200" in description

    def test_storage_attribute_extraction(self):
        """Test Storage attribute extraction."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("Storage")

        storage_data = {
            "storage": "local-lvm",
            "type": "lvmthin",
            "content": ["images", "rootdir"],
            "total_gb": 500,
            "used_gb": 200,
            "available_gb": 300,
            "usage_percent": 40.0,
            "enabled": True,
            "active": True,
            "shared": False,
        }

        for attr in rule.attributes:
            if attr.name == "type":
                assert attr.extract(storage_data) == "lvmthin"
            elif attr.name == "content":
                assert attr.extract(storage_data) == ["images", "rootdir"]
            elif attr.name == "shared":
                assert attr.extract(storage_data) is False
            elif attr.name == "enabled":
                assert attr.extract(storage_data) is True


# =============================================================================
# Cross-Entity Tests
# =============================================================================


class TestProxmoxSchemaConsistency:
    """Tests for Proxmox schema consistency across all entity types."""

    def test_all_rules_have_name_path(self):
        """Test all rules have name_path defined."""
        for rule in PROXMOX_EXTRACTION_SCHEMA.entity_rules:
            assert rule.name_path is not None, f"{rule.entity_type} should have name_path"
            assert len(rule.name_path) > 0, f"{rule.entity_type} should have non-empty name_path"

    def test_all_rules_have_descriptions(self):
        """Test all rules have description templates."""
        for rule in PROXMOX_EXTRACTION_SCHEMA.entity_rules:
            assert rule.description is not None
            assert rule.description.template is not None
            assert len(rule.description.template) > 0

    def test_workload_entities_have_node_scope(self):
        """Test workload entities (VM, Container) have node in scope."""
        workload_types = ["VM", "Container"]

        for entity_type in workload_types:
            rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type(entity_type)
            assert "node" in rule.scope_paths, f"{entity_type} should have node scope"

    def test_workload_entities_have_runs_on_node(self):
        """Test workload entities have runs_on Node relationship."""
        workload_types = ["VM", "Container"]

        for entity_type in workload_types:
            rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type(entity_type)
            runs_on = [r for r in rule.relationships if r.relationship_type == "runs_on"]
            assert len(runs_on) == 1, f"{entity_type} should have runs_on relationship"
            assert runs_on[0].target_type == "Node"

    def test_top_level_entities_have_no_scope(self):
        """Test top-level entities (Node, Storage) have no scope."""
        top_level_types = ["Node", "Storage"]

        for entity_type in top_level_types:
            rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type(entity_type)
            assert rule.scope_paths == {}, f"{entity_type} should have no scope (top-level)"

    def test_node_is_relationship_target(self):
        """Test Node is a relationship target for workload entities."""
        workload_types = ["VM", "Container"]

        for entity_type in workload_types:
            rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type(entity_type)
            node_rels = [r for r in rule.relationships if r.target_type == "Node"]
            assert len(node_rels) >= 1, f"{entity_type} should reference Node"

    def test_vmid_attribute_for_workloads(self):
        """Test VM and Container have vmid attribute."""
        workload_types = ["VM", "Container"]

        for entity_type in workload_types:
            rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type(entity_type)
            vmid_attr = next((a for a in rule.attributes if a.name == "vmid"), None)
            assert vmid_attr is not None, f"{entity_type} should have vmid attribute"

    def test_status_attribute_for_all_entities(self):
        """Test all entities have status attribute."""
        for rule in PROXMOX_EXTRACTION_SCHEMA.entity_rules:
            # Storage doesn't have status, it has enabled/active
            if rule.entity_type == "Storage":
                enabled_attr = next((a for a in rule.attributes if a.name == "enabled"), None)
                assert enabled_attr is not None, "Storage should have enabled attribute"
            else:
                status_attr = next((a for a in rule.attributes if a.name == "status"), None)
                assert status_attr is not None, f"{rule.entity_type} should have status attribute"


# =============================================================================
# Edge Case Tests
# =============================================================================


class TestProxmoxSchemaEdgeCases:
    """Edge case tests for Proxmox schema."""

    def test_unknown_operation_returns_empty(self):
        """Test unknown operation returns no matching rules."""
        rules = PROXMOX_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="unknown_operation",
            result_data={},
        )
        assert len(rules) == 0

    def test_empty_result_data_still_matches(self):
        """Test empty result data still matches by operation_id."""
        rules = PROXMOX_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="list_vms",
            result_data={},
        )
        assert len(rules) == 1

    def test_description_fallback_on_missing_fields(self):
        """Test description uses fallback when fields missing."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("VM")

        # Empty data should use fallback or N/A
        description = rule.description.render({})
        assert description is not None
        assert len(description) > 0

    def test_attribute_defaults(self):
        """Test attributes use defaults when values missing."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("VM")

        cpu_attr = next((a for a in rule.attributes if a.name == "cpu_count"), None)
        assert cpu_attr is not None
        assert cpu_attr.extract({}) == 0  # Default value

        tags_attr = next((a for a in rule.attributes if a.name == "tags"), None)
        assert tags_attr is not None
        assert tags_attr.extract({}) == []  # Default value

    def test_storage_content_defaults_to_empty_list(self):
        """Test Storage content attribute defaults to empty list."""
        rule = PROXMOX_EXTRACTION_SCHEMA.get_rule_for_entity_type("Storage")

        content_attr = next((a for a in rule.attributes if a.name == "content"), None)
        assert content_attr is not None
        assert content_attr.extract({}) == []
