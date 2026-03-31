"""Tests for GCPConnector with mocked Google Cloud SDK clients."""

import asyncio
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from meho_claude.core.connectors.models import (
    ConnectorConfig,
    Operation,
    TrustOverride,
)


@pytest.fixture
def gcp_config():
    """GCP connector config with ADC (no service account path)."""
    return ConnectorConfig(
        name="gcp-prod",
        connector_type="gcp",
        project_id="my-project-123",
    )


@pytest.fixture
def gcp_config_sa():
    """GCP connector config with service account JSON fallback."""
    return ConnectorConfig(
        name="gcp-dev",
        connector_type="gcp",
        project_id="dev-project-456",
        service_account_path="/path/to/service-account.json",
    )


@pytest.fixture
def gcp_config_no_project():
    """GCP connector config with no project_id."""
    return ConnectorConfig(
        name="gcp-bad",
        connector_type="gcp",
    )


# --- Registration Tests ---


class TestGCPConnectorRegistration:
    def test_gcp_registered_in_registry(self):
        from meho_claude.core.connectors.gcp import GCPConnector
        from meho_claude.core.connectors.registry import get_connector_class

        cls = get_connector_class("gcp")
        assert cls is GCPConnector

    def test_gcp_in_list(self):
        from meho_claude.core.connectors.gcp import GCPConnector  # noqa: F401
        from meho_claude.core.connectors.registry import list_connector_types

        types = list_connector_types()
        assert "gcp" in types


# --- Discover Operations Tests ---


class TestGCPConnectorDiscoverOperations:
    def test_discover_returns_9_operations(self, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        connector = GCPConnector(gcp_config)
        ops = asyncio.run(connector.discover_operations())
        assert len(ops) == 9

    def test_discover_returns_operation_models(self, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        connector = GCPConnector(gcp_config)
        ops = asyncio.run(connector.discover_operations())
        for op in ops:
            assert isinstance(op, Operation)
            assert op.connector_name == "gcp-prod"

    def test_discover_includes_all_operation_ids(self, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        connector = GCPConnector(gcp_config)
        ops = asyncio.run(connector.discover_operations())
        op_ids = {op.operation_id for op in ops}
        expected = {
            "compute-list-instances",
            "compute-get-instance",
            "gke-list-clusters",
            "gke-get-cluster",
            "monitoring-query-metrics",
            "cloudsql-list-instances",
            "cloudsql-get-instance",
            "vpc-list-networks",
            "vpc-list-subnetworks",
        }
        assert op_ids == expected

    def test_all_operations_are_read(self, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        connector = GCPConnector(gcp_config)
        ops = asyncio.run(connector.discover_operations())
        for op in ops:
            assert op.trust_tier == "READ", f"{op.operation_id} should be READ"


# --- Credential Tests ---


class TestGCPConnectorCredentials:
    @patch("meho_claude.core.connectors.gcp.google_auth_default")
    def test_adc_credentials_used_by_default(self, mock_auth_default, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        mock_creds = MagicMock()
        mock_auth_default.return_value = (mock_creds, "my-project-123")

        connector = GCPConnector(gcp_config)
        creds = connector._get_credentials()

        assert creds is mock_creds
        mock_auth_default.assert_called_once()

    @patch("meho_claude.core.connectors.gcp.service_account_Credentials")
    def test_service_account_credentials_when_path_set(self, mock_sa, gcp_config_sa):
        from meho_claude.core.connectors.gcp import GCPConnector

        mock_creds = MagicMock()
        mock_sa.from_service_account_file.return_value = mock_creds

        connector = GCPConnector(gcp_config_sa)
        creds = connector._get_credentials()

        assert creds is mock_creds
        mock_sa.from_service_account_file.assert_called_once_with(
            "/path/to/service-account.json",
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )


# --- Project ID Tests ---


class TestGCPConnectorProjectId:
    def test_get_project_id_returns_config_value(self, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        connector = GCPConnector(gcp_config)
        assert connector._get_project_id() == "my-project-123"

    def test_get_project_id_raises_if_none(self, gcp_config_no_project):
        from meho_claude.core.connectors.gcp import GCPConnector

        connector = GCPConnector(gcp_config_no_project)
        with pytest.raises(ValueError, match="project_id"):
            connector._get_project_id()


# --- Test Connection Tests ---


class TestGCPConnectorTestConnection:
    @patch("meho_claude.core.connectors.gcp.asyncio.to_thread")
    @patch("meho_claude.core.connectors.gcp.google_auth_default")
    @pytest.mark.asyncio
    async def test_test_connection_success(self, mock_auth_default, mock_to_thread, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        mock_creds = MagicMock()
        mock_auth_default.return_value = (mock_creds, "my-project-123")

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = GCPConnector(gcp_config)

        with patch("meho_claude.core.connectors.gcp.compute_v1") as mock_compute:
            mock_client = MagicMock()
            mock_compute.InstancesClient.return_value = mock_client
            mock_compute.AggregatedListInstancesRequest.return_value = MagicMock()
            mock_client.aggregated_list.return_value = iter([])

            result = await connector.test_connection()

        assert result["status"] == "ok"
        assert result["project"] == "my-project-123"

    @patch("meho_claude.core.connectors.gcp.asyncio.to_thread")
    @patch("meho_claude.core.connectors.gcp.google_auth_default")
    @pytest.mark.asyncio
    async def test_test_connection_failure(self, mock_auth_default, mock_to_thread, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        mock_auth_default.side_effect = Exception("Could not find default credentials")

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = GCPConnector(gcp_config)
        result = await connector.test_connection()

        assert result["status"] == "error"
        assert "credentials" in result["message"].lower()


# --- Execute Tests ---


class TestGCPConnectorExecuteComputeListInstances:
    @patch("meho_claude.core.connectors.gcp.asyncio.to_thread")
    @patch("meho_claude.core.connectors.gcp.google_auth_default")
    @pytest.mark.asyncio
    async def test_execute_compute_list_instances(self, mock_auth_default, mock_to_thread, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        mock_creds = MagicMock()
        mock_auth_default.return_value = (mock_creds, "my-project-123")

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = GCPConnector(gcp_config)

        with patch("meho_claude.core.connectors.gcp.compute_v1") as mock_compute:
            mock_client = MagicMock()
            mock_compute.InstancesClient.return_value = mock_client
            mock_compute.AggregatedListInstancesRequest.return_value = MagicMock()

            # Simulate aggregated_list response: zone -> instances
            mock_instance = MagicMock()
            mock_instance.name = "instance-1"
            mock_instance.zone = "us-central1-a"
            mock_instance.status = "RUNNING"

            mock_scoped = MagicMock()
            mock_scoped.instances = [mock_instance]

            mock_client.aggregated_list.return_value = iter([
                ("zones/us-central1-a", mock_scoped)
            ])

            # Mock _proto_to_dict to return a plain dict
            connector._proto_to_dict = MagicMock(return_value={"name": "instance-1", "zone": "us-central1-a"})

            op = Operation(
                connector_name="gcp-prod",
                operation_id="compute-list-instances",
                display_name="List Compute Instances",
            )
            result = await connector.execute(op, {})

        assert "data" in result
        assert isinstance(result["data"], list)
        assert len(result["data"]) == 1


class TestGCPConnectorExecuteComputeGetInstance:
    @patch("meho_claude.core.connectors.gcp.asyncio.to_thread")
    @patch("meho_claude.core.connectors.gcp.google_auth_default")
    @pytest.mark.asyncio
    async def test_execute_compute_get_instance(self, mock_auth_default, mock_to_thread, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        mock_creds = MagicMock()
        mock_auth_default.return_value = (mock_creds, "my-project-123")

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = GCPConnector(gcp_config)

        with patch("meho_claude.core.connectors.gcp.compute_v1") as mock_compute:
            mock_client = MagicMock()
            mock_compute.InstancesClient.return_value = mock_client

            mock_instance = MagicMock()
            mock_instance.name = "instance-1"
            mock_client.get.return_value = mock_instance

            connector._proto_to_dict = MagicMock(return_value={"name": "instance-1"})

            op = Operation(
                connector_name="gcp-prod",
                operation_id="compute-get-instance",
                display_name="Get Compute Instance",
            )
            result = await connector.execute(op, {"zone": "us-central1-a", "instance_name": "instance-1"})

        assert "data" in result
        assert result["data"]["name"] == "instance-1"


class TestGCPConnectorExecuteGKE:
    @patch("meho_claude.core.connectors.gcp.asyncio.to_thread")
    @patch("meho_claude.core.connectors.gcp.google_auth_default")
    @pytest.mark.asyncio
    async def test_execute_gke_list_clusters(self, mock_auth_default, mock_to_thread, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        mock_creds = MagicMock()
        mock_auth_default.return_value = (mock_creds, "my-project-123")

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = GCPConnector(gcp_config)

        with patch("meho_claude.core.connectors.gcp.container_v1") as mock_container:
            mock_client = MagicMock()
            mock_container.ClusterManagerClient.return_value = mock_client

            mock_cluster = MagicMock()
            mock_cluster.name = "prod-cluster"
            mock_response = MagicMock()
            mock_response.clusters = [mock_cluster]
            mock_client.list_clusters.return_value = mock_response

            connector._proto_to_dict = MagicMock(return_value={"name": "prod-cluster"})

            op = Operation(
                connector_name="gcp-prod",
                operation_id="gke-list-clusters",
                display_name="List GKE Clusters",
            )
            result = await connector.execute(op, {})

        assert "data" in result
        assert len(result["data"]) == 1

    @patch("meho_claude.core.connectors.gcp.asyncio.to_thread")
    @patch("meho_claude.core.connectors.gcp.google_auth_default")
    @pytest.mark.asyncio
    async def test_execute_gke_get_cluster(self, mock_auth_default, mock_to_thread, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        mock_creds = MagicMock()
        mock_auth_default.return_value = (mock_creds, "my-project-123")

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = GCPConnector(gcp_config)

        with patch("meho_claude.core.connectors.gcp.container_v1") as mock_container:
            mock_client = MagicMock()
            mock_container.ClusterManagerClient.return_value = mock_client

            mock_cluster = MagicMock()
            mock_cluster.name = "prod-cluster"
            mock_client.get_cluster.return_value = mock_cluster

            connector._proto_to_dict = MagicMock(return_value={"name": "prod-cluster"})

            op = Operation(
                connector_name="gcp-prod",
                operation_id="gke-get-cluster",
                display_name="Get GKE Cluster",
            )
            result = await connector.execute(op, {"location": "us-central1", "cluster_name": "prod-cluster"})

        assert "data" in result
        assert result["data"]["name"] == "prod-cluster"


class TestGCPConnectorExecuteMonitoring:
    @patch("meho_claude.core.connectors.gcp.asyncio.to_thread")
    @patch("meho_claude.core.connectors.gcp.google_auth_default")
    @pytest.mark.asyncio
    async def test_execute_monitoring_query_metrics(self, mock_auth_default, mock_to_thread, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        mock_creds = MagicMock()
        mock_auth_default.return_value = (mock_creds, "my-project-123")

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = GCPConnector(gcp_config)

        with patch("meho_claude.core.connectors.gcp.monitoring_v3") as mock_monitoring:
            mock_client = MagicMock()
            mock_monitoring.MetricServiceClient.return_value = mock_client

            mock_ts = MagicMock()
            mock_ts.metric = MagicMock()
            mock_ts.resource = MagicMock()
            mock_client.list_time_series.return_value = iter([mock_ts])

            connector._serialize_time_series = MagicMock(return_value={"metric": "cpu/utilization", "points": []})

            op = Operation(
                connector_name="gcp-prod",
                operation_id="monitoring-query-metrics",
                display_name="Query Metrics",
            )
            result = await connector.execute(op, {"metric_type": "compute.googleapis.com/instance/cpu/utilization"})

        assert "data" in result
        assert isinstance(result["data"], list)

    @patch("meho_claude.core.connectors.gcp.asyncio.to_thread")
    @patch("meho_claude.core.connectors.gcp.google_auth_default")
    @pytest.mark.asyncio
    async def test_monitoring_default_duration_60_minutes(self, mock_auth_default, mock_to_thread, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        mock_creds = MagicMock()
        mock_auth_default.return_value = (mock_creds, "my-project-123")

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = GCPConnector(gcp_config)

        with patch("meho_claude.core.connectors.gcp.monitoring_v3") as mock_monitoring:
            mock_client = MagicMock()
            mock_monitoring.MetricServiceClient.return_value = mock_client
            mock_client.list_time_series.return_value = iter([])

            connector._serialize_time_series = MagicMock(return_value={})

            op = Operation(
                connector_name="gcp-prod",
                operation_id="monitoring-query-metrics",
                display_name="Query Metrics",
            )
            # No duration_minutes param -- should default to 60
            result = await connector.execute(op, {"metric_type": "compute.googleapis.com/instance/cpu/utilization"})

        assert "data" in result


class TestGCPConnectorExecuteCloudSQL:
    @patch("meho_claude.core.connectors.gcp.asyncio.to_thread")
    @patch("meho_claude.core.connectors.gcp.google_auth_default")
    @pytest.mark.asyncio
    async def test_execute_cloudsql_list_instances(self, mock_auth_default, mock_to_thread, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        mock_creds = MagicMock()
        mock_auth_default.return_value = (mock_creds, "my-project-123")

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = GCPConnector(gcp_config)

        with patch("meho_claude.core.connectors.gcp.discovery") as mock_discovery:
            mock_service = MagicMock()
            mock_discovery.build.return_value = mock_service

            mock_instances = MagicMock()
            mock_service.instances.return_value = mock_instances

            mock_list = MagicMock()
            mock_instances.list.return_value = mock_list
            mock_list.execute.return_value = {
                "items": [{"name": "db-instance-1", "state": "RUNNABLE"}]
            }

            op = Operation(
                connector_name="gcp-prod",
                operation_id="cloudsql-list-instances",
                display_name="List Cloud SQL Instances",
            )
            result = await connector.execute(op, {})

        assert "data" in result
        assert isinstance(result["data"], list)
        assert result["data"][0]["name"] == "db-instance-1"

    @patch("meho_claude.core.connectors.gcp.asyncio.to_thread")
    @patch("meho_claude.core.connectors.gcp.google_auth_default")
    @pytest.mark.asyncio
    async def test_execute_cloudsql_get_instance(self, mock_auth_default, mock_to_thread, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        mock_creds = MagicMock()
        mock_auth_default.return_value = (mock_creds, "my-project-123")

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = GCPConnector(gcp_config)

        with patch("meho_claude.core.connectors.gcp.discovery") as mock_discovery:
            mock_service = MagicMock()
            mock_discovery.build.return_value = mock_service

            mock_instances = MagicMock()
            mock_service.instances.return_value = mock_instances

            mock_get = MagicMock()
            mock_instances.get.return_value = mock_get
            mock_get.execute.return_value = {"name": "db-instance-1", "state": "RUNNABLE"}

            op = Operation(
                connector_name="gcp-prod",
                operation_id="cloudsql-get-instance",
                display_name="Get Cloud SQL Instance",
            )
            result = await connector.execute(op, {"instance_name": "db-instance-1"})

        assert "data" in result
        assert result["data"]["name"] == "db-instance-1"


class TestGCPConnectorExecuteVPC:
    @patch("meho_claude.core.connectors.gcp.asyncio.to_thread")
    @patch("meho_claude.core.connectors.gcp.google_auth_default")
    @pytest.mark.asyncio
    async def test_execute_vpc_list_networks(self, mock_auth_default, mock_to_thread, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        mock_creds = MagicMock()
        mock_auth_default.return_value = (mock_creds, "my-project-123")

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = GCPConnector(gcp_config)

        with patch("meho_claude.core.connectors.gcp.compute_v1") as mock_compute:
            mock_client = MagicMock()
            mock_compute.NetworksClient.return_value = mock_client

            mock_network = MagicMock()
            mock_network.name = "default"
            mock_client.list.return_value = iter([mock_network])

            connector._proto_to_dict = MagicMock(return_value={"name": "default"})

            op = Operation(
                connector_name="gcp-prod",
                operation_id="vpc-list-networks",
                display_name="List VPC Networks",
            )
            result = await connector.execute(op, {})

        assert "data" in result
        assert len(result["data"]) == 1

    @patch("meho_claude.core.connectors.gcp.asyncio.to_thread")
    @patch("meho_claude.core.connectors.gcp.google_auth_default")
    @pytest.mark.asyncio
    async def test_execute_vpc_list_subnetworks(self, mock_auth_default, mock_to_thread, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        mock_creds = MagicMock()
        mock_auth_default.return_value = (mock_creds, "my-project-123")

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = GCPConnector(gcp_config)

        with patch("meho_claude.core.connectors.gcp.compute_v1") as mock_compute:
            mock_client = MagicMock()
            mock_compute.SubnetworksClient.return_value = mock_client
            mock_compute.AggregatedListSubnetworksRequest.return_value = MagicMock()

            mock_subnet = MagicMock()
            mock_subnet.name = "default-subnet"

            mock_scoped = MagicMock()
            mock_scoped.subnetworks = [mock_subnet]

            mock_client.aggregated_list.return_value = iter([
                ("regions/us-central1", mock_scoped)
            ])

            connector._proto_to_dict = MagicMock(return_value={"name": "default-subnet"})

            op = Operation(
                connector_name="gcp-prod",
                operation_id="vpc-list-subnetworks",
                display_name="List VPC Subnetworks",
            )
            result = await connector.execute(op, {})

        assert "data" in result
        assert len(result["data"]) == 1


# --- Unknown operation ---


class TestGCPConnectorExecuteUnknown:
    @patch("meho_claude.core.connectors.gcp.asyncio.to_thread")
    @patch("meho_claude.core.connectors.gcp.google_auth_default")
    @pytest.mark.asyncio
    async def test_execute_unknown_raises(self, mock_auth_default, mock_to_thread, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        mock_creds = MagicMock()
        mock_auth_default.return_value = (mock_creds, "my-project-123")

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = GCPConnector(gcp_config)

        op = Operation(
            connector_name="gcp-prod",
            operation_id="unknown-operation",
            display_name="Unknown",
        )
        with pytest.raises(ValueError, match="Unknown operation"):
            await connector.execute(op, {})


# --- Missing project_id in execute ---


class TestGCPConnectorMissingProjectId:
    @patch("meho_claude.core.connectors.gcp.asyncio.to_thread")
    @pytest.mark.asyncio
    async def test_execute_raises_without_project_id(self, mock_to_thread, gcp_config_no_project):
        from meho_claude.core.connectors.gcp import GCPConnector

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = GCPConnector(gcp_config_no_project)

        op = Operation(
            connector_name="gcp-bad",
            operation_id="compute-list-instances",
            display_name="List Instances",
        )
        with pytest.raises(ValueError, match="project_id"):
            await connector.execute(op, {})


# --- Protobuf serialization ---


class TestGCPConnectorProtoToDict:
    def test_proto_to_dict_with_message_to_dict(self, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        connector = GCPConnector(gcp_config)

        # Test with a mock protobuf object that has DESCRIPTOR
        mock_proto = MagicMock()
        mock_proto.DESCRIPTOR = MagicMock()

        with patch("meho_claude.core.connectors.gcp.MessageToDict") as mock_mtd:
            mock_mtd.return_value = {"name": "test", "status": "RUNNING"}
            result = connector._proto_to_dict(mock_proto)

        assert result == {"name": "test", "status": "RUNNING"}
        mock_mtd.assert_called_once()

    def test_proto_to_dict_fallback_for_non_protobuf(self, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        connector = GCPConnector(gcp_config)

        # Test with a plain dict -- should return as-is
        result = connector._proto_to_dict({"name": "test"})
        assert result == {"name": "test"}


# --- Get Trust Tier Tests ---


class TestGCPConnectorGetTrustTier:
    def test_default_tier_from_operation(self, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        connector = GCPConnector(gcp_config)
        op = Operation(
            connector_name="gcp-prod",
            operation_id="compute-list-instances",
            display_name="List Instances",
            trust_tier="READ",
        )
        assert connector.get_trust_tier(op) == "READ"

    def test_override_from_config(self):
        from meho_claude.core.connectors.gcp import GCPConnector

        config = ConnectorConfig(
            name="gcp-prod",
            connector_type="gcp",
            project_id="my-project-123",
            trust_overrides=[
                TrustOverride(operation_id="compute-list-instances", trust_tier="WRITE"),
            ],
        )
        connector = GCPConnector(config)
        op = Operation(
            connector_name="gcp-prod",
            operation_id="compute-list-instances",
            display_name="List Instances",
            trust_tier="READ",
        )
        assert connector.get_trust_tier(op) == "WRITE"


# --- Close Tests ---


class TestGCPConnectorClose:
    def test_close_is_noop(self, gcp_config):
        from meho_claude.core.connectors.gcp import GCPConnector

        connector = GCPConnector(gcp_config)
        # Should not raise
        connector.close()
