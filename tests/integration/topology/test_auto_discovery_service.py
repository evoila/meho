# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for topology auto-discovery service.

Tests the complete flow from operation result to queued discovery
using schema-based extraction.
"""

import pytest

from meho_app.modules.topology.auto_discovery.queue import (
    DiscoveryQueue,
    reset_discovery_queue,
)
from meho_app.modules.topology.auto_discovery.service import (
    TopologyAutoDiscoveryService,
    get_auto_discovery_service,
    reset_auto_discovery_service,
)
from meho_app.modules.topology.extraction import reset_schema_extractor


class TestTopologyAutoDiscoveryService:
    """Integration tests for TopologyAutoDiscoveryService."""

    @pytest.fixture
    def queue(self):
        """Create in-memory discovery queue."""
        reset_discovery_queue()
        return DiscoveryQueue()

    @pytest.fixture
    def service(self, queue):
        """Create auto-discovery service."""
        reset_auto_discovery_service()
        reset_schema_extractor()
        return TopologyAutoDiscoveryService(queue=queue)

    # =========================================================================
    # Basic functionality tests
    # =========================================================================

    def test_init_enabled(self, queue):
        """Test service initializes enabled by default."""
        service = TopologyAutoDiscoveryService(queue=queue)
        assert service.enabled is True

    def test_init_disabled(self, queue):
        """Test service can be disabled."""
        service = TopologyAutoDiscoveryService(queue=queue, enabled=False)
        assert service.enabled is False

    def test_stats_initial(self, service):
        """Test initial statistics."""
        stats = service.stats

        assert stats["entities_queued"] == 0
        assert stats["relationships_queued"] == 0
        assert stats["operations_processed"] == 0
        assert stats["enabled"] is True

    # =========================================================================
    # VMware integration tests (via schema-based extraction)
    # =========================================================================

    @pytest.mark.asyncio
    async def test_process_vmware_vms(self, service, queue):
        """Test processing VMware list_virtual_machines result."""
        vm_data = [
            {
                "name": "web-01",
                "config": {"num_cpu": 4, "memory_mb": 8192, "guest_full_name": "CentOS 7"},
                "guest": {"ip_address": "192.168.1.10"},
                "runtime": {"host": {"name": "esxi-01"}, "power_state": "poweredOn"},
            },
            {
                "name": "web-02",
                "config": {"num_cpu": 2, "memory_mb": 4096},
                "runtime": {"host": {"name": "esxi-02"}},
            },
        ]

        count = await service.process_operation_result(
            connector_type="vmware",
            connector_id="conn-123",
            connector_name="Production vCenter",
            operation_id="list_virtual_machines",
            result_data=vm_data,
            tenant_id="tenant-1",
        )

        # Should extract 2 VMs + stub entities for hosts
        assert count >= 2

        # Check queue has the message
        assert await queue.size() == 1

        # Pop and verify message
        messages = await queue.pop_batch(1)
        assert len(messages) == 1
        msg = messages[0]

        assert msg.tenant_id == "tenant-1"

        # Find VM entities
        vm_entities = [e for e in msg.entities if e.entity_type == "VM"]
        assert len(vm_entities) == 2
        assert vm_entities[0].connector_id == "conn-123"
        assert vm_entities[0].connector_name == "Production vCenter"

    @pytest.mark.asyncio
    async def test_process_vmware_hosts(self, service, queue):
        """Test processing VMware list_hosts result."""
        host_data = [
            {
                "name": "esxi-01.example.com",
                "hardware": {
                    "num_cpu_cores": 32,
                    "memory_size_bytes": 137438953472,
                },
                "parent": {"name": "Production"},
                "runtime": {"connection_state": "connected"},
            },
        ]

        count = await service.process_operation_result(
            connector_type="vmware",
            connector_id="conn-123",
            connector_name="vCenter",
            operation_id="list_hosts",
            result_data=host_data,
            tenant_id="tenant-1",
        )

        assert count >= 1

        messages = await queue.pop_batch(1)
        msg = messages[0]

        # Check for member_of relationship
        member_of_rels = [r for r in msg.relationships if r.relationship_type == "member_of"]
        assert len(member_of_rels) >= 1

    @pytest.mark.asyncio
    async def test_process_vmware_clusters(self, service, queue):
        """Test processing VMware list_clusters result."""
        cluster_data = [
            {
                "name": "Production",
                "configuration": {
                    "das_config": {"enabled": True},
                    "drs_config": {"enabled": True},
                },
                "summary": {"num_hosts": 5},
            },
            {
                "name": "Development",
                "configuration": {
                    "das_config": {"enabled": False},
                    "drs_config": {"enabled": False},
                },
            },
        ]

        count = await service.process_operation_result(
            connector_type="vmware",
            connector_id="conn-123",
            connector_name="vCenter",
            operation_id="list_clusters",
            result_data=cluster_data,
            tenant_id="tenant-1",
        )

        assert count == 2

        messages = await queue.pop_batch(1)
        msg = messages[0]

        cluster_entities = [e for e in msg.entities if e.entity_type == "Cluster"]
        assert len(cluster_entities) == 2

    @pytest.mark.asyncio
    async def test_process_vmware_datastores(self, service, queue):
        """Test processing VMware list_datastores result."""
        ds_data = [
            {
                "name": "datastore1",
                "summary": {
                    "type": "VMFS",
                    "capacity": 2199023255552,
                    "free_space": 549755813888,
                },
            },
        ]

        count = await service.process_operation_result(
            connector_type="vmware",
            connector_id="conn-123",
            connector_name="vCenter",
            operation_id="list_datastores",
            result_data=ds_data,
            tenant_id="tenant-1",
        )

        assert count == 1

    # =========================================================================
    # Edge cases and error handling
    # =========================================================================

    @pytest.mark.asyncio
    async def test_process_disabled_service(self, queue):
        """Test processing when service is disabled."""
        service = TopologyAutoDiscoveryService(queue=queue, enabled=False)

        count = await service.process_operation_result(
            connector_type="vmware",
            connector_id="conn-123",
            connector_name="vCenter",
            operation_id="list_virtual_machines",
            result_data=[{"name": "vm1"}],
            tenant_id="tenant-1",
        )

        assert count == 0
        assert await queue.size() == 0

    @pytest.mark.asyncio
    async def test_process_unknown_connector_type(self, service, queue):
        """Test processing with unknown connector type returns 0."""
        count = await service.process_operation_result(
            connector_type="unknown_connector",
            connector_id="conn-123",
            connector_name="Unknown",
            operation_id="list_items",
            result_data=[{"name": "item1"}],
            tenant_id="tenant-1",
        )

        assert count == 0
        assert await queue.size() == 0

    @pytest.mark.asyncio
    async def test_process_unsupported_operation(self, service, queue):
        """Test processing with unsupported operation returns 0."""
        count = await service.process_operation_result(
            connector_type="vmware",
            connector_id="conn-123",
            connector_name="vCenter",
            operation_id="power_on_vm",
            result_data={"status": "success"},
            tenant_id="tenant-1",
        )

        assert count == 0
        assert await queue.size() == 0

    @pytest.mark.asyncio
    async def test_process_empty_result(self, service, queue):
        """Test processing with empty result."""
        count = await service.process_operation_result(
            connector_type="vmware",
            connector_id="conn-123",
            connector_name="vCenter",
            operation_id="list_virtual_machines",
            result_data=[],
            tenant_id="tenant-1",
        )

        assert count == 0
        assert await queue.size() == 0

    @pytest.mark.asyncio
    async def test_process_error_result(self, service, queue):
        """Test processing error response."""
        count = await service.process_operation_result(
            connector_type="vmware",
            connector_id="conn-123",
            connector_name="vCenter",
            operation_id="list_virtual_machines",
            result_data={"error": "Connection failed"},
            tenant_id="tenant-1",
        )

        assert count == 0
        assert await queue.size() == 0

    @pytest.mark.asyncio
    async def test_process_multiple_operations(self, service, queue):
        """Test processing multiple operations sequentially."""
        # First operation
        await service.process_operation_result(
            connector_type="vmware",
            connector_id="conn-123",
            connector_name="vCenter",
            operation_id="list_virtual_machines",
            result_data=[{"name": "vm1"}],
            tenant_id="tenant-1",
        )

        # Second operation
        await service.process_operation_result(
            connector_type="vmware",
            connector_id="conn-123",
            connector_name="vCenter",
            operation_id="list_clusters",
            result_data=[{"name": "cluster1"}],
            tenant_id="tenant-1",
        )

        assert await queue.size() == 2
        assert service.stats["operations_processed"] == 2

    @pytest.mark.asyncio
    async def test_reset_stats(self, service, queue):
        """Test resetting statistics."""
        await service.process_operation_result(
            connector_type="vmware",
            connector_id="conn-123",
            connector_name="vCenter",
            operation_id="list_virtual_machines",
            result_data=[{"name": "vm1"}],
            tenant_id="tenant-1",
        )

        assert service.stats["entities_queued"] >= 1

        service.reset_stats()

        assert service.stats["entities_queued"] == 0
        assert service.stats["operations_processed"] == 0


class TestAutoDiscoveryServiceSingleton:
    """Tests for auto-discovery service singleton management."""

    def setup_method(self):
        """Reset singletons before each test."""
        reset_auto_discovery_service()
        reset_discovery_queue()
        reset_schema_extractor()

    @pytest.mark.asyncio
    def test_get_service_creates_instance(self):
        """Test that get_auto_discovery_service creates instance."""
        service = get_auto_discovery_service()

        assert service is not None
        assert isinstance(service, TopologyAutoDiscoveryService)

    @pytest.mark.asyncio
    def test_get_service_returns_same_instance(self):
        """Test that get_auto_discovery_service returns singleton."""
        service1 = get_auto_discovery_service()
        service2 = get_auto_discovery_service()

        assert service1 is service2

    @pytest.mark.asyncio
    def test_reset_service(self):
        """Test resetting service singleton."""
        service1 = get_auto_discovery_service()
        reset_auto_discovery_service()
        service2 = get_auto_discovery_service()

        assert service1 is not service2

    @pytest.mark.asyncio
    def test_get_service_with_custom_queue(self):
        """Test getting service with custom queue."""
        custom_queue = DiscoveryQueue()

        service = get_auto_discovery_service(queue=custom_queue)

        assert service.queue is custom_queue

    @pytest.mark.asyncio
    def test_get_service_disabled(self):
        """Test getting disabled service."""
        service = get_auto_discovery_service(enabled=False)

        assert service.enabled is False


class TestEndToEndAutoDiscovery:
    """End-to-end tests for the auto-discovery system."""

    @pytest.fixture
    def queue(self):
        """Create in-memory queue."""
        reset_discovery_queue()
        return DiscoveryQueue()

    @pytest.fixture
    def service(self, queue):
        """Create service."""
        reset_auto_discovery_service()
        reset_schema_extractor()
        return TopologyAutoDiscoveryService(queue=queue)

    @pytest.mark.asyncio
    async def test_full_vmware_discovery_flow(self, service, queue):
        """Test complete VMware discovery flow."""
        # Simulate list_virtual_machines result
        vm_result = [
            {
                "name": "web-server-01",
                "config": {
                    "num_cpu": 4,
                    "memory_mb": 8192,
                    "guest_full_name": "Ubuntu 22.04 LTS",
                },
                "guest": {
                    "ip_address": "10.0.1.10",
                },
                "runtime": {
                    "host": {"name": "esxi-node-01.datacenter.local"},
                    "power_state": "poweredOn",
                },
                "storage": [
                    {"datastore": {"name": "vmfs-datastore-01"}},
                    {"datastore": {"name": "nfs-backup-01"}},
                ],
            },
        ]

        # Process the operation result
        count = await service.process_operation_result(
            connector_type="vmware",
            connector_id="vcenter-prod-001",
            connector_name="Production vCenter",
            operation_id="list_virtual_machines",
            result_data=vm_result,
            tenant_id="acme-corp",
        )

        # Verify entity was queued
        assert count >= 1

        # Pop and inspect the message
        messages = await queue.pop_batch(1)
        assert len(messages) == 1
        msg = messages[0]

        # Verify message content
        assert msg.tenant_id == "acme-corp"

        # Find the VM entity
        vm_entities = [e for e in msg.entities if e.entity_type == "VM"]
        assert len(vm_entities) == 1

        entity = vm_entities[0]
        assert entity.name == "web-server-01"
        assert entity.connector_id == "vcenter-prod-001"
        assert entity.connector_name == "Production vCenter"

        # Verify relationships exist
        runs_on = [r for r in msg.relationships if r.relationship_type == "runs_on"]
        assert len(runs_on) >= 1

    @pytest.mark.asyncio
    async def test_multi_tenant_isolation(self, service, queue):
        """Test that discoveries are properly isolated by tenant."""
        # Tenant 1 discovers VMs
        await service.process_operation_result(
            connector_type="vmware",
            connector_id="conn-1",
            connector_name="vCenter 1",
            operation_id="list_virtual_machines",
            result_data=[{"name": "tenant1-vm"}],
            tenant_id="tenant-1",
        )

        # Tenant 2 discovers VMs
        await service.process_operation_result(
            connector_type="vmware",
            connector_id="conn-2",
            connector_name="vCenter 2",
            operation_id="list_virtual_machines",
            result_data=[{"name": "tenant2-vm"}],
            tenant_id="tenant-2",
        )

        # Both should be queued
        assert await queue.size() == 2

        # Pop and verify tenant isolation
        messages = await queue.pop_batch(2)

        tenant_ids = {msg.tenant_id for msg in messages}
        assert tenant_ids == {"tenant-1", "tenant-2"}


class TestTypedKubernetesAutoDiscovery:
    """Integration tests for typed Kubernetes connector auto-discovery.

    Tests the complete flow from K8s connector operation results
    to queued discovery messages using schema-based extraction.
    """

    @pytest.fixture
    def queue(self):
        """Create in-memory discovery queue."""
        reset_discovery_queue()
        return DiscoveryQueue()

    @pytest.fixture
    def service(self, queue):
        """Create auto-discovery service."""
        reset_auto_discovery_service()
        reset_schema_extractor()
        return TopologyAutoDiscoveryService(queue=queue)

    # =========================================================================
    # Kubernetes connector tests (via schema-based extraction)
    # =========================================================================

    @pytest.mark.asyncio
    async def test_process_kubernetes_pod_list(self, service, queue):
        """Test processing K8s PodList result."""
        pod_data = {
            "apiVersion": "v1",
            "kind": "PodList",
            "items": [
                {
                    "metadata": {
                        "name": "nginx-abc123",
                        "namespace": "production",
                        "labels": {"app": "nginx"},
                    },
                    "spec": {"nodeName": "worker-01"},
                    "status": {
                        "phase": "Running",
                        "podIP": "10.0.1.5",
                    },
                },
                {
                    "metadata": {
                        "name": "redis-xyz789",
                        "namespace": "production",
                    },
                    "spec": {"nodeName": "worker-02"},
                    "status": {"phase": "Running"},
                },
            ],
        }

        count = await service.process_operation_result(
            connector_type="kubernetes",
            connector_id="k8s-conn-1",
            connector_name="Production K8s",
            operation_id=None,  # Schema matches by kind
            result_data=pod_data,
            tenant_id="tenant-1",
        )

        # Should extract pods + namespace + node stubs
        assert count >= 2

        # Check queue has the message
        assert await queue.size() == 1

        # Pop and verify message
        messages = await queue.pop_batch(1)
        msg = messages[0]

        assert msg.tenant_id == "tenant-1"

        pod_entities = [e for e in msg.entities if e.entity_type == "Pod"]
        assert len(pod_entities) == 2

        pod_names = {e.name for e in pod_entities}
        assert pod_names == {"nginx-abc123", "redis-xyz789"}

    @pytest.mark.asyncio
    async def test_process_kubernetes_node_list(self, service, queue):
        """Test processing K8s NodeList result."""
        node_data = {
            "apiVersion": "v1",
            "kind": "NodeList",
            "items": [
                {
                    "metadata": {"name": "master-01"},
                    "status": {
                        "conditions": [{"type": "Ready", "status": "True"}],
                        "capacity": {"cpu": "8", "memory": "16777216Ki"},
                        "nodeInfo": {"kubeletVersion": "v1.28.5"},
                    },
                },
                {
                    "metadata": {"name": "worker-01"},
                    "status": {
                        "conditions": [{"type": "Ready", "status": "True"}],
                        "capacity": {"cpu": "16"},
                    },
                },
            ],
        }

        count = await service.process_operation_result(
            connector_type="kubernetes",
            connector_id="k8s-conn-1",
            connector_name="Production K8s",
            operation_id=None,
            result_data=node_data,
            tenant_id="tenant-1",
        )

        assert count == 2

        messages = await queue.pop_batch(1)
        msg = messages[0]

        node_entities = [e for e in msg.entities if e.entity_type == "Node"]
        assert len(node_entities) == 2

        node_names = {e.name for e in node_entities}
        assert node_names == {"master-01", "worker-01"}

    @pytest.mark.asyncio
    async def test_process_kubernetes_deployment_list(self, service, queue):
        """Test processing K8s DeploymentList result."""
        deploy_data = {
            "apiVersion": "apps/v1",
            "kind": "DeploymentList",
            "items": [
                {
                    "metadata": {"name": "api", "namespace": "production"},
                    "spec": {"replicas": 3, "strategy": {"type": "RollingUpdate"}},
                    "status": {"readyReplicas": 3},
                },
                {
                    "metadata": {"name": "web", "namespace": "staging"},
                    "spec": {"replicas": 2},
                    "status": {"readyReplicas": 1},
                },
            ],
        }

        count = await service.process_operation_result(
            connector_type="kubernetes",
            connector_id="k8s-conn-1",
            connector_name="Production K8s",
            operation_id=None,
            result_data=deploy_data,
            tenant_id="tenant-1",
        )

        # 2 deployments + namespace entities
        assert count >= 2

        messages = await queue.pop_batch(1)
        msg = messages[0]

        deployment_entities = [e for e in msg.entities if e.entity_type == "Deployment"]
        assert len(deployment_entities) == 2

    @pytest.mark.asyncio
    async def test_process_kubernetes_service_list(self, service, queue):
        """Test processing K8s ServiceList result."""
        svc_data = {
            "apiVersion": "v1",
            "kind": "ServiceList",
            "items": [
                {
                    "metadata": {"name": "api-service", "namespace": "production"},
                    "spec": {
                        "type": "ClusterIP",
                        "clusterIP": "10.96.0.100",
                        "ports": [{"port": 80, "targetPort": 8080}],
                        "selector": {"app": "api"},
                    },
                },
            ],
        }

        count = await service.process_operation_result(
            connector_type="kubernetes",
            connector_id="k8s-conn-1",
            connector_name="Production K8s",
            operation_id=None,
            result_data=svc_data,
            tenant_id="tenant-1",
        )

        # 1 service + namespace
        assert count >= 1

        messages = await queue.pop_batch(1)
        msg = messages[0]

        svc_entities = [e for e in msg.entities if e.entity_type == "Service"]
        assert len(svc_entities) == 1
        assert svc_entities[0].name == "api-service"

    @pytest.mark.asyncio
    async def test_process_kubernetes_ingress_list(self, service, queue):
        """Test processing K8s IngressList result."""
        ing_data = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "IngressList",
            "items": [
                {
                    "metadata": {"name": "api-ingress", "namespace": "production"},
                    "spec": {
                        "ingressClassName": "nginx",
                        "rules": [
                            {
                                "host": "api.example.com",
                                "http": {
                                    "paths": [
                                        {
                                            "path": "/",
                                            "backend": {"service": {"name": "api-service"}},
                                        },
                                    ],
                                },
                            },
                        ],
                    },
                },
            ],
        }

        count = await service.process_operation_result(
            connector_type="kubernetes",
            connector_id="k8s-conn-1",
            connector_name="Production K8s",
            operation_id=None,
            result_data=ing_data,
            tenant_id="tenant-1",
        )

        # 1 ingress + namespace + service stub
        assert count >= 1

        messages = await queue.pop_batch(1)
        msg = messages[0]

        # Should have routes_to relationship
        routes_to = [r for r in msg.relationships if r.relationship_type == "routes_to"]
        assert len(routes_to) >= 1

    @pytest.mark.asyncio
    async def test_kubernetes_empty_result_handled(self, service, queue):
        """Test handling empty K8s result."""
        count = await service.process_operation_result(
            connector_type="kubernetes",
            connector_id="k8s-conn-1",
            connector_name="Production K8s",
            operation_id=None,
            result_data={"apiVersion": "v1", "kind": "PodList", "items": []},
            tenant_id="tenant-1",
        )

        assert count == 0
        assert await queue.size() == 0


class TestUnsupportedConnectorTypes:
    """Tests for connector types without extraction schemas."""

    @pytest.fixture
    def queue(self):
        """Create in-memory queue."""
        reset_discovery_queue()
        return DiscoveryQueue()

    @pytest.fixture
    def service(self, queue):
        """Create service."""
        reset_auto_discovery_service()
        reset_schema_extractor()
        return TopologyAutoDiscoveryService(queue=queue)

    @pytest.mark.asyncio
    async def test_gcp_not_supported_yet(self, service, queue):
        """Test GCP connector returns 0 (no schema yet)."""
        count = await service.process_operation_result(
            connector_type="gcp",
            connector_id="gcp-123",
            connector_name="GCP Project",
            operation_id="list_instances",
            result_data=[{"name": "instance-1"}],
            tenant_id="tenant-1",
        )

        assert count == 0
        assert await queue.size() == 0

    @pytest.mark.asyncio
    async def test_proxmox_not_supported_yet(self, service, queue):
        """Test Proxmox connector returns 0 (no schema yet)."""
        count = await service.process_operation_result(
            connector_type="proxmox",
            connector_id="proxmox-123",
            connector_name="Proxmox Node",
            operation_id="list_vms",
            result_data=[{"vmid": 100, "name": "vm-1"}],
            tenant_id="tenant-1",
        )

        assert count == 0
        assert await queue.size() == 0

    @pytest.mark.asyncio
    async def test_rest_connector_no_extraction(self, service, queue):
        """Test generic REST connector returns 0 (no topology for generic APIs)."""
        count = await service.process_operation_result(
            connector_type="rest",
            connector_id="rest-123",
            connector_name="Some API",
            operation_id="get_users",
            result_data=[{"id": 1, "name": "John"}],
            tenant_id="tenant-1",
        )

        assert count == 0
        assert await queue.size() == 0

    @pytest.mark.asyncio
    async def test_soap_connector_no_extraction(self, service, queue):
        """Test generic SOAP connector returns 0 (no topology for generic APIs)."""
        count = await service.process_operation_result(
            connector_type="soap",
            connector_id="soap-123",
            connector_name="Legacy Service",
            operation_id="GetOrders",
            result_data={"orders": [{"id": 1}]},
            tenant_id="tenant-1",
        )

        assert count == 0
        assert await queue.size() == 0
