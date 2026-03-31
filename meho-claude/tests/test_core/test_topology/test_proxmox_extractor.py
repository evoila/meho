"""Tests for ProxmoxEntityExtractor topology extraction."""

import pytest

from meho_claude.core.topology.models import ExtractionResult


# Sample Proxmox API-style result data
SAMPLE_VM_DATA = {
    "data": [
        {
            "vmid": 100,
            "name": "k8s-worker-01",
            "status": "running",
            "node": "pve1",
            "maxmem": 8589934592,
            "maxcpu": 4,
            "netin": 12345678,
            "netout": 87654321,
            "uptime": 86400,
        },
        {
            "vmid": 101,
            "name": "db-server",
            "status": "stopped",
            "node": "pve2",
            "maxmem": 17179869184,
            "maxcpu": 8,
            "netin": 0,
            "netout": 0,
            "uptime": 0,
        },
    ]
}

SAMPLE_VM_DATA_WITH_IP = {
    "data": [
        {
            "vmid": 100,
            "name": "k8s-worker-01",
            "status": "running",
            "node": "pve1",
            "maxmem": 8589934592,
            "maxcpu": 4,
            "ip_address": "10.0.1.100",
            "hostname": "k8s-worker-01.local",
        },
    ]
}

SAMPLE_VM_DATA_NULL_IP = {
    "data": [
        {
            "vmid": 101,
            "name": "db-server",
            "status": "stopped",
            "node": "pve2",
            "maxmem": 17179869184,
            "maxcpu": 8,
            "ip_address": None,
            "hostname": None,
        },
    ]
}

SAMPLE_CONTAINER_DATA = {
    "data": [
        {
            "vmid": 200,
            "name": "nginx-proxy",
            "status": "running",
            "node": "pve1",
            "maxmem": 2147483648,
            "maxcpu": 2,
            "ip_address": "10.0.2.10",
            "hostname": "nginx-proxy.local",
        },
        {
            "vmid": 201,
            "name": "redis-cache",
            "status": "running",
            "node": "pve1",
            "maxmem": 1073741824,
            "maxcpu": 1,
        },
    ]
}

SAMPLE_NODE_DATA = {
    "data": [
        {
            "node": "pve1",
            "status": "online",
            "maxcpu": 16,
            "maxmem": 68719476736,
            "ip": "192.168.1.10",
        },
        {
            "node": "pve2",
            "status": "online",
            "maxcpu": 16,
            "maxmem": 68719476736,
        },
    ]
}

SAMPLE_STORAGE_DATA = {
    "data": [
        {
            "storage": "local",
            "type": "dir",
            "total": 107374182400,
            "used": 53687091200,
            "avail": 53687091200,
            "enabled": 1,
            "node": "pve1",
        },
        {
            "storage": "ceph-rbd",
            "type": "rbd",
            "total": 1099511627776,
            "used": 549755813888,
            "avail": 549755813888,
            "enabled": 1,
            "node": "pve1",
        },
    ]
}

SAMPLE_CEPH_POOL_DATA = {
    "data": [
        {
            "name": "rbd-pool",
            "size": 3,
            "min_size": 2,
            "pg_num": 128,
            "bytes_used": 549755813888,
        },
    ]
}


@pytest.fixture
def extractor():
    from meho_claude.core.topology.extractors.proxmox import ProxmoxEntityExtractor

    return ProxmoxEntityExtractor()


class TestProxmoxExtractorRegistration:
    def test_registered_in_extractor_registry(self):
        from meho_claude.core.topology.extractors.proxmox import ProxmoxEntityExtractor
        from meho_claude.core.topology.extractor import get_extractor_class

        import meho_claude.core.topology.extractors  # noqa: F401

        cls = get_extractor_class("proxmox")
        assert cls is ProxmoxEntityExtractor


class TestProxmoxExtractVMs:
    def test_extract_vms_returns_entities(self, extractor):
        result = extractor.extract("proxmox-prod", "proxmox", "list-vms", SAMPLE_VM_DATA)

        assert isinstance(result, ExtractionResult)
        assert result.source_connector == "proxmox-prod"
        assert result.source_operation == "list-vms"

        vms = [e for e in result.entities if e.entity_type == "proxmox_vm"]
        assert len(vms) == 2

    def test_vm_entity_fields(self, extractor):
        result = extractor.extract("proxmox-prod", "proxmox", "list-vms", SAMPLE_VM_DATA)

        vms = [e for e in result.entities if e.entity_type == "proxmox_vm"]
        vm = next(v for v in vms if v.name == "k8s-worker-01")

        assert vm.connector_name == "proxmox-prod"
        assert vm.connector_type == "proxmox"
        assert vm.canonical_id == "proxmox:proxmox-prod:vm:100"
        assert "k8s-worker-01" in vm.description

    def test_vm_raw_attributes(self, extractor):
        result = extractor.extract("proxmox-prod", "proxmox", "list-vms", SAMPLE_VM_DATA)

        vms = [e for e in result.entities if e.entity_type == "proxmox_vm"]
        vm = next(v for v in vms if v.name == "k8s-worker-01")

        assert vm.raw_attributes["vmid"] == 100
        assert vm.raw_attributes["status"] == "running"
        assert vm.raw_attributes["node"] == "pve1"
        assert vm.raw_attributes["maxmem"] == 8589934592
        assert vm.raw_attributes["maxcpu"] == 4

    def test_vm_with_ip_and_hostname(self, extractor):
        result = extractor.extract("proxmox-prod", "proxmox", "list-vms", SAMPLE_VM_DATA_WITH_IP)

        vms = [e for e in result.entities if e.entity_type == "proxmox_vm"]
        vm = vms[0]

        assert vm.raw_attributes["ip_address"] == "10.0.1.100"
        assert vm.raw_attributes["hostname"] == "k8s-worker-01.local"

    def test_vm_with_null_ip_and_hostname_normalized(self, extractor):
        """None/null IP and hostname must be normalized to empty string."""
        result = extractor.extract("proxmox-prod", "proxmox", "list-vms", SAMPLE_VM_DATA_NULL_IP)

        vms = [e for e in result.entities if e.entity_type == "proxmox_vm"]
        vm = vms[0]

        assert vm.raw_attributes["ip_address"] == ""
        assert vm.raw_attributes["hostname"] == ""

    def test_vm_without_ip_fields_uses_empty_string(self, extractor):
        """When ip_address/hostname keys are missing entirely, use empty string."""
        result = extractor.extract("proxmox-prod", "proxmox", "list-vms", SAMPLE_VM_DATA)

        vms = [e for e in result.entities if e.entity_type == "proxmox_vm"]
        vm = next(v for v in vms if v.name == "k8s-worker-01")

        # SAMPLE_VM_DATA has no ip_address/hostname keys -- should default to ""
        assert vm.raw_attributes["ip_address"] == ""
        assert vm.raw_attributes["hostname"] == ""

    def test_vm_to_node_member_of_relationship(self, extractor):
        result = extractor.extract("proxmox-prod", "proxmox", "list-vms", SAMPLE_VM_DATA)

        rels = result.relationships
        assert len(rels) == 2  # One per VM

        rel = rels[0]
        assert rel.relationship_type == "member_of"


class TestProxmoxExtractContainers:
    def test_extract_containers_returns_entities(self, extractor):
        result = extractor.extract("proxmox-prod", "proxmox", "list-containers", SAMPLE_CONTAINER_DATA)

        cts = [e for e in result.entities if e.entity_type == "proxmox_container"]
        assert len(cts) == 2

    def test_container_entity_type_distinct_from_vm(self, extractor):
        """Containers must have entity_type 'proxmox_container', not 'proxmox_vm'."""
        result = extractor.extract("proxmox-prod", "proxmox", "list-containers", SAMPLE_CONTAINER_DATA)

        for entity in result.entities:
            assert entity.entity_type == "proxmox_container"

    def test_container_canonical_id_uses_ct_prefix(self, extractor):
        result = extractor.extract("proxmox-prod", "proxmox", "list-containers", SAMPLE_CONTAINER_DATA)

        cts = [e for e in result.entities if e.entity_type == "proxmox_container"]
        ct = next(c for c in cts if c.name == "nginx-proxy")

        assert ct.canonical_id == "proxmox:proxmox-prod:ct:200"

    def test_container_ip_and_hostname(self, extractor):
        result = extractor.extract("proxmox-prod", "proxmox", "list-containers", SAMPLE_CONTAINER_DATA)

        cts = [e for e in result.entities if e.entity_type == "proxmox_container"]
        ct = next(c for c in cts if c.name == "nginx-proxy")

        assert ct.raw_attributes["ip_address"] == "10.0.2.10"
        assert ct.raw_attributes["hostname"] == "nginx-proxy.local"

    def test_container_to_node_member_of_relationship(self, extractor):
        result = extractor.extract("proxmox-prod", "proxmox", "list-containers", SAMPLE_CONTAINER_DATA)

        rels = result.relationships
        assert len(rels) == 2  # One per container
        for rel in rels:
            assert rel.relationship_type == "member_of"


class TestProxmoxExtractNodes:
    def test_extract_nodes_returns_entities(self, extractor):
        result = extractor.extract("proxmox-prod", "proxmox", "list-nodes", SAMPLE_NODE_DATA)

        nodes = [e for e in result.entities if e.entity_type == "proxmox_node"]
        assert len(nodes) == 2

    def test_node_entity_fields(self, extractor):
        result = extractor.extract("proxmox-prod", "proxmox", "list-nodes", SAMPLE_NODE_DATA)

        nodes = [e for e in result.entities if e.entity_type == "proxmox_node"]
        node = next(n for n in nodes if n.name == "pve1")

        assert node.connector_name == "proxmox-prod"
        assert node.connector_type == "proxmox"
        assert node.canonical_id == "proxmox:proxmox-prod:node:pve1"

    def test_node_raw_attributes(self, extractor):
        result = extractor.extract("proxmox-prod", "proxmox", "list-nodes", SAMPLE_NODE_DATA)

        nodes = [e for e in result.entities if e.entity_type == "proxmox_node"]
        node = next(n for n in nodes if n.name == "pve1")

        assert node.raw_attributes["status"] == "online"
        assert node.raw_attributes["maxcpu"] == 16
        assert node.raw_attributes["maxmem"] == 68719476736
        assert node.raw_attributes["ip_address"] == "192.168.1.10"

    def test_node_without_ip(self, extractor):
        result = extractor.extract("proxmox-prod", "proxmox", "list-nodes", SAMPLE_NODE_DATA)

        nodes = [e for e in result.entities if e.entity_type == "proxmox_node"]
        node = next(n for n in nodes if n.name == "pve2")

        assert node.raw_attributes["ip_address"] == ""


class TestProxmoxExtractStorage:
    def test_extract_storage_returns_entities(self, extractor):
        result = extractor.extract("proxmox-prod", "proxmox", "list-storage", SAMPLE_STORAGE_DATA)

        storages = [e for e in result.entities if e.entity_type == "proxmox_storage"]
        assert len(storages) == 2

    def test_storage_entity_fields(self, extractor):
        result = extractor.extract("proxmox-prod", "proxmox", "list-storage", SAMPLE_STORAGE_DATA)

        storages = [e for e in result.entities if e.entity_type == "proxmox_storage"]
        st = next(s for s in storages if s.name == "local")

        assert st.canonical_id == "proxmox:proxmox-prod:storage:local"
        assert st.raw_attributes["type"] == "dir"
        assert st.raw_attributes["total"] == 107374182400
        assert st.raw_attributes["used"] == 53687091200
        assert st.raw_attributes["avail"] == 53687091200
        assert st.raw_attributes["enabled"] == 1


class TestProxmoxExtractCephPools:
    def test_extract_ceph_pools_returns_entities(self, extractor):
        result = extractor.extract("proxmox-prod", "proxmox", "list-ceph-pools", SAMPLE_CEPH_POOL_DATA)

        pools = [e for e in result.entities if e.entity_type == "proxmox_ceph_pool"]
        assert len(pools) == 1

    def test_ceph_pool_entity_fields(self, extractor):
        result = extractor.extract("proxmox-prod", "proxmox", "list-ceph-pools", SAMPLE_CEPH_POOL_DATA)

        pools = [e for e in result.entities if e.entity_type == "proxmox_ceph_pool"]
        pool = pools[0]

        assert pool.name == "rbd-pool"
        assert pool.canonical_id == "proxmox:proxmox-prod:ceph:rbd-pool"
        assert pool.raw_attributes["size"] == 3
        assert pool.raw_attributes["min_size"] == 2
        assert pool.raw_attributes["pg_num"] == 128
        assert pool.raw_attributes["bytes_used"] == 549755813888


class TestProxmoxExtractorEdgeCases:
    def test_unknown_operation_returns_empty(self, extractor):
        result = extractor.extract("proxmox-prod", "proxmox", "power-on-vm", {"data": []})

        assert isinstance(result, ExtractionResult)
        assert len(result.entities) == 0
        assert len(result.relationships) == 0

    def test_get_vm_not_extractable(self, extractor):
        result = extractor.extract("proxmox-prod", "proxmox", "get-vm", {"data": {"vmid": 100}})

        assert len(result.entities) == 0

    def test_missing_data_key_returns_empty(self, extractor):
        result = extractor.extract("proxmox-prod", "proxmox", "list-vms", {})

        assert len(result.entities) == 0
        assert len(result.relationships) == 0

    def test_empty_data_list(self, extractor):
        result = extractor.extract("proxmox-prod", "proxmox", "list-vms", {"data": []})

        assert len(result.entities) == 0
        assert len(result.relationships) == 0

    def test_malformed_items_skipped(self, extractor):
        """Items missing required fields should be silently skipped."""
        malformed_data = {
            "data": [
                {},  # Missing name and vmid
                {
                    "vmid": 100,
                    "name": "good-vm",
                    "status": "running",
                    "node": "pve1",
                },
            ]
        }
        result = extractor.extract("proxmox-prod", "proxmox", "list-vms", malformed_data)

        vms = [e for e in result.entities if e.entity_type == "proxmox_vm"]
        assert len(vms) == 1
        assert vms[0].name == "good-vm"
