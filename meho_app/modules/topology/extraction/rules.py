# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Declarative extraction rules for topology auto-discovery.

This module defines dataclasses for schema-driven entity extraction from
connector API responses. Rules use JMESPath expressions to specify how
to extract entities and relationships.

The extraction framework replaces hardcoded Python extraction logic with
declarative rules that can be introspected and extended without code changes.

Classes:
    DescriptionTemplate: Template for generating entity descriptions
    AttributeExtraction: Rule for extracting a single attribute
    RelationshipExtraction: Rule for extracting relationships
    EntityExtractionRule: Complete extraction rule for an entity type
    ConnectorExtractionSchema: Container for all extraction rules of a connector

Example:
    # Define a Pod extraction rule
    pod_rule = EntityExtractionRule(
        entity_type="Pod",
        source_kinds=["Pod", "PodList"],
        items_path="items",
        name_path="metadata.name",
        scope_paths={"namespace": "metadata.namespace"},
        relationships=[
            RelationshipExtraction(
                relationship_type="runs_on",
                target_type="Node",
                target_path="spec.nodeName",
            ),
        ],
    )
"""

import re
from dataclasses import dataclass, field
from typing import Any

import jmespath

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


@dataclass
class DescriptionTemplate:
    """
    Template for generating entity descriptions.

    Uses Python format strings with JMESPath placeholders.
    Placeholders are resolved against the source data.

    Attributes:
        template: Format string with {path} placeholders where path is JMESPath
        fallback: Default description if template rendering fails

    Example:
        template = DescriptionTemplate(
            template="K8s Pod {metadata.name} in {metadata.namespace}, {status.phase}",
            fallback="K8s Pod",
        )
        data = {"metadata": {"name": "nginx", "namespace": "prod"}, "status": {"phase": "Running"}}
        result = template.render(data)
        # Returns: "K8s Pod nginx in prod, Running"
    """

    template: str
    fallback: str = "Unknown entity"

    def render(self, data: dict[str, Any]) -> str:
        """
        Render description from data using JMESPath.

        Finds all {path} placeholders in the template, evaluates each path
        against the data, and substitutes the results.

        Args:
            data: Source data to extract values from

        Returns:
            Rendered description string, or fallback on error
        """
        try:
            result = self.template

            # Find all {path} placeholders
            placeholders = re.findall(r"\{([^}]+)\}", self.template)

            if not placeholders:
                return self.template

            # Replace each placeholder with its value
            for path in placeholders:
                try:
                    value = jmespath.search(path, data)
                    replacement = str(value) if value is not None else "N/A"
                except jmespath.exceptions.JMESPathError:
                    replacement = "N/A"

                # Replace the placeholder in the result string
                result = result.replace("{" + path + "}", replacement, 1)

            return result
        except Exception as e:
            logger.debug(f"Description template render failed: {e}")
            return self.fallback


@dataclass
class AttributeExtraction:
    """
    Rule for extracting an attribute from source data.

    Defines how to extract a single attribute using JMESPath,
    with optional default value and transformation.

    Attributes:
        name: Attribute name in output (stored as _extracted_{name})
        path: JMESPath expression to extract value
        default: Default value if path returns None
        transform: Optional transformation to apply: "lowercase", "uppercase", "first"

    Example:
        # Extract pod phase with default
        AttributeExtraction(
            name="phase",
            path="status.phase",
            default="Unknown",
        )

        # Extract first container image
        AttributeExtraction(
            name="image",
            path="spec.containers[*].image",
            transform="first",
        )
    """

    name: str
    path: str
    default: Any = None
    transform: str | None = None

    def extract(self, data: dict[str, Any]) -> Any:
        """
        Extract attribute value from data.

        Args:
            data: Source data to extract from

        Returns:
            Extracted and transformed value, or default
        """
        try:
            value = jmespath.search(self.path, data)
        except jmespath.exceptions.JMESPathError:
            value = None

        if value is None:
            return self.default

        # Apply transformation
        if self.transform and value is not None:
            if self.transform == "lowercase" and isinstance(value, str):
                value = value.lower()
            elif self.transform == "uppercase" and isinstance(value, str):
                value = value.upper()
            elif self.transform == "first" and isinstance(value, list):
                value = value[0] if value else None

        return value


@dataclass
class RelationshipExtraction:
    """
    Rule for extracting a relationship from source data.

    Defines how to extract relationship targets using JMESPath.
    Supports both single targets and multiple targets (arrays).

    Attributes:
        relationship_type: Type of relationship ("runs_on", "member_of", etc.)
        target_type: Target entity type ("Node", "Namespace", etc.)
        target_path: JMESPath to extract target entity name(s)
        target_scope_path: Optional JMESPath for target's scope
        optional: Whether relationship is optional (don't warn if missing)
        multiple: Whether path returns array of targets

    Example:
        # Pod runs on Node (single, optional)
        RelationshipExtraction(
            relationship_type="runs_on",
            target_type="Node",
            target_path="spec.nodeName",
            optional=True,
        )

        # Ingress routes to multiple Services
        RelationshipExtraction(
            relationship_type="routes_to",
            target_type="Service",
            target_path="spec.rules[*].http.paths[*].backend.service.name",
            multiple=True,
        )
    """

    relationship_type: str
    target_type: str
    target_path: str
    target_scope_path: str | None = None
    optional: bool = True
    multiple: bool = False

    def extract_targets(self, data: dict[str, Any]) -> list[str]:
        """
        Extract target entity names from data.

        Args:
            data: Source data to extract from

        Returns:
            List of target entity names (may be empty)
        """
        try:
            value = jmespath.search(self.target_path, data)
        except jmespath.exceptions.JMESPathError:
            return []

        if value is None:
            return []

        # Normalize to list
        if self.multiple and isinstance(value, list):
            # Flatten nested lists and filter None/empty values
            targets = []
            for item in value:
                if isinstance(item, list):
                    targets.extend([str(x) for x in item if x])
                elif item:
                    targets.append(str(item))
            return targets
        elif value:
            return [str(value)]

        return []


@dataclass
class EntityExtractionRule:
    """
    Complete extraction rule for an entity type.

    Defines how to extract entities of this type from API responses,
    including identity, scope, attributes, and relationships.

    Matching:
        Rules are matched based on:
        - source_operations: Operation IDs (e.g., "list_virtual_machines")
        - source_kinds: K8s-style kind field (e.g., "Pod", "PodList")
        - detection_path/detection_value: Generic response detection

    Attributes:
        entity_type: Type of entity ("Pod", "VM", "Host")
        source_operations: List of operation IDs that produce this entity
        source_kinds: List of K8s kinds that produce this entity
        detection_path: JMESPath that must exist/match for generic detection
        detection_value: Expected value at detection_path
        items_path: JMESPath to items array (None = wrap single item)
        name_path: JMESPath to entity name
        scope_paths: Dict of scope field to JMESPath
        description: Template for generating descriptions
        attributes: List of attribute extraction rules
        relationships: List of relationship extraction rules
        create_targets: Whether to create stub entities for relationship targets

    Example:
        EntityExtractionRule(
            entity_type="Pod",
            source_kinds=["Pod", "PodList"],
            items_path="items",
            name_path="metadata.name",
            scope_paths={"namespace": "metadata.namespace"},
            description=DescriptionTemplate(
                template="K8s Pod {metadata.name}, namespace {metadata.namespace}",
            ),
            relationships=[
                RelationshipExtraction(
                    relationship_type="member_of",
                    target_type="Namespace",
                    target_path="metadata.namespace",
                ),
            ],
        )
    """

    entity_type: str

    # Matching: When does this rule apply?
    source_operations: list[str] = field(default_factory=list)
    source_kinds: list[str] = field(default_factory=list)

    # Detection: How to detect if response matches (for generic APIs)
    detection_path: str | None = None
    detection_value: str | None = None

    # Items: How to get the list of items to extract
    items_path: str | None = None

    # Extraction: How to extract entity fields
    name_path: str = "name"
    scope_paths: dict[str, str] = field(default_factory=dict)
    description: DescriptionTemplate = field(
        default_factory=lambda: DescriptionTemplate(template="Entity {name}")
    )
    attributes: list[AttributeExtraction] = field(default_factory=list)

    # Relationships
    relationships: list[RelationshipExtraction] = field(default_factory=list)

    # Create target entities (for relationship targets that don't exist yet)
    create_targets: bool = True

    def matches_operation(self, operation_id: str | None) -> bool:
        """Check if rule matches the given operation ID."""
        if not operation_id:
            return False
        return operation_id in self.source_operations

    def matches_kind(self, kind: str | None) -> bool:
        """Check if rule matches the given K8s kind."""
        if not kind:
            return False
        return kind in self.source_kinds

    def matches_detection(self, data: dict[str, Any]) -> bool:
        """Check if rule matches based on detection path."""
        if not self.detection_path:
            return False

        try:
            value = jmespath.search(self.detection_path, data)
        except jmespath.exceptions.JMESPathError:
            return False

        if self.detection_value:
            return bool(value == self.detection_value)
        return value is not None


@dataclass
class ConnectorExtractionSchema:
    """
    Complete extraction schema for a connector type.

    Maps operations/kinds to entity extraction rules.
    Provides rule matching and introspection capabilities.

    Attributes:
        connector_type: Type of connector ("kubernetes", "vmware", etc.)
        entity_rules: List of EntityExtractionRule for this connector

    Example:
        schema = ConnectorExtractionSchema(
            connector_type="kubernetes",
            entity_rules=[pod_rule, node_rule, deployment_rule, ...],
        )

        # Find matching rules for a PodList response
        rules = schema.find_matching_rules(
            operation_id=None,
            result_data={"kind": "PodList", "items": [...]},
        )
    """

    connector_type: str
    entity_rules: list[EntityExtractionRule] = field(default_factory=list)

    def find_matching_rules(
        self,
        operation_id: str | None,
        result_data: dict[str, Any],
    ) -> list[EntityExtractionRule]:
        """
        Find all rules that match the operation/response.

        Matching priority:
        1. source_operations match (if operation_id provided)
        2. source_kinds match (if result_data has 'kind')
        3. detection_path match (generic fallback)

        Args:
            operation_id: Operation that produced this result
            result_data: Raw API response data

        Returns:
            List of matching EntityExtractionRule objects
        """
        matching: list[EntityExtractionRule] = []

        # Get kind from response if present
        kind = result_data.get("kind") if isinstance(result_data, dict) else None

        for rule in self.entity_rules:
            # Check operation_id match
            if operation_id and rule.matches_operation(operation_id):
                matching.append(rule)
                continue

            # Check kind match (for K8s-style responses)
            if kind and rule.matches_kind(kind):
                matching.append(rule)
                continue

            # Check detection path match
            if isinstance(result_data, dict) and rule.matches_detection(result_data):
                matching.append(rule)

        return matching

    def get_rule_for_entity_type(self, entity_type: str) -> EntityExtractionRule | None:
        """
        Get the extraction rule for a specific entity type.

        Args:
            entity_type: Entity type name (e.g., "Pod", "VM")

        Returns:
            EntityExtractionRule if found, None otherwise
        """
        for rule in self.entity_rules:
            if rule.entity_type == entity_type:
                return rule
        return None

    def get_all_entity_types(self) -> list[str]:
        """Get all entity types defined in this schema."""
        return [rule.entity_type for rule in self.entity_rules]

    def get_all_operations(self) -> list[str]:
        """Get all operation IDs supported by this schema."""
        operations = set()
        for rule in self.entity_rules:
            operations.update(rule.source_operations)
        return list(operations)

    def get_all_kinds(self) -> list[str]:
        """Get all K8s kinds supported by this schema."""
        kinds = set()
        for rule in self.entity_rules:
            kinds.update(rule.source_kinds)
        return list(kinds)
