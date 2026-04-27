# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for Kubernetes connector operations (TASK-159).

Tests operation execution with mocked kubernetes-asyncio client.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from meho_app.modules.connectors.kubernetes import KubernetesConnector


@pytest.fixture
def mock_k8s_connector():
    """Create a connector with mocked K8s client."""
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

    # Mock the API clients
    connector._core_v1 = MagicMock()
    connector._apps_v1 = MagicMock()
    connector._batch_v1 = MagicMock()
    connector._networking_v1 = MagicMock()
    connector._storage_v1 = MagicMock()
    connector._is_connected = True

    return connector


def create_mock_pod(name: str, namespace: str, phase: str = "Running"):
    """Create a mock pod object."""
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    pod.metadata.uid = f"uid-{name}"
    pod.metadata.labels = {"app": name}
    pod.metadata.annotations = {}
    pod.metadata.creation_timestamp = datetime.now(UTC)

    pod.spec.node_name = "node-1"
    pod.spec.service_account_name = "default"
    pod.spec.containers = []

    pod.status.phase = phase
    pod.status.host_ip = "10.0.0.1"
    pod.status.pod_ip = "10.0.1.1"
    pod.status.pod_ips = []
    pod.status.start_time = datetime.now(UTC)
    pod.status.container_statuses = []
    pod.status.conditions = []
    pod.status.qos_class = "BestEffort"

    return pod


def create_mock_deployment(name: str, namespace: str, replicas: int = 3):
    """Create a mock deployment object."""
    deployment = MagicMock()
    deployment.metadata.name = name
    deployment.metadata.namespace = namespace
    deployment.metadata.uid = f"uid-{name}"
    deployment.metadata.labels = {"app": name}
    deployment.metadata.annotations = {}
    deployment.metadata.creation_timestamp = datetime.now(UTC)

    deployment.spec.replicas = replicas
    deployment.spec.selector.match_labels = {"app": name}
    deployment.spec.strategy.type = "RollingUpdate"

    deployment.status.replicas = replicas
    deployment.status.ready_replicas = replicas
    deployment.status.available_replicas = replicas
    deployment.status.unavailable_replicas = 0
    deployment.status.updated_replicas = replicas
    deployment.status.observed_generation = 1
    deployment.status.conditions = []

    return deployment


def create_mock_service(name: str, namespace: str, service_type: str = "ClusterIP"):
    """Create a mock service object."""
    svc = MagicMock()
    svc.metadata.name = name
    svc.metadata.namespace = namespace
    svc.metadata.uid = f"uid-{name}"
    svc.metadata.labels = {"app": name}
    svc.metadata.annotations = {}
    svc.metadata.creation_timestamp = datetime.now(UTC)

    svc.spec.type = service_type
    svc.spec.cluster_ip = "10.96.0.1"
    svc.spec.cluster_ips = ["10.96.0.1"]
    svc.spec.external_ips = []
    svc.spec.external_name = None
    svc.spec.load_balancer_ip = None
    svc.spec.ports = []
    svc.spec.selector = {"app": name}
    svc.spec.session_affinity = "None"

    svc.status = MagicMock()
    svc.status.load_balancer = MagicMock()
    svc.status.load_balancer.ingress = None

    return svc


def create_mock_node(name: str, ready: bool = True):
    """Create a mock node object."""
    node = MagicMock()
    node.metadata.name = name
    node.metadata.uid = f"uid-{name}"
    node.metadata.labels = {"kubernetes.io/hostname": name}
    node.metadata.annotations = {}
    node.metadata.creation_timestamp = datetime.now(UTC)

    node.spec.unschedulable = False
    node.spec.taints = []

    node.status.addresses = [MagicMock(type="InternalIP", address="10.0.0.1")]
    node.status.capacity = {"cpu": "4", "memory": "16Gi"}
    node.status.allocatable = {"cpu": "3800m", "memory": "15Gi"}
    node.status.conditions = [
        MagicMock(
            type="Ready",
            status="True" if ready else "False",
            reason="KubeletReady",
            message="kubelet is posting ready status",
            last_heartbeat_time=datetime.now(UTC),
            last_transition_time=datetime.now(UTC),
        )
    ]
    node.status.node_info = MagicMock(
        machine_id="abc123",
        system_uuid="def456",
        boot_id="ghi789",
        kernel_version="5.4.0",
        os_image="Ubuntu 20.04",
        container_runtime_version="containerd://1.4.0",
        kubelet_version="v1.26.0",
        kube_proxy_version="v1.26.0",
        operating_system="linux",
        architecture="amd64",
    )

    return node


class TestPodOperations:
    """Test pod operations."""

    @pytest.mark.asyncio
    async def test_list_pods(self, mock_k8s_connector):
        """Test list_pods operation."""
        # Setup mock
        mock_pods = MagicMock()
        mock_pods.items = [
            create_mock_pod("pod-1", "default"),
            create_mock_pod("pod-2", "default"),
        ]
        mock_k8s_connector._core_v1.list_namespaced_pod = AsyncMock(return_value=mock_pods)

        result = await mock_k8s_connector.execute("list_pods", {"namespace": "default"})

        assert result.success
        assert len(result.data) == 2
        assert result.data[0]["name"] == "pod-1"
        assert result.data[1]["name"] == "pod-2"

    @pytest.mark.asyncio
    async def test_list_pods_all_namespaces(self, mock_k8s_connector):
        """Test list_pods across all namespaces."""
        mock_pods = MagicMock()
        mock_pods.items = [
            create_mock_pod("pod-1", "default"),
            create_mock_pod("pod-2", "kube-system"),
        ]
        mock_k8s_connector._core_v1.list_pod_for_all_namespaces = AsyncMock(return_value=mock_pods)

        result = await mock_k8s_connector.execute("list_pods", {})

        assert result.success
        assert len(result.data) == 2
        mock_k8s_connector._core_v1.list_pod_for_all_namespaces.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_pod(self, mock_k8s_connector):
        """Test get_pod operation."""
        mock_pod = create_mock_pod("nginx", "default")
        mock_k8s_connector._core_v1.read_namespaced_pod = AsyncMock(return_value=mock_pod)

        result = await mock_k8s_connector.execute(
            "get_pod", {"name": "nginx", "namespace": "default"}
        )

        assert result.success
        assert result.data["name"] == "nginx"
        assert result.data["namespace"] == "default"

    @pytest.mark.asyncio
    async def test_get_pod_logs(self, mock_k8s_connector):
        """Test get_pod_logs operation."""
        mock_k8s_connector._core_v1.read_namespaced_pod_log = AsyncMock(
            return_value="log line 1\nlog line 2\n"
        )

        result = await mock_k8s_connector.execute(
            "get_pod_logs",
            {"name": "nginx", "namespace": "default", "tail_lines": 100},
        )

        assert result.success
        assert "logs" in result.data
        assert "log line 1" in result.data["logs"]

    @pytest.mark.asyncio
    async def test_describe_pod(self, mock_k8s_connector):
        """Test describe_pod operation."""
        mock_pod = create_mock_pod("nginx", "default")
        mock_k8s_connector._core_v1.read_namespaced_pod = AsyncMock(return_value=mock_pod)

        mock_events = MagicMock()
        mock_events.items = []
        mock_k8s_connector._core_v1.list_namespaced_event = AsyncMock(return_value=mock_events)

        result = await mock_k8s_connector.execute(
            "describe_pod", {"name": "nginx", "namespace": "default"}
        )

        assert result.success
        assert result.data["name"] == "nginx"
        assert "events" in result.data


class TestDeploymentOperations:
    """Test deployment operations."""

    @pytest.mark.asyncio
    async def test_list_deployments(self, mock_k8s_connector):
        """Test list_deployments operation."""
        mock_deployments = MagicMock()
        mock_deployments.items = [
            create_mock_deployment("deploy-1", "default"),
            create_mock_deployment("deploy-2", "default"),
        ]
        mock_k8s_connector._apps_v1.list_namespaced_deployment = AsyncMock(
            return_value=mock_deployments
        )

        result = await mock_k8s_connector.execute("list_deployments", {"namespace": "default"})

        assert result.success
        assert len(result.data) == 2

    @pytest.mark.asyncio
    async def test_get_deployment(self, mock_k8s_connector):
        """Test get_deployment operation."""
        mock_deployment = create_mock_deployment("nginx", "default")
        mock_k8s_connector._apps_v1.read_namespaced_deployment = AsyncMock(
            return_value=mock_deployment
        )

        result = await mock_k8s_connector.execute(
            "get_deployment", {"name": "nginx", "namespace": "default"}
        )

        assert result.success
        assert result.data["name"] == "nginx"
        assert result.data["replicas"] == 3

    @pytest.mark.asyncio
    async def test_scale_deployment(self, mock_k8s_connector):
        """Test scale_deployment operation."""
        mock_k8s_connector._apps_v1.patch_namespaced_deployment_scale = AsyncMock()

        result = await mock_k8s_connector.execute(
            "scale_deployment",
            {"name": "nginx", "namespace": "default", "replicas": 5},
        )

        assert result.success
        assert result.data["replicas"] == 5
        assert result.data["scaled"] is True

    @pytest.mark.asyncio
    async def test_restart_deployment(self, mock_k8s_connector):
        """Test restart_deployment operation."""
        mock_k8s_connector._apps_v1.patch_namespaced_deployment = AsyncMock()

        result = await mock_k8s_connector.execute(
            "restart_deployment", {"name": "nginx", "namespace": "default"}
        )

        assert result.success
        assert result.data["restarted"] is True
        assert "restart_time" in result.data


class TestServiceOperations:
    """Test service operations."""

    @pytest.mark.asyncio
    async def test_list_services(self, mock_k8s_connector):
        """Test list_services operation."""
        mock_services = MagicMock()
        mock_services.items = [
            create_mock_service("svc-1", "default"),
            create_mock_service("svc-2", "default"),
        ]
        mock_k8s_connector._core_v1.list_namespaced_service = AsyncMock(return_value=mock_services)

        result = await mock_k8s_connector.execute("list_services", {"namespace": "default"})

        assert result.success
        assert len(result.data) == 2

    @pytest.mark.asyncio
    async def test_get_service(self, mock_k8s_connector):
        """Test get_service operation."""
        mock_svc = create_mock_service("nginx", "default")
        mock_k8s_connector._core_v1.read_namespaced_service = AsyncMock(return_value=mock_svc)

        result = await mock_k8s_connector.execute(
            "get_service", {"name": "nginx", "namespace": "default"}
        )

        assert result.success
        assert result.data["name"] == "nginx"
        assert result.data["type"] == "ClusterIP"


class TestNodeOperations:
    """Test node operations."""

    @pytest.mark.asyncio
    async def test_list_nodes(self, mock_k8s_connector):
        """Test list_nodes operation."""
        mock_nodes = MagicMock()
        mock_nodes.items = [
            create_mock_node("node-1"),
            create_mock_node("node-2"),
        ]
        mock_k8s_connector._core_v1.list_node = AsyncMock(return_value=mock_nodes)

        result = await mock_k8s_connector.execute("list_nodes", {})

        assert result.success
        assert len(result.data) == 2

    @pytest.mark.asyncio
    async def test_get_node(self, mock_k8s_connector):
        """Test get_node operation."""
        mock_node = create_mock_node("worker-01")
        mock_k8s_connector._core_v1.read_node = AsyncMock(return_value=mock_node)

        result = await mock_k8s_connector.execute("get_node", {"name": "worker-01"})

        assert result.success
        assert result.data["name"] == "worker-01"

    @pytest.mark.asyncio
    async def test_cordon_node(self, mock_k8s_connector):
        """Test cordon_node operation."""
        mock_k8s_connector._core_v1.patch_node = AsyncMock()

        result = await mock_k8s_connector.execute("cordon_node", {"name": "worker-01"})

        assert result.success
        assert result.data["cordoned"] is True
        assert result.data["unschedulable"] is True


class TestEventOperations:
    """Test event operations."""

    @pytest.mark.asyncio
    async def test_list_events(self, mock_k8s_connector):
        """Test list_events operation."""
        mock_events = MagicMock()
        mock_event = MagicMock()
        mock_event.metadata.name = "event-1"
        mock_event.metadata.namespace = "default"
        mock_event.metadata.uid = "uid-event-1"
        mock_event.type = "Warning"
        mock_event.reason = "FailedMount"
        mock_event.message = "Unable to mount volume"
        mock_event.count = 5
        mock_event.first_timestamp = datetime.now(UTC)
        mock_event.last_timestamp = datetime.now(UTC)
        mock_event.involved_object = MagicMock(
            kind="Pod", name="nginx", namespace="default", uid="pod-uid"
        )
        mock_event.source = MagicMock(component="kubelet", host="node-1")
        mock_events.items = [mock_event]

        mock_k8s_connector._core_v1.list_namespaced_event = AsyncMock(return_value=mock_events)

        result = await mock_k8s_connector.execute("list_events", {"namespace": "default"})

        assert result.success
        assert len(result.data) == 1
        assert result.data[0]["reason"] == "FailedMount"


class TestErrorHandling:
    """Test error handling."""

    @pytest.mark.asyncio
    async def test_not_found_error(self, mock_k8s_connector):
        """Test handling of 404 errors."""
        from kubernetes_asyncio.client.exceptions import ApiException

        mock_k8s_connector._core_v1.read_namespaced_pod = AsyncMock(
            side_effect=ApiException(status=404, reason="Not Found")
        )

        result = await mock_k8s_connector.execute(
            "get_pod", {"name": "nonexistent", "namespace": "default"}
        )

        assert not result.success
        assert result.error_code == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_permission_denied_error(self, mock_k8s_connector):
        """Test handling of 403 errors."""
        from kubernetes_asyncio.client.exceptions import ApiException

        mock_k8s_connector._core_v1.read_namespaced_pod = AsyncMock(
            side_effect=ApiException(status=403, reason="Forbidden")
        )

        result = await mock_k8s_connector.execute(
            "get_pod", {"name": "secret-pod", "namespace": "kube-system"}
        )

        assert not result.success
        assert result.error_code == "PERMISSION_DENIED"

    @pytest.mark.asyncio
    async def test_authentication_error(self, mock_k8s_connector):
        """Test handling of 401 errors."""
        from kubernetes_asyncio.client.exceptions import ApiException

        mock_k8s_connector._core_v1.read_namespaced_pod = AsyncMock(
            side_effect=ApiException(status=401, reason="Unauthorized")
        )

        result = await mock_k8s_connector.execute(
            "get_pod", {"name": "some-pod", "namespace": "default"}
        )

        assert not result.success
        assert result.error_code == "AUTHENTICATION_FAILED"
