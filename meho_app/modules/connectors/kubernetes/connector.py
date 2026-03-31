# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Kubernetes Connector using kubernetes-asyncio (TASK-159)

Implements the BaseConnector interface for native Kubernetes API access.

Provides pre-defined operations for:
- Pod management (list, get, logs, describe)
- Deployment operations (list, get, scale, restart)
- Service discovery (list, get)
- Node status (list, get, describe)
- Namespace management (list, get)
- Storage resources (PVCs, PVs, StorageClasses)
- Events and debugging

Authentication Methods:
- Bearer token (service account)
- Kubeconfig file (future)

Example:
    connector = KubernetesConnector(
        connector_id="abc123",
        config={
            "server_url": "https://k8s.example.com:6443",
            "skip_tls_verification": False,
        },
        credentials={
            "token": "eyJhbGciOiJS...",  # Service account token
        }
    )

    async with connector:
        result = await connector.execute("list_pods", {"namespace": "default"})
        print(result.data)
"""

import time
from collections.abc import Callable
from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.base import (
    BaseConnector,
    OperationDefinition,
    OperationResult,
    TypeDefinition,
)

# Import serializers
from meho_app.modules.connectors.kubernetes import serializers

# Import all handler mixins
from meho_app.modules.connectors.kubernetes.handlers import (
    DeploymentHandlerMixin,
    NamespaceHandlerMixin,
    NodeHandlerMixin,
    PodHandlerMixin,
    ServiceHandlerMixin,
)
from meho_app.modules.connectors.kubernetes.operations import KUBERNETES_OPERATIONS
from meho_app.modules.connectors.kubernetes.types import KUBERNETES_TYPES

logger = get_logger(__name__)


class KubernetesConnector(
    BaseConnector,
    PodHandlerMixin,
    DeploymentHandlerMixin,
    ServiceHandlerMixin,
    NodeHandlerMixin,
    NamespaceHandlerMixin,
):
    """
    Kubernetes connector using kubernetes-asyncio.

    Provides native access to Kubernetes clusters for:
    - Pod management (list, get, logs, describe)
    - Deployment operations (list, get, scale, restart)
    - Service discovery (list, get)
    - Node status (list, get, describe)
    - Namespace management (list, get)
    - Storage resources (PVCs, PVs)
    - Events and debugging

    Organization:
    - Pod operations: handlers/pod_handlers.py
    - Deployment operations: handlers/deployment_handlers.py
    - Service operations: handlers/service_handlers.py
    - Node operations: handlers/node_handlers.py
    - Namespace operations: handlers/namespace_handlers.py
    """

    def __init__(
        self,
        connector_id: str,
        config: dict[str, Any],
        credentials: dict[str, Any],
    ):
        super().__init__(connector_id, config, credentials)

        # Kubernetes API clients (initialized on connect)
        self._api_client: Any | None = None
        self._core_v1: Any | None = None
        self._apps_v1: Any | None = None
        self._batch_v1: Any | None = None
        self._networking_v1: Any | None = None
        self._storage_v1: Any | None = None

        # Configuration
        self.server_url = config.get("server_url", "")
        self.skip_tls = config.get("skip_tls_verification", False)
        self.ca_certificate = config.get("ca_certificate")

        # Kubernetes version (detected on connect)
        self.kubernetes_version: str | None = None

        # Temp file for CA cert (cleaned up on disconnect)
        self._ca_cert_file: str | None = None

        # Build operation dispatch table
        self._operation_handlers = self._build_operation_handlers()

    def _build_operation_handlers(self) -> dict[str, Callable]:
        """Build mapping of operation_id to handler method."""
        return {
            # Core: Pods
            "list_pods": self._list_pods,
            "get_pod": self._get_pod,
            "get_pod_logs": self._get_pod_logs,
            "describe_pod": self._describe_pod,
            "delete_pod": self._delete_pod,
            # Core: Nodes
            "list_nodes": self._list_nodes,
            "get_node": self._get_node,
            "describe_node": self._describe_node,
            "cordon_node": self._cordon_node,
            "uncordon_node": self._uncordon_node,
            # Core: Namespaces
            "list_namespaces": self._list_namespaces,
            "get_namespace": self._get_namespace,
            # Core: ConfigMaps
            "list_configmaps": self._list_configmaps,
            "get_configmap": self._get_configmap,
            # Core: Secrets
            "list_secrets": self._list_secrets,
            "get_secret": self._get_secret,
            # Workloads: Deployments
            "list_deployments": self._list_deployments,
            "get_deployment": self._get_deployment,
            "scale_deployment": self._scale_deployment,
            "restart_deployment": self._restart_deployment,
            "describe_deployment": self._describe_deployment,
            # Workloads: ReplicaSets
            "list_replicasets": self._list_replicasets,
            "get_replicaset": self._get_replicaset,
            # Workloads: StatefulSets
            "list_statefulsets": self._list_statefulsets,
            "get_statefulset": self._get_statefulset,
            "scale_statefulset": self._scale_statefulset,
            # Workloads: DaemonSets
            "list_daemonsets": self._list_daemonsets,
            "get_daemonset": self._get_daemonset,
            # Workloads: Jobs
            "list_jobs": self._list_jobs,
            "get_job": self._get_job,
            # Workloads: CronJobs
            "list_cronjobs": self._list_cronjobs,
            "get_cronjob": self._get_cronjob,
            # Networking: Services
            "list_services": self._list_services,
            "get_service": self._get_service,
            "describe_service": self._describe_service,
            # Networking: Ingresses
            "list_ingresses": self._list_ingresses,
            "get_ingress": self._get_ingress,
            # Networking: Endpoints
            "list_endpoints": self._list_endpoints,
            "get_endpoints": self._get_endpoints,
            # Networking: NetworkPolicies
            "list_network_policies": self._list_network_policies,
            "get_network_policy": self._get_network_policy,
            # Storage: PVCs
            "list_pvcs": self._list_pvcs,
            "get_pvc": self._get_pvc,
            # Storage: PVs
            "list_pvs": self._list_pvs,
            "get_pv": self._get_pv,
            # Storage: StorageClasses
            "list_storageclasses": self._list_storageclasses,
            "get_storageclass": self._get_storageclass,
            # Events
            "list_events": self._list_events,
            "get_events_for_resource": self._get_events_for_resource,
        }

    # =========================================================================
    # CONNECTION MANAGEMENT
    # =========================================================================

    async def connect(self) -> bool:
        """Establish connection to Kubernetes cluster."""
        if self._is_connected:
            return True

        try:
            # Import kubernetes-asyncio here to fail gracefully if not installed
            from kubernetes_asyncio import client
            from kubernetes_asyncio.client import ApiClient
        except ImportError:
            raise ImportError(
                "kubernetes-asyncio is required for Kubernetes connector. "
                "Install with: pip install kubernetes-asyncio"
            ) from None

        if not self.server_url:
            raise ValueError("server_url is required in config")

        # Get token
        token = self.credentials.get("token") or self.credentials.get("access_token")
        if not token:
            raise ValueError("token or access_token is required in credentials")

        logger.info(f"🔌 Connecting to Kubernetes: {self.server_url}")

        try:
            # Build configuration
            configuration = client.Configuration()
            configuration.host = self.server_url
            configuration.verify_ssl = not self.skip_tls

            # Set authentication - kubernetes-asyncio expects api_key and api_key_prefix separately
            # The library builds the header as: "{api_key_prefix[key]} {api_key[key]}"
            configuration.api_key = {"authorization": token}
            configuration.api_key_prefix = {"authorization": "Bearer"}

            # Optional CA certificate
            if self.ca_certificate:
                import tempfile

                # Write CA cert to temp file for kubernetes client
                with tempfile.NamedTemporaryFile(mode="w", suffix=".crt", delete=False) as f:
                    f.write(self.ca_certificate)
                    self._ca_cert_file = f.name
                configuration.ssl_ca_cert = self._ca_cert_file

            # Create API client
            self._api_client = ApiClient(configuration)

            # Set Authorization header directly on default_headers for reliability
            # This ensures the header is sent with every request regardless of api_key mechanism
            self._api_client.default_headers["Authorization"] = f"Bearer {token}"

            # Initialize API clients
            self._core_v1 = client.CoreV1Api(self._api_client)
            self._apps_v1 = client.AppsV1Api(self._api_client)
            self._batch_v1 = client.BatchV1Api(self._api_client)
            self._networking_v1 = client.NetworkingV1Api(self._api_client)
            self._storage_v1 = client.StorageV1Api(self._api_client)

            # Test connection and get version
            version_api = client.VersionApi(self._api_client)
            version_info = await version_api.get_code()
            self.kubernetes_version = version_info.git_version

            self._is_connected = True
            logger.info(
                f"✅ Connected to Kubernetes: {self.server_url} "
                f"(version: {self.kubernetes_version})"
            )
            return True

        except Exception as e:
            logger.error(f"❌ Kubernetes connection failed: {e}", exc_info=True)
            self._is_connected = False
            raise

    async def disconnect(self) -> None:
        """Close connection to Kubernetes cluster."""
        if self._api_client:
            try:
                await self._api_client.close()
                logger.info("🔌 Disconnected from Kubernetes")
            except Exception as e:
                logger.warning(f"⚠️ Error disconnecting: {e}")
            finally:
                self._api_client = None

        # Clean up temp CA cert file
        if self._ca_cert_file:
            try:
                import os

                os.unlink(self._ca_cert_file)
            except Exception:  # noqa: S110 -- intentional silent exception handling
                pass
            self._ca_cert_file = None

        self._core_v1 = None
        self._apps_v1 = None
        self._batch_v1 = None
        self._networking_v1 = None
        self._storage_v1 = None
        self._is_connected = False

    async def test_connection(self) -> bool:
        """Test if connection is alive."""
        try:
            if not self._is_connected:
                await self.connect()

            # Quick health check
            from kubernetes_asyncio import client

            version_api = client.VersionApi(self._api_client)
            version_info = await version_api.get_code()
            logger.info(f"✅ Kubernetes connection test passed: {version_info.git_version}")
            return True
        except Exception as e:
            logger.error(f"❌ Connection test failed: {e}", exc_info=True)
            return False

    # =========================================================================
    # OPERATION & TYPE DISCOVERY
    # =========================================================================

    def get_operations(self) -> list[OperationDefinition]:
        """Get Kubernetes operations for registration."""
        return KUBERNETES_OPERATIONS

    def get_types(self) -> list[TypeDefinition]:
        """Get Kubernetes types for registration."""
        return KUBERNETES_TYPES

    # =========================================================================
    # OPERATION EXECUTION
    # =========================================================================

    async def execute(
        self,
        operation_id: str,
        parameters: dict[str, Any],
    ) -> OperationResult:
        """Execute a Kubernetes operation."""
        start_time = time.time()

        if not self._is_connected:
            await self.connect()

        handler = self._operation_handlers.get(operation_id)
        if not handler:
            return OperationResult(
                success=False,
                error=f"Unknown operation: {operation_id}",
                error_code="UNKNOWN_OPERATION",
                operation_id=operation_id,
            )

        try:
            result = await handler(parameters)
            duration_ms = (time.time() - start_time) * 1000

            logger.info(f"✅ {operation_id}: completed in {duration_ms:.1f}ms")

            return OperationResult(
                success=True,
                data=result,
                operation_id=operation_id,
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.error(f"❌ {operation_id} failed: {e}", exc_info=True)

            # Map K8s exceptions to error codes
            error_code = self._map_k8s_exception(e)

            return OperationResult(
                success=False,
                error=str(e),
                error_code=error_code,
                operation_id=operation_id,
                duration_ms=duration_ms,
            )

    def _map_k8s_exception(self, e: Exception) -> str:
        """Map Kubernetes exception to error code."""
        try:
            from kubernetes_asyncio.client.exceptions import ApiException

            if isinstance(e, ApiException):
                if e.status == 401:
                    return "AUTHENTICATION_FAILED"
                elif e.status == 403:
                    return "PERMISSION_DENIED"
                elif e.status == 404:
                    return "NOT_FOUND"
                elif e.status == 409:
                    return "CONFLICT"
                elif e.status == 422:
                    return "INVALID_REQUEST"
                elif e.status == 429:
                    return "RATE_LIMITED"
                elif e.status >= 500:
                    return "SERVER_ERROR"
        except ImportError:
            pass

        return "INTERNAL_ERROR"

    # =========================================================================
    # SERIALIZER WRAPPERS (Delegate to serializers module)
    # =========================================================================

    def _serialize_pod(self, pod: Any) -> dict[str, Any]:
        """Serialize Pod object."""
        return serializers.serialize_pod(pod)

    def _serialize_deployment(self, deployment: Any) -> dict[str, Any]:
        """Serialize Deployment object."""
        return serializers.serialize_deployment(deployment)

    def _serialize_replicaset(self, rs: Any) -> dict[str, Any]:
        """Serialize ReplicaSet object."""
        return serializers.serialize_replicaset(rs)

    def _serialize_statefulset(self, sts: Any) -> dict[str, Any]:
        """Serialize StatefulSet object."""
        return serializers.serialize_statefulset(sts)

    def _serialize_daemonset(self, ds: Any) -> dict[str, Any]:
        """Serialize DaemonSet object."""
        return serializers.serialize_daemonset(ds)

    def _serialize_job(self, job: Any) -> dict[str, Any]:
        """Serialize Job object."""
        return serializers.serialize_job(job)

    def _serialize_cronjob(self, cj: Any) -> dict[str, Any]:
        """Serialize CronJob object."""
        return serializers.serialize_cronjob(cj)

    def _serialize_service(self, svc: Any) -> dict[str, Any]:
        """Serialize Service object."""
        return serializers.serialize_service(svc)

    def _serialize_ingress(self, ing: Any) -> dict[str, Any]:
        """Serialize Ingress object."""
        return serializers.serialize_ingress(ing)

    def _serialize_endpoints(self, ep: Any) -> dict[str, Any]:
        """Serialize Endpoints object."""
        return serializers.serialize_endpoints(ep)

    def _serialize_network_policy(self, np: Any) -> dict[str, Any]:
        """Serialize NetworkPolicy object."""
        return serializers.serialize_network_policy(np)

    def _serialize_node(self, node: Any) -> dict[str, Any]:
        """Serialize Node object."""
        return serializers.serialize_node(node)

    def _serialize_namespace(self, ns: Any) -> dict[str, Any]:
        """Serialize Namespace object."""
        return serializers.serialize_namespace(ns)

    def _serialize_configmap(self, cm: Any) -> dict[str, Any]:
        """Serialize ConfigMap object."""
        return serializers.serialize_configmap(cm)

    def _serialize_secret(self, secret: Any, decode: bool = False) -> dict[str, Any]:
        """Serialize Secret object."""
        return serializers.serialize_secret(secret, decode=decode)

    def _serialize_pvc(self, pvc: Any) -> dict[str, Any]:
        """Serialize PVC object."""
        return serializers.serialize_pvc(pvc)

    def _serialize_pv(self, pv: Any) -> dict[str, Any]:
        """Serialize PV object."""
        return serializers.serialize_pv(pv)

    def _serialize_storageclass(self, sc: Any) -> dict[str, Any]:
        """Serialize StorageClass object."""
        return serializers.serialize_storageclass(sc)

    def _serialize_event(self, event: Any) -> dict[str, Any]:
        """Serialize Event object."""
        return serializers.serialize_event(event)

    # =========================================================================
    # CONTEXT MANAGER
    # =========================================================================

    async def __aenter__(self) -> "KubernetesConnector":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.disconnect()
