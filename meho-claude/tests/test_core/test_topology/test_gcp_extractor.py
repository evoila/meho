"""Tests for GCPEntityExtractor topology extraction."""

import pytest

from meho_claude.core.topology.models import ExtractionResult


# Sample GCP API response data (post-MessageToDict serialization)
SAMPLE_COMPUTE_INSTANCES = {
    "data": [
        {
            "name": "web-server-01",
            "zone": "projects/my-project/zones/us-central1-a",
            "machineType": "projects/my-project/zones/us-central1-a/machineTypes/e2-medium",
            "status": "RUNNING",
            "networkInterfaces": [
                {
                    "networkIP": "10.128.0.2",
                    "accessConfigs": [
                        {"natIP": "35.192.0.1", "type": "ONE_TO_ONE_NAT"}
                    ],
                }
            ],
        },
        {
            "name": "db-server-01",
            "zone": "projects/my-project/zones/us-central1-b",
            "machineType": "projects/my-project/zones/us-central1-b/machineTypes/n2-standard-4",
            "status": "TERMINATED",
            "networkInterfaces": [
                {
                    "networkIP": "10.128.0.3",
                }
            ],
        },
    ]
}

SAMPLE_COMPUTE_NO_NETWORK = {
    "data": [
        {
            "name": "isolated-vm",
            "zone": "projects/my-project/zones/us-east1-a",
            "machineType": "e2-micro",
            "status": "RUNNING",
            # No networkInterfaces key at all
        },
    ]
}

SAMPLE_GKE_CLUSTERS = {
    "data": [
        {
            "name": "prod-cluster",
            "location": "us-central1",
            "status": "RUNNING",
            "endpoint": "34.123.45.67",
            "currentNodeCount": 3,
            "nodeConfig": {"machineType": "e2-standard-4"},
        },
        {
            "name": "staging-cluster",
            "location": "us-east1",
            "status": "RUNNING",
            "endpoint": "35.200.100.50",
            "currentNodeCount": 1,
        },
    ]
}

SAMPLE_SQL_INSTANCES = {
    "data": [
        {
            "name": "main-db",
            "databaseVersion": "POSTGRES_14",
            "region": "us-central1",
            "state": "RUNNABLE",
            "ipAddresses": [
                {"type": "PRIMARY", "ipAddress": "10.0.0.50"},
                {"type": "OUTGOING", "ipAddress": "34.100.0.1"},
            ],
        },
        {
            "name": "replica-db",
            "databaseVersion": "POSTGRES_14",
            "region": "us-east1",
            "state": "RUNNABLE",
            "ipAddresses": [],
        },
    ]
}

SAMPLE_VPC_NETWORKS = {
    "data": [
        {
            "name": "default",
            "autoCreateSubnetworks": True,
            "routingConfig": {"routingMode": "REGIONAL"},
        },
        {
            "name": "custom-vpc",
            "autoCreateSubnetworks": False,
            "routingConfig": {"routingMode": "GLOBAL"},
        },
    ]
}


@pytest.fixture
def extractor():
    from meho_claude.core.topology.extractors.gcp import GCPEntityExtractor

    return GCPEntityExtractor()


class TestGCPExtractorRegistration:
    def test_registered_in_extractor_registry(self):
        from meho_claude.core.topology.extractors.gcp import GCPEntityExtractor
        from meho_claude.core.topology.extractor import get_extractor_class

        # Import extractors package to trigger registration
        import meho_claude.core.topology.extractors  # noqa: F401

        cls = get_extractor_class("gcp")
        assert cls is GCPEntityExtractor


class TestGCPExtractComputeInstances:
    def test_extract_instances_returns_entities(self, extractor):
        result = extractor.extract("gcp-prod", "gcp", "compute-list-instances", SAMPLE_COMPUTE_INSTANCES)

        assert isinstance(result, ExtractionResult)
        assert result.source_connector == "gcp-prod"
        assert result.source_operation == "compute-list-instances"

        instances = [e for e in result.entities if e.entity_type == "gcp_instance"]
        assert len(instances) == 2

    def test_instance_entity_fields(self, extractor):
        result = extractor.extract("gcp-prod", "gcp", "compute-list-instances", SAMPLE_COMPUTE_INSTANCES)

        instances = [e for e in result.entities if e.entity_type == "gcp_instance"]
        inst = next(i for i in instances if i.name == "web-server-01")

        assert inst.connector_name == "gcp-prod"
        assert inst.connector_type == "gcp"
        assert inst.canonical_id == "gcp:gcp-prod:instance:web-server-01"
        assert "GCP" in inst.description or "gcp" in inst.description.lower()

    def test_instance_ip_address_from_network_interfaces(self, extractor):
        """Internal IP extracted from networkInterfaces[0].networkIP."""
        result = extractor.extract("gcp-prod", "gcp", "compute-list-instances", SAMPLE_COMPUTE_INSTANCES)

        instances = [e for e in result.entities if e.entity_type == "gcp_instance"]
        inst = next(i for i in instances if i.name == "web-server-01")

        assert inst.raw_attributes["ip_address"] == "10.128.0.2"

    def test_instance_external_ip_from_access_configs(self, extractor):
        """External IP extracted from networkInterfaces[0].accessConfigs[0].natIP."""
        result = extractor.extract("gcp-prod", "gcp", "compute-list-instances", SAMPLE_COMPUTE_INSTANCES)

        instances = [e for e in result.entities if e.entity_type == "gcp_instance"]
        inst = next(i for i in instances if i.name == "web-server-01")

        assert inst.raw_attributes["external_ip"] == "35.192.0.1"

    def test_instance_without_external_ip(self, extractor):
        """Instance without accessConfigs should have empty external_ip."""
        result = extractor.extract("gcp-prod", "gcp", "compute-list-instances", SAMPLE_COMPUTE_INSTANCES)

        instances = [e for e in result.entities if e.entity_type == "gcp_instance"]
        inst = next(i for i in instances if i.name == "db-server-01")

        assert inst.raw_attributes["external_ip"] == ""

    def test_instance_hostname_defaults_to_name(self, extractor):
        """hostname defaults to the instance name."""
        result = extractor.extract("gcp-prod", "gcp", "compute-list-instances", SAMPLE_COMPUTE_INSTANCES)

        instances = [e for e in result.entities if e.entity_type == "gcp_instance"]
        inst = next(i for i in instances if i.name == "web-server-01")

        assert inst.raw_attributes["hostname"] == "web-server-01"

    def test_instance_without_network_interfaces(self, extractor):
        """Instance with no networkInterfaces should have empty IPs."""
        result = extractor.extract("gcp-prod", "gcp", "compute-list-instances", SAMPLE_COMPUTE_NO_NETWORK)

        instances = [e for e in result.entities if e.entity_type == "gcp_instance"]
        assert len(instances) == 1

        inst = instances[0]
        assert inst.raw_attributes["ip_address"] == ""
        assert inst.raw_attributes["external_ip"] == ""

    def test_instance_raw_attributes_contain_metadata(self, extractor):
        result = extractor.extract("gcp-prod", "gcp", "compute-list-instances", SAMPLE_COMPUTE_INSTANCES)

        instances = [e for e in result.entities if e.entity_type == "gcp_instance"]
        inst = next(i for i in instances if i.name == "web-server-01")

        assert inst.raw_attributes["status"] == "RUNNING"
        assert "zone" in inst.raw_attributes
        assert "machine_type" in inst.raw_attributes


class TestGCPExtractGKEClusters:
    def test_extract_gke_clusters_returns_entities(self, extractor):
        result = extractor.extract("gcp-prod", "gcp", "gke-list-clusters", SAMPLE_GKE_CLUSTERS)

        clusters = [e for e in result.entities if e.entity_type == "gcp_gke_cluster"]
        assert len(clusters) == 2

    def test_gke_cluster_entity_fields(self, extractor):
        result = extractor.extract("gcp-prod", "gcp", "gke-list-clusters", SAMPLE_GKE_CLUSTERS)

        clusters = [e for e in result.entities if e.entity_type == "gcp_gke_cluster"]
        cluster = next(c for c in clusters if c.name == "prod-cluster")

        assert cluster.connector_name == "gcp-prod"
        assert cluster.connector_type == "gcp"
        assert cluster.canonical_id == "gcp:gcp-prod:gke:prod-cluster"

    def test_gke_cluster_endpoint_ip_for_k8s_correlation(self, extractor):
        """CRITICAL: GKE endpoint IP stored in ip_address for SAME_AS with K8s connectors."""
        result = extractor.extract("gcp-prod", "gcp", "gke-list-clusters", SAMPLE_GKE_CLUSTERS)

        clusters = [e for e in result.entities if e.entity_type == "gcp_gke_cluster"]
        cluster = next(c for c in clusters if c.name == "prod-cluster")

        assert cluster.raw_attributes["ip_address"] == "34.123.45.67"
        assert cluster.raw_attributes["endpoint"] == "34.123.45.67"

    def test_gke_cluster_raw_attributes(self, extractor):
        result = extractor.extract("gcp-prod", "gcp", "gke-list-clusters", SAMPLE_GKE_CLUSTERS)

        clusters = [e for e in result.entities if e.entity_type == "gcp_gke_cluster"]
        cluster = next(c for c in clusters if c.name == "prod-cluster")

        assert cluster.raw_attributes["location"] == "us-central1"
        assert cluster.raw_attributes["status"] == "RUNNING"
        assert cluster.raw_attributes["node_count"] == 3


class TestGCPExtractCloudSQL:
    def test_extract_sql_instances_returns_entities(self, extractor):
        result = extractor.extract("gcp-prod", "gcp", "cloudsql-list-instances", SAMPLE_SQL_INSTANCES)

        sql_instances = [e for e in result.entities if e.entity_type == "gcp_sql_instance"]
        assert len(sql_instances) == 2

    def test_sql_instance_entity_fields(self, extractor):
        result = extractor.extract("gcp-prod", "gcp", "cloudsql-list-instances", SAMPLE_SQL_INSTANCES)

        sql_instances = [e for e in result.entities if e.entity_type == "gcp_sql_instance"]
        inst = next(i for i in sql_instances if i.name == "main-db")

        assert inst.connector_name == "gcp-prod"
        assert inst.connector_type == "gcp"
        assert inst.canonical_id == "gcp:gcp-prod:sql:main-db"

    def test_sql_instance_ip_from_ipaddresses(self, extractor):
        """Primary IP extracted from ipAddresses list."""
        result = extractor.extract("gcp-prod", "gcp", "cloudsql-list-instances", SAMPLE_SQL_INSTANCES)

        sql_instances = [e for e in result.entities if e.entity_type == "gcp_sql_instance"]
        inst = next(i for i in sql_instances if i.name == "main-db")

        assert inst.raw_attributes["ip_address"] == "10.0.0.50"

    def test_sql_instance_empty_ipaddresses(self, extractor):
        """Instance with empty ipAddresses list should have empty ip_address."""
        result = extractor.extract("gcp-prod", "gcp", "cloudsql-list-instances", SAMPLE_SQL_INSTANCES)

        sql_instances = [e for e in result.entities if e.entity_type == "gcp_sql_instance"]
        inst = next(i for i in sql_instances if i.name == "replica-db")

        assert inst.raw_attributes["ip_address"] == ""

    def test_sql_instance_raw_attributes(self, extractor):
        result = extractor.extract("gcp-prod", "gcp", "cloudsql-list-instances", SAMPLE_SQL_INSTANCES)

        sql_instances = [e for e in result.entities if e.entity_type == "gcp_sql_instance"]
        inst = next(i for i in sql_instances if i.name == "main-db")

        assert inst.raw_attributes["database_version"] == "POSTGRES_14"
        assert inst.raw_attributes["region"] == "us-central1"
        assert inst.raw_attributes["state"] == "RUNNABLE"


class TestGCPExtractVPCNetworks:
    def test_extract_vpc_networks_returns_entities(self, extractor):
        result = extractor.extract("gcp-prod", "gcp", "vpc-list-networks", SAMPLE_VPC_NETWORKS)

        networks = [e for e in result.entities if e.entity_type == "gcp_vpc_network"]
        assert len(networks) == 2

    def test_vpc_network_entity_fields(self, extractor):
        result = extractor.extract("gcp-prod", "gcp", "vpc-list-networks", SAMPLE_VPC_NETWORKS)

        networks = [e for e in result.entities if e.entity_type == "gcp_vpc_network"]
        net = next(n for n in networks if n.name == "default")

        assert net.connector_name == "gcp-prod"
        assert net.connector_type == "gcp"
        assert net.canonical_id == "gcp:gcp-prod:vpc:default"

    def test_vpc_network_raw_attributes(self, extractor):
        result = extractor.extract("gcp-prod", "gcp", "vpc-list-networks", SAMPLE_VPC_NETWORKS)

        networks = [e for e in result.entities if e.entity_type == "gcp_vpc_network"]
        net = next(n for n in networks if n.name == "default")

        assert net.raw_attributes["auto_create_subnetworks"] is True
        assert net.raw_attributes["routing_config"] == {"routingMode": "REGIONAL"}


class TestGCPExtractorEdgeCases:
    def test_non_extractable_operation_returns_empty(self, extractor):
        """Monitoring and get-* operations are not extractable."""
        result = extractor.extract("gcp-prod", "gcp", "monitoring-query-metrics", {"data": []})

        assert isinstance(result, ExtractionResult)
        assert len(result.entities) == 0
        assert len(result.relationships) == 0

    def test_get_operation_returns_empty(self, extractor):
        """get-* operations return details for known entities, not for extraction."""
        result = extractor.extract("gcp-prod", "gcp", "compute-get-instance", {"data": {}})

        assert len(result.entities) == 0

    def test_missing_data_key_returns_empty(self, extractor):
        result = extractor.extract("gcp-prod", "gcp", "compute-list-instances", {})

        assert len(result.entities) == 0
        assert len(result.relationships) == 0

    def test_empty_data_list(self, extractor):
        result = extractor.extract("gcp-prod", "gcp", "compute-list-instances", {"data": []})

        assert len(result.entities) == 0

    def test_malformed_items_skipped(self, extractor):
        """Items missing name should be silently skipped."""
        malformed_data = {
            "data": [
                {},  # Missing name
                {
                    "name": "good-instance",
                    "status": "RUNNING",
                },
            ]
        }
        result = extractor.extract("gcp-prod", "gcp", "compute-list-instances", malformed_data)

        instances = [e for e in result.entities if e.entity_type == "gcp_instance"]
        assert len(instances) == 1
        assert instances[0].name == "good-instance"

    def test_unknown_operation_returns_empty(self, extractor):
        result = extractor.extract("gcp-prod", "gcp", "unknown-operation", {"data": []})

        assert isinstance(result, ExtractionResult)
        assert len(result.entities) == 0

    def test_none_ip_normalized_to_empty_string(self, extractor):
        """None/null in IP fields should be normalized to empty string."""
        data = {
            "data": [
                {
                    "name": "null-ip-instance",
                    "status": "RUNNING",
                    "networkInterfaces": [
                        {"networkIP": None}
                    ],
                },
            ]
        }
        result = extractor.extract("gcp-prod", "gcp", "compute-list-instances", data)

        instances = [e for e in result.entities if e.entity_type == "gcp_instance"]
        assert instances[0].raw_attributes["ip_address"] == ""
