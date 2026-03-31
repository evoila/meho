# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
ArgoCD Resource Handler Mixin.

Handles resource tree, managed resources, and events inspection.
Emits MANAGED_BY topology edges from top-level K8s resources to the
ArgoCD Server entity when the resource tree is fetched.
"""

from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.argocd.serializers import (
    serialize_events,
    serialize_managed_resources,
    serialize_resource_tree,
)

logger = get_logger(__name__)

# K8s resource kinds that are infrastructure-relevant for topology edges
_TOPOLOGY_RELEVANT_KINDS: set[str] = {
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "Service",
    "ConfigMap",
    "Secret",
    "Ingress",
    "CronJob",
    "Job",
}

# K8s resource kinds eligible for SAME_AS edge resolution to K8s topology entities
_SAME_AS_ELIGIBLE_KINDS: set[str] = {
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "Service",
    "Ingress",
}


class ResourceHandlerMixin:
    """Mixin for ArgoCD resource operations: tree, managed resources, events."""

    # These will be provided by ArgoConnector (base class)
    async def _get(self, path: str, params: dict | None = None) -> dict: ...

    # Provided by ArgoHTTPBase -> BaseConnector
    base_url: str
    connector_id: str

    # =========================================================================
    # HANDLER METHODS
    # =========================================================================

    async def _get_resource_tree_handler(self, params: dict[str, Any]) -> dict:
        """
        Get application resource tree with depth limiting.

        Uses GET /api/v1/applications/{name}/resource-tree.
        Returns top-level resources plus one level of children.
        After serialization, emits MANAGED_BY topology edges lazily.
        """
        app_name = params["application"]
        query_params: dict[str, Any] = {}

        app_namespace = params.get("app_namespace")
        if app_namespace:
            query_params["appNamespace"] = app_namespace

        data = await self._get(
            f"/api/v1/applications/{app_name}/resource-tree",
            params=query_params or None,
        )

        # Emit MANAGED_BY edges from top-level K8s resources to ArgoCD Server
        nodes = data.get("nodes", [])
        if nodes:
            self._emit_managed_by_edges(nodes)
            self._emit_same_as_edges(nodes)

        return serialize_resource_tree(data)

    def _emit_managed_by_edges(self, nodes: list[dict[str, Any]]) -> None:
        """
        Emit MANAGED_BY topology hints from top-level K8s resources to the
        ArgoCD Server entity.

        Top-level resources are those with no parentRefs or whose parentRefs
        reference only the ArgoCD Application itself (kind=Application).

        Edges are stored as topology hints on the connector instance for the
        auto-discovery system to pick up. Uses the same ExtractedEntity /
        ExtractedRelationship pattern as other connectors, queued via the
        discovery queue.

        Args:
            nodes: Raw node list from the ArgoCD resource tree API response.
        """
        from meho_app.modules.topology.auto_discovery.base import (
            ExtractedEntity,
            ExtractedRelationship,
        )
        from meho_app.modules.topology.auto_discovery.queue import (
            DiscoveryMessage,
        )

        # Filter for top-level, infrastructure-relevant resources
        top_level_resources: list[dict[str, Any]] = []
        for node in nodes:
            kind = node.get("kind", "")
            if kind not in _TOPOLOGY_RELEVANT_KINDS:
                continue

            # Check parentRefs -- top-level means no parentRefs or only
            # Application-type parents
            parent_refs = node.get("parentRefs", [])
            is_top_level = True
            if parent_refs:
                for ref in parent_refs:
                    ref_kind = ref.get("kind", "")
                    if ref_kind not in ("Application", "ApplicationSet"):
                        is_top_level = False
                        break

            if is_top_level:
                top_level_resources.append(node)

        if not top_level_resources:
            return

        # Build topology entities and relationships
        argocd_server_name = getattr(self, "base_url", "argocd-server")
        connector_id = getattr(self, "connector_id", "")

        entities: list[ExtractedEntity] = [
            ExtractedEntity(
                name=argocd_server_name,
                description=f"ArgoCD GitOps server at {argocd_server_name}",
                connector_id=connector_id,
                entity_type="ArgoCD Server",
                scope={},
                connector_name=argocd_server_name,
                raw_attributes={
                    "is_connector_entity": True,
                    "connector_type": "argocd",
                    "base_url": argocd_server_name,
                },
            ),
        ]

        relationships: list[ExtractedRelationship] = []
        for resource in top_level_resources:
            kind = resource.get("kind", "")
            name = resource.get("name", "")
            namespace = resource.get("namespace", "")
            resource_id = f"{namespace}/{name}" if namespace else name

            relationships.append(
                ExtractedRelationship(
                    from_entity_name=resource_id,
                    to_entity_name=argocd_server_name,
                    relationship_type="managed_by",
                    from_entity_type=kind,
                    to_entity_type="ArgoCD Server",
                )
            )

        logger.debug(
            f"ArgoCD topology: emitting {len(relationships)} MANAGED_BY edges "
            f"from resource tree to {argocd_server_name}"
        )

        # Queue the discovery message for background processing.
        # Use fire-and-forget via in-memory accumulation -- the discovery
        # queue is async but we're in a sync method. Store on the instance
        # for the connector's execute() to pick up, or enqueue directly
        # if the queue singleton is available.
        try:
            message = DiscoveryMessage(
                entities=entities,
                relationships=relationships,
                tenant_id="",  # Injected by _forward_topology_hints() from OTel context
                connector_type="argocd",
            )

            # Store hints on instance for pool-level pickup
            if not hasattr(self, "_topology_hints"):
                self._topology_hints: list[DiscoveryMessage] = []
            self._topology_hints.append(message)

        except Exception as e:
            # Topology emission is best-effort -- never fail the operation
            logger.warning(f"Failed to emit MANAGED_BY topology edges: {e}")

    def _emit_same_as_edges(self, nodes: list[dict[str, Any]]) -> None:
        """
        Emit SAME_AS topology hints from K8s resources in the ArgoCD resource
        tree to their corresponding K8s topology entities.

        Unlike MANAGED_BY which only targets top-level resources, SAME_AS
        applies to ANY K8s resource in the tree regardless of parent hierarchy.
        This enables the DeterministicResolver to create SAME_AS edges between
        ArgoCD's view and K8s topology's view of the same resource.

        Args:
            nodes: Raw node list from the ArgoCD resource tree API response.
        """
        from meho_app.modules.topology.auto_discovery.base import (
            ExtractedRelationship,
        )
        from meho_app.modules.topology.auto_discovery.queue import (
            DiscoveryMessage,
        )

        relationships: list[ExtractedRelationship] = []
        for node in nodes:
            kind = node.get("kind", "")
            if kind not in _SAME_AS_ELIGIBLE_KINDS:
                continue

            name = node.get("name", "")
            if not name:
                continue

            namespace = node.get("namespace", "")
            resource_id = f"{namespace}/{name}" if namespace else name

            relationships.append(
                ExtractedRelationship(
                    from_entity_name=resource_id,
                    to_entity_name=resource_id,
                    relationship_type="same_as",
                    from_entity_type=kind,
                    to_entity_type=kind,
                )
            )

        if not relationships:
            return

        logger.debug(
            f"ArgoCD topology: emitting {len(relationships)} SAME_AS edges from resource tree"
        )

        try:
            message = DiscoveryMessage(
                entities=[],
                relationships=relationships,
                tenant_id="",  # Injected by _forward_topology_hints() from OTel context
                connector_type="argocd",
            )

            # Store hints on instance for pool-level pickup
            if not hasattr(self, "_topology_hints"):
                self._topology_hints: list[DiscoveryMessage] = []
            self._topology_hints.append(message)

        except Exception as e:
            # Topology emission is best-effort -- never fail the operation
            logger.warning(f"Failed to emit SAME_AS topology edges: {e}")

    async def _get_managed_resources_handler(self, params: dict[str, Any]) -> list[dict]:
        """
        Get managed resources with drift detection info.

        Uses GET /api/v1/applications/{applicationName}/managed-resources.
        Supports optional group and kind filters.
        """
        app_name = params["application"]
        query_params: dict[str, Any] = {}

        app_namespace = params.get("app_namespace")
        if app_namespace:
            query_params["appNamespace"] = app_namespace

        group = params.get("group")
        if group:
            query_params["resourceGroup"] = group

        kind = params.get("kind")
        if kind:
            query_params["resourceKind"] = kind

        data = await self._get(
            f"/api/v1/applications/{app_name}/managed-resources",
            params=query_params or None,
        )
        return serialize_managed_resources(data)

    async def _get_application_events_handler(self, params: dict[str, Any]) -> list[dict]:
        """
        Get K8s events for application's managed resources.

        Uses GET /api/v1/applications/{name}/events.
        Returns event type, reason, message, and involved resource info.
        """
        app_name = params["application"]
        query_params: dict[str, Any] = {}

        app_namespace = params.get("app_namespace")
        if app_namespace:
            query_params["appNamespace"] = app_namespace

        data = await self._get(
            f"/api/v1/applications/{app_name}/events",
            params=query_params or None,
        )
        return serialize_events(data)
