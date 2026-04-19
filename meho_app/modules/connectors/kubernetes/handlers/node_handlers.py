# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Node and Storage Operation Handlers

Mixin class containing node, PVC, PV, storage class, and event
operation handlers for Kubernetes connector.
"""

from typing import Any


class NodeHandlerMixin:
    """Mixin for node, storage, and event operation handlers."""

    # These will be provided by KubernetesConnector (base class)
    _core_v1: Any
    _storage_v1: Any

    # Serializer methods (will be provided by KubernetesConnector)
    def _serialize_node(self, _node: Any) -> dict[str, Any]:
        return {}

    def _serialize_pvc(self, _pvc: Any) -> dict[str, Any]:
        return {}

    def _serialize_pv(self, _pv: Any) -> dict[str, Any]:
        return {}

    def _serialize_storageclass(self, _sc: Any) -> dict[str, Any]:
        return {}

    def _serialize_event(self, _event: Any) -> dict[str, Any]:
        return {}

    # ==========================================================================
    # Nodes
    # ==========================================================================

    async def _list_nodes(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """List all nodes in the cluster."""
        label_selector = params.get("label_selector", "")

        result = await self._core_v1.list_node(label_selector=label_selector)
        return [self._serialize_node(node) for node in result.items]

    async def _get_node(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get details about a specific node."""
        name = params["name"]

        node = await self._core_v1.read_node(name=name)
        return self._serialize_node(node)

    async def _describe_node(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get comprehensive node information including events and pods."""
        name = params["name"]

        # Get node details
        node = await self._core_v1.read_node(name=name)
        node_data = self._serialize_node(node)

        # Get pods running on this node
        pods = await self._core_v1.list_pod_for_all_namespaces(
            field_selector=f"spec.nodeName={name}"
        )
        node_data["running_pods"] = len(pods.items)
        node_data["pod_list"] = [
            {"name": p.metadata.name, "namespace": p.metadata.namespace}
            for p in pods.items[:20]  # Limit to first 20
        ]

        # Get events for this node
        events = await self._core_v1.list_event_for_all_namespaces(
            field_selector=f"involvedObject.name={name},involvedObject.kind=Node"
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

        node_data["events"] = event_list
        return node_data

    async def _cordon_node(self, params: dict[str, Any]) -> dict[str, Any]:
        """Mark a node as unschedulable."""
        name = params["name"]

        body = {"spec": {"unschedulable": True}}
        await self._core_v1.patch_node(name=name, body=body)

        return {
            "node": name,
            "cordoned": True,
            "unschedulable": True,
        }

    async def _uncordon_node(self, params: dict[str, Any]) -> dict[str, Any]:
        """Mark a node as schedulable."""
        name = params["name"]

        body = {"spec": {"unschedulable": False}}
        await self._core_v1.patch_node(name=name, body=body)

        return {
            "node": name,
            "cordoned": False,
            "unschedulable": False,
        }

    # ==========================================================================
    # PVCs
    # ==========================================================================

    async def _list_pvcs(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """List all PersistentVolumeClaims in a namespace or across all namespaces."""
        namespace = params.get("namespace")
        label_selector = params.get("label_selector", "")

        if namespace:
            result = await self._core_v1.list_namespaced_persistent_volume_claim(
                namespace=namespace,
                label_selector=label_selector,
            )
        else:
            result = await self._core_v1.list_persistent_volume_claim_for_all_namespaces(
                label_selector=label_selector,
            )

        return [self._serialize_pvc(pvc) for pvc in result.items]

    async def _get_pvc(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get details about a specific PVC."""
        name = params["name"]
        namespace = params["namespace"]

        pvc = await self._core_v1.read_namespaced_persistent_volume_claim(
            name=name, namespace=namespace
        )
        return self._serialize_pvc(pvc)

    # ==========================================================================
    # PVs
    # ==========================================================================

    async def _list_pvs(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """List all PersistentVolumes in the cluster."""
        label_selector = params.get("label_selector", "")

        result = await self._core_v1.list_persistent_volume(label_selector=label_selector)
        return [self._serialize_pv(pv) for pv in result.items]

    async def _get_pv(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get details about a specific PV."""
        name = params["name"]

        pv = await self._core_v1.read_persistent_volume(name=name)
        return self._serialize_pv(pv)

    # ==========================================================================
    # StorageClasses
    # ==========================================================================

    async def _list_storageclasses(self, _params: dict[str, Any]) -> list[dict[str, Any]]:
        """List all StorageClasses in the cluster."""
        result = await self._storage_v1.list_storage_class()
        return [self._serialize_storageclass(sc) for sc in result.items]

    async def _get_storageclass(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get details about a specific StorageClass."""
        name = params["name"]

        sc = await self._storage_v1.read_storage_class(name=name)
        return self._serialize_storageclass(sc)

    # ==========================================================================
    # Events
    # ==========================================================================

    async def _list_events(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """List events in a namespace or across all namespaces."""
        namespace = params.get("namespace")
        field_selector = params.get("field_selector", "")
        limit = params.get("limit", 100)

        if namespace:
            result = await self._core_v1.list_namespaced_event(
                namespace=namespace,
                field_selector=field_selector,
                limit=limit,
            )
        else:
            result = await self._core_v1.list_event_for_all_namespaces(
                field_selector=field_selector,
                limit=limit,
            )

        return [self._serialize_event(event) for event in result.items]

    async def _get_events_for_resource(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Get all events related to a specific resource."""
        name = params["name"]
        namespace = params.get("namespace")
        kind = params.get("kind", "Pod")

        field_selector = f"involvedObject.name={name},involvedObject.kind={kind}"

        if namespace:
            result = await self._core_v1.list_namespaced_event(
                namespace=namespace,
                field_selector=field_selector,
            )
        else:
            result = await self._core_v1.list_event_for_all_namespaces(
                field_selector=field_selector,
            )

        return [self._serialize_event(event) for event in result.items]
