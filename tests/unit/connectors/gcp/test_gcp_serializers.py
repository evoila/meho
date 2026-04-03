# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for GCP Serializers (TASK-102)

Tests object serialization from GCP SDK types to dictionaries.
"""

from unittest.mock import MagicMock

from meho_app.modules.connectors.gcp.serializers import (
    serialize_cluster,
    serialize_disk,
    serialize_instance,
)


def create_mock_instance():
    """Create a mock Compute Engine Instance."""
    instance = MagicMock()
    instance.id = 123456789
    instance.name = "test-vm"
    instance.description = "Test VM"
    instance.zone = "https://www.googleapis.com/compute/v1/projects/my-project/zones/us-central1-a"
    instance.machine_type = "zones/us-central1-a/machineTypes/n1-standard-4"
    instance.status = "RUNNING"
    instance.status_message = None
    instance.creation_timestamp = "2024-01-15T10:00:00.000-08:00"
    instance.last_start_timestamp = "2024-01-16T08:00:00.000-08:00"
    instance.last_stop_timestamp = None
    instance.cpu_platform = "Intel Haswell"
    instance.labels = {"env": "test", "team": "dev"}
    instance.can_ip_forward = False
    instance.deletion_protection = False
    instance.fingerprint = "abc123"
    instance.self_link = "https://www.googleapis.com/compute/v1/projects/my-project/zones/us-central1-a/instances/test-vm"

    # Network interfaces
    nic = MagicMock()
    nic.name = "nic0"
    nic.network = (
        "https://www.googleapis.com/compute/v1/projects/my-project/global/networks/default"
    )
    nic.subnetwork = "https://www.googleapis.com/compute/v1/projects/my-project/regions/us-central1/subnetworks/default"
    nic.network_i_p = "10.128.0.2"

    access_config = MagicMock()
    access_config.nat_i_p = "35.192.0.1"
    nic.access_configs = [access_config]

    instance.network_interfaces = [nic]

    # Disks
    disk = MagicMock()
    disk.source = "https://www.googleapis.com/compute/v1/projects/my-project/zones/us-central1-a/disks/test-vm"
    disk.boot = True
    disk.auto_delete = True
    disk.mode = "READ_WRITE"
    disk.type_ = "PERSISTENT"
    disk.disk_size_gb = 100
    instance.disks = [disk]

    return instance


def create_mock_disk():
    """Create a mock Persistent Disk."""
    disk = MagicMock()
    disk.id = 987654321
    disk.name = "test-disk"
    disk.description = "Test disk"
    disk.zone = "https://www.googleapis.com/compute/v1/projects/my-project/zones/us-central1-a"
    disk.size_gb = 100
    disk.type_ = "https://www.googleapis.com/compute/v1/projects/my-project/zones/us-central1-a/diskTypes/pd-ssd"
    disk.status = "READY"
    disk.source_image = "https://www.googleapis.com/compute/v1/projects/debian-cloud/global/images/debian-11-bullseye-v20240115"
    disk.source_snapshot = None
    disk.users = [
        "https://www.googleapis.com/compute/v1/projects/my-project/zones/us-central1-a/instances/test-vm"
    ]
    disk.labels = {"env": "test"}
    disk.creation_timestamp = "2024-01-15T10:00:00.000-08:00"
    disk.last_attach_timestamp = "2024-01-16T08:00:00.000-08:00"
    disk.last_detach_timestamp = None
    disk.physical_block_size_bytes = 4096
    disk.self_link = "https://www.googleapis.com/compute/v1/projects/my-project/zones/us-central1-a/disks/test-disk"

    return disk


def create_mock_cluster():
    """Create a mock GKE Cluster."""
    cluster = MagicMock()
    cluster.name = "my-cluster"
    cluster.description = "Test GKE cluster"
    cluster.location = "us-central1"
    cluster.status = MagicMock()
    cluster.status.name = "RUNNING"
    cluster.status_message = None
    cluster.current_master_version = "1.27.8-gke.1067000"
    cluster.current_node_version = "1.27.8-gke.1067000"
    cluster.current_node_count = 3
    cluster.endpoint = "35.192.0.100"
    cluster.initial_cluster_version = "1.27.8-gke.1067000"
    cluster.network = "default"
    cluster.subnetwork = "default"
    cluster.cluster_ipv4_cidr = "10.44.0.0/14"
    cluster.services_ipv4_cidr = "10.48.0.0/20"
    cluster.resource_labels = {"env": "test"}
    cluster.legacy_abac = MagicMock()
    cluster.legacy_abac.enabled = False
    cluster.master_authorized_networks_config = MagicMock()
    cluster.master_authorized_networks_config.enabled = True
    cluster.create_time = "2024-01-15T10:00:00.000Z"
    cluster.expire_time = None
    cluster.self_link = "https://container.googleapis.com/v1/projects/my-project/locations/us-central1/clusters/my-cluster"

    # Node pools
    np = MagicMock()
    np.name = "default-pool"
    np.status = MagicMock()
    np.status.name = "RUNNING"
    np.initial_node_count = 3
    np.config = MagicMock()
    np.config.machine_type = "n1-standard-2"
    np.config.disk_size_gb = 100
    np.config.disk_type = "pd-standard"
    np.autoscaling = MagicMock()
    np.autoscaling.enabled = True
    np.autoscaling.min_node_count = 1
    np.autoscaling.max_node_count = 5
    cluster.node_pools = [np]

    return cluster


class TestSerializeInstance:
    """Test instance serialization."""

    def test_serialize_basic_fields(self):
        """Test basic field serialization."""
        instance = create_mock_instance()
        result = serialize_instance(instance)

        assert result["id"] == "123456789"
        assert result["name"] == "test-vm"
        assert result["status"] == "RUNNING"
        assert result["zone"] == "us-central1-a"
        assert result["machine_type"] == "n1-standard-4"

    def test_serialize_labels(self):
        """Test label serialization."""
        instance = create_mock_instance()
        result = serialize_instance(instance)

        assert result["labels"] == {"env": "test", "team": "dev"}

    def test_serialize_network_interfaces(self):
        """Test network interface serialization."""
        instance = create_mock_instance()
        result = serialize_instance(instance)

        assert len(result["network_interfaces"]) == 1
        nic = result["network_interfaces"][0]
        assert nic["internal_ip"] == "10.128.0.2"
        assert nic["external_ip"] == "35.192.0.1"
        assert nic["network"] == "default"

    def test_serialize_disks(self):
        """Test disk serialization."""
        instance = create_mock_instance()
        result = serialize_instance(instance)

        assert len(result["disks"]) == 1
        disk = result["disks"][0]
        assert disk["boot"] is True
        assert disk["name"] == "test-vm"


class TestSerializeDisk:
    """Test disk serialization."""

    def test_serialize_basic_fields(self):
        """Test basic field serialization."""
        disk = create_mock_disk()
        result = serialize_disk(disk)

        assert result["id"] == "987654321"
        assert result["name"] == "test-disk"
        assert result["size_gb"] == 100
        assert result["status"] == "READY"
        assert result["zone"] == "us-central1-a"

    def test_serialize_type(self):
        """Test disk type extraction."""
        disk = create_mock_disk()
        result = serialize_disk(disk)

        assert result["type"] == "pd-ssd"

    def test_serialize_users(self):
        """Test user extraction."""
        disk = create_mock_disk()
        result = serialize_disk(disk)

        assert "test-vm" in result["users"]


class TestSerializeCluster:
    """Test GKE cluster serialization."""

    def test_serialize_basic_fields(self):
        """Test basic field serialization."""
        cluster = create_mock_cluster()
        result = serialize_cluster(cluster)

        assert result["name"] == "my-cluster"
        assert result["location"] == "us-central1"
        assert result["status"] == "RUNNING"
        assert result["endpoint"] == "35.192.0.100"

    def test_serialize_versions(self):
        """Test version serialization."""
        cluster = create_mock_cluster()
        result = serialize_cluster(cluster)

        assert result["current_master_version"] == "1.27.8-gke.1067000"
        assert result["current_node_version"] == "1.27.8-gke.1067000"

    def test_serialize_node_pools(self):
        """Test node pool serialization."""
        cluster = create_mock_cluster()
        result = serialize_cluster(cluster)

        assert len(result["node_pools"]) == 1
        np = result["node_pools"][0]
        assert np["name"] == "default-pool"
        assert np["machine_type"] == "n1-standard-2"
        assert np["autoscaling"]["enabled"] is True
