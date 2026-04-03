# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Pydantic schemas for topology service.

Defines the data structures for entities, relationships, and search results.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

FIRST_ENTITY_NAME = "First entity name"
SECOND_ENTITY_NAME = "Second entity name"

# =============================================================================
# Enums
# =============================================================================


class RelationshipType(StrEnum):
    """
    Supported relationship types between entities.

    Direction is FROM → TO:
    - runs_on: Workload → Host (workload executes on host)
    - member_of: Part → Whole (part belongs to whole)
    - routes_to: Entry → Backend (traffic flows to backend)
    - resolves_to: Name → Target (name resolves to target)
    - uses: Consumer → Resource (consumer uses resource)
    - depends_on: Dependent → Dependency (service requires another)
    - connects_to: Source → Target (network/logical connection)
    - related_to: Connector → Connector (connectors are related, e.g., GKE runs on GCP)
    - manages: Parent → Child (K8s ownership: Deployment → ReplicaSet → Pod)
    """

    RUNS_ON = "runs_on"
    MEMBER_OF = "member_of"
    ROUTES_TO = "routes_to"
    RESOLVES_TO = "resolves_to"
    USES = "uses"
    DEPENDS_ON = "depends_on"
    CONNECTS_TO = "connects_to"
    RELATED_TO = "related_to"
    MANAGES = "manages"


# Valid relationship types as a set for validation
VALID_RELATIONSHIP_TYPES = {rt.value for rt in RelationshipType}


class ConnectorRelationshipType(StrEnum):
    """
    Fixed vocabulary for connector-to-connector relationships (D-12).

    These describe how connector INSTANCES relate to each other,
    not entity-to-entity relationships within a connector's domain.

    Direction is FROM --> TO:
    - monitors: Prometheus monitors K8s
    - logs_for: Loki provides logs for K8s
    - traces_for: Tempo provides traces for K8s
    - deploys_to: ArgoCD deploys to K8s
    - manages: VMware manages VMs that K8s runs on
    - alerts_for: Alertmanager alerts for Prometheus/K8s
    - tracks_issues_for: Jira tracks issues for services
    """

    MONITORS = "monitors"
    LOGS_FOR = "logs_for"
    TRACES_FOR = "traces_for"
    DEPLOYS_TO = "deploys_to"
    MANAGES = "manages"
    ALERTS_FOR = "alerts_for"
    TRACKS_ISSUES_FOR = "tracks_issues_for"


CONNECTOR_RELATIONSHIP_TYPES = {rt.value for rt in ConnectorRelationshipType}


class ConnectorRelationshipCreate(BaseModel):
    """Create a relationship between connector instances."""

    from_connector_id: UUID
    to_connector_id: UUID
    relationship_type: str = Field(..., description="Must be one of CONNECTOR_RELATIONSHIP_TYPES")

    @field_validator("relationship_type")
    @classmethod
    def validate_relationship_type(cls, v: str) -> str:
        if v not in CONNECTOR_RELATIONSHIP_TYPES:
            raise ValueError(
                f"Invalid connector relationship type '{v}'. "
                f"Valid types: {sorted(CONNECTOR_RELATIONSHIP_TYPES)}"
            )
        return v


class ConnectorRelationshipResponse(BaseModel):
    """Response for a connector-to-connector relationship."""

    id: UUID
    from_connector_id: UUID
    from_connector_name: str
    to_connector_id: UUID
    to_connector_name: str
    relationship_type: str
    discovered_at: datetime
    last_verified_at: datetime | None = None

    model_config = {"from_attributes": True}


# =============================================================================
# Entity Schemas
# =============================================================================


class TopologyEntityBase(BaseModel):
    """Base schema for topology entities."""

    name: str = Field(..., description="Entity name (e.g., 'shop-ingress', 'node-01')")
    entity_type: str = Field(..., description="Entity type (Pod, VM, Namespace, Host, etc.)")
    connector_type: str | None = Field(None, description="Connector type (kubernetes, vmware, gcp)")
    connector_id: UUID | None = Field(
        None, description="Connector ID, NULL for external entities like URLs"
    )
    connector_name: str | None = Field(None, description="Connector name for display (cached)")
    scope: dict[str, Any] | None = Field(
        default_factory=dict, description="Scoping context (e.g., {'namespace': 'prod'})"
    )
    canonical_id: str | None = Field(
        None, description="Unique ID within connector+type (e.g., 'prod/nginx')"
    )
    description: str = Field(..., description="Rich description for embedding generation")
    raw_attributes: dict[str, Any] | None = Field(
        default_factory=dict, description="Raw attributes from source system"
    )


class TopologyEntityCreate(TopologyEntityBase):
    """Schema for creating a new entity."""

    pass


class TopologyEntity(TopologyEntityBase):
    """Full entity schema with all fields."""

    id: UUID
    entity_type: str  # Override to make required in response
    connector_type: str  # Override to make required in response
    canonical_id: str  # Override to make required in response
    discovered_at: datetime
    last_verified_at: datetime | None = None
    stale_at: datetime | None = None
    tenant_id: str

    model_config = {"from_attributes": True}


# =============================================================================
# Relationship Schemas
# =============================================================================


class TopologyRelationshipBase(BaseModel):
    """Base schema for relationships."""

    from_entity_name: str = Field(..., description="Source entity name")
    to_entity_name: str = Field(..., description="Target entity name")
    relationship_type: str = Field(..., description="Type of relationship")


class TopologyRelationshipCreate(TopologyRelationshipBase):
    """Schema for creating a new relationship."""

    pass


class TopologyRelationship(BaseModel):
    """Full relationship schema with resolved entities."""

    id: UUID
    from_entity_id: UUID
    to_entity_id: UUID
    relationship_type: str
    discovered_at: datetime
    last_verified_at: datetime | None = None

    model_config = {"from_attributes": True}


# =============================================================================
# SAME_AS Schemas
# =============================================================================


class TopologySameAsCreate(BaseModel):
    """Schema for creating a SAME_AS relationship."""

    entity_a_name: str = Field(..., description=FIRST_ENTITY_NAME)
    entity_b_name: str = Field(..., description=SECOND_ENTITY_NAME)
    similarity_score: float = Field(..., ge=0.0, le=1.0, description="Embedding similarity score")
    verified_via: list[str] = Field(..., description="How the relationship was verified")
    # Identity disambiguation — pass when available for precise entity resolution
    entity_a_connector_id: UUID | None = Field(
        None, description="Connector ID for entity A (for identity-correct resolution)"
    )
    entity_b_connector_id: UUID | None = Field(
        None, description="Connector ID for entity B (for identity-correct resolution)"
    )
    entity_a_type: str | None = Field(
        None, description="Entity type for entity A (e.g., 'Pod', 'VirtualMachine')"
    )
    entity_b_type: str | None = Field(
        None, description="Entity type for entity B (e.g., 'Node', 'Instance')"
    )


class TopologySameAs(BaseModel):
    """Full SAME_AS relationship schema."""

    id: UUID
    entity_a_id: UUID
    entity_b_id: UUID
    similarity_score: float
    verified_via: list[str]
    discovered_at: datetime
    last_verified_at: datetime | None = None

    model_config = {"from_attributes": True}


class ConfirmedSameAs(BaseModel):
    """
    LLM-confirmed SAME_AS relationship.

    Produced by hybrid correlation: embedding pre-filter + LLM confirmation.
    """

    entity_a_name: str = Field(..., description=FIRST_ENTITY_NAME)
    entity_b_name: str = Field(..., description=SECOND_ENTITY_NAME)
    entity_a_connector_id: UUID | None = Field(None, description="First entity's connector")
    entity_b_connector_id: UUID | None = Field(None, description="Second entity's connector")
    similarity_score: float = Field(..., ge=0.0, le=1.0, description="Embedding similarity score")
    llm_confidence: float = Field(..., ge=0.0, le=1.0, description="LLM confidence in correlation")
    reasoning: str = Field(..., description="LLM explanation for why entities are the same")
    verified_via: list[str] = Field(
        default_factory=list, description="Methods used for verification"
    )


# =============================================================================
# SAME_AS Suggestion Schemas (Phase 2 Correlation)
# =============================================================================


class SuggestionStatus(StrEnum):
    """Status of a SAME_AS suggestion."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class SuggestionMatchType(StrEnum):
    """How the correlation was detected."""

    HOSTNAME_MATCH = "hostname_match"
    IP_MATCH = "ip_match"
    PARTIAL_HOSTNAME = "partial_hostname"


class SameAsSuggestionCreate(BaseModel):
    """Schema for creating a SAME_AS suggestion."""

    entity_a_id: UUID = Field(..., description="First entity ID")
    entity_b_id: UUID = Field(..., description="Second entity ID")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Match confidence score")
    match_type: str = Field(..., description="Type of match (hostname_match, ip_match)")
    match_details: str | None = Field(None, description="Details about how match was detected")


class SameAsSuggestion(BaseModel):
    """Full SAME_AS suggestion schema with all fields."""

    id: UUID
    entity_a_id: UUID
    entity_b_id: UUID
    confidence: float
    match_type: str
    match_details: str | None = None
    status: str
    suggested_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    tenant_id: str
    # LLM verification fields (exist on DB model, needed for frontend display)
    llm_verification_attempted: bool = False
    llm_verification_result: dict | None = None

    model_config = {"from_attributes": True}


class SameAsSuggestionWithEntities(SameAsSuggestion):
    """Suggestion with embedded entity information for API responses."""

    entity_a_name: str = Field(..., description=FIRST_ENTITY_NAME)
    entity_b_name: str = Field(..., description=SECOND_ENTITY_NAME)
    entity_a_connector_name: str | None = Field(None, description="First entity's connector name")
    entity_b_connector_name: str | None = Field(None, description="Second entity's connector name")


class SuggestionListResponse(BaseModel):
    """API response for listing suggestions."""

    suggestions: list[SameAsSuggestionWithEntities]
    total: int


class SuggestionApproveRequest(BaseModel):
    """Request body for approving a suggestion."""

    pass  # No additional fields needed, user info comes from auth


class SuggestionRejectRequest(BaseModel):
    """Request body for rejecting a suggestion."""

    reason: str | None = Field(None, description="Optional reason for rejection")


class SuggestionActionResponse(BaseModel):
    """Response after approving or rejecting a suggestion."""

    success: bool
    message: str
    same_as_created: bool = False  # True if SAME_AS was created (for approve)


# =============================================================================
# Store Discovery Schemas (Agent Tool Input/Output)
# =============================================================================


class StoreDiscoveryInput(BaseModel):
    """Input for the store_discovery agent tool."""

    connector_type: str = Field(
        ...,
        description="Connector type for schema validation (kubernetes, vmware, gcp, proxmox, rest, soap)",
    )
    connector_id: UUID | None = Field(None, description="Connector ID for entity association")
    entities: list[TopologyEntityCreate] = Field(
        default_factory=list, description="Entities to store"
    )
    relationships: list[TopologyRelationshipCreate] = Field(
        default_factory=list, description="Relationships to store"
    )
    same_as: list[TopologySameAsCreate] = Field(
        default_factory=list, description="SAME_AS relationships to store (require verified_via)"
    )


class StoreDiscoveryResult(BaseModel):
    """Result from the store_discovery agent tool."""

    stored: bool
    entities_created: int = 0
    relationships_created: int = 0
    same_as_created: int = 0
    validation_errors: list[str] = Field(
        default_factory=list,
        description="List of validation errors for rejected entities/relationships",
    )
    message: str


# =============================================================================
# Lookup Schemas (Agent Tool Input/Output)
# =============================================================================


class TopologyChainItem(BaseModel):
    """Single item in a topology chain."""

    depth: int
    entity: str
    entity_type: str | None = None
    connector: str | None = None
    connector_id: UUID | None = None
    relationship: str | None = None  # The relationship from the previous entity


class PossiblyRelatedEntity(BaseModel):
    """Entity that might be related (not yet verified as SAME_AS)."""

    entity: str
    entity_type: str | None = None
    connector: str | None = None
    connector_id: UUID | None = None
    similarity: float


class CorrelatedEntity(BaseModel):
    """
    A confirmed SAME_AS entity from another connector.

    Unlike PossiblyRelatedEntity which represents embedding similarity,
    this represents a verified cross-connector correlation that the user
    or system has confirmed.
    """

    entity: TopologyEntity = Field(..., description="The correlated entity")
    connector_type: str = Field(..., description="Connector type (kubernetes, vmware, etc.)")
    connector_name: str | None = Field(None, description="Connector name for display")
    verified_via: list[str] = Field(
        default_factory=list, description="How the SAME_AS was verified"
    )


class LookupTopologyInput(BaseModel):
    """Input for the lookup_topology agent tool."""

    query: str = Field(..., description="Entity name or identifier to search for")
    traverse_depth: int = Field(default=10, ge=1, le=50, description="Maximum traversal depth")
    cross_connectors: bool = Field(
        default=True, description="Whether to follow SAME_AS relationships"
    )


class LookupTopologyResult(BaseModel):
    """Result from the lookup_topology agent tool."""

    found: bool
    entity: TopologyEntity | None = None
    topology_chain: list[TopologyChainItem] = Field(default_factory=list)
    connectors_traversed: list[str] = Field(default_factory=list)
    same_as_entities: list[CorrelatedEntity] = Field(
        default_factory=list, description="Confirmed SAME_AS entities from other connectors"
    )
    possibly_related: list[PossiblyRelatedEntity] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


# =============================================================================
# Invalidate Schemas (Agent Tool Input/Output)
# =============================================================================


class InvalidateTopologyInput(BaseModel):
    """Input for the invalidate_topology tool."""

    entity_name: str = Field(..., description="Entity to invalidate")
    reason: str = Field(..., description="Why the entity is being invalidated")


class InvalidateTopologyResult(BaseModel):
    """Result from the invalidate_topology tool."""

    invalidated: bool
    entities_affected: int = 0
    relationships_affected: int = 0
    message: str


# =============================================================================
# API Response Schemas
# =============================================================================


class TopologyEntityResponse(TopologyEntity):
    """API response for a single entity."""

    has_embedding: bool = False


class TopologySearchResponse(BaseModel):
    """API response for topology search."""

    entities: list[TopologyEntityResponse]
    total: int


class TopologyGraphNode(BaseModel):
    """Node in topology graph (simplified entity)."""

    id: UUID
    name: str
    entity_type: str
    connector_type: str
    connector_id: UUID | None = None
    scope: dict[str, Any] | None = None
    canonical_id: str
    description: str
    raw_attributes: dict[str, Any] | None = None
    discovered_at: datetime
    last_verified_at: datetime | None = None
    stale_at: datetime | None = None
    tenant_id: str

    model_config = {"from_attributes": True}


class TopologyGraphRelationship(BaseModel):
    """Relationship in topology graph."""

    id: UUID
    from_entity_id: UUID
    to_entity_id: UUID
    relationship_type: str
    discovered_at: datetime
    last_verified_at: datetime | None = None

    model_config = {"from_attributes": True}


class TopologyGraphSameAs(BaseModel):
    """Same-as link in topology graph."""

    id: UUID
    entity_a_id: UUID
    entity_b_id: UUID
    similarity_score: float
    verified_via: list[str]
    discovered_at: datetime
    last_verified_at: datetime | None = None

    model_config = {"from_attributes": True}


class TopologyGraphResponse(BaseModel):
    """API response for topology graph visualization."""

    nodes: list[TopologyGraphNode]
    relationships: list[TopologyGraphRelationship]
    same_as: list[TopologyGraphSameAs]
