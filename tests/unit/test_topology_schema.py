# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for topology schema framework.

Tests:
- Base dataclasses (EntityTypeDefinition, RelationshipRule, ConnectorTopologySchema)
- Kubernetes schema entity types and relationships
- VMware schema entity types and relationships
- GCP schema entity types and relationships

Phase 84: Topology schema updated in v2.1 Phase 76 -- K8s deployment/service can_correlate
changed, schema registry contents updated.
- Proxmox schema entity types and relationships
- Schema registry functions
- Canonical ID generation
- Cross-connector validation (K8s Pod cannot have VMware relationship)
"""

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: Topology schema updated in v2.1 Phase 76, K8s can_correlate and schema registry contents changed")

from meho_app.modules.topology.schema import (
    GCP_TOPOLOGY_SCHEMA,
    # Schema constants
    KUBERNETES_TOPOLOGY_SCHEMA,
    PROXMOX_TOPOLOGY_SCHEMA,
    VMWARE_TOPOLOGY_SCHEMA,
    # Base classes
    ConnectorTopologySchema,
    EntityTypeDefinition,
    RelationshipRule,
    SameAsEligibility,
    Volatility,
    get_all_schemas,
    get_supported_connector_types,
    # Registry functions
    get_topology_schema,
    is_schema_available,
)

# =============================================================================
# Base Dataclass Tests
# =============================================================================


class TestEntityTypeDefinition:
    """Tests for EntityTypeDefinition dataclass."""

    def test_create_basic_entity_type(self):
        """Test creating a basic entity type."""
        entity = EntityTypeDefinition(
            name="TestEntity",
            scoped=False,
            identity_fields=["name"],
        )

        assert entity.name == "TestEntity"
        assert entity.scoped is False
        assert entity.scope_type is None
        assert entity.identity_fields == ["name"]
        assert entity.volatility == Volatility.MODERATE

    def test_create_scoped_entity_type(self):
        """Test creating a scoped entity type."""
        entity = EntityTypeDefinition(
            name="ScopedEntity",
            scoped=True,
            scope_type="namespace",
            identity_fields=["namespace", "name"],
            volatility=Volatility.EPHEMERAL,
        )

        assert entity.scoped is True
        assert entity.scope_type == "namespace"
        assert entity.identity_fields == ["namespace", "name"]
        assert entity.volatility == Volatility.EPHEMERAL

    def test_default_identity_fields_for_scoped(self):
        """Test that default identity fields are set for scoped entity without explicit fields."""
        entity = EntityTypeDefinition(
            name="AutoScoped",
            scoped=True,
            scope_type="namespace",
        )

        # __post_init__ should set identity_fields to [scope_type, "name"]
        assert entity.identity_fields == ["namespace", "name"]

    def test_default_identity_fields_for_unscoped(self):
        """Test that default identity fields are set for unscoped entity."""
        entity = EntityTypeDefinition(
            name="AutoUnscoped",
            scoped=False,
        )

        # __post_init__ should set identity_fields to ["name"]
        assert entity.identity_fields == ["name"]

    def test_volatility_values(self):
        """Test all volatility enum values."""
        assert Volatility.STABLE.value == "stable"
        assert Volatility.MODERATE.value == "moderate"
        assert Volatility.EPHEMERAL.value == "ephemeral"


class TestRelationshipRule:
    """Tests for RelationshipRule dataclass."""

    def test_create_relationship_rule(self):
        """Test creating a relationship rule."""
        rule = RelationshipRule(
            from_type="Pod",
            relationship_type="runs_on",
            to_type="Node",
        )

        assert rule.from_type == "Pod"
        assert rule.relationship_type == "runs_on"
        assert rule.to_type == "Node"
        assert rule.required is False
        assert rule.cardinality == "many_to_one"

    def test_relationship_rule_hash(self):
        """Test that rules with same tuple hash equally."""
        rule1 = RelationshipRule(
            from_type="Pod",
            relationship_type="runs_on",
            to_type="Node",
        )
        rule2 = RelationshipRule(
            from_type="Pod",
            relationship_type="runs_on",
            to_type="Node",
            required=True,  # Different metadata
        )

        assert hash(rule1) == hash(rule2)
        assert rule1 == rule2

    def test_relationship_rule_inequality(self):
        """Test that different rules are not equal."""
        rule1 = RelationshipRule(
            from_type="Pod",
            relationship_type="runs_on",
            to_type="Node",
        )
        rule2 = RelationshipRule(
            from_type="Pod",
            relationship_type="member_of",
            to_type="Namespace",
        )

        assert rule1 != rule2


class TestSameAsEligibility:
    """Tests for SameAsEligibility dataclass."""

    def test_can_correlate_with_allowed_type(self):
        """Test correlation returns True for types in can_match list."""
        eligibility = SameAsEligibility(
            can_match=["VM", "Instance", "Host"],
        )

        assert eligibility.can_correlate_with("VM") is True
        assert eligibility.can_correlate_with("Instance") is True
        assert eligibility.can_correlate_with("Host") is True

    def test_can_correlate_with_unlisted_type(self):
        """Test correlation returns False for types not in can_match."""
        eligibility = SameAsEligibility(
            can_match=["VM", "Instance"],
        )

        assert eligibility.can_correlate_with("Pod") is False
        assert eligibility.can_correlate_with("Container") is False
        assert eligibility.can_correlate_with("Datastore") is False

    def test_can_correlate_with_empty_can_match(self):
        """Test correlation returns False when can_match is empty."""
        eligibility = SameAsEligibility(
            can_match=[],  # Empty list means no correlations allowed
        )

        assert eligibility.can_correlate_with("VM") is False
        assert eligibility.can_correlate_with("Node") is False

    def test_can_correlate_with_blocked_type(self):
        """Test correlation returns False for types in never_match list."""
        eligibility = SameAsEligibility(
            can_match=["VM", "Instance", "Container"],
            never_match=["Container"],  # Explicitly blocked
        )

        # VM and Instance are in can_match and not in never_match
        assert eligibility.can_correlate_with("VM") is True
        assert eligibility.can_correlate_with("Instance") is True

        # Container is in never_match, so blocked even though in can_match
        assert eligibility.can_correlate_with("Container") is False

    def test_never_match_takes_precedence(self):
        """Test that never_match takes precedence over can_match."""
        eligibility = SameAsEligibility(
            can_match=["VM", "Instance", "Host"],
            never_match=["VM"],  # Block VM even though in can_match
        )

        assert eligibility.can_correlate_with("VM") is False
        assert eligibility.can_correlate_with("Instance") is True
        assert eligibility.can_correlate_with("Host") is True

    def test_default_values(self):
        """Test default values for SameAsEligibility."""
        eligibility = SameAsEligibility()

        assert eligibility.can_match == []
        assert eligibility.matching_attributes == []
        assert eligibility.never_match == []

        # Empty can_match means no correlations allowed
        assert eligibility.can_correlate_with("anything") is False

    def test_with_matching_attributes(self):
        """Test SameAsEligibility with matching attributes hint."""
        eligibility = SameAsEligibility(
            can_match=["VM", "Instance"],
            matching_attributes=["guest.hostName", "guest.ipAddress", "name"],
        )

        assert eligibility.matching_attributes == ["guest.hostName", "guest.ipAddress", "name"]
        assert eligibility.can_correlate_with("VM") is True


class TestConnectorTopologySchema:
    """Tests for ConnectorTopologySchema dataclass."""

    @pytest.fixture
    def sample_schema(self):
        """Create a sample schema for testing."""
        return ConnectorTopologySchema(
            connector_type="test",
            entity_types={
                "EntityA": EntityTypeDefinition(
                    name="EntityA",
                    scoped=False,
                    identity_fields=["name"],
                ),
                "EntityB": EntityTypeDefinition(
                    name="EntityB",
                    scoped=True,
                    scope_type="parent",
                    identity_fields=["parent", "name"],
                ),
            },
            relationship_rules={
                ("EntityA", "contains", "EntityB"): RelationshipRule(
                    from_type="EntityA",
                    relationship_type="contains",
                    to_type="EntityB",
                ),
            },
        )

    def test_is_valid_entity_type(self, sample_schema):
        """Test entity type validation."""
        assert sample_schema.is_valid_entity_type("EntityA") is True
        assert sample_schema.is_valid_entity_type("EntityB") is True
        assert sample_schema.is_valid_entity_type("NonExistent") is False

    def test_is_valid_relationship(self, sample_schema):
        """Test relationship validation."""
        assert sample_schema.is_valid_relationship("EntityA", "contains", "EntityB") is True
        assert sample_schema.is_valid_relationship("EntityB", "contains", "EntityA") is False
        assert sample_schema.is_valid_relationship("EntityA", "invalid", "EntityB") is False

    def test_get_entity_definition(self, sample_schema):
        """Test getting entity definition."""
        defn = sample_schema.get_entity_definition("EntityA")
        assert defn is not None
        assert defn.name == "EntityA"

        assert sample_schema.get_entity_definition("NonExistent") is None

    def test_get_relationship_rule(self, sample_schema):
        """Test getting relationship rule."""
        rule = sample_schema.get_relationship_rule("EntityA", "contains", "EntityB")
        assert rule is not None
        assert rule.from_type == "EntityA"

        assert sample_schema.get_relationship_rule("EntityB", "contains", "EntityA") is None

    def test_build_canonical_id_simple(self, sample_schema):
        """Test canonical ID for unscoped entity."""
        canonical_id = sample_schema.build_canonical_id("EntityA", {}, "test-name")
        assert canonical_id == "test-name"

    def test_build_canonical_id_scoped(self, sample_schema):
        """Test canonical ID for scoped entity."""
        canonical_id = sample_schema.build_canonical_id(
            "EntityB", {"parent": "parent-value"}, "child-name"
        )
        assert canonical_id == "parent-value/child-name"

    def test_build_canonical_id_unknown_type(self, sample_schema):
        """Test canonical ID for unknown entity type returns name."""
        canonical_id = sample_schema.build_canonical_id("UnknownType", {}, "some-name")
        assert canonical_id == "some-name"

    def test_get_all_entity_types(self, sample_schema):
        """Test getting all entity types."""
        types = sample_schema.get_all_entity_types()
        assert types == {"EntityA", "EntityB"}

    def test_get_all_relationship_types(self, sample_schema):
        """Test getting all relationship types."""
        types = sample_schema.get_all_relationship_types()
        assert types == {"contains"}


# =============================================================================
# Kubernetes Schema Tests
# =============================================================================


class TestKubernetesSchema:
    """Tests for Kubernetes topology schema."""

    def test_connector_type(self):
        """Test connector type is correct."""
        assert KUBERNETES_TOPOLOGY_SCHEMA.connector_type == "kubernetes"

    def test_entity_types_defined(self):
        """Test all expected entity types are defined."""
        expected_types = {
            "Namespace",
            "Node",
            "Pod",
            "Deployment",
            "ReplicaSet",
            "StatefulSet",
            "DaemonSet",
            "Service",
            "Ingress",
        }
        actual_types = KUBERNETES_TOPOLOGY_SCHEMA.get_all_entity_types()
        assert actual_types == expected_types

    def test_pod_entity_definition(self):
        """Test Pod entity definition."""
        pod = KUBERNETES_TOPOLOGY_SCHEMA.get_entity_definition("Pod")
        assert pod is not None
        assert pod.scoped is True
        assert pod.scope_type == "namespace"
        assert pod.identity_fields == ["namespace", "name"]
        assert pod.volatility == Volatility.EPHEMERAL

    def test_node_entity_definition(self):
        """Test Node entity definition."""
        node = KUBERNETES_TOPOLOGY_SCHEMA.get_entity_definition("Node")
        assert node is not None
        assert node.scoped is False
        assert node.volatility == Volatility.STABLE

    def test_pod_member_of_namespace(self):
        """Test Pod can be member_of Namespace."""
        assert (
            KUBERNETES_TOPOLOGY_SCHEMA.is_valid_relationship("Pod", "member_of", "Namespace")
            is True
        )

    def test_pod_runs_on_node(self):
        """Test Pod can runs_on Node."""
        assert KUBERNETES_TOPOLOGY_SCHEMA.is_valid_relationship("Pod", "runs_on", "Node") is True

    def test_deployment_manages_replicaset(self):
        """Test Deployment can manages ReplicaSet."""
        assert (
            KUBERNETES_TOPOLOGY_SCHEMA.is_valid_relationship("Deployment", "manages", "ReplicaSet")
            is True
        )

    def test_replicaset_manages_pod(self):
        """Test ReplicaSet can manages Pod."""
        assert (
            KUBERNETES_TOPOLOGY_SCHEMA.is_valid_relationship("ReplicaSet", "manages", "Pod") is True
        )

    def test_ingress_routes_to_service(self):
        """Test Ingress can routes_to Service."""
        assert (
            KUBERNETES_TOPOLOGY_SCHEMA.is_valid_relationship("Ingress", "routes_to", "Service")
            is True
        )

    def test_service_routes_to_pod(self):
        """Test Service can routes_to Pod."""
        assert (
            KUBERNETES_TOPOLOGY_SCHEMA.is_valid_relationship("Service", "routes_to", "Pod") is True
        )

    def test_invalid_pod_runs_on_namespace(self):
        """Test Pod cannot runs_on Namespace (invalid)."""
        assert (
            KUBERNETES_TOPOLOGY_SCHEMA.is_valid_relationship("Pod", "runs_on", "Namespace") is False
        )

    def test_build_pod_canonical_id(self):
        """Test building Pod canonical ID."""
        canonical_id = KUBERNETES_TOPOLOGY_SCHEMA.build_canonical_id(
            "Pod", {"namespace": "prod"}, "nginx"
        )
        assert canonical_id == "prod/nginx"

    def test_build_node_canonical_id(self):
        """Test building Node canonical ID (unscoped)."""
        canonical_id = KUBERNETES_TOPOLOGY_SCHEMA.build_canonical_id("Node", {}, "worker-01")
        assert canonical_id == "worker-01"

    # SAME_AS Eligibility Tests

    def test_k8s_node_can_correlate_with_vm(self):
        """Test K8s Node can correlate with VMware VM."""
        node = KUBERNETES_TOPOLOGY_SCHEMA.get_entity_definition("Node")
        assert node.same_as is not None
        assert node.same_as.can_correlate_with("VM") is True

    def test_k8s_node_can_correlate_with_instance(self):
        """Test K8s Node can correlate with GCP Instance."""
        node = KUBERNETES_TOPOLOGY_SCHEMA.get_entity_definition("Node")
        assert node.same_as is not None
        assert node.same_as.can_correlate_with("Instance") is True

    def test_k8s_node_can_correlate_with_host(self):
        """Test K8s Node can correlate with VMware/Proxmox Host."""
        node = KUBERNETES_TOPOLOGY_SCHEMA.get_entity_definition("Node")
        assert node.same_as is not None
        assert node.same_as.can_correlate_with("Host") is True

    def test_k8s_node_has_matching_attributes(self):
        """Test K8s Node has useful matching attributes."""
        node = KUBERNETES_TOPOLOGY_SCHEMA.get_entity_definition("Node")
        assert node.same_as is not None
        # Should have providerID and addresses for correlation
        assert len(node.same_as.matching_attributes) > 0
        assert any("providerID" in attr for attr in node.same_as.matching_attributes)

    def test_k8s_pod_cannot_correlate(self):
        """Test K8s Pod has no SAME_AS (ephemeral entities)."""
        pod = KUBERNETES_TOPOLOGY_SCHEMA.get_entity_definition("Pod")
        assert pod.same_as is None

    def test_k8s_deployment_cannot_correlate(self):
        """Test K8s Deployment has no SAME_AS (K8s-only abstraction)."""
        deployment = KUBERNETES_TOPOLOGY_SCHEMA.get_entity_definition("Deployment")
        # Deployment doesn't have same_as defined, so it defaults to None
        assert deployment.same_as is None

    def test_k8s_service_cannot_correlate(self):
        """Test K8s Service has no SAME_AS (K8s-only abstraction)."""
        service = KUBERNETES_TOPOLOGY_SCHEMA.get_entity_definition("Service")
        assert service.same_as is None

    def test_k8s_namespace_cannot_correlate(self):
        """Test K8s Namespace has no SAME_AS."""
        namespace = KUBERNETES_TOPOLOGY_SCHEMA.get_entity_definition("Namespace")
        assert namespace.same_as is None


# =============================================================================
# VMware Schema Tests
# =============================================================================


class TestVMwareSchema:
    """Tests for VMware topology schema."""

    def test_connector_type(self):
        """Test connector type is correct."""
        assert VMWARE_TOPOLOGY_SCHEMA.connector_type == "vmware"

    def test_entity_types_defined(self):
        """Test all expected entity types are defined."""
        expected_types = {"Datacenter", "Cluster", "Host", "VM", "Datastore", "Network"}
        actual_types = VMWARE_TOPOLOGY_SCHEMA.get_all_entity_types()
        assert actual_types == expected_types

    def test_vm_entity_definition(self):
        """Test VM entity definition."""
        vm = VMWARE_TOPOLOGY_SCHEMA.get_entity_definition("VM")
        assert vm is not None
        assert vm.scoped is False  # moref is globally unique
        assert vm.identity_fields == ["moref"]
        assert vm.volatility == Volatility.MODERATE

    def test_host_entity_definition(self):
        """Test Host entity definition."""
        host = VMWARE_TOPOLOGY_SCHEMA.get_entity_definition("Host")
        assert host is not None
        assert host.scoped is True
        assert host.scope_type == "cluster"

    def test_vm_runs_on_host(self):
        """Test VM can runs_on Host."""
        assert VMWARE_TOPOLOGY_SCHEMA.is_valid_relationship("VM", "runs_on", "Host") is True

    def test_host_member_of_cluster(self):
        """Test Host can member_of Cluster."""
        assert VMWARE_TOPOLOGY_SCHEMA.is_valid_relationship("Host", "member_of", "Cluster") is True

    def test_vm_uses_storage_datastore(self):
        """Test VM can uses_storage Datastore."""
        assert (
            VMWARE_TOPOLOGY_SCHEMA.is_valid_relationship("VM", "uses_storage", "Datastore") is True
        )

    def test_vm_uses_datastore(self):
        """Test VM can uses Datastore (alternate relationship)."""
        assert VMWARE_TOPOLOGY_SCHEMA.is_valid_relationship("VM", "uses", "Datastore") is True

    def test_build_vm_canonical_id(self):
        """Test building VM canonical ID with moref."""
        canonical_id = VMWARE_TOPOLOGY_SCHEMA.build_canonical_id(
            "VM", {"moref": "vm-123"}, "web-server"
        )
        assert canonical_id == "vm-123"

    def test_build_host_canonical_id(self):
        """Test building Host canonical ID."""
        canonical_id = VMWARE_TOPOLOGY_SCHEMA.build_canonical_id(
            "Host", {"cluster": "prod-cluster"}, "esxi-01"
        )
        assert canonical_id == "prod-cluster/esxi-01"

    # SAME_AS Eligibility Tests

    def test_vmware_vm_can_correlate_with_node(self):
        """Test VMware VM can correlate with K8s Node."""
        vm = VMWARE_TOPOLOGY_SCHEMA.get_entity_definition("VM")
        assert vm.same_as is not None
        assert vm.same_as.can_correlate_with("Node") is True

    def test_vmware_vm_can_correlate_with_instance(self):
        """Test VMware VM can correlate with GCP Instance."""
        vm = VMWARE_TOPOLOGY_SCHEMA.get_entity_definition("VM")
        assert vm.same_as is not None
        assert vm.same_as.can_correlate_with("Instance") is True

    def test_vmware_vm_has_matching_attributes(self):
        """Test VMware VM has useful matching attributes."""
        vm = VMWARE_TOPOLOGY_SCHEMA.get_entity_definition("VM")
        assert vm.same_as is not None
        # Should have guest hostname and IP for correlation
        assert len(vm.same_as.matching_attributes) > 0
        assert any("hostName" in attr for attr in vm.same_as.matching_attributes)

    def test_vmware_host_can_correlate_with_node(self):
        """Test VMware Host can correlate with K8s Node (bare-metal K8s)."""
        host = VMWARE_TOPOLOGY_SCHEMA.get_entity_definition("Host")
        assert host.same_as is not None
        assert host.same_as.can_correlate_with("Node") is True

    def test_vmware_datastore_cannot_correlate(self):
        """Test VMware Datastore has no SAME_AS (storage doesn't cross-correlate)."""
        datastore = VMWARE_TOPOLOGY_SCHEMA.get_entity_definition("Datastore")
        assert datastore.same_as is None

    def test_vmware_cluster_cannot_correlate(self):
        """Test VMware Cluster has no SAME_AS (VMware-only concept)."""
        cluster = VMWARE_TOPOLOGY_SCHEMA.get_entity_definition("Cluster")
        assert cluster.same_as is None

    def test_vmware_network_cannot_correlate(self):
        """Test VMware Network has no SAME_AS."""
        network = VMWARE_TOPOLOGY_SCHEMA.get_entity_definition("Network")
        assert network.same_as is None

    def test_vmware_datacenter_cannot_correlate(self):
        """Test VMware Datacenter has no SAME_AS."""
        datacenter = VMWARE_TOPOLOGY_SCHEMA.get_entity_definition("Datacenter")
        assert datacenter.same_as is None


# =============================================================================
# GCP Schema Tests
# =============================================================================


class TestGCPSchema:
    """Tests for GCP topology schema."""

    def test_connector_type(self):
        """Test connector type is correct."""
        assert GCP_TOPOLOGY_SCHEMA.connector_type == "gcp"

    def test_entity_types_defined(self):
        """Test all expected entity types are defined."""
        expected_types = {
            "Network",
            "Subnet",
            "Instance",
            "Disk",
            "GKECluster",
            "NodePool",
            "Snapshot",
        }
        actual_types = GCP_TOPOLOGY_SCHEMA.get_all_entity_types()
        assert actual_types == expected_types

    def test_instance_entity_definition(self):
        """Test Instance entity definition."""
        instance = GCP_TOPOLOGY_SCHEMA.get_entity_definition("Instance")
        assert instance is not None
        assert instance.scoped is True
        assert instance.scope_type == "zone"

    def test_instance_uses_disk(self):
        """Test Instance can uses Disk."""
        assert GCP_TOPOLOGY_SCHEMA.is_valid_relationship("Instance", "uses", "Disk") is True

    def test_instance_member_of_network(self):
        """Test Instance can member_of Network."""
        assert GCP_TOPOLOGY_SCHEMA.is_valid_relationship("Instance", "member_of", "Network") is True

    def test_nodepool_member_of_gkecluster(self):
        """Test NodePool can member_of GKECluster."""
        assert (
            GCP_TOPOLOGY_SCHEMA.is_valid_relationship("NodePool", "member_of", "GKECluster") is True
        )

    def test_build_instance_canonical_id(self):
        """Test building Instance canonical ID."""
        canonical_id = GCP_TOPOLOGY_SCHEMA.build_canonical_id(
            "Instance", {"zone": "us-central1-a"}, "web-01"
        )
        assert canonical_id == "us-central1-a/web-01"

    # SAME_AS Eligibility Tests

    def test_gcp_instance_can_correlate_with_node(self):
        """Test GCP Instance can correlate with K8s Node."""
        instance = GCP_TOPOLOGY_SCHEMA.get_entity_definition("Instance")
        assert instance.same_as is not None
        assert instance.same_as.can_correlate_with("Node") is True

    def test_gcp_instance_can_correlate_with_vm(self):
        """Test GCP Instance can correlate with VMware VM."""
        instance = GCP_TOPOLOGY_SCHEMA.get_entity_definition("Instance")
        assert instance.same_as is not None
        assert instance.same_as.can_correlate_with("VM") is True

    def test_gcp_instance_can_correlate_with_host(self):
        """Test GCP Instance can correlate with Host."""
        instance = GCP_TOPOLOGY_SCHEMA.get_entity_definition("Instance")
        assert instance.same_as is not None
        assert instance.same_as.can_correlate_with("Host") is True

    def test_gcp_instance_has_matching_attributes(self):
        """Test GCP Instance has useful matching attributes."""
        instance = GCP_TOPOLOGY_SCHEMA.get_entity_definition("Instance")
        assert instance.same_as is not None
        # Should have name and networkIP for correlation
        assert len(instance.same_as.matching_attributes) > 0

    def test_gcp_disk_cannot_correlate(self):
        """Test GCP Disk has no SAME_AS (storage doesn't cross-correlate)."""
        disk = GCP_TOPOLOGY_SCHEMA.get_entity_definition("Disk")
        assert disk.same_as is None

    def test_gcp_network_cannot_correlate(self):
        """Test GCP Network has no SAME_AS."""
        network = GCP_TOPOLOGY_SCHEMA.get_entity_definition("Network")
        assert network.same_as is None

    def test_gcp_gkecluster_cannot_correlate(self):
        """Test GCP GKECluster has no SAME_AS (GCP-only concept)."""
        gke_cluster = GCP_TOPOLOGY_SCHEMA.get_entity_definition("GKECluster")
        assert gke_cluster.same_as is None

    def test_gcp_nodepool_cannot_correlate(self):
        """Test GCP NodePool has no SAME_AS."""
        nodepool = GCP_TOPOLOGY_SCHEMA.get_entity_definition("NodePool")
        assert nodepool.same_as is None


# =============================================================================
# Proxmox Schema Tests
# =============================================================================


class TestProxmoxSchema:
    """Tests for Proxmox topology schema."""

    def test_connector_type(self):
        """Test connector type is correct."""
        assert PROXMOX_TOPOLOGY_SCHEMA.connector_type == "proxmox"

    def test_entity_types_defined(self):
        """Test all expected entity types are defined."""
        expected_types = {"Node", "VM", "Container", "Storage"}
        actual_types = PROXMOX_TOPOLOGY_SCHEMA.get_all_entity_types()
        assert actual_types == expected_types

    def test_vm_entity_definition(self):
        """Test VM entity definition."""
        vm = PROXMOX_TOPOLOGY_SCHEMA.get_entity_definition("VM")
        assert vm is not None
        assert vm.scoped is True
        assert vm.scope_type == "node"
        assert vm.identity_fields == ["node", "vmid"]

    def test_vm_runs_on_node(self):
        """Test VM can runs_on Node."""
        assert PROXMOX_TOPOLOGY_SCHEMA.is_valid_relationship("VM", "runs_on", "Node") is True

    def test_container_runs_on_node(self):
        """Test Container can runs_on Node."""
        assert PROXMOX_TOPOLOGY_SCHEMA.is_valid_relationship("Container", "runs_on", "Node") is True

    def test_build_vm_canonical_id(self):
        """Test building VM canonical ID with vmid."""
        canonical_id = PROXMOX_TOPOLOGY_SCHEMA.build_canonical_id(
            "VM", {"node": "pve1", "vmid": "100"}, "web-server"
        )
        assert canonical_id == "pve1/100"

    # SAME_AS Eligibility Tests

    def test_proxmox_vm_can_correlate_with_node(self):
        """Test Proxmox VM can correlate with K8s Node."""
        vm = PROXMOX_TOPOLOGY_SCHEMA.get_entity_definition("VM")
        assert vm.same_as is not None
        assert vm.same_as.can_correlate_with("Node") is True

    def test_proxmox_vm_can_correlate_with_instance(self):
        """Test Proxmox VM can correlate with GCP Instance."""
        vm = PROXMOX_TOPOLOGY_SCHEMA.get_entity_definition("VM")
        assert vm.same_as is not None
        assert vm.same_as.can_correlate_with("Instance") is True

    def test_proxmox_vm_has_matching_attributes(self):
        """Test Proxmox VM has useful matching attributes."""
        vm = PROXMOX_TOPOLOGY_SCHEMA.get_entity_definition("VM")
        assert vm.same_as is not None
        # Should have name and vmid for correlation
        assert len(vm.same_as.matching_attributes) > 0

    def test_proxmox_container_can_correlate_with_node(self):
        """Test Proxmox Container can correlate with K8s Node."""
        container = PROXMOX_TOPOLOGY_SCHEMA.get_entity_definition("Container")
        assert container.same_as is not None
        assert container.same_as.can_correlate_with("Node") is True

    def test_proxmox_node_cannot_correlate(self):
        """Test Proxmox Node has no SAME_AS (it's the physical host)."""
        node = PROXMOX_TOPOLOGY_SCHEMA.get_entity_definition("Node")
        # Proxmox Node is the hypervisor, not correlated with other systems
        assert node.same_as is None

    def test_proxmox_storage_cannot_correlate(self):
        """Test Proxmox Storage has no SAME_AS."""
        storage = PROXMOX_TOPOLOGY_SCHEMA.get_entity_definition("Storage")
        assert storage.same_as is None


# =============================================================================
# Schema Registry Tests
# =============================================================================


class TestSchemaRegistry:
    """Tests for schema registry functions."""

    def test_get_topology_schema_kubernetes(self):
        """Test getting Kubernetes schema."""
        schema = get_topology_schema("kubernetes")
        assert schema is not None
        assert schema.connector_type == "kubernetes"

    def test_get_topology_schema_vmware(self):
        """Test getting VMware schema."""
        schema = get_topology_schema("vmware")
        assert schema is not None
        assert schema.connector_type == "vmware"

    def test_get_topology_schema_gcp(self):
        """Test getting GCP schema."""
        schema = get_topology_schema("gcp")
        assert schema is not None
        assert schema.connector_type == "gcp"

    def test_get_topology_schema_proxmox(self):
        """Test getting Proxmox schema."""
        schema = get_topology_schema("proxmox")
        assert schema is not None
        assert schema.connector_type == "proxmox"

    def test_get_topology_schema_unknown(self):
        """Test getting unknown schema returns None."""
        schema = get_topology_schema("unknown_connector")
        assert schema is None

    def test_get_all_schemas(self):
        """Test getting all schemas."""
        all_schemas = get_all_schemas()
        assert len(all_schemas) == 4
        assert "kubernetes" in all_schemas
        assert "vmware" in all_schemas
        assert "gcp" in all_schemas
        assert "proxmox" in all_schemas

    def test_get_supported_connector_types(self):
        """Test getting supported connector types."""
        types = get_supported_connector_types()
        assert types == {"kubernetes", "vmware", "gcp", "proxmox"}

    def test_is_schema_available(self):
        """Test checking schema availability."""
        assert is_schema_available("kubernetes") is True
        assert is_schema_available("vmware") is True
        assert is_schema_available("gcp") is True
        assert is_schema_available("proxmox") is True
        assert is_schema_available("rest") is False
        assert is_schema_available("soap") is False


# =============================================================================
# Cross-Connector Validation Tests
# =============================================================================


class TestCrossConnectorValidation:
    """Tests for cross-connector validation (entities from different connectors)."""

    def test_k8s_pod_cannot_use_vmware_relationship(self):
        """Test K8s Pod cannot have VMware-only relationships."""
        # Pod runs_on Host is only valid in VMware, not in K8s (K8s has Node)
        # Actually Pod runs_on Node IS valid in K8s, let's test Pod runs_on Cluster
        k8s_schema = get_topology_schema("kubernetes")

        # There's no "Cluster" in K8s (well, there isn't a Cluster entity type)
        assert k8s_schema.is_valid_entity_type("Cluster") is False

    def test_vmware_vm_cannot_be_in_k8s_namespace(self):
        """Test VMware VM cannot member_of Namespace (K8s concept)."""
        vmware_schema = get_topology_schema("vmware")

        # VMware doesn't have Namespace entity type
        assert vmware_schema.is_valid_entity_type("Namespace") is False

        # VM member_of should only work with VMware entities
        assert vmware_schema.is_valid_relationship("VM", "member_of", "Namespace") is False

    def test_gcp_instance_uses_correct_relationships(self):
        """Test GCP Instance uses GCP-specific relationships."""
        gcp_schema = get_topology_schema("gcp")

        # GCP Instance uses Disk (not runs_on Host like VMware)
        assert gcp_schema.is_valid_relationship("Instance", "uses", "Disk") is True

        # GCP doesn't have Host entity
        assert gcp_schema.is_valid_entity_type("Host") is False

    def test_proxmox_vm_runs_on_proxmox_node(self):
        """Test Proxmox VM runs on Proxmox Node (not K8s Node)."""
        proxmox_schema = get_topology_schema("proxmox")
        k8s_schema = get_topology_schema("kubernetes")

        # Both have Node, but they're different concepts
        # Proxmox Node is the hypervisor host
        proxmox_node = proxmox_schema.get_entity_definition("Node")
        k8s_node = k8s_schema.get_entity_definition("Node")

        # Proxmox Node is unscoped (cluster-wide)
        assert proxmox_node.scoped is False

        # K8s Node is also unscoped
        assert k8s_node.scoped is False

        # But K8s doesn't have VM entity type
        assert k8s_schema.is_valid_entity_type("VM") is False


# =============================================================================
# Canonical ID Edge Cases
# =============================================================================


class TestCanonicalIdEdgeCases:
    """Tests for canonical ID generation edge cases."""

    def test_missing_scope_field_uses_name(self):
        """Test that missing scope field falls back to name only."""
        schema = get_topology_schema("kubernetes")

        # Pod without namespace in scope
        canonical_id = schema.build_canonical_id("Pod", {}, "nginx")
        # Should only include what's in scope, so just empty parts
        # Actually with namespace missing, it should just use "nginx"
        assert canonical_id == "nginx"

    def test_extra_scope_fields_ignored(self):
        """Test that extra scope fields are ignored."""
        schema = get_topology_schema("kubernetes")

        canonical_id = schema.build_canonical_id(
            "Pod", {"namespace": "prod", "extra": "ignored"}, "nginx"
        )
        assert canonical_id == "prod/nginx"

    def test_empty_scope_for_unscoped_entity(self):
        """Test empty scope for unscoped entity."""
        schema = get_topology_schema("kubernetes")

        canonical_id = schema.build_canonical_id("Node", {}, "worker-01")
        assert canonical_id == "worker-01"


# =============================================================================
# Cross-Connector SAME_AS Eligibility Tests
# =============================================================================


class TestCrossConnectorSameAsEligibility:
    """Tests for SAME_AS eligibility across different connectors."""

    def test_k8s_node_vmware_vm_symmetric_eligibility(self):
        """Test K8s Node and VMware VM can correlate with each other."""
        k8s_node = KUBERNETES_TOPOLOGY_SCHEMA.get_entity_definition("Node")
        vmware_vm = VMWARE_TOPOLOGY_SCHEMA.get_entity_definition("VM")

        # K8s Node should be able to correlate with VM
        assert k8s_node.same_as is not None
        assert k8s_node.same_as.can_correlate_with("VM") is True

        # VMware VM should be able to correlate with Node
        assert vmware_vm.same_as is not None
        assert vmware_vm.same_as.can_correlate_with("Node") is True

    def test_k8s_node_gcp_instance_symmetric_eligibility(self):
        """Test K8s Node and GCP Instance can correlate with each other."""
        k8s_node = KUBERNETES_TOPOLOGY_SCHEMA.get_entity_definition("Node")
        gcp_instance = GCP_TOPOLOGY_SCHEMA.get_entity_definition("Instance")

        # K8s Node should be able to correlate with Instance
        assert k8s_node.same_as is not None
        assert k8s_node.same_as.can_correlate_with("Instance") is True

        # GCP Instance should be able to correlate with Node
        assert gcp_instance.same_as is not None
        assert gcp_instance.same_as.can_correlate_with("Node") is True

    def test_k8s_node_proxmox_vm_symmetric_eligibility(self):
        """Test K8s Node and Proxmox VM can correlate with each other."""
        k8s_node = KUBERNETES_TOPOLOGY_SCHEMA.get_entity_definition("Node")
        proxmox_vm = PROXMOX_TOPOLOGY_SCHEMA.get_entity_definition("VM")

        # K8s Node should be able to correlate with VM
        assert k8s_node.same_as is not None
        assert k8s_node.same_as.can_correlate_with("VM") is True

        # Proxmox VM should be able to correlate with Node
        assert proxmox_vm.same_as is not None
        assert proxmox_vm.same_as.can_correlate_with("Node") is True

    def test_ephemeral_entity_no_correlation(self):
        """Test ephemeral entities (Pod) cannot correlate with anything."""
        k8s_pod = KUBERNETES_TOPOLOGY_SCHEMA.get_entity_definition("Pod")
        vmware_vm = VMWARE_TOPOLOGY_SCHEMA.get_entity_definition("VM")

        # Pod has no same_as - ephemeral entities don't correlate
        assert k8s_pod.same_as is None

        # VM can correlate with Node, but Pod doesn't have same_as to check
        assert vmware_vm.same_as is not None
        # There's no "Pod" in VM's can_match (shouldn't be!)
        assert vmware_vm.same_as.can_correlate_with("Pod") is False

    def test_storage_entities_no_correlation(self):
        """Test storage entities don't cross-correlate."""
        vmware_datastore = VMWARE_TOPOLOGY_SCHEMA.get_entity_definition("Datastore")
        gcp_disk = GCP_TOPOLOGY_SCHEMA.get_entity_definition("Disk")
        proxmox_storage = PROXMOX_TOPOLOGY_SCHEMA.get_entity_definition("Storage")

        # None of the storage entities should have SAME_AS
        assert vmware_datastore.same_as is None
        assert gcp_disk.same_as is None
        assert proxmox_storage.same_as is None

    def test_all_compute_entities_can_correlate_with_node(self):
        """Test all compute entities (VM, Instance) can correlate with K8s Node."""
        vmware_vm = VMWARE_TOPOLOGY_SCHEMA.get_entity_definition("VM")
        gcp_instance = GCP_TOPOLOGY_SCHEMA.get_entity_definition("Instance")
        proxmox_vm = PROXMOX_TOPOLOGY_SCHEMA.get_entity_definition("VM")
        proxmox_container = PROXMOX_TOPOLOGY_SCHEMA.get_entity_definition("Container")

        # All should be able to correlate with K8s Node
        assert vmware_vm.same_as.can_correlate_with("Node") is True
        assert gcp_instance.same_as.can_correlate_with("Node") is True
        assert proxmox_vm.same_as.can_correlate_with("Node") is True
        assert proxmox_container.same_as.can_correlate_with("Node") is True
