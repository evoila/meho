"""Tests for KubernetesConnector with mocked kubernetes-asyncio."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_claude.core.connectors.models import (
    AuthConfig,
    ConnectorConfig,
    Operation,
    TrustOverride,
)


@pytest.fixture
def k8s_config():
    """K8s connector config with kubeconfig-only auth."""
    return ConnectorConfig(
        name="test-cluster",
        connector_type="kubernetes",
        kubeconfig_path="/tmp/test-kubeconfig",
        kubeconfig_context="test-ctx",
    )


@pytest.fixture
def k8s_config_with_auth():
    """K8s connector config with bearer auth."""
    return ConnectorConfig(
        name="test-cluster",
        connector_type="kubernetes",
        auth=AuthConfig(method="bearer", credential_name="k8s-token"),
    )


# Sample K8s data returned by .to_dict()
SAMPLE_POD_LIST = {
    "items": [
        {
            "metadata": {"name": "nginx-abc", "namespace": "default", "uid": "pod-uid-1"},
            "spec": {"nodeName": "node-1"},
            "status": {"phase": "Running", "podIP": "10.0.0.5"},
        }
    ],
    "metadata": {"resourceVersion": "12345"},
}

SAMPLE_NAMESPACE_LIST = {
    "items": [
        {"metadata": {"name": "default", "uid": "ns-uid-1"}},
        {"metadata": {"name": "kube-system", "uid": "ns-uid-2"}},
    ],
    "metadata": {"resourceVersion": "100"},
}


def _make_mock_response(data_dict):
    """Create a mock API response with .to_dict() method."""
    mock = MagicMock()
    mock.to_dict.return_value = data_dict
    return mock


class TestKubernetesConnectorRegistration:
    def test_kubernetes_registered_in_registry(self):
        from meho_claude.core.connectors.kubernetes import KubernetesConnector
        from meho_claude.core.connectors.registry import get_connector_class

        cls = get_connector_class("kubernetes")
        assert cls is KubernetesConnector

    def test_kubernetes_in_list(self):
        from meho_claude.core.connectors.kubernetes import KubernetesConnector  # noqa: F401
        from meho_claude.core.connectors.registry import list_connector_types

        types = list_connector_types()
        assert "kubernetes" in types


class TestKubernetesConnectorDiscoverOperations:
    def test_discover_returns_correct_count(self, k8s_config):
        from meho_claude.core.connectors.kubernetes import KubernetesConnector

        connector = KubernetesConnector(k8s_config)
        import asyncio

        ops = asyncio.run(connector.discover_operations())
        # 10 READ + 4 WRITE/DESTRUCTIVE = 14
        assert len(ops) == 14

    def test_discover_returns_operation_models(self, k8s_config):
        from meho_claude.core.connectors.kubernetes import KubernetesConnector

        connector = KubernetesConnector(k8s_config)
        import asyncio

        ops = asyncio.run(connector.discover_operations())
        for op in ops:
            assert isinstance(op, Operation)
            assert op.connector_name == "test-cluster"

    def test_discover_includes_all_operation_ids(self, k8s_config):
        from meho_claude.core.connectors.kubernetes import KubernetesConnector

        connector = KubernetesConnector(k8s_config)
        import asyncio

        ops = asyncio.run(connector.discover_operations())
        op_ids = {op.operation_id for op in ops}
        expected = {
            "list-pods", "get-pod",
            "list-deployments", "get-deployment",
            "list-services", "get-service",
            "list-nodes", "get-node",
            "list-ingresses",
            "list-namespaces",
            "scale-deployment",
            "cordon-node",
            "uncordon-node",
            "delete-pod",
        }
        assert op_ids == expected

    def test_write_operations_have_correct_trust_tier(self, k8s_config):
        from meho_claude.core.connectors.kubernetes import KubernetesConnector

        connector = KubernetesConnector(k8s_config)
        import asyncio

        ops = asyncio.run(connector.discover_operations())
        op_map = {op.operation_id: op for op in ops}

        assert op_map["scale-deployment"].trust_tier == "WRITE"
        assert op_map["cordon-node"].trust_tier == "WRITE"
        assert op_map["uncordon-node"].trust_tier == "WRITE"
        assert op_map["delete-pod"].trust_tier == "DESTRUCTIVE"

    def test_read_operations_have_read_trust_tier(self, k8s_config):
        from meho_claude.core.connectors.kubernetes import KubernetesConnector

        connector = KubernetesConnector(k8s_config)
        import asyncio

        ops = asyncio.run(connector.discover_operations())
        read_ops = [op for op in ops if op.operation_id.startswith("list-") or op.operation_id.startswith("get-")]
        for op in read_ops:
            assert op.trust_tier == "READ"


class TestKubernetesConnectorGetTrustTier:
    def test_default_tier_from_operation(self, k8s_config):
        from meho_claude.core.connectors.kubernetes import KubernetesConnector

        connector = KubernetesConnector(k8s_config)
        op = Operation(
            connector_name="test-cluster",
            operation_id="list-pods",
            display_name="List Pods",
            trust_tier="READ",
        )
        assert connector.get_trust_tier(op) == "READ"

    def test_override_from_config(self):
        from meho_claude.core.connectors.kubernetes import KubernetesConnector

        config = ConnectorConfig(
            name="test-cluster",
            connector_type="kubernetes",
            trust_overrides=[
                TrustOverride(operation_id="list-pods", trust_tier="WRITE"),
            ],
        )
        connector = KubernetesConnector(config)
        op = Operation(
            connector_name="test-cluster",
            operation_id="list-pods",
            display_name="List Pods",
            trust_tier="READ",
        )
        assert connector.get_trust_tier(op) == "WRITE"


class TestKubernetesConnectorExecute:
    @patch("meho_claude.core.connectors.kubernetes.config")
    @patch("meho_claude.core.connectors.kubernetes.ApiClient")
    @pytest.mark.asyncio
    async def test_execute_list_pods_all_namespaces(self, mock_api_client_cls, mock_config, k8s_config):
        from meho_claude.core.connectors.kubernetes import KubernetesConnector

        mock_config.load_kube_config = AsyncMock()

        mock_core_api = MagicMock()
        mock_core_api.list_pod_for_all_namespaces = AsyncMock(
            return_value=_make_mock_response(SAMPLE_POD_LIST)
        )

        mock_api_instance = AsyncMock()
        mock_api_instance.__aenter__ = AsyncMock(return_value=mock_api_instance)
        mock_api_instance.__aexit__ = AsyncMock(return_value=False)
        mock_api_client_cls.return_value = mock_api_instance

        with patch("meho_claude.core.connectors.kubernetes.client") as mock_client:
            mock_client.CoreV1Api.return_value = mock_core_api
            connector = KubernetesConnector(k8s_config)
            op = Operation(
                connector_name="test-cluster",
                operation_id="list-pods",
                display_name="List Pods",
            )
            result = await connector.execute(op, {})

        assert result["data"] == SAMPLE_POD_LIST
        mock_core_api.list_pod_for_all_namespaces.assert_called_once()

    @patch("meho_claude.core.connectors.kubernetes.config")
    @patch("meho_claude.core.connectors.kubernetes.ApiClient")
    @pytest.mark.asyncio
    async def test_execute_list_pods_namespaced(self, mock_api_client_cls, mock_config, k8s_config):
        from meho_claude.core.connectors.kubernetes import KubernetesConnector

        mock_config.load_kube_config = AsyncMock()

        mock_core_api = MagicMock()
        mock_core_api.list_namespaced_pod = AsyncMock(
            return_value=_make_mock_response(SAMPLE_POD_LIST)
        )

        mock_api_instance = AsyncMock()
        mock_api_instance.__aenter__ = AsyncMock(return_value=mock_api_instance)
        mock_api_instance.__aexit__ = AsyncMock(return_value=False)
        mock_api_client_cls.return_value = mock_api_instance

        with patch("meho_claude.core.connectors.kubernetes.client") as mock_client:
            mock_client.CoreV1Api.return_value = mock_core_api
            connector = KubernetesConnector(k8s_config)
            op = Operation(
                connector_name="test-cluster",
                operation_id="list-pods",
                display_name="List Pods",
            )
            result = await connector.execute(op, {"namespace": "default"})

        assert result["data"] == SAMPLE_POD_LIST
        mock_core_api.list_namespaced_pod.assert_called_once_with(namespace="default")

    @patch("meho_claude.core.connectors.kubernetes.config")
    @patch("meho_claude.core.connectors.kubernetes.ApiClient")
    @pytest.mark.asyncio
    async def test_execute_get_pod(self, mock_api_client_cls, mock_config, k8s_config):
        from meho_claude.core.connectors.kubernetes import KubernetesConnector

        mock_config.load_kube_config = AsyncMock()

        single_pod = SAMPLE_POD_LIST["items"][0]
        mock_core_api = MagicMock()
        mock_core_api.read_namespaced_pod = AsyncMock(
            return_value=_make_mock_response(single_pod)
        )

        mock_api_instance = AsyncMock()
        mock_api_instance.__aenter__ = AsyncMock(return_value=mock_api_instance)
        mock_api_instance.__aexit__ = AsyncMock(return_value=False)
        mock_api_client_cls.return_value = mock_api_instance

        with patch("meho_claude.core.connectors.kubernetes.client") as mock_client:
            mock_client.CoreV1Api.return_value = mock_core_api
            connector = KubernetesConnector(k8s_config)
            op = Operation(
                connector_name="test-cluster",
                operation_id="get-pod",
                display_name="Get Pod",
            )
            result = await connector.execute(op, {"name": "nginx-abc", "namespace": "default"})

        assert result["data"] == single_pod
        mock_core_api.read_namespaced_pod.assert_called_once_with(
            name="nginx-abc", namespace="default"
        )

    @patch("meho_claude.core.connectors.kubernetes.config")
    @patch("meho_claude.core.connectors.kubernetes.ApiClient")
    @pytest.mark.asyncio
    async def test_execute_delete_pod(self, mock_api_client_cls, mock_config, k8s_config):
        from meho_claude.core.connectors.kubernetes import KubernetesConnector

        mock_config.load_kube_config = AsyncMock()

        mock_core_api = MagicMock()
        mock_core_api.delete_namespaced_pod = AsyncMock(
            return_value=_make_mock_response({"status": "Success"})
        )

        mock_api_instance = AsyncMock()
        mock_api_instance.__aenter__ = AsyncMock(return_value=mock_api_instance)
        mock_api_instance.__aexit__ = AsyncMock(return_value=False)
        mock_api_client_cls.return_value = mock_api_instance

        with patch("meho_claude.core.connectors.kubernetes.client") as mock_client:
            mock_client.CoreV1Api.return_value = mock_core_api
            connector = KubernetesConnector(k8s_config)
            op = Operation(
                connector_name="test-cluster",
                operation_id="delete-pod",
                display_name="Delete Pod",
                trust_tier="DESTRUCTIVE",
            )
            result = await connector.execute(op, {"name": "stale-pod", "namespace": "default"})

        assert result["data"] == {"status": "Success"}
        mock_core_api.delete_namespaced_pod.assert_called_once_with(
            name="stale-pod", namespace="default"
        )

    @patch("meho_claude.core.connectors.kubernetes.config")
    @patch("meho_claude.core.connectors.kubernetes.ApiClient")
    @pytest.mark.asyncio
    async def test_execute_scale_deployment(self, mock_api_client_cls, mock_config, k8s_config):
        from meho_claude.core.connectors.kubernetes import KubernetesConnector

        mock_config.load_kube_config = AsyncMock()

        mock_apps_api = MagicMock()
        mock_apps_api.patch_namespaced_deployment_scale = AsyncMock(
            return_value=_make_mock_response({"spec": {"replicas": 3}})
        )

        mock_api_instance = AsyncMock()
        mock_api_instance.__aenter__ = AsyncMock(return_value=mock_api_instance)
        mock_api_instance.__aexit__ = AsyncMock(return_value=False)
        mock_api_client_cls.return_value = mock_api_instance

        with patch("meho_claude.core.connectors.kubernetes.client") as mock_client:
            mock_client.AppsV1Api.return_value = mock_apps_api
            connector = KubernetesConnector(k8s_config)
            op = Operation(
                connector_name="test-cluster",
                operation_id="scale-deployment",
                display_name="Scale Deployment",
                trust_tier="WRITE",
            )
            result = await connector.execute(
                op, {"name": "web", "namespace": "default", "replicas": "3"}
            )

        assert result["data"] == {"spec": {"replicas": 3}}
        mock_apps_api.patch_namespaced_deployment_scale.assert_called_once_with(
            name="web",
            namespace="default",
            body={"spec": {"replicas": 3}},
        )

    @patch("meho_claude.core.connectors.kubernetes.config")
    @patch("meho_claude.core.connectors.kubernetes.ApiClient")
    @pytest.mark.asyncio
    async def test_execute_cordon_node(self, mock_api_client_cls, mock_config, k8s_config):
        from meho_claude.core.connectors.kubernetes import KubernetesConnector

        mock_config.load_kube_config = AsyncMock()

        mock_core_api = MagicMock()
        mock_core_api.patch_node = AsyncMock(
            return_value=_make_mock_response({"spec": {"unschedulable": True}})
        )

        mock_api_instance = AsyncMock()
        mock_api_instance.__aenter__ = AsyncMock(return_value=mock_api_instance)
        mock_api_instance.__aexit__ = AsyncMock(return_value=False)
        mock_api_client_cls.return_value = mock_api_instance

        with patch("meho_claude.core.connectors.kubernetes.client") as mock_client:
            mock_client.CoreV1Api.return_value = mock_core_api
            connector = KubernetesConnector(k8s_config)
            op = Operation(
                connector_name="test-cluster",
                operation_id="cordon-node",
                display_name="Cordon Node",
                trust_tier="WRITE",
            )
            result = await connector.execute(op, {"name": "node-1"})

        assert result["data"] == {"spec": {"unschedulable": True}}
        mock_core_api.patch_node.assert_called_once_with(
            name="node-1", body={"spec": {"unschedulable": True}}
        )


class TestKubernetesConnectorTestConnection:
    @patch("meho_claude.core.connectors.kubernetes.config")
    @patch("meho_claude.core.connectors.kubernetes.ApiClient")
    @pytest.mark.asyncio
    async def test_test_connection_success(self, mock_api_client_cls, mock_config, k8s_config):
        from meho_claude.core.connectors.kubernetes import KubernetesConnector

        mock_config.load_kube_config = AsyncMock()

        mock_core_api = MagicMock()
        mock_core_api.list_namespace = AsyncMock(
            return_value=_make_mock_response(SAMPLE_NAMESPACE_LIST)
        )

        mock_api_instance = AsyncMock()
        mock_api_instance.__aenter__ = AsyncMock(return_value=mock_api_instance)
        mock_api_instance.__aexit__ = AsyncMock(return_value=False)
        mock_api_client_cls.return_value = mock_api_instance

        with patch("meho_claude.core.connectors.kubernetes.client") as mock_client:
            mock_client.CoreV1Api.return_value = mock_core_api
            connector = KubernetesConnector(k8s_config)
            result = await connector.test_connection()

        assert result["status"] == "ok"
        assert result["namespaces"] == 2

    @patch("meho_claude.core.connectors.kubernetes.config")
    @patch("meho_claude.core.connectors.kubernetes.ApiClient")
    @pytest.mark.asyncio
    async def test_test_connection_failure(self, mock_api_client_cls, mock_config, k8s_config):
        from meho_claude.core.connectors.kubernetes import KubernetesConnector

        mock_config.load_kube_config = AsyncMock(side_effect=Exception("Connection refused"))

        connector = KubernetesConnector(k8s_config)
        result = await connector.test_connection()

        assert result["status"] == "error"
        assert "Connection refused" in result["message"]


class TestKubernetesConnectorClose:
    def test_close_is_noop(self, k8s_config):
        from meho_claude.core.connectors.kubernetes import KubernetesConnector

        connector = KubernetesConnector(k8s_config)
        # Should not raise
        connector.close()
