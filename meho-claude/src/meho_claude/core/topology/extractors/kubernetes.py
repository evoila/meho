"""Kubernetes entity extractor for topology auto-discovery.

Extracts pods, deployments, services, nodes, ingresses, and namespaces
from Kubernetes API responses into TopologyEntity/TopologyRelationship models.

Registered as "kubernetes" in the extractor registry via @register_extractor.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from meho_claude.core.topology.extractor import BaseEntityExtractor, register_extractor
from meho_claude.core.topology.models import (
    ExtractionResult,
    TopologyEntity,
    TopologyRelationship,
)

logger = structlog.get_logger()

# Operation IDs that this extractor handles
_EXTRACTABLE_OPERATIONS = {
    "list-pods",
    "list-nodes",
    "list-deployments",
    "list-services",
    "list-ingresses",
    "list-namespaces",
}


def _safe_get(d: dict, *keys: str, default: Any = None) -> Any:
    """Safely traverse nested dict keys, returning default if any key is missing."""
    current = d
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key, default)
        if current is default:
            return default
    return current


def _normalize_provider_id(raw_provider_id: str) -> str:
    """Normalize vsphere provider ID to lowercase.

    vsphere://421A6D12-ABC -> vsphere://421a6d12-abc
    """
    if not raw_provider_id:
        return ""
    if raw_provider_id.startswith("vsphere://"):
        uuid_part = raw_provider_id[len("vsphere://"):]
        return f"vsphere://{uuid_part.lower()}"
    return raw_provider_id.lower()


def _find_address(addresses: list[dict], addr_type: str) -> str:
    """Find an address of a given type from K8s node status.addresses."""
    if not addresses:
        return ""
    for addr in addresses:
        if isinstance(addr, dict) and addr.get("type") == addr_type:
            return addr.get("address", "")
    return ""


@register_extractor("kubernetes")
class K8sEntityExtractor(BaseEntityExtractor):
    """Extract topology entities from Kubernetes API responses.

    Handles all 6 resource types: pods, deployments, services, nodes,
    ingresses, namespaces. Unknown operations return empty results.
    """

    def extract(
        self,
        connector_name: str,
        connector_type: str,
        operation_id: str,
        result_data: dict[str, Any],
    ) -> ExtractionResult:
        """Extract entities and relationships from K8s API response.

        Routes to type-specific extraction methods based on operation_id.
        Unknown operations and missing data return empty ExtractionResult.
        """
        entities: list[TopologyEntity] = []
        relationships: list[TopologyRelationship] = []

        # Get items from standard K8s list response structure
        items = _safe_get(result_data, "data", "items", default=[])
        if not items or not isinstance(items, list):
            return ExtractionResult(
                entities=[],
                relationships=[],
                source_connector=connector_name,
                source_operation=operation_id,
            )

        if operation_id == "list-pods":
            self._extract_pods(connector_name, connector_type, items, entities, relationships)
        elif operation_id == "list-nodes":
            self._extract_nodes(connector_name, connector_type, items, entities, relationships)
        elif operation_id == "list-deployments":
            self._extract_deployments(connector_name, connector_type, items, entities, relationships)
        elif operation_id == "list-services":
            self._extract_services(connector_name, connector_type, items, entities, relationships)
        elif operation_id == "list-ingresses":
            self._extract_ingresses(connector_name, connector_type, items, entities, relationships)
        elif operation_id == "list-namespaces":
            self._extract_namespaces(connector_name, connector_type, items, entities, relationships)
        # Unknown operations: return empty result (no crash)

        return ExtractionResult(
            entities=entities,
            relationships=relationships,
            source_connector=connector_name,
            source_operation=operation_id,
        )

    def _extract_pods(
        self,
        connector_name: str,
        connector_type: str,
        items: list[dict],
        entities: list[TopologyEntity],
        relationships: list[TopologyRelationship],
    ) -> None:
        """Extract pod entities with member_of namespace relationships."""
        # Track synthesized namespaces to avoid duplicates
        seen_namespaces: dict[str, str] = {}  # ns_name -> entity_id

        for item in items:
            try:
                metadata = item.get("metadata", {})
                uid = metadata.get("uid")
                name = metadata.get("name")
                namespace = metadata.get("namespace", "")

                if not uid or not name:
                    continue  # Skip malformed items

                spec = item.get("spec", {})
                status = item.get("status", {})

                pod_entity = TopologyEntity(
                    name=name,
                    connector_name=connector_name,
                    entity_type="kubernetes_pod",
                    connector_type=connector_type,
                    scope={"namespace": namespace} if namespace else {},
                    canonical_id=uid,
                    description=f"Pod {namespace}/{name}" if namespace else f"Pod {name}",
                    raw_attributes={
                        "ip_address": status.get("podIP", ""),
                        "hostname": name,
                        "node_name": spec.get("nodeName", ""),
                        "phase": status.get("phase", ""),
                        "conditions": status.get("conditions", []),
                    },
                )
                entities.append(pod_entity)

                # Synthesize namespace entity and create member_of relationship
                if namespace and namespace not in seen_namespaces:
                    ns_entity_id = str(uuid.uuid4())
                    ns_entity = TopologyEntity(
                        id=ns_entity_id,
                        name=namespace,
                        connector_name=connector_name,
                        entity_type="kubernetes_namespace",
                        connector_type=connector_type,
                        canonical_id=f"ns-{namespace}",
                        description=f"Namespace {namespace} (synthesized from pod extraction)",
                    )
                    entities.append(ns_entity)
                    seen_namespaces[namespace] = ns_entity_id

                if namespace and namespace in seen_namespaces:
                    relationships.append(
                        TopologyRelationship(
                            from_entity_id=pod_entity.id,
                            to_entity_id=seen_namespaces[namespace],
                            relationship_type="member_of",
                        )
                    )

            except Exception:
                logger.warning(
                    "k8s_pod_extraction_skipped",
                    connector_name=connector_name,
                    item_name=_safe_get(item, "metadata", "name", default="unknown"),
                )

    def _extract_nodes(
        self,
        connector_name: str,
        connector_type: str,
        items: list[dict],
        entities: list[TopologyEntity],
        relationships: list[TopologyRelationship],
    ) -> None:
        """Extract node entities with provider_id, ip_address, hostname for correlation."""
        for item in items:
            try:
                metadata = item.get("metadata", {})
                uid = metadata.get("uid")
                name = metadata.get("name")

                if not uid or not name:
                    continue

                spec = item.get("spec", {})
                status = item.get("status", {})
                addresses = status.get("addresses", [])

                # Normalize provider_id for cross-system correlation
                raw_provider_id = spec.get("providerID", "")
                provider_id = _normalize_provider_id(raw_provider_id)

                # Extract IP and hostname from status.addresses
                ip_address = _find_address(addresses, "InternalIP")
                hostname = _find_address(addresses, "Hostname")

                node_entity = TopologyEntity(
                    name=name,
                    connector_name=connector_name,
                    entity_type="kubernetes_node",
                    connector_type=connector_type,
                    canonical_id=uid,
                    description=f"K8s node {name}",
                    raw_attributes={
                        "provider_id": provider_id,
                        "ip_address": ip_address,
                        "hostname": hostname,
                        "conditions": status.get("conditions", []),
                    },
                )
                entities.append(node_entity)

            except Exception:
                logger.warning(
                    "k8s_node_extraction_skipped",
                    connector_name=connector_name,
                    item_name=_safe_get(item, "metadata", "name", default="unknown"),
                )

    def _extract_deployments(
        self,
        connector_name: str,
        connector_type: str,
        items: list[dict],
        entities: list[TopologyEntity],
        relationships: list[TopologyRelationship],
    ) -> None:
        """Extract deployment entities."""
        for item in items:
            try:
                metadata = item.get("metadata", {})
                uid = metadata.get("uid")
                name = metadata.get("name")
                namespace = metadata.get("namespace", "")

                if not uid or not name:
                    continue

                spec = item.get("spec", {})
                status = item.get("status", {})

                deploy_entity = TopologyEntity(
                    name=name,
                    connector_name=connector_name,
                    entity_type="kubernetes_deployment",
                    connector_type=connector_type,
                    scope={"namespace": namespace} if namespace else {},
                    canonical_id=uid,
                    description=f"Deployment {namespace}/{name}" if namespace else f"Deployment {name}",
                    raw_attributes={
                        "replicas": spec.get("replicas", 0),
                        "available_replicas": status.get("availableReplicas", 0),
                        "ready_replicas": status.get("readyReplicas", 0),
                    },
                )
                entities.append(deploy_entity)

            except Exception:
                logger.warning(
                    "k8s_deployment_extraction_skipped",
                    connector_name=connector_name,
                    item_name=_safe_get(item, "metadata", "name", default="unknown"),
                )

    def _extract_services(
        self,
        connector_name: str,
        connector_type: str,
        items: list[dict],
        entities: list[TopologyEntity],
        relationships: list[TopologyRelationship],
    ) -> None:
        """Extract service entities."""
        for item in items:
            try:
                metadata = item.get("metadata", {})
                uid = metadata.get("uid")
                name = metadata.get("name")
                namespace = metadata.get("namespace", "")

                if not uid or not name:
                    continue

                spec = item.get("spec", {})

                svc_entity = TopologyEntity(
                    name=name,
                    connector_name=connector_name,
                    entity_type="kubernetes_service",
                    connector_type=connector_type,
                    scope={"namespace": namespace} if namespace else {},
                    canonical_id=uid,
                    description=f"Service {namespace}/{name}" if namespace else f"Service {name}",
                    raw_attributes={
                        "type": spec.get("type", ""),
                        "cluster_ip": spec.get("clusterIP", ""),
                        "ports": spec.get("ports", []),
                        "selector": spec.get("selector", {}),
                    },
                )
                entities.append(svc_entity)

            except Exception:
                logger.warning(
                    "k8s_service_extraction_skipped",
                    connector_name=connector_name,
                    item_name=_safe_get(item, "metadata", "name", default="unknown"),
                )

    def _extract_ingresses(
        self,
        connector_name: str,
        connector_type: str,
        items: list[dict],
        entities: list[TopologyEntity],
        relationships: list[TopologyRelationship],
    ) -> None:
        """Extract ingress entities with routes_to service relationships."""
        # Collect service references for routes_to relationships
        for item in items:
            try:
                metadata = item.get("metadata", {})
                uid = metadata.get("uid")
                name = metadata.get("name")
                namespace = metadata.get("namespace", "")

                if not uid or not name:
                    continue

                spec = item.get("spec", {})

                ingress_entity = TopologyEntity(
                    name=name,
                    connector_name=connector_name,
                    entity_type="kubernetes_ingress",
                    connector_type=connector_type,
                    scope={"namespace": namespace} if namespace else {},
                    canonical_id=uid,
                    description=f"Ingress {namespace}/{name}" if namespace else f"Ingress {name}",
                    raw_attributes={
                        "rules": spec.get("rules", []),
                    },
                )
                entities.append(ingress_entity)

                # Create routes_to relationships for backend services
                # Synthesize service entity references from ingress rules
                rules = spec.get("rules", [])
                for rule in rules:
                    http = rule.get("http", {}) if isinstance(rule, dict) else {}
                    paths = http.get("paths", []) if isinstance(http, dict) else []
                    for path_entry in paths:
                        if not isinstance(path_entry, dict):
                            continue
                        backend = path_entry.get("backend", {})
                        if not isinstance(backend, dict):
                            continue
                        service = backend.get("service", {})
                        if not isinstance(service, dict):
                            continue
                        svc_name = service.get("name")
                        if svc_name:
                            # Synthesize a service entity reference for the relationship
                            svc_ref_id = str(uuid.uuid4())
                            svc_entity = TopologyEntity(
                                id=svc_ref_id,
                                name=svc_name,
                                connector_name=connector_name,
                                entity_type="kubernetes_service",
                                connector_type=connector_type,
                                scope={"namespace": namespace} if namespace else {},
                                canonical_id=f"svc-ref-{namespace}-{svc_name}",
                                description=f"Service {namespace}/{svc_name} (ref from ingress)",
                            )
                            entities.append(svc_entity)
                            relationships.append(
                                TopologyRelationship(
                                    from_entity_id=ingress_entity.id,
                                    to_entity_id=svc_ref_id,
                                    relationship_type="routes_to",
                                )
                            )

            except Exception:
                logger.warning(
                    "k8s_ingress_extraction_skipped",
                    connector_name=connector_name,
                    item_name=_safe_get(item, "metadata", "name", default="unknown"),
                )

    def _extract_namespaces(
        self,
        connector_name: str,
        connector_type: str,
        items: list[dict],
        entities: list[TopologyEntity],
        relationships: list[TopologyRelationship],
    ) -> None:
        """Extract namespace entities."""
        for item in items:
            try:
                metadata = item.get("metadata", {})
                uid = metadata.get("uid")
                name = metadata.get("name")

                if not uid or not name:
                    continue

                status = item.get("status", {})

                ns_entity = TopologyEntity(
                    name=name,
                    connector_name=connector_name,
                    entity_type="kubernetes_namespace",
                    connector_type=connector_type,
                    canonical_id=uid,
                    description=f"Namespace {name}",
                    raw_attributes={
                        "phase": status.get("phase", ""),
                    },
                )
                entities.append(ns_entity)

            except Exception:
                logger.warning(
                    "k8s_namespace_extraction_skipped",
                    connector_name=connector_name,
                    item_name=_safe_get(item, "metadata", "name", default="unknown"),
                )
