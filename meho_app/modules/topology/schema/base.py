# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Base dataclasses for topology schema definitions.

Defines the core data structures for schema-driven topology validation:
- EntityTypeDefinition: Defines entity type properties
- RelationshipRule: Defines valid relationships between entity types
- ConnectorTopologySchema: Container for entity types and relationship rules

These schemas enable:
1. Validation of entity types at storage time
2. Validation of relationships (Pod can't belong to Datacenter)
3. Scoped canonical ID generation (namespace/pod-name)
4. LLM navigation hints (how to find related entities)
"""

from dataclasses import dataclass, field
from enum import StrEnum


class Volatility(StrEnum):
    """
    Entity volatility classification for cache/verification strategy.

    Based on memory 12911065: Topology entities should have volatility
    classification to distinguish stable from ephemeral entities.
    """

    STABLE = "stable"  # Trust longer: Clusters, Namespaces, VMs, Datastores
    MODERATE = "moderate"  # Verify periodically: Deployments, Services
    EPHEMERAL = "ephemeral"  # Always re-verify: Pods, Tasks, Containers


@dataclass
class SameAsEligibility:
    """
    Defines which external entity types this entity can correlate with.

    Used to filter SAME_AS suggestions - prevents nonsensical matches
    like Pod ↔ VM (pods are ephemeral, VMs are not).

    Examples:
        # K8s Node can match VMware VM, GCP Instance, or Proxmox Host
        SameAsEligibility(
            can_match=["VM", "Instance", "Host"],
            matching_attributes=["spec.providerID", "status.addresses[*].address"],
        )

        # Explicitly exclude certain types
        SameAsEligibility(
            can_match=["VM", "Instance"],
            never_match=["Container"],  # Never match with containers
        )
    """

    # Entity types this can match (from ANY connector)
    # Examples: ["VM", "Instance", "Host", "Node"]
    can_match: list[str] = field(default_factory=list)

    # Attributes to compare for matching (JMESPath expressions)
    # These hints help the correlation service know what to compare
    matching_attributes: list[str] = field(default_factory=list)

    # Entity types to explicitly exclude (takes precedence over can_match)
    never_match: list[str] = field(default_factory=list)

    def can_correlate_with(self, other_entity_type: str) -> bool:
        """
        Check if this entity type can have SAME_AS with another type.

        Args:
            other_entity_type: The entity type to check correlation with

        Returns:
            True if SAME_AS is allowed between these types

        Examples:
            >>> eligibility = SameAsEligibility(can_match=["VM", "Instance"])
            >>> eligibility.can_correlate_with("VM")
            True
            >>> eligibility.can_correlate_with("Pod")
            False
        """
        if other_entity_type in self.never_match:
            return False
        if not self.can_match:
            return False  # Empty list means no SAME_AS allowed
        return other_entity_type in self.can_match


@dataclass
class EntityTypeDefinition:
    """
    Definition of an entity type within a connector.

    Specifies:
    - Identity rules (how to build canonical_id)
    - Scoping (whether entity belongs to a parent scope)
    - Volatility (how often to re-verify)
    - Navigation hints (for LLM assistance)
    - SAME_AS eligibility (cross-connector correlation rules)

    Examples:
        # K8s Pod - scoped to namespace, ephemeral (no SAME_AS)
        EntityTypeDefinition(
            name="Pod",
            scoped=True,
            scope_type="namespace",
            identity_fields=["namespace", "name"],
            volatility=Volatility.EPHEMERAL,
            same_as=None,  # Pods cannot have cross-connector correlations
        )

        # K8s Node - can correlate with VMs from other connectors
        EntityTypeDefinition(
            name="Node",
            scoped=False,
            identity_fields=["name"],
            volatility=Volatility.STABLE,
            same_as=SameAsEligibility(
                can_match=["VM", "Instance", "Host"],
                matching_attributes=["spec.providerID", "status.addresses[*].address"],
            ),
        )

        # VMware VM - globally unique moref, can correlate with K8s Node
        EntityTypeDefinition(
            name="VM",
            scoped=False,
            identity_fields=["moref"],
            volatility=Volatility.MODERATE,
            same_as=SameAsEligibility(
                can_match=["Node", "Instance"],
                matching_attributes=["guest.hostName", "guest.ipAddress"],
            ),
        )
    """

    name: str  # "Pod", "VM", "Host", "Namespace"

    # Scoping configuration
    scoped: bool = False  # Whether entity is scoped (e.g., to namespace)
    scope_type: str | None = None  # "namespace", "cluster", "datacenter", "zone"

    # Identity configuration
    identity_fields: list[str] = field(default_factory=list)  # Fields that make canonical_id

    # Lifecycle hints
    volatility: Volatility = Volatility.MODERATE

    # SAME_AS eligibility rules (TASK-160)
    # None = this entity type cannot have SAME_AS relationships
    same_as: SameAsEligibility | None = None

    # LLM navigation hints (used in TASK-158)
    navigation_hints: list[str] = field(default_factory=list)
    common_queries: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Set default identity fields if not provided."""
        if not self.identity_fields:
            if self.scoped and self.scope_type:
                self.identity_fields = [self.scope_type, "name"]
            else:
                self.identity_fields = ["name"]


@dataclass
class RelationshipRule:
    """
    Rule defining a valid relationship between entity types.

    Used to validate relationships at storage time:
    - A Pod CAN runs_on a Node (valid)
    - A Pod CANNOT runs_on a Namespace (invalid)

    Examples:
        # Pod runs on Node
        RelationshipRule(
            from_type="Pod",
            relationship_type="runs_on",
            to_type="Node",
        )

        # Deployment manages ReplicaSet
        RelationshipRule(
            from_type="Deployment",
            relationship_type="manages",
            to_type="ReplicaSet",
        )
    """

    from_type: str  # "Pod", "VM", "Service"
    relationship_type: str  # "runs_on", "member_of", "manages", "routes_to"
    to_type: str  # "Node", "Host", "Namespace"

    # Constraint metadata
    required: bool = False  # Whether this relationship must exist
    cardinality: str = "many_to_one"  # "one_to_one", "one_to_many", "many_to_many"

    def __hash__(self) -> int:
        """Hash by the relationship tuple for set operations."""
        return hash((self.from_type, self.relationship_type, self.to_type))

    def __eq__(self, other: object) -> bool:
        """Equality by the relationship tuple."""
        if not isinstance(other, RelationshipRule):
            return False
        return (
            self.from_type == other.from_type
            and self.relationship_type == other.relationship_type
            and self.to_type == other.to_type
        )


@dataclass
class ConnectorTopologySchema:
    """
    Complete topology schema for a connector type.

    Defines what entities exist and what relationships are valid.
    This is the source of truth for topology validation.

    Usage:
        schema = KUBERNETES_TOPOLOGY_SCHEMA

        # Validate entity type
        if not schema.is_valid_entity_type("Pod"):
            raise ValueError("Invalid entity type")

        # Validate relationship
        if not schema.is_valid_relationship("Pod", "runs_on", "Node"):
            raise ValueError("Invalid relationship")

        # Build canonical ID
        canonical_id = schema.build_canonical_id("Pod", {"namespace": "prod"}, "nginx")
        # Returns: "prod/nginx"
    """

    connector_type: str  # "kubernetes", "vmware", "gcp", "proxmox"

    # Entity type definitions
    entity_types: dict[str, EntityTypeDefinition] = field(default_factory=dict)

    # Valid relationships: {(from_type, rel_type, to_type): RelationshipRule}
    relationship_rules: dict[tuple[str, str, str], RelationshipRule] = field(default_factory=dict)

    def is_valid_entity_type(self, entity_type: str) -> bool:
        """
        Check if entity type is valid for this connector.

        Args:
            entity_type: The entity type to check (e.g., "Pod", "VM")

        Returns:
            True if entity type is defined in this schema
        """
        return entity_type in self.entity_types

    def is_valid_relationship(
        self,
        from_type: str,
        relationship_type: str,
        to_type: str,
    ) -> bool:
        """
        Check if relationship is valid for this connector.

        Args:
            from_type: Source entity type
            relationship_type: Type of relationship
            to_type: Target entity type

        Returns:
            True if this relationship is allowed by the schema
        """
        key = (from_type, relationship_type, to_type)
        return key in self.relationship_rules

    def get_entity_definition(self, entity_type: str) -> EntityTypeDefinition | None:
        """
        Get the full definition for an entity type.

        Args:
            entity_type: The entity type to look up

        Returns:
            EntityTypeDefinition if found, None otherwise
        """
        return self.entity_types.get(entity_type)

    def get_relationship_rule(
        self,
        from_type: str,
        relationship_type: str,
        to_type: str,
    ) -> RelationshipRule | None:
        """
        Get the relationship rule for a specific relationship.

        Args:
            from_type: Source entity type
            relationship_type: Type of relationship
            to_type: Target entity type

        Returns:
            RelationshipRule if found, None otherwise
        """
        key = (from_type, relationship_type, to_type)
        return self.relationship_rules.get(key)

    def build_canonical_id(
        self,
        entity_type: str,
        scope: dict[str, str],
        name: str,
    ) -> str:
        """
        Build canonical ID from entity definition.

        The canonical ID is a scoped identifier unique within
        (tenant_id, connector_id, entity_type).

        Examples:
            # K8s Pod in namespace "prod" named "nginx"
            build_canonical_id("Pod", {"namespace": "prod"}, "nginx")
            # Returns: "prod/nginx"

            # K8s Node (not scoped)
            build_canonical_id("Node", {}, "worker-01")
            # Returns: "worker-01"

            # VMware VM with moref
            build_canonical_id("VM", {"moref": "vm-123"}, "web-server")
            # Returns: "vm-123"  (moref takes precedence as identity)

        Args:
            entity_type: The entity type
            scope: Scope context (e.g., {"namespace": "prod"})
            name: Entity name

        Returns:
            Canonical ID string
        """
        defn = self.entity_types.get(entity_type)
        if not defn:
            return name

        parts = []
        for field_name in defn.identity_fields:
            if field_name == "name":
                parts.append(name)
            elif field_name in scope:
                parts.append(str(scope[field_name]))

        return "/".join(parts) if parts else name

    def get_valid_relationships_for_type(
        self,
        entity_type: str,
        direction: str = "from",
    ) -> list[RelationshipRule]:
        """
        Get all valid relationships for an entity type.

        Args:
            entity_type: The entity type to query
            direction: "from" (entity is source) or "to" (entity is target)

        Returns:
            List of RelationshipRule objects
        """
        results = []
        for (from_type, _rel_type, to_type), rule in self.relationship_rules.items():
            if (direction == "from" and from_type == entity_type) or (
                direction == "to" and to_type == entity_type
            ):
                results.append(rule)
        return results

    def get_all_entity_types(self) -> set[str]:
        """Get all entity type names in this schema."""
        return set(self.entity_types.keys())

    def get_all_relationship_types(self) -> set[str]:
        """Get all relationship type names used in this schema."""
        return {rule.relationship_type for rule in self.relationship_rules.values()}
