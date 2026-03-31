# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Service Operation Handlers

Mixin class containing service, ingress, endpoints, and network policy
operation handlers for Kubernetes connector.
"""

from typing import Any


class ServiceHandlerMixin:
    """Mixin for service and networking operation handlers."""

    # These will be provided by KubernetesConnector (base class)
    _core_v1: Any
    _networking_v1: Any

    # Serializer methods (will be provided by KubernetesConnector)
    def _serialize_service(self, svc: Any) -> dict[str, Any]:
        return {}

    def _serialize_ingress(self, ing: Any) -> dict[str, Any]:
        return {}

    def _serialize_endpoints(self, ep: Any) -> dict[str, Any]:
        return {}

    def _serialize_network_policy(self, np: Any) -> dict[str, Any]:
        return {}

    # ==========================================================================
    # Services
    # ==========================================================================

    async def _list_services(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """List all services in a namespace or across all namespaces."""
        namespace = params.get("namespace")
        label_selector = params.get("label_selector", "")

        if namespace:
            result = await self._core_v1.list_namespaced_service(
                namespace=namespace,
                label_selector=label_selector,
            )
        else:
            result = await self._core_v1.list_service_for_all_namespaces(
                label_selector=label_selector,
            )

        return [self._serialize_service(svc) for svc in result.items]

    async def _get_service(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get details about a specific service."""
        name = params["name"]
        namespace = params["namespace"]

        svc = await self._core_v1.read_namespaced_service(name=name, namespace=namespace)
        return self._serialize_service(svc)

    async def _describe_service(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get comprehensive service information including endpoints and events."""
        name = params["name"]
        namespace = params["namespace"]

        # Get service details
        svc = await self._core_v1.read_namespaced_service(name=name, namespace=namespace)
        service_data = self._serialize_service(svc)

        # Get endpoints for this service
        try:
            endpoints = await self._core_v1.read_namespaced_endpoints(
                name=name, namespace=namespace
            )
            service_data["endpoints"] = self._serialize_endpoints(endpoints)
        except Exception:
            service_data["endpoints"] = None

        # Get events for this service
        events = await self._core_v1.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={name},involvedObject.kind=Service",
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

        service_data["events"] = event_list
        return service_data

    # ==========================================================================
    # Ingresses
    # ==========================================================================

    async def _list_ingresses(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """List all ingresses in a namespace or across all namespaces."""
        namespace = params.get("namespace")

        if namespace:
            result = await self._networking_v1.list_namespaced_ingress(
                namespace=namespace,
            )
        else:
            result = await self._networking_v1.list_ingress_for_all_namespaces()

        return [self._serialize_ingress(ing) for ing in result.items]

    async def _get_ingress(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get details about a specific ingress."""
        name = params["name"]
        namespace = params["namespace"]

        ing = await self._networking_v1.read_namespaced_ingress(name=name, namespace=namespace)
        return self._serialize_ingress(ing)

    # ==========================================================================
    # Endpoints
    # ==========================================================================

    async def _list_endpoints(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """List all endpoints in a namespace."""
        namespace = params.get("namespace", "default")

        result = await self._core_v1.list_namespaced_endpoints(namespace=namespace)
        return [self._serialize_endpoints(ep) for ep in result.items]

    async def _get_endpoints(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get endpoints for a specific service."""
        name = params["name"]
        namespace = params["namespace"]

        ep = await self._core_v1.read_namespaced_endpoints(name=name, namespace=namespace)
        return self._serialize_endpoints(ep)

    # ==========================================================================
    # Network Policies
    # ==========================================================================

    async def _list_network_policies(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """List all NetworkPolicies in a namespace."""
        namespace = params.get("namespace", "default")

        result = await self._networking_v1.list_namespaced_network_policy(namespace=namespace)
        return [self._serialize_network_policy(np) for np in result.items]

    async def _get_network_policy(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get details about a specific NetworkPolicy."""
        name = params["name"]
        namespace = params["namespace"]

        np = await self._networking_v1.read_namespaced_network_policy(
            name=name, namespace=namespace
        )
        return self._serialize_network_policy(np)
