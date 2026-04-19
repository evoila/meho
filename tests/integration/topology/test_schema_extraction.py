# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for schema-based topology extraction.

Tests the SchemaBasedExtractor for correct behavior and edge cases.

These tests ensure that:
1. Schema extraction produces correct output for Kubernetes and VMware
2. Edge cases (empty responses, errors, single items) are handled correctly
3. Relationships and stub entities are created correctly
4. Entity type and scope information is properly extracted
"""

from typing import Any

import pytest

from meho_app.modules.topology.extraction import (
    get_schema_extractor,
    reset_schema_extractor,
)

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def schema_extractor():
    """Get a fresh schema-based extractor."""
    reset_schema_extractor()
    return get_schema_extractor()


# =============================================================================
# Sample Data
# =============================================================================


def make_k8s_pod(name: str, namespace: str = "default", node: str | None = None) -> dict[str, Any]:
    """Create sample K8s Pod data."""
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app": name},
        },
        "spec": {},
        "status": {
            "phase": "Running",
            "podIP": f"10.0.0.{hash(name) % 256}",
        },
    }
    if node:
        pod["spec"]["nodeName"] = node
    return pod


def make_k8s_pod_list(pods: list[dict[str, Any]]) -> dict[str, Any]:
    """Create K8s PodList response."""
    return {
        "apiVersion": "v1",
        "kind": "PodList",
        "items": pods,
    }


def make_k8s_deployment(name: str, namespace: str = "default", replicas: int = 3) -> dict[str, Any]:
    """Create sample K8s Deployment data."""
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app": name},
        },
        "spec": {
            "replicas": replicas,
            "strategy": {"type": "RollingUpdate"},
        },
        "status": {
            "readyReplicas": replicas,
            "availableReplicas": replicas,
        },
    }


def make_k8s_ingress(
    name: str,
    namespace: str = "default",
    services: list[str] | None = None,
) -> dict[str, Any]:
    """Create sample K8s Ingress data."""
    rules = []
    if services:
        paths = [{"backend": {"service": {"name": svc}}} for svc in services]
        rules.append(
            {
                "host": f"{name}.example.com",
                "http": {"paths": paths},
            }
        )

    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": name,
            "namespace": namespace,
        },
        "spec": {
            "ingressClassName": "nginx",
            "rules": rules,
        },
    }


def make_vmware_vm(
    name: str,
    host: str | None = None,
    datastores: list[str] | None = None,
) -> dict[str, Any]:
    """Create sample VMware VM data.

    Format matches what VMware connector serializer returns:
    - runtime.host is a string (host name)
    - datastores is an array of strings (datastore names)
    """
    vm = {
        "name": name,
        "moref": f"vm-{hash(name) % 1000}",
        "config": {
            "num_cpu": 4,
            "memory_mb": 8192,
            "guest_full_name": "CentOS 7",
        },
        "runtime": {
            "power_state": "poweredOn",
        },
        "guest": {
            "ip_address": f"192.168.1.{hash(name) % 256}",
        },
    }
    if host:
        vm["runtime"]["host"] = host  # Host name as string
    if datastores:
        vm["datastores"] = datastores  # Array of datastore names
    return vm


def make_vmware_host(name: str, cluster: str | None = None) -> dict[str, Any]:
    """Create sample VMware Host data.

    Format matches what VMware connector serializer returns:
    - cluster is a string (cluster name)
    """
    host = {
        "name": name,
        "moref": f"host-{hash(name) % 1000}",
        "runtime": {
            "connection_state": "connected",
        },
        "hardware": {
            "num_cpu_cores": 32,
            "memory_size_bytes": 137438953472,
        },
    }
    if cluster:
        host["cluster"] = cluster  # Cluster name as string
    return host


# =============================================================================
# Schema Extractor Tests
# =============================================================================


class TestSchemaExtractionBasic:
    """Basic functionality tests for SchemaBasedExtractor."""

    def test_extract_returns_tuple(self, schema_extractor):
        """Test extract returns tuple of entities and relationships."""
        result = schema_extractor.extract(
            connector_type="kubernetes",
            operation_id=None,
            result_data={"kind": "PodList", "items": []},
            connector_id="test-id",
        )

        assert isinstance(result, tuple)
        assert len(result) == 2
        entities, relationships = result
        assert isinstance(entities, list)
        assert isinstance(relationships, list)

    def test_extract_unknown_connector_type(self, schema_extractor):
        """Test extract with unknown connector type returns empty."""
        entities, relationships = schema_extractor.extract(
            connector_type="unknown",
            operation_id="some_op",
            result_data={"data": "test"},
            connector_id="test-id",
        )

        assert entities == []
        assert relationships == []

    def test_extract_error_response(self, schema_extractor):
        """Test extract skips error responses."""
        entities, relationships = schema_extractor.extract(
            connector_type="kubernetes",
            operation_id=None,
            result_data={"error": "Some error", "kind": "PodList"},
            connector_id="test-id",
        )

        assert entities == []
        assert relationships == []

    def test_extract_none_data(self, schema_extractor):
        """Test extract handles None data."""
        entities, relationships = schema_extractor.extract(
            connector_type="kubernetes",
            operation_id=None,
            result_data=None,
            connector_id="test-id",
        )

        assert entities == []
        assert relationships == []


# =============================================================================
# Kubernetes Extraction Tests
# =============================================================================


class TestKubernetesSchemaExtraction:
    """Tests for Kubernetes schema-based extraction."""

    def test_extract_single_pod(self, schema_extractor):
        """Test extracting a single Pod."""
        pod_data = make_k8s_pod("nginx", namespace="production", node="worker-01")

        entities, relationships = schema_extractor.extract(
            connector_type="kubernetes",
            operation_id=None,
            result_data=pod_data,
            connector_id="k8s-123",
            connector_name="Prod K8s",
        )

        # Should have Pod + Namespace entities
        assert len(entities) >= 1

        # Find the Pod entity
        pod = next((e for e in entities if e.entity_type == "Pod"), None)
        assert pod is not None
        assert pod.name == "nginx"
        assert pod.entity_type == "Pod"
        assert pod.scope == {"namespace": "production"}
        assert pod.connector_id == "k8s-123"
        assert "nginx" in pod.description

        # Check relationships
        member_of_rels = [r for r in relationships if r.relationship_type == "member_of"]
        assert len(member_of_rels) >= 1
        assert any(r.to_entity_name == "production" for r in member_of_rels)

        runs_on_rels = [r for r in relationships if r.relationship_type == "runs_on"]
        assert len(runs_on_rels) == 1
        assert runs_on_rels[0].to_entity_name == "worker-01"

    def test_extract_pod_list(self, schema_extractor):
        """Test extracting PodList."""
        pods = [
            make_k8s_pod("nginx", namespace="default"),
            make_k8s_pod("redis", namespace="default"),
            make_k8s_pod("postgres", namespace="database"),
        ]
        pod_list = make_k8s_pod_list(pods)

        entities, _relationships = schema_extractor.extract(
            connector_type="kubernetes",
            operation_id=None,
            result_data=pod_list,
            connector_id="k8s-123",
        )

        # Should have 3 Pods + namespaces
        pod_entities = [e for e in entities if e.entity_type == "Pod"]
        assert len(pod_entities) == 3

        # Check all pods extracted
        pod_names = {p.name for p in pod_entities}
        assert pod_names == {"nginx", "redis", "postgres"}

        # Check namespace entities created
        ns_entities = [e for e in entities if e.entity_type == "Namespace"]
        ns_names = {n.name for n in ns_entities}
        assert "default" in ns_names
        assert "database" in ns_names

    def test_extract_deployment(self, schema_extractor):
        """Test extracting Deployment."""
        deploy = make_k8s_deployment("frontend", namespace="production", replicas=5)

        entities, relationships = schema_extractor.extract(
            connector_type="kubernetes",
            operation_id=None,
            result_data=deploy,
            connector_id="k8s-123",
        )

        deploy_entity = next((e for e in entities if e.entity_type == "Deployment"), None)
        assert deploy_entity is not None
        assert deploy_entity.name == "frontend"
        assert deploy_entity.scope == {"namespace": "production"}
        assert "frontend" in deploy_entity.description

        # Check member_of namespace relationship
        member_of = [r for r in relationships if r.relationship_type == "member_of"]
        assert len(member_of) == 1
        assert member_of[0].to_entity_name == "production"
        assert member_of[0].to_entity_type == "Namespace"

    def test_extract_ingress_with_services(self, schema_extractor):
        """Test extracting Ingress with routes_to relationships."""
        ingress = make_k8s_ingress(
            "main-ingress",
            namespace="production",
            services=["frontend-svc", "api-svc"],
        )

        entities, relationships = schema_extractor.extract(
            connector_type="kubernetes",
            operation_id=None,
            result_data=ingress,
            connector_id="k8s-123",
        )

        ingress_entity = next((e for e in entities if e.entity_type == "Ingress"), None)
        assert ingress_entity is not None
        assert ingress_entity.name == "main-ingress"

        # Check routes_to relationships
        routes_to = [r for r in relationships if r.relationship_type == "routes_to"]
        assert len(routes_to) == 2

        route_targets = {r.to_entity_name for r in routes_to}
        assert route_targets == {"frontend-svc", "api-svc"}

        # Check stub entities created for services
        service_stubs = [e for e in entities if e.entity_type == "Service"]
        assert len(service_stubs) == 2

    def test_entity_type_info_in_relationships(self, schema_extractor):
        """Test that relationships include entity type information."""
        pod = make_k8s_pod("nginx", namespace="default", node="worker-01")

        _entities, relationships = schema_extractor.extract(
            connector_type="kubernetes",
            operation_id=None,
            result_data=pod,
            connector_id="k8s-123",
        )

        for rel in relationships:
            assert rel.from_entity_type is not None
            assert rel.to_entity_type is not None

            if rel.relationship_type == "member_of":
                assert rel.from_entity_type == "Pod"
                assert rel.to_entity_type == "Namespace"
            elif rel.relationship_type == "runs_on":
                assert rel.from_entity_type == "Pod"
                assert rel.to_entity_type == "Node"


# =============================================================================
# VMware Extraction Tests
# =============================================================================


class TestVMwareSchemaExtraction:
    """Tests for VMware schema-based extraction."""

    def test_extract_vm_list(self, schema_extractor):
        """Test extracting list of VMs."""
        vms = [
            make_vmware_vm("web-01", host="esxi-01", datastores=["ds1"]),
            make_vmware_vm("db-01", host="esxi-02", datastores=["ds1", "ds2"]),
        ]

        entities, relationships = schema_extractor.extract(
            connector_type="vmware",
            operation_id="list_virtual_machines",
            result_data=vms,
            connector_id="vc-123",
            connector_name="Production vCenter",
        )

        # Check VMs extracted
        vm_entities = [e for e in entities if e.entity_type == "VM"]
        assert len(vm_entities) == 2

        vm_names = {v.name for v in vm_entities}
        assert vm_names == {"web-01", "db-01"}

        # Check runs_on relationships
        runs_on = [r for r in relationships if r.relationship_type == "runs_on"]
        assert len(runs_on) == 2

        # Check uses relationships for datastores
        uses = [r for r in relationships if r.relationship_type == "uses"]
        assert len(uses) == 3  # web-01->ds1, db-01->ds1, db-01->ds2

    def test_extract_single_vm(self, schema_extractor):
        """Test extracting single VM."""
        vm = make_vmware_vm("test-vm", host="esxi-01")

        entities, _relationships = schema_extractor.extract(
            connector_type="vmware",
            operation_id="get_virtual_machine",
            result_data=vm,
            connector_id="vc-123",
        )

        assert len(entities) >= 1
        vm_entity = next((e for e in entities if e.entity_type == "VM"), None)
        assert vm_entity is not None
        assert vm_entity.name == "test-vm"
        assert "test-vm" in vm_entity.description

    def test_extract_hosts(self, schema_extractor):
        """Test extracting hosts."""
        hosts = [
            make_vmware_host("esxi-01", cluster="prod-cluster"),
            make_vmware_host("esxi-02", cluster="prod-cluster"),
        ]

        entities, relationships = schema_extractor.extract(
            connector_type="vmware",
            operation_id="list_hosts",
            result_data=hosts,
            connector_id="vc-123",
        )

        host_entities = [e for e in entities if e.entity_type == "Host"]
        assert len(host_entities) == 2

        # Check member_of cluster relationships
        member_of = [r for r in relationships if r.relationship_type == "member_of"]
        assert len(member_of) >= 2
        assert all(r.to_entity_name == "prod-cluster" for r in member_of[:2])


# =============================================================================
# Edge Case Tests
# =============================================================================


class TestSchemaExtractionEdgeCases:
    """Edge case tests for schema-based extraction."""

    def test_empty_pod_list(self, schema_extractor):
        """Test extracting empty PodList."""
        entities, relationships = schema_extractor.extract(
            connector_type="kubernetes",
            operation_id=None,
            result_data={"kind": "PodList", "items": []},
            connector_id="k8s-123",
        )

        assert entities == []
        assert relationships == []

    def test_missing_fields(self, schema_extractor):
        """Test extraction with missing fields."""
        # Pod without nodeName
        pod = {
            "kind": "Pod",
            "metadata": {
                "name": "minimal-pod",
                "namespace": "default",
            },
            "spec": {},
            "status": {},
        }

        entities, relationships = schema_extractor.extract(
            connector_type="kubernetes",
            operation_id=None,
            result_data=pod,
            connector_id="k8s-123",
        )

        pod_entity = next((e for e in entities if e.entity_type == "Pod"), None)
        assert pod_entity is not None
        assert pod_entity.name == "minimal-pod"

        # No runs_on relationship since nodeName is missing
        runs_on = [r for r in relationships if r.relationship_type == "runs_on"]
        assert len(runs_on) == 0

    def test_wrapped_response(self, schema_extractor):
        """Test extraction from wrapped response."""
        wrapped = {
            "data": [make_vmware_vm("vm-01"), make_vmware_vm("vm-02")],
        }

        # This should be handled by the extractor
        entities, _relationships = schema_extractor.extract(
            connector_type="vmware",
            operation_id="list_virtual_machines",
            result_data=wrapped,
            connector_id="vc-123",
        )

        vm_entities = [e for e in entities if e.entity_type == "VM"]
        assert len(vm_entities) == 2

    def test_pod_without_name(self, schema_extractor):
        """Test extraction skips items without name."""
        pod = {
            "kind": "Pod",
            "metadata": {"namespace": "default"},  # No name
            "spec": {},
            "status": {},
        }

        entities, _relationships = schema_extractor.extract(
            connector_type="kubernetes",
            operation_id=None,
            result_data=pod,
            connector_id="k8s-123",
        )

        # Should not extract any entities
        pod_entities = [e for e in entities if e.entity_type == "Pod"]
        assert len(pod_entities) == 0

    def test_no_matching_rules(self, schema_extractor):
        """Test extraction with no matching rules."""
        entities, relationships = schema_extractor.extract(
            connector_type="kubernetes",
            operation_id="unknown_operation",
            result_data={"some": "data"},  # No kind field
            connector_id="k8s-123",
        )

        assert entities == []
        assert relationships == []


# =============================================================================
# Entity and Relationship Type Tests
# =============================================================================


class TestExtractedEntityTypes:
    """Tests for entity type and scope in extracted entities."""

    def test_pod_has_entity_type(self, schema_extractor):
        """Test Pod entity has correct entity_type."""
        pod = make_k8s_pod("nginx", namespace="default")

        entities, _ = schema_extractor.extract(
            connector_type="kubernetes",
            operation_id=None,
            result_data=pod,
            connector_id="k8s-123",
        )

        pod_entity = next((e for e in entities if e.name == "nginx"), None)
        assert pod_entity is not None
        assert pod_entity.entity_type == "Pod"

    def test_pod_has_scope(self, schema_extractor):
        """Test Pod entity has correct scope."""
        pod = make_k8s_pod("nginx", namespace="production")

        entities, _ = schema_extractor.extract(
            connector_type="kubernetes",
            operation_id=None,
            result_data=pod,
            connector_id="k8s-123",
        )

        pod_entity = next((e for e in entities if e.name == "nginx"), None)
        assert pod_entity is not None
        assert pod_entity.scope == {"namespace": "production"}

    def test_namespace_has_no_scope(self, schema_extractor):
        """Test Namespace entity has empty scope (cluster-scoped)."""
        pod = make_k8s_pod("nginx", namespace="default")

        entities, _ = schema_extractor.extract(
            connector_type="kubernetes",
            operation_id=None,
            result_data=pod,
            connector_id="k8s-123",
        )

        ns_entity = next((e for e in entities if e.entity_type == "Namespace"), None)
        assert ns_entity is not None
        assert ns_entity.scope == {}

    def test_vm_has_entity_type(self, schema_extractor):
        """Test VM entity has correct entity_type."""
        vm = make_vmware_vm("test-vm")

        entities, _ = schema_extractor.extract(
            connector_type="vmware",
            operation_id="list_virtual_machines",
            result_data=[vm],
            connector_id="vc-123",
        )

        vm_entity = next((e for e in entities if e.name == "test-vm"), None)
        assert vm_entity is not None
        assert vm_entity.entity_type == "VM"


# =============================================================================
# Stub Entity Tests
# =============================================================================


class TestStubEntityCreation:
    """Tests for stub entity creation."""

    def test_ingress_creates_service_stubs(self, schema_extractor):
        """Test Ingress creates stub entities for Services."""
        ingress = make_k8s_ingress(
            "test-ingress",
            namespace="default",
            services=["svc-a", "svc-b"],
        )

        entities, _ = schema_extractor.extract(
            connector_type="kubernetes",
            operation_id=None,
            result_data=ingress,
            connector_id="k8s-123",
        )

        service_stubs = [e for e in entities if e.entity_type == "Service"]
        assert len(service_stubs) == 2

        for stub in service_stubs:
            assert stub.raw_attributes.get("_stub") is True
            assert "(stub)" in stub.description

    def test_vm_creates_host_stubs(self, schema_extractor):
        """Test VM creates stub entity for Host."""
        vm = make_vmware_vm("test-vm", host="esxi-01")

        entities, _ = schema_extractor.extract(
            connector_type="vmware",
            operation_id="list_virtual_machines",
            result_data=[vm],
            connector_id="vc-123",
        )

        host_stubs = [e for e in entities if e.entity_type == "Host"]
        assert len(host_stubs) == 1
        assert host_stubs[0].name == "esxi-01"
        assert host_stubs[0].raw_attributes.get("_stub") is True

    def test_namespace_not_created_as_stub(self, schema_extractor):
        """Test Namespace is created specially, not as stub."""
        pod = make_k8s_pod("nginx", namespace="production")

        entities, _ = schema_extractor.extract(
            connector_type="kubernetes",
            operation_id=None,
            result_data=pod,
            connector_id="k8s-123",
        )

        ns_entity = next((e for e in entities if e.entity_type == "Namespace"), None)
        assert ns_entity is not None
        assert ns_entity.raw_attributes.get("_stub") is not True


# =============================================================================
# Deduplication Tests
# =============================================================================


class TestEntityDeduplication:
    """Tests for entity deduplication."""

    def test_duplicate_namespaces_deduped(self, schema_extractor):
        """Test duplicate namespaces are deduplicated."""
        pods = [
            make_k8s_pod("pod1", namespace="default"),
            make_k8s_pod("pod2", namespace="default"),
            make_k8s_pod("pod3", namespace="default"),
        ]
        pod_list = make_k8s_pod_list(pods)

        entities, _ = schema_extractor.extract(
            connector_type="kubernetes",
            operation_id=None,
            result_data=pod_list,
            connector_id="k8s-123",
        )

        ns_entities = [e for e in entities if e.entity_type == "Namespace"]
        assert len(ns_entities) == 1
        assert ns_entities[0].name == "default"

    def test_duplicate_stubs_deduped(self, schema_extractor):
        """Test duplicate stub entities are deduplicated."""
        vms = [
            make_vmware_vm("vm1", host="esxi-01"),
            make_vmware_vm("vm2", host="esxi-01"),
            make_vmware_vm("vm3", host="esxi-01"),
        ]

        entities, _ = schema_extractor.extract(
            connector_type="vmware",
            operation_id="list_virtual_machines",
            result_data=vms,
            connector_id="vc-123",
        )

        host_stubs = [e for e in entities if e.entity_type == "Host"]
        assert len(host_stubs) == 1
        assert host_stubs[0].name == "esxi-01"


# =============================================================================
# GCP Sample Data
# =============================================================================


def make_gcp_instance(
    name: str,
    zone: str = "us-central1-a",
    status: str = "RUNNING",
    network: str | None = None,
    disks: list[str] | None = None,
) -> dict[str, Any]:
    """
    Create sample GCP Instance data matching serializer output.

    Format matches what GCP connector serializer returns from serialize_instance().
    """
    instance = {
        "id": str(hash(name) % 1000000000),
        "name": name,
        "zone": zone,
        "machine_type": "n1-standard-2",
        "status": status,
        "status_message": "",
        "creation_timestamp": "2024-01-01T00:00:00Z",
        "cpu_platform": "Intel Haswell",
        "labels": {"app": name},
        "network_interfaces": [],
        "disks": [],
        "can_ip_forward": False,
        "deletion_protection": False,
        "fingerprint": "abc123",
        "self_link": f"https://compute.googleapis.com/compute/v1/projects/test/{name}",
    }

    if network:
        instance["network_interfaces"] = [
            {
                "name": "nic0",
                "network": network,
                "subnetwork": "default",
                "internal_ip": f"10.0.0.{hash(name) % 256}",
                "external_ip": None,
            }
        ]

    if disks:
        instance["disks"] = [
            {"name": d, "boot": i == 0, "auto_delete": True, "mode": "READ_WRITE"}
            for i, d in enumerate(disks)
        ]

    return instance


def make_gcp_disk(
    name: str,
    zone: str = "us-central1-a",
    size_gb: int = 100,
    status: str = "READY",
) -> dict[str, Any]:
    """
    Create sample GCP Disk data matching serializer output.

    Format matches what GCP connector serializer returns from serialize_disk().
    """
    return {
        "id": str(hash(name) % 1000000000),
        "name": name,
        "zone": zone,
        "size_gb": size_gb,
        "size_formatted": f"{size_gb} GB",
        "type": "pd-ssd",
        "status": status,
        "source_image": "debian-11",
        "source_snapshot": "",
        "users": [],
        "labels": {},
        "creation_timestamp": "2024-01-01T00:00:00Z",
        "self_link": f"https://compute.googleapis.com/compute/v1/projects/test/disks/{name}",
    }


def make_gcp_network(
    name: str,
    auto_create: bool = False,
    routing_mode: str = "REGIONAL",
) -> dict[str, Any]:
    """
    Create sample GCP Network data matching serializer output.

    Format matches what GCP connector serializer returns from serialize_network().
    """
    return {
        "id": str(hash(name) % 1000000000),
        "name": name,
        "auto_create_subnetworks": auto_create,
        "routing_mode": routing_mode,
        "mtu": 1460,
        "subnetworks": [],
        "peerings": [],
        "creation_timestamp": "2024-01-01T00:00:00Z",
        "self_link": f"https://compute.googleapis.com/compute/v1/projects/test/networks/{name}",
    }


def make_gcp_subnet(
    name: str,
    network: str,
    region: str = "us-central1",
    ip_cidr_range: str = "10.0.0.0/24",
) -> dict[str, Any]:
    """
    Create sample GCP Subnet data matching serializer output.

    Format matches what GCP connector serializer returns from serialize_subnetwork().
    """
    return {
        "id": str(hash(name) % 1000000000),
        "name": name,
        "network": network,
        "region": region,
        "ip_cidr_range": ip_cidr_range,
        "gateway_address": ip_cidr_range.replace("/24", "1").replace("0/", ""),
        "private_ip_google_access": True,
        "purpose": "PRIVATE",
        "state": "READY",
        "secondary_ip_ranges": [],
        "creation_timestamp": "2024-01-01T00:00:00Z",
        "self_link": f"https://compute.googleapis.com/compute/v1/projects/test/subnetworks/{name}",
    }


def make_gcp_firewall(
    name: str,
    network: str,
    direction: str = "INGRESS",
    priority: int = 1000,
) -> dict[str, Any]:
    """
    Create sample GCP Firewall data matching serializer output.

    Format matches what GCP connector serializer returns from serialize_firewall().
    """
    return {
        "id": str(hash(name) % 1000000000),
        "name": name,
        "network": network,
        "priority": priority,
        "direction": direction,
        "disabled": False,
        "source_ranges": ["0.0.0.0/0"],
        "destination_ranges": [],
        "source_tags": [],
        "target_tags": [],
        "allowed": [{"protocol": "tcp", "ports": ["22"]}],
        "denied": [],
        "log_config": {"enable": False},
        "creation_timestamp": "2024-01-01T00:00:00Z",
        "self_link": f"https://compute.googleapis.com/compute/v1/projects/test/firewalls/{name}",
    }


def make_gcp_cluster(
    name: str,
    location: str = "us-central1",
    status: str = "RUNNING",
    network: str | None = None,
    node_count: int = 3,
) -> dict[str, Any]:
    """
    Create sample GKE Cluster data matching serializer output.

    Format matches what GCP connector serializer returns from serialize_cluster().
    """
    return {
        "name": name,
        "description": f"GKE cluster {name}",
        "location": location,
        "status": status,
        "status_message": "",
        "current_master_version": "1.28.3-gke.1200",
        "current_node_version": "1.28.3-gke.1200",
        "current_node_count": node_count,
        "endpoint": f"35.192.0.{hash(name) % 256}",
        "initial_cluster_version": "1.28.3-gke.1200",
        "node_pools": [
            {
                "name": "default-pool",
                "status": "RUNNING",
                "initial_node_count": node_count,
                "machine_type": "e2-medium",
            }
        ],
        "network": network or "default",
        "subnetwork": "default",
        "cluster_ipv4_cidr": "10.4.0.0/14",
        "services_ipv4_cidr": "10.8.0.0/20",
        "labels": {"env": "prod"},
        "create_time": "2024-01-01T00:00:00Z",
        "self_link": f"https://container.googleapis.com/v1/projects/test/clusters/{name}",
    }


# =============================================================================
# GCP Extraction Tests
# =============================================================================


class TestGCPSchemaExtraction:
    """Tests for GCP schema-based extraction."""

    def test_extract_instance_list(self, schema_extractor):
        """Test extracting list of Instances."""
        instances = [
            make_gcp_instance("web-1", zone="us-central1-a", network="vpc-main", disks=["disk-1"]),
            make_gcp_instance("web-2", zone="us-central1-b", network="vpc-main", disks=["disk-2"]),
        ]

        entities, _relationships = schema_extractor.extract(
            connector_type="gcp",
            operation_id="list_instances",
            result_data=instances,
            connector_id="gcp-123",
            connector_name="Production GCP",
        )

        # Check Instances extracted
        instance_entities = [e for e in entities if e.entity_type == "Instance"]
        assert len(instance_entities) == 2

        instance_names = {i.name for i in instance_entities}
        assert instance_names == {"web-1", "web-2"}

        # Check zones in scope
        for inst in instance_entities:
            assert "zone" in inst.scope
            assert inst.scope["zone"] in ("us-central1-a", "us-central1-b")

    def test_extract_single_instance(self, schema_extractor):
        """Test extracting single Instance."""
        instance = make_gcp_instance(
            "test-vm",
            zone="us-central1-a",
            network="default",
            disks=["boot-disk"],
        )

        entities, _relationships = schema_extractor.extract(
            connector_type="gcp",
            operation_id="get_instance",
            result_data=instance,
            connector_id="gcp-123",
        )

        instance_entity = next((e for e in entities if e.entity_type == "Instance"), None)
        assert instance_entity is not None
        assert instance_entity.name == "test-vm"
        assert instance_entity.scope == {"zone": "us-central1-a"}
        assert "test-vm" in instance_entity.description

    def test_instance_uses_disk_relationships(self, schema_extractor):
        """Test Instance creates uses relationships to Disks."""
        instance = make_gcp_instance(
            "multi-disk-vm",
            disks=["boot-disk", "data-disk-1", "data-disk-2"],
        )

        _entities, relationships = schema_extractor.extract(
            connector_type="gcp",
            operation_id="list_instances",
            result_data=[instance],
            connector_id="gcp-123",
        )

        # Check uses relationships to disks
        uses_rels = [r for r in relationships if r.relationship_type == "uses"]
        assert len(uses_rels) == 3

        disk_targets = {r.to_entity_name for r in uses_rels}
        assert disk_targets == {"boot-disk", "data-disk-1", "data-disk-2"}

    def test_instance_creates_network_stubs(self, schema_extractor):
        """Test Instance creates stub entities for Network."""
        instance = make_gcp_instance("test-vm", network="vpc-main")

        entities, _relationships = schema_extractor.extract(
            connector_type="gcp",
            operation_id="list_instances",
            result_data=[instance],
            connector_id="gcp-123",
        )

        # Check Network stub created
        network_stubs = [e for e in entities if e.entity_type == "Network"]
        assert len(network_stubs) == 1
        assert network_stubs[0].name == "vpc-main"
        assert network_stubs[0].raw_attributes.get("_stub") is True

    def test_extract_disk_list(self, schema_extractor):
        """Test extracting list of Disks."""
        disks = [
            make_gcp_disk("disk-1", zone="us-central1-a", size_gb=100),
            make_gcp_disk("disk-2", zone="us-central1-a", size_gb=500),
        ]

        entities, relationships = schema_extractor.extract(
            connector_type="gcp",
            operation_id="list_disks",
            result_data=disks,
            connector_id="gcp-123",
        )

        disk_entities = [e for e in entities if e.entity_type == "Disk"]
        assert len(disk_entities) == 2

        disk_names = {d.name for d in disk_entities}
        assert disk_names == {"disk-1", "disk-2"}

        # Disks have no outgoing relationships
        assert len(relationships) == 0

    def test_extract_network(self, schema_extractor):
        """Test extracting Network."""
        network = make_gcp_network("vpc-main", routing_mode="GLOBAL")

        entities, relationships = schema_extractor.extract(
            connector_type="gcp",
            operation_id="get_network",
            result_data=network,
            connector_id="gcp-123",
        )

        network_entity = next((e for e in entities if e.entity_type == "Network"), None)
        assert network_entity is not None
        assert network_entity.name == "vpc-main"
        assert network_entity.scope == {}  # Global, no scope
        assert "vpc-main" in network_entity.description

        # Networks have no outgoing relationships
        assert len(relationships) == 0

    def test_extract_subnet_with_network_relationship(self, schema_extractor):
        """Test extracting Subnet with member_of Network relationship."""
        subnet = make_gcp_subnet("subnet-main", network="vpc-main", region="us-central1")

        entities, relationships = schema_extractor.extract(
            connector_type="gcp",
            operation_id="list_subnetworks",
            result_data=[subnet],
            connector_id="gcp-123",
        )

        subnet_entity = next((e for e in entities if e.entity_type == "Subnet"), None)
        assert subnet_entity is not None
        assert subnet_entity.name == "subnet-main"
        assert subnet_entity.scope == {"region": "us-central1"}

        # Check member_of relationship
        member_of = [r for r in relationships if r.relationship_type == "member_of"]
        assert len(member_of) == 1
        assert member_of[0].to_entity_name == "vpc-main"
        assert member_of[0].to_entity_type == "Network"

    def test_extract_firewall_with_applies_to_relationship(self, schema_extractor):
        """Test extracting Firewall with applies_to Network relationship."""
        firewall = make_gcp_firewall("allow-ssh", network="default", direction="INGRESS")

        entities, relationships = schema_extractor.extract(
            connector_type="gcp",
            operation_id="list_firewalls",
            result_data=[firewall],
            connector_id="gcp-123",
        )

        firewall_entity = next((e for e in entities if e.entity_type == "Firewall"), None)
        assert firewall_entity is not None
        assert firewall_entity.name == "allow-ssh"
        assert firewall_entity.scope == {}  # Global, no scope

        # Check applies_to relationship
        applies_to = [r for r in relationships if r.relationship_type == "applies_to"]
        assert len(applies_to) == 1
        assert applies_to[0].to_entity_name == "default"
        assert applies_to[0].to_entity_type == "Network"

    def test_extract_gke_cluster(self, schema_extractor):
        """Test extracting GKE Cluster."""
        cluster = make_gcp_cluster("prod-cluster", location="us-central1", node_count=6)

        entities, _relationships = schema_extractor.extract(
            connector_type="gcp",
            operation_id="get_cluster",
            result_data=cluster,
            connector_id="gcp-123",
        )

        cluster_entity = next((e for e in entities if e.entity_type == "GKECluster"), None)
        assert cluster_entity is not None
        assert cluster_entity.name == "prod-cluster"
        assert cluster_entity.scope == {"location": "us-central1"}
        assert "prod-cluster" in cluster_entity.description
        assert "6" in cluster_entity.description  # node count

    def test_gcp_entity_type_in_relationships(self, schema_extractor):
        """Test that GCP relationships include entity type information."""
        instance = make_gcp_instance("test-vm", network="vpc-main", disks=["disk-1"])

        _entities, relationships = schema_extractor.extract(
            connector_type="gcp",
            operation_id="list_instances",
            result_data=[instance],
            connector_id="gcp-123",
        )

        for rel in relationships:
            assert rel.from_entity_type is not None
            assert rel.to_entity_type is not None

            if rel.relationship_type == "uses":
                assert rel.from_entity_type == "Instance"
                assert rel.to_entity_type == "Disk"
            elif rel.relationship_type == "member_of":
                assert rel.from_entity_type == "Instance"
                assert rel.to_entity_type == "Network"


class TestGCPDeduplication:
    """Tests for GCP entity deduplication."""

    def test_duplicate_disk_stubs_deduped(self, schema_extractor):
        """Test duplicate disk stubs are deduplicated."""
        instances = [
            make_gcp_instance("vm1", disks=["shared-disk"]),
            make_gcp_instance("vm2", disks=["shared-disk"]),
            make_gcp_instance("vm3", disks=["shared-disk"]),
        ]

        entities, _ = schema_extractor.extract(
            connector_type="gcp",
            operation_id="list_instances",
            result_data=instances,
            connector_id="gcp-123",
        )

        disk_stubs = [e for e in entities if e.entity_type == "Disk"]
        assert len(disk_stubs) == 1
        assert disk_stubs[0].name == "shared-disk"

    def test_duplicate_network_stubs_deduped(self, schema_extractor):
        """Test duplicate network stubs are deduplicated."""
        instances = [
            make_gcp_instance("vm1", network="vpc-main"),
            make_gcp_instance("vm2", network="vpc-main"),
        ]

        entities, _ = schema_extractor.extract(
            connector_type="gcp",
            operation_id="list_instances",
            result_data=instances,
            connector_id="gcp-123",
        )

        network_stubs = [e for e in entities if e.entity_type == "Network"]
        assert len(network_stubs) == 1
        assert network_stubs[0].name == "vpc-main"


# =============================================================================
# Proxmox Sample Data
# =============================================================================


def make_proxmox_vm(
    name: str,
    vmid: int = 100,
    node: str = "pve1",
    status: str = "running",
    cpu_count: int = 2,
    memory_mb: int = 4096,
) -> dict[str, Any]:
    """
    Create sample Proxmox VM data matching serializer output.

    Format matches what Proxmox connector serializer returns from serialize_vm().
    """
    return {
        "vmid": vmid,
        "name": name,
        "node": node,
        "status": status,
        "cpu_count": cpu_count,
        "cpu_usage_percent": 25.5,
        "memory_mb": memory_mb,
        "memory_used_mb": int(memory_mb * 0.6),
        "memory_usage_percent": 60.0,
        "disk_size_gb": 100,
        "disk_used_gb": 40,
        "uptime": "5 days, 3 hours",
        "uptime_seconds": 442800,
        "template": False,
        "tags": ["prod"],
        "network_in_bytes": 1024000,
        "network_out_bytes": 512000,
        "disk_read_bytes": 10240000,
        "disk_write_bytes": 5120000,
    }


def make_proxmox_container(
    name: str,
    vmid: int = 200,
    node: str = "pve1",
    status: str = "running",
) -> dict[str, Any]:
    """
    Create sample Proxmox Container data matching serializer output.

    Format matches what Proxmox connector serializer returns from serialize_container().
    """
    return {
        "vmid": vmid,
        "name": name,
        "node": node,
        "status": status,
        "type": "lxc",
        "cpu_count": 1,
        "cpu_usage_percent": 10.0,
        "memory_mb": 1024,
        "memory_used_mb": 512,
        "memory_usage_percent": 50.0,
        "disk_size_gb": 20,
        "disk_used_gb": 8,
        "swap_mb": 512,
        "swap_used_mb": 100,
        "uptime": "2 days, 1 hour",
        "uptime_seconds": 176400,
        "template": False,
        "tags": [],
        "network_in_bytes": 256000,
        "network_out_bytes": 128000,
        "disk_read_bytes": 2048000,
        "disk_write_bytes": 1024000,
    }


def make_proxmox_node(
    name: str,
    status: str = "online",
    cpu_usage: float = 35.0,
    memory_usage: float = 65.0,
) -> dict[str, Any]:
    """
    Create sample Proxmox Node data matching serializer output.

    Format matches what Proxmox connector serializer returns from serialize_node().
    """
    return {
        "name": name,
        "status": status,
        "uptime": "30 days, 5 hours",
        "uptime_seconds": 2610000,
        "cpu_usage_percent": cpu_usage,
        "memory_used_mb": 32768,
        "memory_total_mb": 65536,
        "memory_usage_percent": memory_usage,
        "disk_used_gb": 200,
        "disk_total_gb": 500,
        "disk_usage_percent": 40.0,
        "kernel_version": "6.2.16-3-pve",
        "pve_version": "8.0.3",
    }


def make_proxmox_storage(
    storage: str,
    storage_type: str = "lvmthin",
    total_gb: int = 500,
    used_gb: int = 200,
) -> dict[str, Any]:
    """
    Create sample Proxmox Storage data matching serializer output.

    Format matches what Proxmox connector serializer returns from serialize_storage().
    """
    return {
        "storage": storage,
        "type": storage_type,
        "content": ["images", "rootdir"],
        "total_gb": total_gb,
        "used_gb": used_gb,
        "available_gb": total_gb - used_gb,
        "usage_percent": round((used_gb / total_gb) * 100, 1),
        "enabled": True,
        "active": True,
        "shared": False,
    }


# =============================================================================
# Proxmox Extraction Tests
# =============================================================================


class TestProxmoxSchemaExtraction:
    """Tests for Proxmox schema-based extraction."""

    def test_extract_vm_list(self, schema_extractor):
        """Test extracting list of VMs."""
        vms = [
            make_proxmox_vm("web-01", vmid=100, node="pve1"),
            make_proxmox_vm("db-01", vmid=101, node="pve2"),
        ]

        entities, _relationships = schema_extractor.extract(
            connector_type="proxmox",
            operation_id="list_vms",
            result_data=vms,
            connector_id="pve-123",
            connector_name="Production Proxmox",
        )

        # Check VMs extracted
        vm_entities = [e for e in entities if e.entity_type == "VM"]
        assert len(vm_entities) == 2

        vm_names = {v.name for v in vm_entities}
        assert vm_names == {"web-01", "db-01"}

        # Check node scope
        for vm in vm_entities:
            assert "node" in vm.scope

    def test_extract_single_vm(self, schema_extractor):
        """Test extracting single VM."""
        vm = make_proxmox_vm("test-vm", vmid=100, node="pve1")

        entities, _relationships = schema_extractor.extract(
            connector_type="proxmox",
            operation_id="get_vm",
            result_data=vm,
            connector_id="pve-123",
        )

        vm_entity = next((e for e in entities if e.entity_type == "VM"), None)
        assert vm_entity is not None
        assert vm_entity.name == "test-vm"
        assert vm_entity.scope == {"node": "pve1"}
        assert "test-vm" in vm_entity.description
        assert "100" in vm_entity.description  # vmid

    def test_vm_runs_on_node_relationship(self, schema_extractor):
        """Test VM creates runs_on relationship to Node."""
        vm = make_proxmox_vm("web-vm", node="pve1")

        _entities, relationships = schema_extractor.extract(
            connector_type="proxmox",
            operation_id="list_vms",
            result_data=[vm],
            connector_id="pve-123",
        )

        # Check runs_on relationship
        runs_on = [r for r in relationships if r.relationship_type == "runs_on"]
        assert len(runs_on) == 1
        assert runs_on[0].to_entity_name == "pve1"
        assert runs_on[0].to_entity_type == "Node"

    def test_vm_creates_node_stubs(self, schema_extractor):
        """Test VM creates stub entity for Node."""
        vm = make_proxmox_vm("test-vm", node="pve1")

        entities, _relationships = schema_extractor.extract(
            connector_type="proxmox",
            operation_id="list_vms",
            result_data=[vm],
            connector_id="pve-123",
        )

        # Check Node stub created
        node_stubs = [e for e in entities if e.entity_type == "Node"]
        assert len(node_stubs) == 1
        assert node_stubs[0].name == "pve1"
        assert node_stubs[0].raw_attributes.get("_stub") is True

    def test_extract_container_list(self, schema_extractor):
        """Test extracting list of Containers."""
        containers = [
            make_proxmox_container("nginx-ct", vmid=200, node="pve1"),
            make_proxmox_container("redis-ct", vmid=201, node="pve1"),
        ]

        entities, _relationships = schema_extractor.extract(
            connector_type="proxmox",
            operation_id="list_containers",
            result_data=containers,
            connector_id="pve-123",
        )

        # Check Containers extracted
        ct_entities = [e for e in entities if e.entity_type == "Container"]
        assert len(ct_entities) == 2

        ct_names = {c.name for c in ct_entities}
        assert ct_names == {"nginx-ct", "redis-ct"}

    def test_extract_single_container(self, schema_extractor):
        """Test extracting single Container."""
        container = make_proxmox_container("app-ct", vmid=200, node="pve2")

        entities, _relationships = schema_extractor.extract(
            connector_type="proxmox",
            operation_id="get_container",
            result_data=container,
            connector_id="pve-123",
        )

        ct_entity = next((e for e in entities if e.entity_type == "Container"), None)
        assert ct_entity is not None
        assert ct_entity.name == "app-ct"
        assert ct_entity.scope == {"node": "pve2"}

    def test_container_runs_on_node_relationship(self, schema_extractor):
        """Test Container creates runs_on relationship to Node."""
        container = make_proxmox_container("app-ct", node="pve2")

        _entities, relationships = schema_extractor.extract(
            connector_type="proxmox",
            operation_id="list_containers",
            result_data=[container],
            connector_id="pve-123",
        )

        # Check runs_on relationship
        runs_on = [r for r in relationships if r.relationship_type == "runs_on"]
        assert len(runs_on) == 1
        assert runs_on[0].to_entity_name == "pve2"
        assert runs_on[0].to_entity_type == "Node"

    def test_extract_node_list(self, schema_extractor):
        """Test extracting list of Nodes."""
        nodes = [
            make_proxmox_node("pve1", status="online"),
            make_proxmox_node("pve2", status="online"),
            make_proxmox_node("pve3", status="offline"),
        ]

        entities, relationships = schema_extractor.extract(
            connector_type="proxmox",
            operation_id="list_nodes",
            result_data=nodes,
            connector_id="pve-123",
        )

        # Check Nodes extracted
        node_entities = [e for e in entities if e.entity_type == "Node"]
        assert len(node_entities) == 3

        node_names = {n.name for n in node_entities}
        assert node_names == {"pve1", "pve2", "pve3"}

        # Nodes have no scope (cluster-scoped)
        for node in node_entities:
            assert node.scope == {}

        # Nodes have no outgoing relationships
        assert len(relationships) == 0

    def test_extract_single_node(self, schema_extractor):
        """Test extracting single Node."""
        node = make_proxmox_node("pve1")

        entities, _relationships = schema_extractor.extract(
            connector_type="proxmox",
            operation_id="get_node",
            result_data=node,
            connector_id="pve-123",
        )

        node_entity = next((e for e in entities if e.entity_type == "Node"), None)
        assert node_entity is not None
        assert node_entity.name == "pve1"
        assert node_entity.scope == {}
        assert "pve1" in node_entity.description

    def test_extract_storage_list(self, schema_extractor):
        """Test extracting list of Storage pools."""
        storage_pools = [
            make_proxmox_storage("local-lvm", storage_type="lvmthin"),
            make_proxmox_storage("local", storage_type="dir"),
            make_proxmox_storage("nfs-share", storage_type="nfs"),
        ]

        entities, relationships = schema_extractor.extract(
            connector_type="proxmox",
            operation_id="list_storage",
            result_data=storage_pools,
            connector_id="pve-123",
        )

        # Check Storage extracted
        storage_entities = [e for e in entities if e.entity_type == "Storage"]
        assert len(storage_entities) == 3

        storage_names = {s.name for s in storage_entities}
        assert storage_names == {"local-lvm", "local", "nfs-share"}

        # Storage has no outgoing relationships in this schema
        assert len(relationships) == 0

    def test_extract_single_storage(self, schema_extractor):
        """Test extracting single Storage."""
        storage = make_proxmox_storage("local-lvm", total_gb=1000, used_gb=400)

        entities, _relationships = schema_extractor.extract(
            connector_type="proxmox",
            operation_id="get_storage",
            result_data=storage,
            connector_id="pve-123",
        )

        storage_entity = next((e for e in entities if e.entity_type == "Storage"), None)
        assert storage_entity is not None
        assert storage_entity.name == "local-lvm"
        assert "local-lvm" in storage_entity.description
        assert "lvmthin" in storage_entity.description

    def test_proxmox_entity_type_in_relationships(self, schema_extractor):
        """Test that Proxmox relationships include entity type information."""
        vm = make_proxmox_vm("test-vm", node="pve1")

        _entities, relationships = schema_extractor.extract(
            connector_type="proxmox",
            operation_id="list_vms",
            result_data=[vm],
            connector_id="pve-123",
        )

        for rel in relationships:
            assert rel.from_entity_type is not None
            assert rel.to_entity_type is not None

            if rel.relationship_type == "runs_on":
                assert rel.from_entity_type == "VM"
                assert rel.to_entity_type == "Node"


class TestProxmoxDeduplication:
    """Tests for Proxmox entity deduplication."""

    def test_duplicate_node_stubs_deduped(self, schema_extractor):
        """Test duplicate node stubs are deduplicated."""
        vms = [
            make_proxmox_vm("vm1", node="pve1"),
            make_proxmox_vm("vm2", node="pve1"),
            make_proxmox_vm("vm3", node="pve1"),
        ]

        entities, _ = schema_extractor.extract(
            connector_type="proxmox",
            operation_id="list_vms",
            result_data=vms,
            connector_id="pve-123",
        )

        node_stubs = [e for e in entities if e.entity_type == "Node"]
        assert len(node_stubs) == 1
        assert node_stubs[0].name == "pve1"

    def test_vms_and_containers_on_same_node(self, schema_extractor):
        """Test VMs and Containers on same node share deduplicated stub."""
        vms = [make_proxmox_vm("vm1", node="pve1")]

        vm_entities, _vm_rels = schema_extractor.extract(
            connector_type="proxmox",
            operation_id="list_vms",
            result_data=vms,
            connector_id="pve-123",
        )

        vm_node_stubs = [e for e in vm_entities if e.entity_type == "Node"]
        assert len(vm_node_stubs) == 1

        containers = [make_proxmox_container("ct1", node="pve1")]

        ct_entities, _ct_rels = schema_extractor.extract(
            connector_type="proxmox",
            operation_id="list_containers",
            result_data=containers,
            connector_id="pve-123",
        )

        ct_node_stubs = [e for e in ct_entities if e.entity_type == "Node"]
        assert len(ct_node_stubs) == 1

        # Both reference the same node
        assert vm_node_stubs[0].name == ct_node_stubs[0].name == "pve1"
