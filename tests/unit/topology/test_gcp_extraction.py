# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for GCP topology extraction schema.

Tests the declarative extraction rules for GCP resources including:
- Schema structure and entity types
- Operation ID matching
- Relationship extraction paths
- Description template rendering
- Scope extraction (zone, region, location)
"""

from meho_app.modules.topology.extraction import (
    GCP_EXTRACTION_SCHEMA,
    get_extraction_schema,
    is_extraction_available,
)

# =============================================================================
# GCP Extraction Schema Structure Tests
# =============================================================================


class TestGCPExtractionSchemaStructure:
    """Tests for GCP extraction schema structure."""

    def test_schema_connector_type(self):
        """Test schema has correct connector type."""
        assert GCP_EXTRACTION_SCHEMA.connector_type == "gcp"

    def test_schema_registered(self):
        """Test GCP schema is registered in the registry."""
        assert is_extraction_available("gcp") is True
        schema = get_extraction_schema("gcp")
        assert schema is not None
        assert schema.connector_type == "gcp"

    def test_all_entity_types_defined(self):
        """Test all expected entity types are defined."""
        expected_types = {"Instance", "Disk", "Network", "Subnet", "Firewall", "GKECluster"}
        actual_types = set(GCP_EXTRACTION_SCHEMA.get_all_entity_types())
        assert expected_types == actual_types

    def test_all_operations_defined(self):
        """Test all expected operations are defined."""
        expected_ops = {
            "list_instances",
            "get_instance",
            "list_disks",
            "get_disk",
            "list_networks",
            "get_network",
            "list_subnetworks",
            "get_subnetwork",
            "list_firewalls",
            "get_firewall",
            "list_clusters",
            "get_cluster",
        }
        actual_ops = set(GCP_EXTRACTION_SCHEMA.get_all_operations())
        assert expected_ops == actual_ops


# =============================================================================
# Instance Extraction Tests
# =============================================================================


class TestGCPInstanceExtraction:
    """Tests for GCP Instance extraction rule."""

    def test_instance_rule_exists(self):
        """Test Instance rule exists."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Instance")
        assert rule is not None
        assert rule.entity_type == "Instance"

    def test_instance_matches_operations(self):
        """Test Instance rule matches correct operations."""
        rules = GCP_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="list_instances",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "Instance"

        rules = GCP_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="get_instance",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "Instance"

    def test_instance_has_zone_scope(self):
        """Test Instance rule has zone scope."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Instance")
        assert "zone" in rule.scope_paths
        assert rule.scope_paths["zone"] == "zone"

    def test_instance_has_relationships(self):
        """Test Instance rule has expected relationships."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Instance")
        rel_types = {r.relationship_type for r in rule.relationships}
        assert "uses" in rel_types
        assert "member_of" in rel_types

    def test_instance_uses_disk_multiple(self):
        """Test Instance uses Disk relationship is multiple."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Instance")
        uses_disk = [
            r
            for r in rule.relationships
            if r.relationship_type == "uses" and r.target_type == "Disk"
        ]
        assert len(uses_disk) == 1
        assert uses_disk[0].multiple is True

    def test_instance_description_rendering(self):
        """Test Instance description template rendering."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Instance")

        instance_data = {
            "name": "web-server-1",
            "zone": "us-central1-a",
            "machine_type": "n1-standard-2",
            "status": "RUNNING",
        }

        description = rule.description.render(instance_data)
        assert "web-server-1" in description
        assert "us-central1-a" in description
        assert "n1-standard-2" in description
        assert "RUNNING" in description

    def test_instance_attribute_extraction(self):
        """Test Instance attribute extraction."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Instance")

        instance_data = {
            "id": "123456789",
            "name": "test-vm",
            "status": "RUNNING",
            "zone": "us-central1-a",
            "labels": {"env": "prod"},
        }

        for attr in rule.attributes:
            if attr.name == "id":
                assert attr.extract(instance_data) == "123456789"
            elif attr.name == "status":
                assert attr.extract(instance_data) == "RUNNING"
            elif attr.name == "zone":
                assert attr.extract(instance_data) == "us-central1-a"
            elif attr.name == "labels":
                assert attr.extract(instance_data) == {"env": "prod"}

    def test_instance_relationship_extraction(self):
        """Test Instance relationship target extraction."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Instance")

        instance_data = {
            "name": "test-vm",
            "disks": [
                {"name": "disk-1"},
                {"name": "disk-2"},
            ],
            "network_interfaces": [
                {"network": "default", "internal_ip": "10.0.0.5"},
            ],
        }

        for rel in rule.relationships:
            if rel.relationship_type == "uses" and rel.target_type == "Disk":
                targets = rel.extract_targets(instance_data)
                assert set(targets) == {"disk-1", "disk-2"}
            elif rel.relationship_type == "member_of" and rel.target_type == "Network":
                targets = rel.extract_targets(instance_data)
                assert targets == ["default"]


# =============================================================================
# Disk Extraction Tests
# =============================================================================


class TestGCPDiskExtraction:
    """Tests for GCP Disk extraction rule."""

    def test_disk_rule_exists(self):
        """Test Disk rule exists."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Disk")
        assert rule is not None
        assert rule.entity_type == "Disk"

    def test_disk_matches_operations(self):
        """Test Disk rule matches correct operations."""
        rules = GCP_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="list_disks",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "Disk"

    def test_disk_has_zone_scope(self):
        """Test Disk rule has zone scope."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Disk")
        assert "zone" in rule.scope_paths

    def test_disk_no_outgoing_relationships(self):
        """Test Disk has no outgoing relationships."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Disk")
        assert len(rule.relationships) == 0

    def test_disk_description_rendering(self):
        """Test Disk description template rendering."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Disk")

        disk_data = {
            "name": "data-disk",
            "zone": "us-central1-a",
            "type": "pd-ssd",
            "size_gb": 100,
            "status": "READY",
        }

        description = rule.description.render(disk_data)
        assert "data-disk" in description
        assert "us-central1-a" in description
        assert "pd-ssd" in description
        assert "100" in description


# =============================================================================
# Network Extraction Tests
# =============================================================================


class TestGCPNetworkExtraction:
    """Tests for GCP Network extraction rule."""

    def test_network_rule_exists(self):
        """Test Network rule exists."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Network")
        assert rule is not None
        assert rule.entity_type == "Network"

    def test_network_matches_operations(self):
        """Test Network rule matches correct operations."""
        rules = GCP_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="list_networks",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "Network"

    def test_network_no_scope(self):
        """Test Network rule has no scope (global/project-scoped)."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Network")
        assert rule.scope_paths == {}

    def test_network_no_outgoing_relationships(self):
        """Test Network has no outgoing relationships (top-level)."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Network")
        assert len(rule.relationships) == 0

    def test_network_description_rendering(self):
        """Test Network description template rendering."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Network")

        network_data = {
            "name": "vpc-main",
            "routing_mode": "REGIONAL",
            "auto_create_subnetworks": False,
        }

        description = rule.description.render(network_data)
        assert "vpc-main" in description
        assert "REGIONAL" in description


# =============================================================================
# Subnet Extraction Tests
# =============================================================================


class TestGCPSubnetExtraction:
    """Tests for GCP Subnet extraction rule."""

    def test_subnet_rule_exists(self):
        """Test Subnet rule exists."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Subnet")
        assert rule is not None
        assert rule.entity_type == "Subnet"

    def test_subnet_matches_operations(self):
        """Test Subnet rule matches correct operations."""
        rules = GCP_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="list_subnetworks",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "Subnet"

    def test_subnet_has_region_scope(self):
        """Test Subnet rule has region scope."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Subnet")
        assert "region" in rule.scope_paths
        assert rule.scope_paths["region"] == "region"

    def test_subnet_member_of_network(self):
        """Test Subnet has member_of Network relationship."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Subnet")
        member_of = [
            r
            for r in rule.relationships
            if r.relationship_type == "member_of" and r.target_type == "Network"
        ]
        assert len(member_of) == 1
        assert member_of[0].optional is False

    def test_subnet_relationship_extraction(self):
        """Test Subnet relationship target extraction."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Subnet")

        subnet_data = {
            "name": "subnet-1",
            "network": "vpc-main",
            "region": "us-central1",
            "ip_cidr_range": "10.0.0.0/24",
        }

        for rel in rule.relationships:
            if rel.relationship_type == "member_of":
                targets = rel.extract_targets(subnet_data)
                assert targets == ["vpc-main"]


# =============================================================================
# Firewall Extraction Tests
# =============================================================================


class TestGCPFirewallExtraction:
    """Tests for GCP Firewall extraction rule."""

    def test_firewall_rule_exists(self):
        """Test Firewall rule exists."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Firewall")
        assert rule is not None
        assert rule.entity_type == "Firewall"

    def test_firewall_matches_operations(self):
        """Test Firewall rule matches correct operations."""
        rules = GCP_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="list_firewalls",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "Firewall"

    def test_firewall_no_scope(self):
        """Test Firewall rule has no scope (global/project-scoped)."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Firewall")
        assert rule.scope_paths == {}

    def test_firewall_applies_to_network(self):
        """Test Firewall has applies_to Network relationship."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Firewall")
        applies_to = [
            r
            for r in rule.relationships
            if r.relationship_type == "applies_to" and r.target_type == "Network"
        ]
        assert len(applies_to) == 1
        assert applies_to[0].optional is False

    def test_firewall_description_rendering(self):
        """Test Firewall description template rendering."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("Firewall")

        firewall_data = {
            "name": "allow-ssh",
            "direction": "INGRESS",
            "priority": 1000,
            "network": "default",
        }

        description = rule.description.render(firewall_data)
        assert "allow-ssh" in description
        assert "INGRESS" in description
        assert "1000" in description


# =============================================================================
# GKECluster Extraction Tests
# =============================================================================


class TestGCPGKEClusterExtraction:
    """Tests for GCP GKECluster extraction rule."""

    def test_gkecluster_rule_exists(self):
        """Test GKECluster rule exists."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("GKECluster")
        assert rule is not None
        assert rule.entity_type == "GKECluster"

    def test_gkecluster_matches_operations(self):
        """Test GKECluster rule matches correct operations."""
        rules = GCP_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="list_clusters",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "GKECluster"

        rules = GCP_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="get_cluster",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "GKECluster"

    def test_gkecluster_has_location_scope(self):
        """Test GKECluster rule has location scope."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("GKECluster")
        assert "location" in rule.scope_paths
        assert rule.scope_paths["location"] == "location"

    def test_gkecluster_member_of_network(self):
        """Test GKECluster has member_of Network relationship."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("GKECluster")
        member_of = [
            r
            for r in rule.relationships
            if r.relationship_type == "member_of" and r.target_type == "Network"
        ]
        assert len(member_of) == 1
        assert member_of[0].optional is True  # Network is optional for some clusters

    def test_gkecluster_description_rendering(self):
        """Test GKECluster description template rendering."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("GKECluster")

        cluster_data = {
            "name": "prod-cluster",
            "location": "us-central1",
            "status": "RUNNING",
            "current_node_count": 6,
        }

        description = rule.description.render(cluster_data)
        assert "prod-cluster" in description
        assert "us-central1" in description
        assert "RUNNING" in description
        assert "6" in description

    def test_gkecluster_attribute_extraction(self):
        """Test GKECluster attribute extraction."""
        rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type("GKECluster")

        cluster_data = {
            "name": "test-cluster",
            "location": "us-central1",
            "status": "RUNNING",
            "current_node_count": 3,
            "endpoint": "35.192.0.1",
            "labels": {"team": "platform"},
        }

        for attr in rule.attributes:
            if attr.name == "location":
                assert attr.extract(cluster_data) == "us-central1"
            elif attr.name == "status":
                assert attr.extract(cluster_data) == "RUNNING"
            elif attr.name == "current_node_count":
                assert attr.extract(cluster_data) == 3
            elif attr.name == "labels":
                assert attr.extract(cluster_data) == {"team": "platform"}


# =============================================================================
# Cross-Entity Tests
# =============================================================================


class TestGCPSchemaConsistency:
    """Tests for GCP schema consistency across all entity types."""

    def test_all_rules_have_name_path(self):
        """Test all rules have name_path defined as 'name'."""
        for rule in GCP_EXTRACTION_SCHEMA.entity_rules:
            assert rule.name_path == "name", f"{rule.entity_type} should use 'name' path"

    def test_all_rules_have_descriptions(self):
        """Test all rules have description templates."""
        for rule in GCP_EXTRACTION_SCHEMA.entity_rules:
            assert rule.description is not None
            assert rule.description.template is not None
            assert len(rule.description.template) > 0

    def test_zonal_entities_have_zone_scope(self):
        """Test zonal entities have zone in scope."""
        zonal_types = ["Instance", "Disk"]

        for entity_type in zonal_types:
            rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type(entity_type)
            assert "zone" in rule.scope_paths, f"{entity_type} should have zone scope"

    def test_regional_entities_have_region_scope(self):
        """Test regional entities have region in scope."""
        regional_types = ["Subnet"]

        for entity_type in regional_types:
            rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type(entity_type)
            assert "region" in rule.scope_paths, f"{entity_type} should have region scope"

    def test_global_entities_have_no_scope(self):
        """Test global entities have no scope."""
        global_types = ["Network", "Firewall"]

        for entity_type in global_types:
            rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type(entity_type)
            assert rule.scope_paths == {}, f"{entity_type} should have no scope (global)"

    def test_network_related_entities_reference_network(self):
        """Test entities that relate to Network have member_of or applies_to."""
        network_related = ["Instance", "Subnet", "Firewall", "GKECluster"]

        for entity_type in network_related:
            rule = GCP_EXTRACTION_SCHEMA.get_rule_for_entity_type(entity_type)
            network_rels = [r for r in rule.relationships if r.target_type == "Network"]
            assert len(network_rels) >= 1, f"{entity_type} should have Network relationship"
