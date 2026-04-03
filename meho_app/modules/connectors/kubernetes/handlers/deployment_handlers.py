# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Deployment Operation Handlers

Mixin class containing deployment, replicaset, statefulset, daemonset,
job, and cronjob operation handlers for Kubernetes connector.
"""

from datetime import UTC, datetime
from typing import Any


class DeploymentHandlerMixin:
    """Mixin for deployment and workload operation handlers."""

    # These will be provided by KubernetesConnector (base class)
    _apps_v1: Any
    _batch_v1: Any
    _core_v1: Any

    # Serializer methods (will be provided by KubernetesConnector)
    def _serialize_deployment(self, _deployment: Any) -> dict[str, Any]:
        return {}

    def _serialize_replicaset(self, _rs: Any) -> dict[str, Any]:
        return {}

    def _serialize_statefulset(self, _sts: Any) -> dict[str, Any]:
        return {}

    def _serialize_daemonset(self, _ds: Any) -> dict[str, Any]:
        return {}

    def _serialize_job(self, _job: Any) -> dict[str, Any]:
        return {}

    def _serialize_cronjob(self, _cj: Any) -> dict[str, Any]:
        return {}

    # ==========================================================================
    # Deployments
    # ==========================================================================

    async def _list_deployments(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """List all deployments in a namespace or across all namespaces."""
        namespace = params.get("namespace")
        label_selector = params.get("label_selector", "")

        if namespace:
            result = await self._apps_v1.list_namespaced_deployment(
                namespace=namespace,
                label_selector=label_selector,
            )
        else:
            result = await self._apps_v1.list_deployment_for_all_namespaces(
                label_selector=label_selector,
            )

        return [self._serialize_deployment(d) for d in result.items]

    async def _get_deployment(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get details about a specific deployment."""
        name = params["name"]
        namespace = params["namespace"]

        deployment = await self._apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
        return self._serialize_deployment(deployment)

    async def _scale_deployment(self, params: dict[str, Any]) -> dict[str, Any]:
        """Scale a deployment to a specific number of replicas."""
        name = params["name"]
        namespace = params["namespace"]
        replicas = params["replicas"]

        # Patch the deployment scale
        body = {"spec": {"replicas": replicas}}
        await self._apps_v1.patch_namespaced_deployment_scale(
            name=name,
            namespace=namespace,
            body=body,
        )

        return {
            "deployment": name,
            "namespace": namespace,
            "replicas": replicas,
            "scaled": True,
        }

    async def _restart_deployment(self, params: dict[str, Any]) -> dict[str, Any]:
        """Trigger a rolling restart of a deployment."""
        name = params["name"]
        namespace = params["namespace"]

        # Add annotation to trigger rollout (same as kubectl rollout restart)
        now = datetime.now(UTC).isoformat()
        body = {
            "spec": {
                "template": {
                    "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}
                }
            }
        }

        await self._apps_v1.patch_namespaced_deployment(
            name=name,
            namespace=namespace,
            body=body,
        )

        return {
            "deployment": name,
            "namespace": namespace,
            "restarted": True,
            "restart_time": now,
        }

    async def _describe_deployment(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get comprehensive deployment information including events."""
        name = params["name"]
        namespace = params["namespace"]

        # Get deployment details
        deployment = await self._apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
        deployment_data = self._serialize_deployment(deployment)

        # Get events for this deployment
        events = await self._core_v1.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={name},involvedObject.kind=Deployment",
        )
        event_list = []
        for event in events.items:
            event_list.append(
                {
                    "type": event.type,
                    "reason": event.reason,
                    "message": event.message,
                    "count": event.count,
                    "first_timestamp": (
                        event.first_timestamp.isoformat() if event.first_timestamp else None
                    ),
                    "last_timestamp": (
                        event.last_timestamp.isoformat() if event.last_timestamp else None
                    ),
                }
            )

        deployment_data["events"] = event_list
        return deployment_data

    # ==========================================================================
    # ReplicaSets
    # ==========================================================================

    async def _list_replicasets(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """List all ReplicaSets in a namespace or across all namespaces."""
        namespace = params.get("namespace")
        label_selector = params.get("label_selector", "")

        if namespace:
            result = await self._apps_v1.list_namespaced_replica_set(
                namespace=namespace,
                label_selector=label_selector,
            )
        else:
            result = await self._apps_v1.list_replica_set_for_all_namespaces(
                label_selector=label_selector,
            )

        return [self._serialize_replicaset(rs) for rs in result.items]

    async def _get_replicaset(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get details about a specific ReplicaSet."""
        name = params["name"]
        namespace = params["namespace"]

        rs = await self._apps_v1.read_namespaced_replica_set(name=name, namespace=namespace)
        return self._serialize_replicaset(rs)

    # ==========================================================================
    # StatefulSets
    # ==========================================================================

    async def _list_statefulsets(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """List all StatefulSets in a namespace or across all namespaces."""
        namespace = params.get("namespace")
        label_selector = params.get("label_selector", "")

        if namespace:
            result = await self._apps_v1.list_namespaced_stateful_set(
                namespace=namespace,
                label_selector=label_selector,
            )
        else:
            result = await self._apps_v1.list_stateful_set_for_all_namespaces(
                label_selector=label_selector,
            )

        return [self._serialize_statefulset(sts) for sts in result.items]

    async def _get_statefulset(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get details about a specific StatefulSet."""
        name = params["name"]
        namespace = params["namespace"]

        sts = await self._apps_v1.read_namespaced_stateful_set(name=name, namespace=namespace)
        return self._serialize_statefulset(sts)

    async def _scale_statefulset(self, params: dict[str, Any]) -> dict[str, Any]:
        """Scale a StatefulSet to a specific number of replicas."""
        name = params["name"]
        namespace = params["namespace"]
        replicas = params["replicas"]

        body = {"spec": {"replicas": replicas}}
        await self._apps_v1.patch_namespaced_stateful_set_scale(
            name=name,
            namespace=namespace,
            body=body,
        )

        return {
            "statefulset": name,
            "namespace": namespace,
            "replicas": replicas,
            "scaled": True,
        }

    # ==========================================================================
    # DaemonSets
    # ==========================================================================

    async def _list_daemonsets(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """List all DaemonSets in a namespace or across all namespaces."""
        namespace = params.get("namespace")
        label_selector = params.get("label_selector", "")

        if namespace:
            result = await self._apps_v1.list_namespaced_daemon_set(
                namespace=namespace,
                label_selector=label_selector,
            )
        else:
            result = await self._apps_v1.list_daemon_set_for_all_namespaces(
                label_selector=label_selector,
            )

        return [self._serialize_daemonset(ds) for ds in result.items]

    async def _get_daemonset(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get details about a specific DaemonSet."""
        name = params["name"]
        namespace = params["namespace"]

        ds = await self._apps_v1.read_namespaced_daemon_set(name=name, namespace=namespace)
        return self._serialize_daemonset(ds)

    # ==========================================================================
    # Jobs
    # ==========================================================================

    async def _list_jobs(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """List all Jobs in a namespace or across all namespaces."""
        namespace = params.get("namespace")
        label_selector = params.get("label_selector", "")

        if namespace:
            result = await self._batch_v1.list_namespaced_job(
                namespace=namespace,
                label_selector=label_selector,
            )
        else:
            result = await self._batch_v1.list_job_for_all_namespaces(
                label_selector=label_selector,
            )

        return [self._serialize_job(job) for job in result.items]

    async def _get_job(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get details about a specific Job."""
        name = params["name"]
        namespace = params["namespace"]

        job = await self._batch_v1.read_namespaced_job(name=name, namespace=namespace)
        return self._serialize_job(job)

    # ==========================================================================
    # CronJobs
    # ==========================================================================

    async def _list_cronjobs(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """List all CronJobs in a namespace or across all namespaces."""
        namespace = params.get("namespace")
        label_selector = params.get("label_selector", "")

        if namespace:
            result = await self._batch_v1.list_namespaced_cron_job(
                namespace=namespace,
                label_selector=label_selector,
            )
        else:
            result = await self._batch_v1.list_cron_job_for_all_namespaces(
                label_selector=label_selector,
            )

        return [self._serialize_cronjob(cj) for cj in result.items]

    async def _get_cronjob(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get details about a specific CronJob."""
        name = params["name"]
        namespace = params["namespace"]

        cj = await self._batch_v1.read_namespaced_cron_job(name=name, namespace=namespace)
        return self._serialize_cronjob(cj)
