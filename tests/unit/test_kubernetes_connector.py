# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for Kubernetes typed connector (TASK-159).

Tests:
- Connector initialization
- Operation handler registration
- Exception mapping
- Operation definitions
- Type definitions
- Serializers
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.modules.connectors.kubernetes import (
    KUBERNETES_OPERATIONS,
    KUBERNETES_OPERATIONS_VERSION,
    KUBERNETES_TYPES,
    KubernetesConnector,
    serializers,
)
from meho_app.modules.connectors.kubernetes.operations import (
    CORE_OPERATIONS,
    NETWORKING_OPERATIONS,
    WORKLOADS_OPERATIONS,
)


class TestKubernetesConnectorInit:
    """Test connector initialization."""

    def test_init_with_valid_config(self):
        """Test connector initializes with valid config."""
        connector = KubernetesConnector(
            connector_id="test-123",
            config={
                "server_url": "https://k8s.example.com:6443",
                "skip_tls_verification": False,
            },
            credentials={
                "token": "test-token",
            },
        )

        assert connector.connector_id == "test-123"
        assert connector.server_url == "https://k8s.example.com:6443"
        assert connector.skip_tls is False
        assert not connector.is_connected

    def test_init_with_skip_tls(self):
        """Test connector initializes with TLS skip option."""
        connector = KubernetesConnector(
            connector_id="test-123",
            config={
                "server_url": "https://k8s.example.com:6443",
                "skip_tls_verification": True,
            },
            credentials={
                "token": "test-token",
            },
        )

        assert connector.skip_tls is True

    def test_init_with_ca_certificate(self):
        """Test connector initializes with CA certificate."""
        connector = KubernetesConnector(
            connector_id="test-123",
            config={
                "server_url": "https://k8s.example.com:6443",
                "ca_certificate": "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----",
            },
            credentials={
                "token": "test-token",
            },
        )

        assert connector.ca_certificate is not None


class TestOperationHandlers:
    """Test operation handler registration."""

    def test_all_operations_have_handlers(self):
        """All defined operations should have corresponding handlers."""
        connector = KubernetesConnector(
            connector_id="test",
            config={"server_url": "https://localhost:6443"},
            credentials={"token": "test"},
        )

        # Get all operation IDs from definitions
        operation_ids = {op.operation_id for op in KUBERNETES_OPERATIONS}

        # Check each has a handler
        for op_id in operation_ids:
            assert op_id in connector._operation_handlers, f"Missing handler for {op_id}"

    def test_handler_count_matches_operations(self):
        """Number of handlers should match number of operations."""
        connector = KubernetesConnector(
            connector_id="test",
            config={"server_url": "https://localhost:6443"},
            credentials={"token": "test"},
        )

        assert len(connector._operation_handlers) >= len(KUBERNETES_OPERATIONS)

    def test_core_operations_have_handlers(self):
        """Core operations should all have handlers."""
        connector = KubernetesConnector(
            connector_id="test",
            config={"server_url": "https://localhost:6443"},
            credentials={"token": "test"},
        )

        core_op_ids = [op.operation_id for op in CORE_OPERATIONS]
        for op_id in core_op_ids:
            assert op_id in connector._operation_handlers, f"Missing handler for {op_id}"


class TestExceptionMapping:
    """Test Kubernetes exception to error code mapping."""

    def test_map_401_to_authentication_failed(self):
        """401 should map to AUTHENTICATION_FAILED."""
        connector = KubernetesConnector(
            connector_id="test",
            config={"server_url": "https://localhost:6443"},
            credentials={"token": "test"},
        )

        with patch(
            "meho_app.modules.connectors.kubernetes.connector.ApiException",
            create=True,
        ):
            from kubernetes_asyncio.client.exceptions import ApiException

            e = ApiException(status=401)
            assert connector._map_k8s_exception(e) == "AUTHENTICATION_FAILED"

    def test_map_403_to_permission_denied(self):
        """403 should map to PERMISSION_DENIED."""
        connector = KubernetesConnector(
            connector_id="test",
            config={"server_url": "https://localhost:6443"},
            credentials={"token": "test"},
        )

        from kubernetes_asyncio.client.exceptions import ApiException

        e = ApiException(status=403)
        assert connector._map_k8s_exception(e) == "PERMISSION_DENIED"

    def test_map_404_to_not_found(self):
        """404 should map to NOT_FOUND."""
        connector = KubernetesConnector(
            connector_id="test",
            config={"server_url": "https://localhost:6443"},
            credentials={"token": "test"},
        )

        from kubernetes_asyncio.client.exceptions import ApiException

        e = ApiException(status=404)
        assert connector._map_k8s_exception(e) == "NOT_FOUND"

    def test_map_409_to_conflict(self):
        """409 should map to CONFLICT."""
        connector = KubernetesConnector(
            connector_id="test",
            config={"server_url": "https://localhost:6443"},
            credentials={"token": "test"},
        )

        from kubernetes_asyncio.client.exceptions import ApiException

        e = ApiException(status=409)
        assert connector._map_k8s_exception(e) == "CONFLICT"

    def test_map_generic_exception_to_internal_error(self):
        """Generic exceptions should map to INTERNAL_ERROR."""
        connector = KubernetesConnector(
            connector_id="test",
            config={"server_url": "https://localhost:6443"},
            credentials={"token": "test"},
        )

        e = RuntimeError("Something went wrong")
        assert connector._map_k8s_exception(e) == "INTERNAL_ERROR"


class TestOperationDefinitions:
    """Test operation definitions."""

    def test_operations_count(self):
        """Should have at least 40 operations defined."""
        assert len(KUBERNETES_OPERATIONS) >= 40

    def test_operations_version_format(self):
        """Operations version should be in YYYY.MM.DD.revision format."""
        parts = KUBERNETES_OPERATIONS_VERSION.split(".")
        assert len(parts) == 4
        assert int(parts[0]) >= 2026  # Year
        assert 1 <= int(parts[1]) <= 12  # Month
        assert 1 <= int(parts[2]) <= 31  # Day
        assert int(parts[3]) >= 1  # Revision

    def test_all_operations_have_required_fields(self):
        """All operations should have required fields."""
        for op in KUBERNETES_OPERATIONS:
            assert op.operation_id, "Missing operation_id"
            assert op.name, f"Missing name for {op.operation_id}"
            assert op.description, f"Missing description for {op.operation_id}"
            assert op.category, f"Missing category for {op.operation_id}"

    def test_operations_have_unique_ids(self):
        """All operation IDs should be unique."""
        ids = [op.operation_id for op in KUBERNETES_OPERATIONS]
        assert len(ids) == len(set(ids)), "Duplicate operation IDs found"

    def test_core_operations_defined(self):
        """Core operations should be defined."""
        core_ids = {op.operation_id for op in CORE_OPERATIONS}
        assert "list_pods" in core_ids
        assert "get_pod" in core_ids
        assert "list_nodes" in core_ids
        assert "list_namespaces" in core_ids

    def test_workloads_operations_defined(self):
        """Workloads operations should be defined."""
        workload_ids = {op.operation_id for op in WORKLOADS_OPERATIONS}
        assert "list_deployments" in workload_ids
        assert "get_deployment" in workload_ids
        assert "scale_deployment" in workload_ids
        assert "restart_deployment" in workload_ids

    def test_networking_operations_defined(self):
        """Networking operations should be defined."""
        network_ids = {op.operation_id for op in NETWORKING_OPERATIONS}
        assert "list_services" in network_ids
        assert "get_service" in network_ids
        assert "list_ingresses" in network_ids


class TestTypeDefinitions:
    """Test type definitions."""

    def test_types_count(self):
        """Should have at least 15 types defined."""
        assert len(KUBERNETES_TYPES) >= 15

    def test_all_types_have_required_fields(self):
        """All types should have required fields."""
        for t in KUBERNETES_TYPES:
            assert t.type_name, "Missing type_name"
            assert t.description, f"Missing description for {t.type_name}"
            assert t.category, f"Missing category for {t.type_name}"

    def test_types_have_unique_names(self):
        """All type names should be unique."""
        names = [t.type_name for t in KUBERNETES_TYPES]
        assert len(names) == len(set(names)), "Duplicate type names found"

    def test_core_types_defined(self):
        """Core types should be defined."""
        type_names = {t.type_name for t in KUBERNETES_TYPES}
        assert "Pod" in type_names
        assert "Node" in type_names
        assert "Namespace" in type_names
        assert "ConfigMap" in type_names
        assert "Secret" in type_names

    def test_workload_types_defined(self):
        """Workload types should be defined."""
        type_names = {t.type_name for t in KUBERNETES_TYPES}
        assert "Deployment" in type_names
        assert "ReplicaSet" in type_names
        assert "StatefulSet" in type_names
        assert "DaemonSet" in type_names

    def test_networking_types_defined(self):
        """Networking types should be defined."""
        type_names = {t.type_name for t in KUBERNETES_TYPES}
        assert "Service" in type_names
        assert "Ingress" in type_names


class TestConnectorInterface:
    """Test connector interface methods."""

    def test_get_operations_returns_all(self):
        """get_operations should return all defined operations."""
        connector = KubernetesConnector(
            connector_id="test",
            config={"server_url": "https://localhost:6443"},
            credentials={"token": "test"},
        )

        ops = connector.get_operations()
        assert len(ops) == len(KUBERNETES_OPERATIONS)

    def test_get_types_returns_all(self):
        """get_types should return all defined types."""
        connector = KubernetesConnector(
            connector_id="test",
            config={"server_url": "https://localhost:6443"},
            credentials={"token": "test"},
        )

        types = connector.get_types()
        assert len(types) == len(KUBERNETES_TYPES)


class TestSerializers:
    """Test serializer functions."""

    def test_serialize_pod_with_minimal_data(self):
        """serialize_pod should handle minimal pod data."""
        # Create mock pod with minimal data
        pod = MagicMock()
        pod.metadata.name = "test-pod"
        pod.metadata.namespace = "default"
        pod.metadata.uid = "123"
        pod.metadata.labels = None
        pod.metadata.annotations = None
        pod.metadata.creation_timestamp = None
        pod.spec = None
        pod.status = None

        result = serializers.serialize_pod(pod)

        assert result["name"] == "test-pod"
        assert result["namespace"] == "default"
        assert result["uid"] == "123"
        assert result["labels"] == {}

    def test_serialize_deployment_with_minimal_data(self):
        """serialize_deployment should handle minimal deployment data."""
        deployment = MagicMock()
        deployment.metadata.name = "test-deploy"
        deployment.metadata.namespace = "default"
        deployment.metadata.uid = "456"
        deployment.metadata.labels = None
        deployment.metadata.annotations = None
        deployment.metadata.creation_timestamp = None
        deployment.spec = None
        deployment.status = None

        result = serializers.serialize_deployment(deployment)

        assert result["name"] == "test-deploy"
        assert result["namespace"] == "default"

    def test_serialize_service_with_minimal_data(self):
        """serialize_service should handle minimal service data."""
        svc = MagicMock()
        svc.metadata.name = "test-svc"
        svc.metadata.namespace = "default"
        svc.metadata.uid = "789"
        svc.metadata.labels = None
        svc.metadata.annotations = None
        svc.metadata.creation_timestamp = None
        svc.spec = None
        svc.status = None

        result = serializers.serialize_service(svc)

        assert result["name"] == "test-svc"
        assert result["namespace"] == "default"

    def test_serialize_node_with_minimal_data(self):
        """serialize_node should handle minimal node data."""
        node = MagicMock()
        node.metadata.name = "test-node"
        node.metadata.uid = "abc"
        node.metadata.labels = None
        node.metadata.annotations = None
        node.metadata.creation_timestamp = None
        node.spec = None
        node.status = None

        result = serializers.serialize_node(node)

        assert result["name"] == "test-node"

    def test_serialize_namespace_with_minimal_data(self):
        """serialize_namespace should handle minimal namespace data."""
        ns = MagicMock()
        ns.metadata.name = "test-ns"
        ns.metadata.uid = "def"
        ns.metadata.labels = None
        ns.metadata.annotations = None
        ns.metadata.creation_timestamp = None
        ns.status = None

        result = serializers.serialize_namespace(ns)

        assert result["name"] == "test-ns"


class TestExecute:
    """Test execute method."""

    @pytest.mark.asyncio
    async def test_execute_unknown_operation(self):
        """Execute should return error for unknown operation."""
        connector = KubernetesConnector(
            connector_id="test",
            config={"server_url": "https://localhost:6443"},
            credentials={"token": "test"},
        )
        # Mock connection
        connector._is_connected = True

        result = await connector.execute("unknown_operation", {})

        assert not result.success
        assert result.error_code == "UNKNOWN_OPERATION"
        assert "unknown_operation" in result.error

    @pytest.mark.asyncio
    async def test_execute_connects_if_not_connected(self):
        """Execute should connect if not already connected."""
        connector = KubernetesConnector(
            connector_id="test",
            config={"server_url": "https://localhost:6443"},
            credentials={"token": "test"},
        )

        with patch.object(connector, "connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = True
            # Mock the handler to avoid actual K8s call
            connector._operation_handlers["list_pods"] = AsyncMock(return_value=[])
            connector._is_connected = False

            await connector.execute("list_pods", {})

            mock_connect.assert_called_once()


class TestPoolIntegration:
    """Test connector pool integration."""

    @pytest.mark.asyncio
    async def test_pool_creates_kubernetes_connector(self):
        """Pool should create KubernetesConnector for kubernetes type."""
        from meho_app.modules.connectors.pool import get_connector_instance

        connector = await get_connector_instance(
            connector_type="kubernetes",
            connector_id="test-123",
            config={"server_url": "https://localhost:6443"},
            credentials={"token": "test"},
        )

        assert isinstance(connector, KubernetesConnector)
        assert connector.connector_id == "test-123"
