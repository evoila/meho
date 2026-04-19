# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for topology extraction rules dataclasses.

Tests the declarative extraction framework including:
- DescriptionTemplate rendering
- AttributeExtraction with JMESPath
- RelationshipExtraction with single/multiple targets
- EntityExtractionRule matching
- ConnectorExtractionSchema rule matching
- Kubernetes extraction schema coverage
- VMware extraction schema coverage
"""

import pytest

from meho_app.modules.topology.extraction import (
    KUBERNETES_EXTRACTION_SCHEMA,
    VMWARE_EXTRACTION_SCHEMA,
    get_all_extraction_schemas,
    get_extraction_schema,
    get_supported_extraction_types,
    is_extraction_available,
)
from meho_app.modules.topology.extraction.rules import (
    AttributeExtraction,
    ConnectorExtractionSchema,
    DescriptionTemplate,
    EntityExtractionRule,
    RelationshipExtraction,
)

# =============================================================================
# DescriptionTemplate Tests
# =============================================================================


class TestDescriptionTemplate:
    """Tests for DescriptionTemplate rendering."""

    def test_simple_template_rendering(self):
        """Test basic placeholder substitution."""
        template = DescriptionTemplate(
            template="Hello {name}",
            fallback="Unknown",
        )
        result = template.render({"name": "World"})
        assert result == "Hello World"

    def test_nested_path_rendering(self):
        """Test JMESPath nested path substitution."""
        template = DescriptionTemplate(
            template="Pod {metadata.name} in {metadata.namespace}",
        )
        data = {
            "metadata": {
                "name": "nginx",
                "namespace": "default",
            }
        }
        result = template.render(data)
        assert result == "Pod nginx in default"

    def test_missing_value_shows_na(self):
        """Test missing values are replaced with N/A."""
        template = DescriptionTemplate(
            template="Status: {missing.path}",
        )
        result = template.render({})
        assert "N/A" in result

    def test_missing_data_returns_na(self):
        """Test missing data is replaced with N/A."""
        template = DescriptionTemplate(
            template="Test {path.to.value}",
            fallback="Fallback value",
        )
        # When data doesn't have the path, N/A is substituted
        result = template.render({})
        assert result == "Test N/A"

    def test_none_data_returns_na(self):
        """Test None data is handled gracefully with N/A."""
        template = DescriptionTemplate(
            template="Test {value}",
            fallback="Fallback value",
        )
        # When data is None, JMESPath handles it and returns N/A
        result = template.render(None)  # type: ignore
        assert result == "Test N/A"

    def test_template_with_multiple_placeholders(self):
        """Test template with multiple placeholders."""
        template = DescriptionTemplate(
            template="K8s Pod {metadata.name}, ns {metadata.namespace}, {status.phase}",
        )
        data = {
            "metadata": {"name": "nginx", "namespace": "prod"},
            "status": {"phase": "Running"},
        }
        result = template.render(data)
        assert result == "K8s Pod nginx, ns prod, Running"

    def test_template_with_no_placeholders(self):
        """Test template with no placeholders returns as-is."""
        template = DescriptionTemplate(template="Static text")
        result = template.render({"anything": "here"})
        assert result == "Static text"

    def test_array_index_path(self):
        """Test JMESPath array indexing."""
        template = DescriptionTemplate(
            template="First item: {items[0].name}",
        )
        data = {"items": [{"name": "first"}, {"name": "second"}]}
        result = template.render(data)
        assert result == "First item: first"

    def test_jmespath_filter_expression(self):
        """Test JMESPath filter expression."""
        template = DescriptionTemplate(
            template="Ready: {conditions[?type=='Ready'].status | [0]}",
        )
        data = {
            "conditions": [
                {"type": "Ready", "status": "True"},
                {"type": "DiskPressure", "status": "False"},
            ]
        }
        result = template.render(data)
        assert "True" in result


# =============================================================================
# AttributeExtraction Tests
# =============================================================================


class TestAttributeExtraction:
    """Tests for AttributeExtraction JMESPath extraction."""

    def test_simple_path_extraction(self):
        """Test simple path extraction."""
        attr = AttributeExtraction(name="phase", path="status.phase")
        data = {"status": {"phase": "Running"}}
        result = attr.extract(data)
        assert result == "Running"

    def test_missing_path_returns_default(self):
        """Test missing path returns default."""
        attr = AttributeExtraction(name="phase", path="status.phase", default="Unknown")
        result = attr.extract({})
        assert result == "Unknown"

    def test_none_value_returns_default(self):
        """Test None value returns default."""
        attr = AttributeExtraction(name="phase", path="status.phase", default="N/A")
        data = {"status": {"phase": None}}
        result = attr.extract(data)
        assert result == "N/A"

    def test_transform_lowercase(self):
        """Test lowercase transformation."""
        attr = AttributeExtraction(
            name="name",
            path="name",
            transform="lowercase",
        )
        result = attr.extract({"name": "MyApp"})
        assert result == "myapp"

    def test_transform_uppercase(self):
        """Test uppercase transformation."""
        attr = AttributeExtraction(
            name="name",
            path="name",
            transform="uppercase",
        )
        result = attr.extract({"name": "myapp"})
        assert result == "MYAPP"

    def test_transform_first_from_list(self):
        """Test first item extraction from list."""
        attr = AttributeExtraction(
            name="image",
            path="containers[*].image",
            transform="first",
        )
        data = {"containers": [{"image": "nginx:1.19"}, {"image": "redis:6"}]}
        result = attr.extract(data)
        assert result == "nginx:1.19"

    def test_transform_first_empty_list(self):
        """Test first transform on empty list returns None."""
        attr = AttributeExtraction(
            name="image",
            path="containers[*].image",
            transform="first",
        )
        data = {"containers": []}
        result = attr.extract(data)
        assert result is None

    def test_transform_on_non_string(self):
        """Test transform on non-string value preserves value."""
        attr = AttributeExtraction(
            name="count",
            path="count",
            transform="lowercase",  # Won't apply to int
        )
        result = attr.extract({"count": 42})
        assert result == 42

    def test_jmespath_error_returns_default(self):
        """Test invalid JMESPath returns default."""
        # Create an attribute with a path that could cause issues
        attr = AttributeExtraction(
            name="test",
            path="[invalid",  # Invalid JMESPath
            default="fallback",
        )
        result = attr.extract({"data": "value"})
        assert result == "fallback"

    def test_dict_extraction(self):
        """Test extracting dict values."""
        attr = AttributeExtraction(
            name="labels",
            path="metadata.labels",
            default={},
        )
        data = {"metadata": {"labels": {"app": "nginx", "env": "prod"}}}
        result = attr.extract(data)
        assert result == {"app": "nginx", "env": "prod"}

    def test_list_extraction(self):
        """Test extracting list values."""
        attr = AttributeExtraction(
            name="ports",
            path="spec.ports",
            default=[],
        )
        data = {"spec": {"ports": [80, 443]}}
        result = attr.extract(data)
        assert result == [80, 443]


# =============================================================================
# RelationshipExtraction Tests
# =============================================================================


class TestRelationshipExtraction:
    """Tests for RelationshipExtraction target extraction."""

    def test_single_target_extraction(self):
        """Test extracting single target."""
        rel = RelationshipExtraction(
            relationship_type="runs_on",
            target_type="Node",
            target_path="spec.nodeName",
        )
        data = {"spec": {"nodeName": "worker-01"}}
        targets = rel.extract_targets(data)
        assert targets == ["worker-01"]

    def test_missing_target_returns_empty(self):
        """Test missing target returns empty list."""
        rel = RelationshipExtraction(
            relationship_type="runs_on",
            target_type="Node",
            target_path="spec.nodeName",
            optional=True,
        )
        targets = rel.extract_targets({})
        assert targets == []

    def test_multiple_targets_extraction(self):
        """Test extracting multiple targets."""
        rel = RelationshipExtraction(
            relationship_type="uses",
            target_type="Datastore",
            target_path="datastores",
            multiple=True,
        )
        data = {"datastores": ["ds1", "ds2", "ds3"]}
        targets = rel.extract_targets(data)
        assert targets == ["ds1", "ds2", "ds3"]

    def test_nested_multiple_targets(self):
        """Test extracting nested multiple targets."""
        rel = RelationshipExtraction(
            relationship_type="routes_to",
            target_type="Service",
            target_path="spec.rules[*].http.paths[*].backend.service.name",
            multiple=True,
        )
        data = {
            "spec": {
                "rules": [
                    {
                        "http": {
                            "paths": [
                                {"backend": {"service": {"name": "svc-a"}}},
                                {"backend": {"service": {"name": "svc-b"}}},
                            ]
                        }
                    }
                ]
            }
        }
        targets = rel.extract_targets(data)
        assert set(targets) == {"svc-a", "svc-b"}

    def test_none_in_list_filtered(self):
        """Test None values in list are filtered."""
        rel = RelationshipExtraction(
            relationship_type="uses",
            target_type="Resource",
            target_path="resources",
            multiple=True,
        )
        data = {"resources": ["res1", None, "res2", ""]}
        targets = rel.extract_targets(data)
        assert targets == ["res1", "res2"]

    def test_jmespath_filter_for_owner(self):
        """Test JMESPath filter for owner references."""
        rel = RelationshipExtraction(
            relationship_type="managed_by",
            target_type="ReplicaSet",
            target_path="metadata.ownerReferences[?kind=='ReplicaSet'].name | [0]",
        )
        data = {
            "metadata": {
                "ownerReferences": [
                    {"kind": "ReplicaSet", "name": "nginx-abc123"},
                    {"kind": "Node", "name": "some-node"},
                ]
            }
        }
        targets = rel.extract_targets(data)
        assert targets == ["nginx-abc123"]

    def test_non_string_target_converted(self):
        """Test non-string targets are converted to strings."""
        rel = RelationshipExtraction(
            relationship_type="uses",
            target_type="Port",
            target_path="ports",
            multiple=True,
        )
        data = {"ports": [80, 443]}
        targets = rel.extract_targets(data)
        assert targets == ["80", "443"]

    def test_invalid_jmespath_returns_empty(self):
        """Test invalid JMESPath returns empty list."""
        rel = RelationshipExtraction(
            relationship_type="test",
            target_type="Test",
            target_path="[invalid",  # Invalid JMESPath
        )
        targets = rel.extract_targets({"data": "value"})
        assert targets == []


# =============================================================================
# EntityExtractionRule Tests
# =============================================================================


class TestEntityExtractionRule:
    """Tests for EntityExtractionRule matching."""

    def test_matches_operation(self):
        """Test operation ID matching."""
        rule = EntityExtractionRule(
            entity_type="VM",
            source_operations=["list_virtual_machines", "get_virtual_machine"],
        )
        assert rule.matches_operation("list_virtual_machines") is True
        assert rule.matches_operation("get_virtual_machine") is True
        assert rule.matches_operation("delete_vm") is False
        assert rule.matches_operation(None) is False

    def test_matches_kind(self):
        """Test K8s kind matching."""
        rule = EntityExtractionRule(
            entity_type="Pod",
            source_kinds=["Pod", "PodList"],
        )
        assert rule.matches_kind("Pod") is True
        assert rule.matches_kind("PodList") is True
        assert rule.matches_kind("Deployment") is False
        assert rule.matches_kind(None) is False

    def test_matches_detection_path_exists(self):
        """Test detection path existence check."""
        rule = EntityExtractionRule(
            entity_type="Resource",
            detection_path="data.type",
        )
        assert rule.matches_detection({"data": {"type": "vm"}}) is True
        assert rule.matches_detection({"data": {}}) is False
        assert rule.matches_detection({}) is False

    def test_matches_detection_path_with_value(self):
        """Test detection path with specific value."""
        rule = EntityExtractionRule(
            entity_type="Resource",
            detection_path="data.type",
            detection_value="virtual_machine",
        )
        assert rule.matches_detection({"data": {"type": "virtual_machine"}}) is True
        assert rule.matches_detection({"data": {"type": "host"}}) is False

    def test_default_description_template(self):
        """Test default description template."""
        rule = EntityExtractionRule(entity_type="Test")
        assert "Entity" in rule.description.template

    def test_rule_with_all_components(self):
        """Test rule with all components defined."""
        rule = EntityExtractionRule(
            entity_type="Pod",
            source_kinds=["Pod", "PodList"],
            items_path="items",
            name_path="metadata.name",
            scope_paths={"namespace": "metadata.namespace"},
            description=DescriptionTemplate(template="Pod {metadata.name}"),
            attributes=[
                AttributeExtraction(name="phase", path="status.phase"),
            ],
            relationships=[
                RelationshipExtraction(
                    relationship_type="runs_on",
                    target_type="Node",
                    target_path="spec.nodeName",
                ),
            ],
            create_targets=True,
        )
        assert rule.entity_type == "Pod"
        assert len(rule.attributes) == 1
        assert len(rule.relationships) == 1
        assert rule.create_targets is True


# =============================================================================
# ConnectorExtractionSchema Tests
# =============================================================================


class TestConnectorExtractionSchema:
    """Tests for ConnectorExtractionSchema rule matching."""

    @pytest.fixture
    def sample_schema(self):
        """Create a sample schema for testing."""
        return ConnectorExtractionSchema(
            connector_type="test",
            entity_rules=[
                EntityExtractionRule(
                    entity_type="Resource",
                    source_operations=["list_resources"],
                ),
                EntityExtractionRule(
                    entity_type="Item",
                    source_kinds=["Item", "ItemList"],
                ),
                EntityExtractionRule(
                    entity_type="Custom",
                    detection_path="metadata.type",
                    detection_value="custom",
                ),
            ],
        )

    def test_find_rules_by_operation(self, sample_schema):
        """Test finding rules by operation ID."""
        rules = sample_schema.find_matching_rules(
            operation_id="list_resources",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "Resource"

    def test_find_rules_by_kind(self, sample_schema):
        """Test finding rules by K8s kind."""
        rules = sample_schema.find_matching_rules(
            operation_id=None,
            result_data={"kind": "ItemList"},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "Item"

    def test_find_rules_by_detection(self, sample_schema):
        """Test finding rules by detection path."""
        rules = sample_schema.find_matching_rules(
            operation_id=None,
            result_data={"metadata": {"type": "custom"}},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "Custom"

    def test_find_no_matching_rules(self, sample_schema):
        """Test no matching rules returns empty list."""
        rules = sample_schema.find_matching_rules(
            operation_id="unknown_op",
            result_data={"kind": "Unknown"},
        )
        assert rules == []

    def test_get_rule_for_entity_type(self, sample_schema):
        """Test getting rule by entity type."""
        rule = sample_schema.get_rule_for_entity_type("Resource")
        assert rule is not None
        assert rule.entity_type == "Resource"

        rule = sample_schema.get_rule_for_entity_type("NonExistent")
        assert rule is None

    def test_get_all_entity_types(self, sample_schema):
        """Test getting all entity types."""
        types = sample_schema.get_all_entity_types()
        assert set(types) == {"Resource", "Item", "Custom"}

    def test_get_all_operations(self, sample_schema):
        """Test getting all operations."""
        ops = sample_schema.get_all_operations()
        assert "list_resources" in ops

    def test_get_all_kinds(self, sample_schema):
        """Test getting all kinds."""
        kinds = sample_schema.get_all_kinds()
        assert set(kinds) == {"Item", "ItemList"}


# =============================================================================
# Extraction Registry Tests
# =============================================================================


class TestExtractionRegistry:
    """Tests for extraction schema registry functions."""

    def test_get_kubernetes_schema(self):
        """Test getting Kubernetes schema."""
        schema = get_extraction_schema("kubernetes")
        assert schema is not None
        assert schema.connector_type == "kubernetes"

    def test_get_vmware_schema(self):
        """Test getting VMware schema."""
        schema = get_extraction_schema("vmware")
        assert schema is not None
        assert schema.connector_type == "vmware"

    def test_get_nonexistent_schema(self):
        """Test getting non-existent schema returns None."""
        schema = get_extraction_schema("nonexistent")
        assert schema is None

    def test_get_all_schemas(self):
        """Test getting all schemas."""
        schemas = get_all_extraction_schemas()
        assert "kubernetes" in schemas
        assert "vmware" in schemas

    def test_get_supported_types(self):
        """Test getting supported types."""
        types = get_supported_extraction_types()
        assert "kubernetes" in types
        assert "vmware" in types

    def test_is_extraction_available(self):
        """Test extraction availability check."""
        assert is_extraction_available("kubernetes") is True
        assert is_extraction_available("vmware") is True
        assert is_extraction_available("nonexistent") is False


# =============================================================================
# Kubernetes Extraction Schema Tests
# =============================================================================


class TestKubernetesExtractionSchema:
    """Tests for Kubernetes extraction schema coverage."""

    def test_schema_connector_type(self):
        """Test schema has correct connector type."""
        assert KUBERNETES_EXTRACTION_SCHEMA.connector_type == "kubernetes"

    def test_all_entity_types_defined(self):
        """Test all expected entity types are defined."""
        expected_types = {
            "Pod",
            "Node",
            "Namespace",
            "Deployment",
            "ReplicaSet",
            "Service",
            "Ingress",
            "StatefulSet",
            "DaemonSet",
        }
        actual_types = set(KUBERNETES_EXTRACTION_SCHEMA.get_all_entity_types())
        assert expected_types == actual_types

    def test_all_kinds_defined(self):
        """Test all expected kinds are defined."""
        expected_kinds = {
            "Pod",
            "PodList",
            "Node",
            "NodeList",
            "Namespace",
            "NamespaceList",
            "Deployment",
            "DeploymentList",
            "ReplicaSet",
            "ReplicaSetList",
            "Service",
            "ServiceList",
            "Ingress",
            "IngressList",
            "StatefulSet",
            "StatefulSetList",
            "DaemonSet",
            "DaemonSetList",
        }
        actual_kinds = set(KUBERNETES_EXTRACTION_SCHEMA.get_all_kinds())
        assert expected_kinds == actual_kinds

    def test_pod_rule_matching(self):
        """Test Pod rule matches PodList response."""
        rules = KUBERNETES_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id=None,
            result_data={"kind": "PodList", "items": []},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "Pod"

    def test_pod_rule_has_relationships(self):
        """Test Pod rule has expected relationships."""
        rule = KUBERNETES_EXTRACTION_SCHEMA.get_rule_for_entity_type("Pod")
        assert rule is not None

        rel_types = {r.relationship_type for r in rule.relationships}
        assert "member_of" in rel_types
        assert "runs_on" in rel_types
        assert "managed_by" in rel_types

    def test_pod_rule_has_scope(self):
        """Test Pod rule has namespace scope."""
        rule = KUBERNETES_EXTRACTION_SCHEMA.get_rule_for_entity_type("Pod")
        assert "namespace" in rule.scope_paths

    def test_node_rule_no_scope(self):
        """Test Node rule has no scope (cluster-scoped)."""
        rule = KUBERNETES_EXTRACTION_SCHEMA.get_rule_for_entity_type("Node")
        assert rule.scope_paths == {}

    def test_deployment_rule_matching(self):
        """Test Deployment rule matches DeploymentList response."""
        rules = KUBERNETES_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id=None,
            result_data={"kind": "DeploymentList", "items": []},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "Deployment"

    def test_ingress_routes_to_service(self):
        """Test Ingress rule has routes_to Service relationship."""
        rule = KUBERNETES_EXTRACTION_SCHEMA.get_rule_for_entity_type("Ingress")
        routes_to = [r for r in rule.relationships if r.relationship_type == "routes_to"]
        assert len(routes_to) == 1
        assert routes_to[0].target_type == "Service"
        assert routes_to[0].multiple is True

    def test_all_namespaced_resources_have_member_of(self):
        """Test all namespaced resources have member_of Namespace."""
        namespaced_types = [
            "Pod",
            "Deployment",
            "ReplicaSet",
            "Service",
            "Ingress",
            "StatefulSet",
            "DaemonSet",
        ]

        for entity_type in namespaced_types:
            rule = KUBERNETES_EXTRACTION_SCHEMA.get_rule_for_entity_type(entity_type)
            member_of = [
                r
                for r in rule.relationships
                if r.relationship_type == "member_of" and r.target_type == "Namespace"
            ]
            assert len(member_of) == 1, f"{entity_type} should have member_of Namespace"


# =============================================================================
# VMware Extraction Schema Tests
# =============================================================================


class TestVMwareExtractionSchema:
    """Tests for VMware extraction schema coverage."""

    def test_schema_connector_type(self):
        """Test schema has correct connector type."""
        assert VMWARE_EXTRACTION_SCHEMA.connector_type == "vmware"

    def test_all_entity_types_defined(self):
        """Test all expected entity types are defined."""
        expected_types = {"VM", "Host", "Cluster", "Datastore"}
        actual_types = set(VMWARE_EXTRACTION_SCHEMA.get_all_entity_types())
        assert expected_types == actual_types

    def test_all_operations_defined(self):
        """Test all expected operations are defined."""
        expected_ops = {
            "list_virtual_machines",
            "get_virtual_machine",
            "list_hosts",
            "get_host",
            "list_clusters",
            "get_cluster",
            "list_datastores",
            "get_datastore",
        }
        actual_ops = set(VMWARE_EXTRACTION_SCHEMA.get_all_operations())
        assert expected_ops == actual_ops

    def test_vm_rule_matching(self):
        """Test VM rule matches list_virtual_machines operation."""
        rules = VMWARE_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="list_virtual_machines",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "VM"

    def test_vm_rule_has_relationships(self):
        """Test VM rule has expected relationships."""
        rule = VMWARE_EXTRACTION_SCHEMA.get_rule_for_entity_type("VM")
        assert rule is not None

        rel_types = {r.relationship_type for r in rule.relationships}
        assert "runs_on" in rel_types
        assert "uses" in rel_types

    def test_vm_uses_datastore_multiple(self):
        """Test VM uses Datastore relationship is multiple."""
        rule = VMWARE_EXTRACTION_SCHEMA.get_rule_for_entity_type("VM")
        uses_ds = [
            r
            for r in rule.relationships
            if r.relationship_type == "uses" and r.target_type == "Datastore"
        ]
        assert len(uses_ds) == 1
        assert uses_ds[0].multiple is True

    def test_host_rule_matching(self):
        """Test Host rule matches list_hosts operation."""
        rules = VMWARE_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="list_hosts",
            result_data={},
        )
        assert len(rules) == 1
        assert rules[0].entity_type == "Host"

    def test_host_member_of_cluster(self):
        """Test Host rule has member_of Cluster relationship."""
        rule = VMWARE_EXTRACTION_SCHEMA.get_rule_for_entity_type("Host")
        member_of = [
            r
            for r in rule.relationships
            if r.relationship_type == "member_of" and r.target_type == "Cluster"
        ]
        assert len(member_of) >= 1

    def test_cluster_no_outgoing_relationships(self):
        """Test Cluster has no outgoing relationships in schema."""
        rule = VMWARE_EXTRACTION_SCHEMA.get_rule_for_entity_type("Cluster")
        assert len(rule.relationships) == 0

    def test_datastore_no_outgoing_relationships(self):
        """Test Datastore has no outgoing relationships in schema."""
        rule = VMWARE_EXTRACTION_SCHEMA.get_rule_for_entity_type("Datastore")
        assert len(rule.relationships) == 0

    def test_all_rules_have_name_path(self):
        """Test all rules have name_path defined."""
        for rule in VMWARE_EXTRACTION_SCHEMA.entity_rules:
            assert rule.name_path is not None
            assert rule.name_path == "name"  # VMware uses simple name


# =============================================================================
# Integration-like Tests (Schema + Extraction)
# =============================================================================


class TestSchemaExtraction:
    """Tests combining schema matching with data extraction."""

    def test_kubernetes_pod_extraction(self):
        """Test extracting Pod entity details."""
        pod_data = {
            "kind": "Pod",
            "metadata": {
                "name": "nginx-pod",
                "namespace": "production",
            },
            "spec": {
                "nodeName": "worker-01",
            },
            "status": {
                "phase": "Running",
                "podIP": "10.0.0.5",
            },
        }

        # Find matching rules
        rules = KUBERNETES_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id=None,
            result_data=pod_data,
        )
        assert len(rules) == 1

        rule = rules[0]
        assert rule.entity_type == "Pod"

        # Test description rendering
        description = rule.description.render(pod_data)
        assert "nginx-pod" in description
        assert "production" in description

        # Test attribute extraction
        for attr in rule.attributes:
            if attr.name == "phase":
                assert attr.extract(pod_data) == "Running"
            elif attr.name == "pod_ip":
                assert attr.extract(pod_data) == "10.0.0.5"
            elif attr.name == "node_name":
                assert attr.extract(pod_data) == "worker-01"

        # Test relationship extraction
        for rel in rule.relationships:
            if rel.relationship_type == "member_of":
                targets = rel.extract_targets(pod_data)
                assert targets == ["production"]
            elif rel.relationship_type == "runs_on":
                targets = rel.extract_targets(pod_data)
                assert targets == ["worker-01"]

    def test_vmware_vm_extraction(self):
        """Test extracting VM entity details."""
        vm_data = {
            "name": "web-server-01",
            "config": {
                "num_cpu": 4,
                "memory_mb": 8192,
                "guest_os": "CentOS",
            },
            "runtime": {
                "power_state": "poweredOn",
                "host": "esxi-01",
            },
            "datastores": ["datastore1", "datastore2"],
        }

        # Find matching rules
        rules = VMWARE_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id="get_virtual_machine",
            result_data=vm_data,
        )
        assert len(rules) == 1

        rule = rules[0]
        assert rule.entity_type == "VM"

        # Test description rendering
        description = rule.description.render(vm_data)
        assert "web-server-01" in description
        assert "4" in description  # vCPU

        # Test attribute extraction
        for attr in rule.attributes:
            if attr.name == "power_state":
                assert attr.extract(vm_data) == "poweredOn"
            elif attr.name == "num_cpu":
                assert attr.extract(vm_data) == 4

        # Test relationship extraction
        for rel in rule.relationships:
            if rel.relationship_type == "runs_on":
                targets = rel.extract_targets(vm_data)
                assert targets == ["esxi-01"]
            elif rel.relationship_type == "uses":
                targets = rel.extract_targets(vm_data)
                assert set(targets) == {"datastore1", "datastore2"}

    def test_kubernetes_deployment_extraction(self):
        """Test extracting Deployment entity details."""
        deploy_data = {
            "kind": "Deployment",
            "metadata": {
                "name": "frontend",
                "namespace": "default",
            },
            "spec": {
                "replicas": 3,
                "strategy": {"type": "RollingUpdate"},
            },
            "status": {
                "readyReplicas": 3,
                "availableReplicas": 3,
            },
        }

        rules = KUBERNETES_EXTRACTION_SCHEMA.find_matching_rules(
            operation_id=None,
            result_data=deploy_data,
        )
        assert len(rules) == 1
        rule = rules[0]

        # Test attributes
        for attr in rule.attributes:
            if attr.name == "replicas":
                assert attr.extract(deploy_data) == 3
            elif attr.name == "strategy":
                assert attr.extract(deploy_data) == "RollingUpdate"
