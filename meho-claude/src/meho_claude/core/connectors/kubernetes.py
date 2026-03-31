"""Kubernetes connector implementing BaseConnector via kubernetes-asyncio.

Registered as "kubernetes" in the connector registry. Uses kubernetes-asyncio
for async API calls, supporting all 6 resource types (pods, deployments,
services, nodes, ingresses, namespaces) plus write operations.
"""

from __future__ import annotations

from typing import Any

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiClient

from meho_claude.core.connectors.base import BaseConnector
from meho_claude.core.connectors.models import ConnectorConfig, Operation
from meho_claude.core.connectors.registry import register_connector

# Operation definitions (connector_name is substituted at discover time)
_K8S_OPERATIONS = [
    # READ operations
    {
        "operation_id": "list-pods",
        "display_name": "List Pods",
        "description": "List pods across all namespaces or a specific namespace",
        "trust_tier": "READ",
        "input_schema": {"namespace": {"type": "string", "required": False}},
        "tags": ["kubernetes", "pods", "workload"],
    },
    {
        "operation_id": "get-pod",
        "display_name": "Get Pod",
        "description": "Get a specific pod by name and namespace",
        "trust_tier": "READ",
        "input_schema": {
            "name": {"type": "string", "required": True},
            "namespace": {"type": "string", "required": True},
        },
        "tags": ["kubernetes", "pods", "workload"],
    },
    {
        "operation_id": "list-deployments",
        "display_name": "List Deployments",
        "description": "List deployments across all namespaces or a specific namespace",
        "trust_tier": "READ",
        "input_schema": {"namespace": {"type": "string", "required": False}},
        "tags": ["kubernetes", "deployments", "workload"],
    },
    {
        "operation_id": "get-deployment",
        "display_name": "Get Deployment",
        "description": "Get a specific deployment by name and namespace",
        "trust_tier": "READ",
        "input_schema": {
            "name": {"type": "string", "required": True},
            "namespace": {"type": "string", "required": True},
        },
        "tags": ["kubernetes", "deployments", "workload"],
    },
    {
        "operation_id": "list-services",
        "display_name": "List Services",
        "description": "List services across all namespaces or a specific namespace",
        "trust_tier": "READ",
        "input_schema": {"namespace": {"type": "string", "required": False}},
        "tags": ["kubernetes", "services", "networking"],
    },
    {
        "operation_id": "get-service",
        "display_name": "Get Service",
        "description": "Get a specific service by name and namespace",
        "trust_tier": "READ",
        "input_schema": {
            "name": {"type": "string", "required": True},
            "namespace": {"type": "string", "required": True},
        },
        "tags": ["kubernetes", "services", "networking"],
    },
    {
        "operation_id": "list-nodes",
        "display_name": "List Nodes",
        "description": "List all cluster nodes with status and capacity",
        "trust_tier": "READ",
        "input_schema": {},
        "tags": ["kubernetes", "nodes", "infrastructure"],
    },
    {
        "operation_id": "get-node",
        "display_name": "Get Node",
        "description": "Get a specific node by name",
        "trust_tier": "READ",
        "input_schema": {"name": {"type": "string", "required": True}},
        "tags": ["kubernetes", "nodes", "infrastructure"],
    },
    {
        "operation_id": "list-ingresses",
        "display_name": "List Ingresses",
        "description": "List ingresses across all namespaces or a specific namespace",
        "trust_tier": "READ",
        "input_schema": {"namespace": {"type": "string", "required": False}},
        "tags": ["kubernetes", "ingresses", "networking"],
    },
    {
        "operation_id": "list-namespaces",
        "display_name": "List Namespaces",
        "description": "List all namespaces in the cluster",
        "trust_tier": "READ",
        "input_schema": {},
        "tags": ["kubernetes", "namespaces"],
    },
    # WRITE operations
    {
        "operation_id": "scale-deployment",
        "display_name": "Scale Deployment",
        "description": "Scale a deployment to the specified number of replicas",
        "trust_tier": "WRITE",
        "input_schema": {
            "name": {"type": "string", "required": True},
            "namespace": {"type": "string", "required": True},
            "replicas": {"type": "integer", "required": True},
        },
        "tags": ["kubernetes", "deployments", "scaling"],
    },
    {
        "operation_id": "cordon-node",
        "display_name": "Cordon Node",
        "description": "Mark a node as unschedulable (cordon)",
        "trust_tier": "WRITE",
        "input_schema": {"name": {"type": "string", "required": True}},
        "tags": ["kubernetes", "nodes", "scheduling"],
    },
    {
        "operation_id": "uncordon-node",
        "display_name": "Uncordon Node",
        "description": "Mark a node as schedulable (uncordon)",
        "trust_tier": "WRITE",
        "input_schema": {"name": {"type": "string", "required": True}},
        "tags": ["kubernetes", "nodes", "scheduling"],
    },
    # DESTRUCTIVE operations
    {
        "operation_id": "delete-pod",
        "display_name": "Delete Pod",
        "description": "Delete a specific pod by name and namespace",
        "trust_tier": "DESTRUCTIVE",
        "input_schema": {
            "name": {"type": "string", "required": True},
            "namespace": {"type": "string", "required": True},
        },
        "tags": ["kubernetes", "pods", "destructive"],
    },
]


@register_connector("kubernetes")
class KubernetesConnector(BaseConnector):
    """Kubernetes connector using kubernetes-asyncio.

    Supports all 6 resource types (pods, deployments, services, nodes,
    ingresses, namespaces) plus write operations (scale, cordon, delete).
    Uses async with ApiClient() context manager per-execute to prevent leaks.
    """

    def __init__(self, config_obj: ConnectorConfig, credentials: dict | None = None) -> None:
        super().__init__(config_obj, credentials)

    async def _load_config(self) -> None:
        """Load kubeconfig from config settings."""
        await config.load_kube_config(
            config_file=self.config.kubeconfig_path,
            context=self.config.kubeconfig_context,
        )

    async def test_connection(self) -> dict[str, Any]:
        """Test connectivity by listing namespaces.

        Returns:
            Dict with status and namespace count on success,
            or status and error message on failure.
        """
        try:
            await self._load_config()
            async with ApiClient() as api:
                v1 = client.CoreV1Api(api)
                ns_list = await v1.list_namespace()
                ns_dict = ns_list.to_dict()
                count = len(ns_dict.get("items", []))
                return {"status": "ok", "namespaces": count}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    async def discover_operations(self) -> list[Operation]:
        """Return hardcoded operations for all K8s resource types.

        Operations are defined at module level and built with the
        actual connector name at discovery time.
        """
        operations = []
        for op_def in _K8S_OPERATIONS:
            operations.append(
                Operation(
                    connector_name=self.config.name,
                    operation_id=op_def["operation_id"],
                    display_name=op_def["display_name"],
                    description=op_def["description"],
                    trust_tier=op_def["trust_tier"],
                    input_schema=op_def["input_schema"],
                    tags=op_def["tags"],
                )
            )
        return operations

    async def execute(self, operation: Operation, params: dict[str, Any]) -> dict[str, Any]:
        """Execute a K8s operation by routing to the correct API method.

        Uses async with ApiClient() context manager to prevent session leaks.
        """
        await self._load_config()

        async with ApiClient() as api:
            op_id = operation.operation_id
            ns = params.get("namespace")
            name = params.get("name")

            if op_id in ("list-pods", "get-pod", "delete-pod",
                         "list-services", "get-service",
                         "list-nodes", "get-node",
                         "list-namespaces",
                         "cordon-node", "uncordon-node"):
                v1 = client.CoreV1Api(api)
                return await self._execute_core(v1, op_id, name, ns, params)

            elif op_id in ("list-deployments", "get-deployment", "scale-deployment"):
                apps_v1 = client.AppsV1Api(api)
                return await self._execute_apps(apps_v1, op_id, name, ns, params)

            elif op_id == "list-ingresses":
                networking_v1 = client.NetworkingV1Api(api)
                return await self._execute_networking(networking_v1, op_id, name, ns)

            else:
                raise ValueError(f"Unknown operation: {op_id}")

    async def _execute_core(
        self,
        v1: client.CoreV1Api,
        op_id: str,
        name: str | None,
        ns: str | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute CoreV1Api operations."""
        if op_id == "list-pods":
            if ns:
                resp = await v1.list_namespaced_pod(namespace=ns)
            else:
                resp = await v1.list_pod_for_all_namespaces()
            return {"data": resp.to_dict()}

        elif op_id == "get-pod":
            resp = await v1.read_namespaced_pod(name=name, namespace=ns)
            return {"data": resp.to_dict()}

        elif op_id == "delete-pod":
            resp = await v1.delete_namespaced_pod(name=name, namespace=ns)
            return {"data": resp.to_dict()}

        elif op_id == "list-services":
            if ns:
                resp = await v1.list_namespaced_service(namespace=ns)
            else:
                resp = await v1.list_service_for_all_namespaces()
            return {"data": resp.to_dict()}

        elif op_id == "get-service":
            resp = await v1.read_namespaced_service(name=name, namespace=ns)
            return {"data": resp.to_dict()}

        elif op_id == "list-nodes":
            resp = await v1.list_node()
            return {"data": resp.to_dict()}

        elif op_id == "get-node":
            resp = await v1.read_node(name=name)
            return {"data": resp.to_dict()}

        elif op_id == "list-namespaces":
            resp = await v1.list_namespace()
            return {"data": resp.to_dict()}

        elif op_id == "cordon-node":
            resp = await v1.patch_node(
                name=name, body={"spec": {"unschedulable": True}}
            )
            return {"data": resp.to_dict()}

        elif op_id == "uncordon-node":
            resp = await v1.patch_node(
                name=name, body={"spec": {"unschedulable": False}}
            )
            return {"data": resp.to_dict()}

        else:
            raise ValueError(f"Unknown core operation: {op_id}")

    async def _execute_apps(
        self,
        apps_v1: client.AppsV1Api,
        op_id: str,
        name: str | None,
        ns: str | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute AppsV1Api operations."""
        if op_id == "list-deployments":
            if ns:
                resp = await apps_v1.list_namespaced_deployment(namespace=ns)
            else:
                resp = await apps_v1.list_deployment_for_all_namespaces()
            return {"data": resp.to_dict()}

        elif op_id == "get-deployment":
            resp = await apps_v1.read_namespaced_deployment(name=name, namespace=ns)
            return {"data": resp.to_dict()}

        elif op_id == "scale-deployment":
            replicas = int(params["replicas"])
            resp = await apps_v1.patch_namespaced_deployment_scale(
                name=name,
                namespace=ns,
                body={"spec": {"replicas": replicas}},
            )
            return {"data": resp.to_dict()}

        else:
            raise ValueError(f"Unknown apps operation: {op_id}")

    async def _execute_networking(
        self,
        networking_v1: client.NetworkingV1Api,
        op_id: str,
        name: str | None,
        ns: str | None,
    ) -> dict[str, Any]:
        """Execute NetworkingV1Api operations."""
        if op_id == "list-ingresses":
            if ns:
                resp = await networking_v1.list_namespaced_ingress(namespace=ns)
            else:
                resp = await networking_v1.list_ingress_for_all_namespaces()
            return {"data": resp.to_dict()}

        else:
            raise ValueError(f"Unknown networking operation: {op_id}")

    def get_trust_tier(self, operation: Operation) -> str:
        """Determine trust tier, checking config overrides first."""
        override_map = {o.operation_id: o.trust_tier for o in self.config.trust_overrides}
        if operation.operation_id in override_map:
            return override_map[operation.operation_id]
        return operation.trust_tier

    def close(self) -> None:
        """No-op -- ApiClient is created per-execute via context manager."""
        pass
