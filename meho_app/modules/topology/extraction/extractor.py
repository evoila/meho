# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Schema-based entity extractor for topology auto-discovery.

Provides a generic extractor that uses declarative extraction schemas
to extract entities and relationships from connector API responses.

This replaces hardcoded extraction logic in legacy extractors
(VMwareExtractor, KubernetesExtractor, etc.) with a single
schema-driven implementation.

Usage:
    from meho_app.modules.topology.extraction import get_schema_extractor

    extractor = get_schema_extractor()
    entities, relationships = extractor.extract(
        connector_type="kubernetes",
        operation_id=None,
        result_data={"kind": "PodList", "items": [...]},
        connector_id="abc123",
        connector_name="Production K8s",
    )

See Also:
    - TASK-157 for architecture details
    - meho_app.modules.topology.extraction.rules for schema dataclasses
"""

import re
from typing import Any

import jmespath

from meho_app.core.otel import get_logger

from ..auto_discovery.base import ExtractedEntity, ExtractedRelationship
from .rules import (
    EntityExtractionRule,
    RelationshipExtraction,
)

logger = get_logger(__name__)


class SchemaBasedExtractor:
    """
    Generic extractor that uses declarative extraction schemas.

    Replaces all connector-specific extractors (VMwareExtractor,
    KubernetesExtractor, etc.) with a single schema-driven implementation.

    Features:
    - JMESPath-based field extraction
    - Automatic stub entity creation for relationship targets
    - Implicit namespace entity creation for K8s resources
    - Type information attached to entities and relationships

    Example:
        extractor = SchemaBasedExtractor()

        # Kubernetes PodList extraction
        entities, relationships = extractor.extract(
            connector_type="kubernetes",
            operation_id=None,
            result_data={"kind": "PodList", "items": [...]},
            connector_id="abc123",
        )

        # VMware VM list extraction
        entities, relationships = extractor.extract(
            connector_type="vmware",
            operation_id="list_virtual_machines",
            result_data=[{"name": "vm-01", ...}],
            connector_id="def456",
        )
    """

    def __init__(self) -> None:
        """Initialize the schema-based extractor."""
        pass

    def extract(
        self,
        connector_type: str,
        operation_id: str | None,
        result_data: Any,
        connector_id: str,
        connector_name: str | None = None,
    ) -> tuple[list[ExtractedEntity], list[ExtractedRelationship]]:
        """
        Extract entities and relationships using the connector's schema.

        Args:
            connector_type: Type of connector (kubernetes, vmware, etc.)
            operation_id: Operation that produced this result
            result_data: Raw API response data
            connector_id: Connector ID
            connector_name: Optional connector display name

        Returns:
            Tuple of (entities, relationships)
        """
        # Import here to avoid circular import
        from .gcp import GCP_EXTRACTION_SCHEMA
        from .kubernetes import KUBERNETES_EXTRACTION_SCHEMA
        from .prometheus import PROMETHEUS_EXTRACTION_SCHEMA
        from .proxmox import PROXMOX_EXTRACTION_SCHEMA
        from .vmware import VMWARE_EXTRACTION_SCHEMA
        from .argocd import ARGOCD_EXTRACTION_SCHEMA
        from .github import GITHUB_EXTRACTION_SCHEMA

        schemas = {
            "kubernetes": KUBERNETES_EXTRACTION_SCHEMA,
            "vmware": VMWARE_EXTRACTION_SCHEMA,
            "gcp": GCP_EXTRACTION_SCHEMA,
            "proxmox": PROXMOX_EXTRACTION_SCHEMA,
            "prometheus": PROMETHEUS_EXTRACTION_SCHEMA,
            "argocd": ARGOCD_EXTRACTION_SCHEMA,
            "github": GITHUB_EXTRACTION_SCHEMA,
        }
        schema = schemas.get(connector_type)

        if not schema:
            logger.warning(
                f"No extraction schema for {connector_type}",
                connector_type=connector_type,
            )
            return [], []

        # Handle error responses
        if isinstance(result_data, dict) and "error" in result_data:
            logger.debug(f"Skipping error response for {connector_type}/{operation_id}")
            return [], []

        # Handle None/empty data
        if result_data is None:
            return [], []

        # Find matching extraction rules
        # For list data, pass {} but rely on operation_id matching
        matching_rules = schema.find_matching_rules(
            operation_id=operation_id,
            result_data=result_data if isinstance(result_data, dict) else {},
        )

        if not matching_rules:
            available_rules = [r.entity_type for r in schema.entity_rules][:5]
            logger.warning(
                f"No extraction rules match {connector_type}/{operation_id}",
                connector_type=connector_type,
                operation_id=operation_id,
                available_rules=available_rules,
                data_type=type(result_data).__name__,
            )
            return [], []

        # Log matched rules
        matched_types = [r.entity_type for r in matching_rules]
        logger.info(
            f"Matched {len(matching_rules)} extraction rules: {matched_types}",
            rule_count=len(matching_rules),
            matched_types=matched_types,
            connector_type=connector_type,
            operation_id=operation_id,
        )

        all_entities: list[ExtractedEntity] = []
        all_relationships: list[ExtractedRelationship] = []
        seen_entities: set[tuple[str, str]] = set()  # Dedupe by (type, name)
        namespaces_seen: set[str] = set()  # Track K8s namespaces

        for rule in matching_rules:
            entities, relationships, namespaces = self._extract_with_rule(
                rule=rule,
                result_data=result_data,
                connector_id=connector_id,
                connector_name=connector_name,
                connector_type=connector_type,
            )

            for entity in entities:
                key = (entity.entity_type, entity.name)
                if key not in seen_entities:
                    all_entities.append(entity)
                    seen_entities.add(key)

            all_relationships.extend(relationships)
            namespaces_seen.update(namespaces)

        # Create namespace entities for Kubernetes
        if connector_type == "kubernetes" and namespaces_seen:
            for ns in namespaces_seen:
                ns_key = ("Namespace", ns)
                if ns_key not in seen_entities:
                    all_entities.append(
                        self._create_namespace_entity(
                            namespace=ns,
                            connector_id=connector_id,
                            connector_name=connector_name,
                        )
                    )
                    seen_entities.add(ns_key)

        logger.info(
            f"SchemaBasedExtractor: Extracted {len(all_entities)} entities, "
            f"{len(all_relationships)} relationships from {connector_type}/{operation_id}"
        )

        return all_entities, all_relationships

    def _extract_with_rule(
        self,
        rule: EntityExtractionRule,
        result_data: Any,
        connector_id: str,
        connector_name: str | None,
        connector_type: str,
    ) -> tuple[list[ExtractedEntity], list[ExtractedRelationship], set[str]]:
        """
        Extract entities using a specific rule.

        Returns:
            Tuple of (entities, relationships, namespaces_seen)
        """
        entities: list[ExtractedEntity] = []
        relationships: list[ExtractedRelationship] = []
        namespaces_seen: set[str] = set()
        stub_entities: dict[tuple[str, str], ExtractedEntity] = {}

        # Get items to process
        items = self._get_items(rule, result_data)

        for item in items:
            if not isinstance(item, dict):
                continue

            # Extract entity
            entity = self._extract_entity(
                rule=rule,
                item=item,
                connector_id=connector_id,
                connector_name=connector_name,
            )

            if entity:
                entities.append(entity)

                # Track namespace for K8s resources
                if "namespace" in entity.scope:
                    namespaces_seen.add(entity.scope["namespace"])

                # Extract relationships
                for rel_rule in rule.relationships:
                    rels, stubs = self._extract_relationships(
                        entity=entity,
                        rel_rule=rel_rule,
                        item=item,
                        connector_id=connector_id,
                        connector_name=connector_name,
                    )
                    relationships.extend(rels)
                    stub_entities.update(stubs)

        # Add stub entities for relationship targets
        if rule.create_targets:
            entities.extend(stub_entities.values())

        return entities, relationships, namespaces_seen

    def _extract_value(self, path: str, item: dict[str, Any]) -> Any:
        """
        Extract value using JMESPath, with fallback for flat serialized data.

        Typed connectors (Kubernetes, VMware) serialize data to flat dictionaries,
        but extraction rules use nested K8s API paths like 'metadata.name'.

        This method tries:
        1. Full JMESPath (e.g., 'metadata.name')
        2. Flat key fallback (e.g., 'name' from 'metadata.name')
        """
        # Try full JMESPath first
        try:
            value = jmespath.search(path, item)
            if value is not None:
                return value
        except jmespath.exceptions.JMESPathError:
            pass

        # Fallback: try flat key (last part of the path)
        # Convert 'metadata.name' -> 'name', 'status.phase' -> 'phase'
        if "." in path:
            flat_key = path.split(".")[-1]
            # Handle snake_case conversion: 'nodeName' -> 'node_name'
            # But first try direct flat key
            if flat_key in item:
                return item[flat_key]
            # Try snake_case version
            snake_key = self._camel_to_snake(flat_key)
            if snake_key in item:
                return item[snake_key]

        return None

    def _camel_to_snake(self, name: str) -> str:
        """Convert camelCase to snake_case."""
        s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
        return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1).lower()

    def _get_items(
        self,
        rule: EntityExtractionRule,
        result_data: Any,
    ) -> list[dict[str, Any]]:
        """
        Get list of items to extract from result.

        Handles:
        - K8s List kinds with items_path (e.g., PodList)
        - K8s single resources (e.g., Pod)
        - Wrapped responses ({"data": [...]})
        - Single items wrapped as list
        - Direct list responses
        """
        # Handle non-dict data
        if isinstance(result_data, list):
            return result_data

        if not isinstance(result_data, dict):
            return []

        # Check if this is a K8s List kind (has items array)
        kind = result_data.get("kind", "")

        # If rule has items_path, try to use it
        if rule.items_path:
            # For K8s List kinds (PodList, DeploymentList, etc.)
            if kind.endswith("List"):
                try:
                    items = jmespath.search(rule.items_path, result_data)
                    if isinstance(items, list):
                        return items
                except jmespath.exceptions.JMESPathError:
                    pass
                return []
            elif kind and kind in [k for k in rule.source_kinds if not k.endswith("List")]:
                # Single K8s resource (Pod, Deployment, etc.) - wrap as single item
                return [result_data]
            elif not kind:
                # Non-K8s data (Prometheus, etc.) -- use items_path via JMESPath
                try:
                    items = jmespath.search(rule.items_path, result_data)
                    if isinstance(items, list):
                        return items
                except jmespath.exceptions.JMESPathError:
                    pass

        # Handle wrapped responses
        if "data" in result_data and isinstance(result_data["data"], list):
            return result_data["data"]

        # Single item - wrap in list
        return [result_data]

    def _extract_entity(
        self,
        rule: EntityExtractionRule,
        item: dict[str, Any],
        connector_id: str,
        connector_name: str | None,
    ) -> ExtractedEntity | None:
        """Extract a single entity from item data."""
        # Get name - try nested path first, then flat path fallback
        name = self._extract_value(rule.name_path, item)

        if not name:
            return None

        name = str(name)

        # Get scope - use _extract_value for flat data compatibility
        scope: dict[str, Any] = {}
        for scope_key, scope_path in rule.scope_paths.items():
            value = self._extract_value(scope_path, item)
            if value:
                scope[scope_key] = value

        # Generate description
        description = rule.description.render(item)

        # Build raw_attributes with extracted attributes
        raw_attributes = dict(item)

        for attr in rule.attributes:
            value = attr.extract(item)
            if value is not None:
                raw_attributes[f"_extracted_{attr.name}"] = value

        # Add metadata
        raw_attributes["_entity_type"] = rule.entity_type
        raw_attributes["_scope"] = scope

        # For Kubernetes, add common fields
        if scope.get("namespace"):
            raw_attributes["_k8s_namespace"] = scope["namespace"]

        # Extract labels for K8s - use _extract_value for flat data compatibility
        labels = self._extract_value("metadata.labels", item)
        if labels:
            raw_attributes["_k8s_labels"] = labels

        # Add kind for K8s
        if "kind" in item:
            raw_attributes["kind"] = item["kind"]
        elif rule.source_kinds:
            # Infer kind from rule
            kind = rule.source_kinds[0]
            if kind.endswith("List"):
                kind = kind[:-4]  # Remove "List" suffix
            raw_attributes["kind"] = kind

        return ExtractedEntity(
            name=name,
            description=description,
            connector_id=connector_id,
            entity_type=rule.entity_type,
            scope=scope,
            connector_name=connector_name,
            raw_attributes=raw_attributes,
        )

    def _extract_relationship_targets(
        self, rel_rule: RelationshipExtraction, item: dict[str, Any]
    ) -> list[str]:
        """
        Extract relationship target names using flat path fallback.

        This replaces rel_rule.extract_targets() to support both nested
        K8s API format and flat typed connector serialized format.
        """
        # Use _extract_value for flat path fallback
        value = self._extract_value(rel_rule.target_path, item)

        if value is None:
            return []

        # Normalize to list
        if rel_rule.multiple and isinstance(value, list):
            # Flatten nested lists and filter None/empty values
            targets = []
            for v in value:
                if isinstance(v, list):
                    targets.extend([str(x) for x in v if x])
                elif v:
                    targets.append(str(v))
            return targets
        elif value:
            return [str(value)]

        return []

    def _extract_relationships(
        self,
        entity: ExtractedEntity,
        rel_rule: RelationshipExtraction,
        item: dict[str, Any],
        connector_id: str,
        connector_name: str | None,
    ) -> tuple[list[ExtractedRelationship], dict[tuple[str, str], ExtractedEntity]]:
        """
        Extract relationships from item data.

        Returns:
            Tuple of (relationships, stub_entities)
        """
        relationships: list[ExtractedRelationship] = []
        stubs: dict[tuple[str, str], ExtractedEntity] = {}

        # Get target name(s) - use _extract_value for flat path fallback
        targets = self._extract_relationship_targets(rel_rule, item)

        if not targets:
            if not rel_rule.optional:
                logger.debug(
                    f"Required relationship {rel_rule.relationship_type} -> "
                    f"{rel_rule.target_type} missing for {entity.name}"
                )
            return [], {}

        for target in targets:
            if not target:
                continue

            target_str = str(target)

            # Create relationship
            relationships.append(
                ExtractedRelationship(
                    from_entity_name=entity.name,
                    to_entity_name=target_str,
                    relationship_type=rel_rule.relationship_type,
                    from_entity_type=entity.entity_type,
                    to_entity_type=rel_rule.target_type,
                )
            )

            # Create stub entity for target (skip for Namespace - handled separately)
            if rel_rule.target_type != "Namespace":
                stub_key = (rel_rule.target_type, target_str)
                if stub_key not in stubs:
                    # Get target scope if specified - use _extract_value for flat path fallback
                    target_scope: dict[str, Any] = {}
                    if rel_rule.target_scope_path:
                        scope_value = self._extract_value(rel_rule.target_scope_path, item)
                        if scope_value:
                            target_scope = {"scope": scope_value}

                    # Inherit namespace from source entity for K8s resources
                    if "namespace" in entity.scope and not target_scope:
                        target_scope["namespace"] = entity.scope["namespace"]

                    stubs[stub_key] = ExtractedEntity(
                        name=target_str,
                        description=f"{rel_rule.target_type} {target_str} (stub)",
                        connector_id=connector_id,
                        entity_type=rel_rule.target_type,
                        scope=target_scope,
                        connector_name=connector_name,
                        raw_attributes={
                            "_stub": True,
                            "_entity_type": rel_rule.target_type,
                        },
                    )

        return relationships, stubs

    def _create_namespace_entity(
        self,
        namespace: str,
        connector_id: str,
        connector_name: str | None,
    ) -> ExtractedEntity:
        """
        Create a namespace entity for K8s relationship targets.

        Called when extracting namespaced resources to ensure the namespace
        entity exists for the member_of relationship.
        """
        return ExtractedEntity(
            name=namespace,
            description=f"K8s Namespace {namespace}",
            connector_id=connector_id,
            entity_type="Namespace",
            scope={},
            connector_name=connector_name,
            raw_attributes={
                "kind": "Namespace",
                "metadata": {"name": namespace},
                "_entity_type": "Namespace",
            },
        )


# =============================================================================
# Singleton / Factory
# =============================================================================

_extractor: SchemaBasedExtractor | None = None


def get_schema_extractor() -> SchemaBasedExtractor:
    """
    Get the singleton schema-based extractor.

    Returns:
        SchemaBasedExtractor instance
    """
    global _extractor
    if _extractor is None:
        _extractor = SchemaBasedExtractor()
    return _extractor


def reset_schema_extractor() -> None:
    """Reset the extractor singleton (for testing)."""
    global _extractor
    _extractor = None
