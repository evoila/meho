"""Tests for VMwareEntityExtractor topology extraction."""

import pytest

from meho_claude.core.topology.models import ExtractionResult


# Sample PropertyCollector-style result data
SAMPLE_VM_DATA = {
    "data": [
        {
            "name": "k8s-node-01",
            "config.instanceUuid": "421A6D12-ABCD-EF01-2345-678901ABCDEF",
            "config.guestFullName": "Ubuntu 22.04",
            "config.hardware.numCPU": 4,
            "config.hardware.memoryMB": 8192,
            "summary.runtime.powerState": "poweredOn",
            "summary.runtime.host": "host-42",
            "summary.runtime.connectionState": "connected",
            "guest.ipAddress": "10.0.1.100",
            "guest.hostName": "k8s-node-01",
            "guest.toolsStatus": "toolsOk",
            "resourcePool": "resgroup-8",
            "network": ["network-10", "network-11"],
            "datastore": ["datastore-15"],
            "_moref": "vim.VirtualMachine:vm-100",
        },
        {
            "name": "db-server-01",
            "config.instanceUuid": "BBBBB111-2222-3333-4444-555566667777",
            "config.guestFullName": "CentOS 8",
            "config.hardware.numCPU": 8,
            "config.hardware.memoryMB": 16384,
            "summary.runtime.powerState": "poweredOff",
            "summary.runtime.host": "host-43",
            "summary.runtime.connectionState": "connected",
            "guest.ipAddress": None,
            "guest.hostName": None,
            "guest.toolsStatus": "toolsNotRunning",
            "resourcePool": "resgroup-8",
            "network": ["network-10"],
            "datastore": ["datastore-15", "datastore-16"],
            "_moref": "vim.VirtualMachine:vm-101",
        },
    ]
}

SAMPLE_HOST_DATA = {
    "data": [
        {
            "name": "esxi-01.lab.local",
            "summary.hardware.cpuModel": "Intel Xeon Gold 6248",
            "summary.hardware.numCpuCores": 20,
            "summary.hardware.memorySize": 137438953472,
            "summary.runtime.connectionState": "connected",
            "summary.runtime.powerState": "poweredOn",
            "parent": "domain-c7",
            "_moref": "vim.HostSystem:host-42",
        },
    ]
}

SAMPLE_CLUSTER_DATA = {
    "data": [
        {
            "name": "Production-Cluster",
            "summary.numHosts": 3,
            "summary.numEffectiveHosts": 3,
            "summary.totalCpu": 120000,
            "summary.totalMemory": 412316860416,
            "_moref": "vim.ClusterComputeResource:domain-c7",
        },
    ]
}

SAMPLE_DATASTORE_DATA = {
    "data": [
        {
            "name": "vsan-datastore-1",
            "summary.type": "vsan",
            "summary.capacity": 10995116277760,
            "summary.freeSpace": 5497558138880,
            "summary.accessible": True,
            "_moref": "vim.Datastore:datastore-15",
        },
    ]
}

SAMPLE_NETWORK_DATA = {
    "data": [
        {
            "name": "VM Network",
            "summary.accessible": True,
            "_moref": "vim.Network:network-10",
        },
    ]
}


@pytest.fixture
def extractor():
    from meho_claude.core.topology.extractors.vmware import VMwareEntityExtractor

    return VMwareEntityExtractor()


class TestVMwareExtractorRegistration:
    def test_registered_in_extractor_registry(self):
        from meho_claude.core.topology.extractors.vmware import VMwareEntityExtractor
        from meho_claude.core.topology.extractor import get_extractor_class

        # Import extractors package to trigger registration
        import meho_claude.core.topology.extractors  # noqa: F401

        cls = get_extractor_class("vmware")
        assert cls is VMwareEntityExtractor


class TestVMwareExtractVMs:
    def test_extract_vms_returns_entities(self, extractor):
        result = extractor.extract("vcenter-prod", "vmware", "list-vms", SAMPLE_VM_DATA)

        assert isinstance(result, ExtractionResult)
        assert result.source_connector == "vcenter-prod"
        assert result.source_operation == "list-vms"

        vms = [e for e in result.entities if e.entity_type == "vmware_vm"]
        assert len(vms) == 2

    def test_vm_entity_fields(self, extractor):
        result = extractor.extract("vcenter-prod", "vmware", "list-vms", SAMPLE_VM_DATA)

        vms = [e for e in result.entities if e.entity_type == "vmware_vm"]
        vm = next(v for v in vms if v.name == "k8s-node-01")

        assert vm.connector_name == "vcenter-prod"
        assert vm.connector_type == "vmware"
        assert vm.canonical_id == "421A6D12-ABCD-EF01-2345-678901ABCDEF"
        assert "VMware VM k8s-node-01" in vm.description

    def test_vm_provider_id_is_lowercase_vsphere_format(self, extractor):
        """CRITICAL: provider_id must be vsphere://<lowercase-uuid> for SAME_AS correlation."""
        result = extractor.extract("vcenter-prod", "vmware", "list-vms", SAMPLE_VM_DATA)

        vms = [e for e in result.entities if e.entity_type == "vmware_vm"]
        vm = next(v for v in vms if v.name == "k8s-node-01")

        provider_id = vm.raw_attributes["provider_id"]
        assert provider_id == "vsphere://421a6d12-abcd-ef01-2345-678901abcdef"

    def test_vm_provider_id_uppercase_input_normalized(self, extractor):
        """Uppercase UUID in config.instanceUuid must be normalized to lowercase."""
        result = extractor.extract("vcenter-prod", "vmware", "list-vms", SAMPLE_VM_DATA)

        vms = [e for e in result.entities if e.entity_type == "vmware_vm"]
        vm = next(v for v in vms if v.name == "db-server-01")

        provider_id = vm.raw_attributes["provider_id"]
        # Must be all lowercase
        assert provider_id == provider_id.lower()
        assert provider_id.startswith("vsphere://")

    def test_vm_raw_attributes_contain_ip_hostname_power_state(self, extractor):
        result = extractor.extract("vcenter-prod", "vmware", "list-vms", SAMPLE_VM_DATA)

        vms = [e for e in result.entities if e.entity_type == "vmware_vm"]
        vm = next(v for v in vms if v.name == "k8s-node-01")

        assert vm.raw_attributes["ip_address"] == "10.0.1.100"
        assert vm.raw_attributes["hostname"] == "k8s-node-01"
        assert vm.raw_attributes["power_state"] == "poweredOn"

    def test_vm_raw_attributes_contain_hardware_info(self, extractor):
        result = extractor.extract("vcenter-prod", "vmware", "list-vms", SAMPLE_VM_DATA)

        vms = [e for e in result.entities if e.entity_type == "vmware_vm"]
        vm = next(v for v in vms if v.name == "k8s-node-01")

        assert vm.raw_attributes["cpu"] == 4
        assert vm.raw_attributes["memory_mb"] == 8192
        assert vm.raw_attributes["guest_os"] == "Ubuntu 22.04"
        assert vm.raw_attributes["tools_status"] == "toolsOk"
        assert vm.raw_attributes["connection_state"] == "connected"

    def test_vm_raw_attributes_store_references(self, extractor):
        """VM raw_attributes should store host, datastore, network references for future reconciliation."""
        result = extractor.extract("vcenter-prod", "vmware", "list-vms", SAMPLE_VM_DATA)

        vms = [e for e in result.entities if e.entity_type == "vmware_vm"]
        vm = next(v for v in vms if v.name == "k8s-node-01")

        assert vm.raw_attributes["host_ref"] == "host-42"
        assert vm.raw_attributes["datastore_refs"] == ["datastore-15"]
        assert vm.raw_attributes["network_refs"] == ["network-10", "network-11"]

    def test_vm_with_none_ip_and_hostname(self, extractor):
        """VMs that are powered off may have None ip/hostname -- should be empty string."""
        result = extractor.extract("vcenter-prod", "vmware", "list-vms", SAMPLE_VM_DATA)

        vms = [e for e in result.entities if e.entity_type == "vmware_vm"]
        vm = next(v for v in vms if v.name == "db-server-01")

        # None should be stored as empty string for correlation matching
        assert vm.raw_attributes["ip_address"] == ""
        assert vm.raw_attributes["hostname"] == ""


class TestVMwareExtractHosts:
    def test_extract_hosts_returns_entities(self, extractor):
        result = extractor.extract("vcenter-prod", "vmware", "list-hosts", SAMPLE_HOST_DATA)

        hosts = [e for e in result.entities if e.entity_type == "vmware_host"]
        assert len(hosts) == 1

    def test_host_entity_fields(self, extractor):
        result = extractor.extract("vcenter-prod", "vmware", "list-hosts", SAMPLE_HOST_DATA)

        host = [e for e in result.entities if e.entity_type == "vmware_host"][0]
        assert host.name == "esxi-01.lab.local"
        assert host.connector_name == "vcenter-prod"
        assert host.connector_type == "vmware"
        assert host.canonical_id == "vim.HostSystem:host-42"

    def test_host_raw_attributes(self, extractor):
        result = extractor.extract("vcenter-prod", "vmware", "list-hosts", SAMPLE_HOST_DATA)

        host = [e for e in result.entities if e.entity_type == "vmware_host"][0]
        assert host.raw_attributes["cpu_model"] == "Intel Xeon Gold 6248"
        assert host.raw_attributes["cpu_cores"] == 20
        assert host.raw_attributes["memory_size"] == 137438953472
        assert host.raw_attributes["connection_state"] == "connected"
        assert host.raw_attributes["power_state"] == "poweredOn"
        assert host.raw_attributes["parent_ref"] == "domain-c7"


class TestVMwareExtractClusters:
    def test_extract_clusters_returns_entities(self, extractor):
        result = extractor.extract("vcenter-prod", "vmware", "list-clusters", SAMPLE_CLUSTER_DATA)

        clusters = [e for e in result.entities if e.entity_type == "vmware_cluster"]
        assert len(clusters) == 1

    def test_cluster_entity_fields(self, extractor):
        result = extractor.extract("vcenter-prod", "vmware", "list-clusters", SAMPLE_CLUSTER_DATA)

        cluster = [e for e in result.entities if e.entity_type == "vmware_cluster"][0]
        assert cluster.name == "Production-Cluster"
        assert cluster.canonical_id == "vim.ClusterComputeResource:domain-c7"


class TestVMwareExtractDatastores:
    def test_extract_datastores_returns_entities(self, extractor):
        result = extractor.extract("vcenter-prod", "vmware", "list-datastores", SAMPLE_DATASTORE_DATA)

        datastores = [e for e in result.entities if e.entity_type == "vmware_datastore"]
        assert len(datastores) == 1

    def test_datastore_entity_fields(self, extractor):
        result = extractor.extract("vcenter-prod", "vmware", "list-datastores", SAMPLE_DATASTORE_DATA)

        ds = [e for e in result.entities if e.entity_type == "vmware_datastore"][0]
        assert ds.name == "vsan-datastore-1"
        assert ds.canonical_id == "vim.Datastore:datastore-15"


class TestVMwareExtractNetworks:
    def test_extract_networks_returns_entities(self, extractor):
        result = extractor.extract("vcenter-prod", "vmware", "list-networks", SAMPLE_NETWORK_DATA)

        networks = [e for e in result.entities if e.entity_type == "vmware_network"]
        assert len(networks) == 1

    def test_network_entity_fields(self, extractor):
        result = extractor.extract("vcenter-prod", "vmware", "list-networks", SAMPLE_NETWORK_DATA)

        net = [e for e in result.entities if e.entity_type == "vmware_network"][0]
        assert net.name == "VM Network"
        assert net.canonical_id == "vim.Network:network-10"


class TestVMwareExtractorEdgeCases:
    def test_unknown_operation_returns_empty(self, extractor):
        result = extractor.extract("vcenter-prod", "vmware", "power-on-vm", {"data": []})

        assert isinstance(result, ExtractionResult)
        assert len(result.entities) == 0
        assert len(result.relationships) == 0

    def test_missing_data_key_returns_empty(self, extractor):
        result = extractor.extract("vcenter-prod", "vmware", "list-vms", {})

        assert len(result.entities) == 0
        assert len(result.relationships) == 0

    def test_malformed_items_skipped(self, extractor):
        """Items missing required fields should be silently skipped."""
        malformed_data = {
            "data": [
                {},  # Missing name and _moref
                {
                    "name": "good-vm",
                    "config.instanceUuid": "valid-uuid",
                    "_moref": "vim.VirtualMachine:vm-200",
                },
            ]
        }
        result = extractor.extract("vcenter-prod", "vmware", "list-vms", malformed_data)

        vms = [e for e in result.entities if e.entity_type == "vmware_vm"]
        assert len(vms) == 1
        assert vms[0].name == "good-vm"

    def test_empty_data_list(self, extractor):
        result = extractor.extract("vcenter-prod", "vmware", "list-vms", {"data": []})

        assert len(result.entities) == 0
        assert len(result.relationships) == 0


class TestVMwareProviderIdCrossSystemMatch:
    """CRITICAL: Verify provider_id format matches K8s node format for SAME_AS correlation."""

    def test_provider_id_format_matches_k8s_node(self):
        """The K8s and VMware extractors must produce identical provider_id for the same VM."""
        from meho_claude.core.topology.extractors.kubernetes import K8sEntityExtractor
        from meho_claude.core.topology.extractors.vmware import VMwareEntityExtractor

        k8s = K8sEntityExtractor()
        vmware = VMwareEntityExtractor()

        # K8s node with vsphere providerID
        k8s_data = {
            "data": {
                "items": [
                    {
                        "metadata": {"uid": "node-uid-1", "name": "k8s-node-01"},
                        "spec": {"providerID": "vsphere://421A6D12-ABCD-EF01-2345-678901ABCDEF"},
                        "status": {
                            "addresses": [
                                {"type": "InternalIP", "address": "10.0.1.100"},
                                {"type": "Hostname", "address": "k8s-node-01"},
                            ],
                        },
                    }
                ]
            }
        }

        # VMware VM with matching instanceUuid (lowercase)
        vmware_data = {
            "data": [
                {
                    "name": "k8s-node-01",
                    "config.instanceUuid": "421a6d12-abcd-ef01-2345-678901abcdef",
                    "guest.ipAddress": "10.0.1.100",
                    "guest.hostName": "k8s-node-01",
                    "_moref": "vm-100",
                },
            ]
        }

        k8s_result = k8s.extract("k8s-prod", "kubernetes", "list-nodes", k8s_data)
        vmware_result = vmware.extract("vcenter-prod", "vmware", "list-vms", vmware_data)

        k8s_node = k8s_result.entities[0]
        vmware_vm = vmware_result.entities[0]

        assert k8s_node.raw_attributes["provider_id"] == vmware_vm.raw_attributes["provider_id"]
        assert k8s_node.raw_attributes["provider_id"] == "vsphere://421a6d12-abcd-ef01-2345-678901abcdef"
