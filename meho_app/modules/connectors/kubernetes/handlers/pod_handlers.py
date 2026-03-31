# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Pod Operation Handlers

Mixin class containing pod operation handlers for Kubernetes connector.
"""

from typing import Any


class PodHandlerMixin:
    """Mixin for pod operation handlers."""

    # These will be provided by KubernetesConnector (base class)
    _core_v1: Any

    # Serializer methods (will be provided by KubernetesConnector)
    def _serialize_pod(self, pod: Any) -> dict[str, Any]:
        return {}

    async def _list_pods(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """List all pods in a namespace or across all namespaces."""
        namespace = params.get("namespace")
        label_selector = params.get("label_selector", "")
        field_selector = params.get("field_selector", "")

        if namespace:
            result = await self._core_v1.list_namespaced_pod(
                namespace=namespace,
                label_selector=label_selector,
                field_selector=field_selector,
            )
        else:
            result = await self._core_v1.list_pod_for_all_namespaces(
                label_selector=label_selector,
                field_selector=field_selector,
            )

        return [self._serialize_pod(pod) for pod in result.items]

    async def _get_pod(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get detailed information about a specific pod."""
        name = params["name"]
        namespace = params["namespace"]

        pod = await self._core_v1.read_namespaced_pod(name=name, namespace=namespace)
        return self._serialize_pod(pod)

    async def _get_pod_logs(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get logs from a pod container."""
        name = params["name"]
        namespace = params["namespace"]
        container = params.get("container")
        tail_lines = params.get("tail_lines", 100)
        since_seconds = params.get("since_seconds")
        previous = params.get("previous", False)

        kwargs: dict[str, Any] = {
            "name": name,
            "namespace": namespace,
            "tail_lines": tail_lines,
            "previous": previous,
        }
        if container:
            kwargs["container"] = container
        if since_seconds:
            kwargs["since_seconds"] = since_seconds

        logs = await self._core_v1.read_namespaced_pod_log(**kwargs)

        return {
            "pod": name,
            "namespace": namespace,
            "container": container,
            "logs": logs,
            "tail_lines": tail_lines,
        }

    async def _describe_pod(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get comprehensive pod information including events."""
        name = params["name"]
        namespace = params["namespace"]

        # Get pod details
        pod = await self._core_v1.read_namespaced_pod(name=name, namespace=namespace)
        pod_data = self._serialize_pod(pod)

        # Get events for this pod
        events = await self._core_v1.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={name},involvedObject.kind=Pod",
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

        pod_data["events"] = event_list
        return pod_data

    async def _delete_pod(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delete a pod."""
        name = params["name"]
        namespace = params["namespace"]
        grace_period = params.get("grace_period_seconds", 30)

        from kubernetes_asyncio.client import V1DeleteOptions

        await self._core_v1.delete_namespaced_pod(
            name=name,
            namespace=namespace,
            body=V1DeleteOptions(grace_period_seconds=grace_period),
        )

        return {
            "deleted": True,
            "pod": name,
            "namespace": namespace,
        }
