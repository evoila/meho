# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Namespace and ConfigMap/Secret Operation Handlers

Mixin class containing namespace, configmap, and secret
operation handlers for Kubernetes connector.
"""

from typing import Any


class NamespaceHandlerMixin:
    """Mixin for namespace, configmap, and secret operation handlers."""

    # These will be provided by KubernetesConnector (base class)
    _core_v1: Any

    # Serializer methods (will be provided by KubernetesConnector)
    def _serialize_namespace(self, ns: Any) -> dict[str, Any]:
        return {}

    def _serialize_configmap(self, cm: Any) -> dict[str, Any]:
        return {}

    def _serialize_secret(self, secret: Any, decode: bool = False) -> dict[str, Any]:
        return {}

    # ==========================================================================
    # Namespaces
    # ==========================================================================

    async def _list_namespaces(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """List all namespaces in the cluster."""
        result = await self._core_v1.list_namespace()
        return [self._serialize_namespace(ns) for ns in result.items]

    async def _get_namespace(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get details about a specific namespace."""
        name = params["name"]

        ns = await self._core_v1.read_namespace(name=name)
        return self._serialize_namespace(ns)

    # ==========================================================================
    # ConfigMaps
    # ==========================================================================

    async def _list_configmaps(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """List all ConfigMaps in a namespace."""
        namespace = params.get("namespace", "default")

        result = await self._core_v1.list_namespaced_config_map(namespace=namespace)
        return [self._serialize_configmap(cm) for cm in result.items]

    async def _get_configmap(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get a specific ConfigMap including its data."""
        name = params["name"]
        namespace = params["namespace"]

        cm = await self._core_v1.read_namespaced_config_map(name=name, namespace=namespace)
        return self._serialize_configmap(cm)

    # ==========================================================================
    # Secrets
    # ==========================================================================

    async def _list_secrets(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """List all Secrets in a namespace (data values are NOT returned)."""
        namespace = params.get("namespace", "default")

        result = await self._core_v1.list_namespaced_secret(namespace=namespace)
        # Don't include data in list for security
        secrets = []
        for secret in result.items:
            secrets.append(
                {
                    "name": secret.metadata.name,
                    "namespace": secret.metadata.namespace,
                    "type": secret.type,
                    "keys": list(secret.data.keys()) if secret.data else [],
                    "created": (
                        secret.metadata.creation_timestamp.isoformat()
                        if secret.metadata.creation_timestamp
                        else None
                    ),
                }
            )
        return secrets

    async def _get_secret(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get a specific Secret metadata (optionally decode values)."""
        name = params["name"]
        namespace = params["namespace"]
        decode = params.get("decode", False)

        secret = await self._core_v1.read_namespaced_secret(name=name, namespace=namespace)
        return self._serialize_secret(secret, decode=decode)
