# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
SQLAlchemy models for topology service.

Stores discovered system topology (entities, relationships, embeddings).
The agent learns and remembers topology as it investigates systems.
"""

# mypy: disable-error-code="valid-type,misc,assignment"
import uuid
from datetime import UTC, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import TIMESTAMP, Boolean, Column, Float, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, relationship

from meho_app.database import Base

FK_TOPOLOGY_ENTITY_ID = "topology_entities.id"


class TopologyEntityModel(Base):
    """
    Entities discovered by the agent during investigation.

    Each entity belongs to a connector (or is external like URLs).
    Uniquely identified by (tenant_id, connector_id, entity_type, canonical_id).

    The canonical_id is a scoped identifier that includes namespace/scope context,
    e.g., "prod/nginx" for a K8s Pod in namespace "prod" named "nginx".
    """

    __tablename__ = "topology_entities"

    id: Mapped[uuid.UUID] = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = Column(String(255), nullable=False)
    connector_id: Mapped[uuid.UUID | None] = Column(
        UUID(as_uuid=True), nullable=True
    )  # NULL for external entities like URLs
    connector_name: Mapped[str | None] = Column(
        String(255), nullable=True
    )  # Cached connector name for display

    # Entity classification (NEW in TASK-156)
    entity_type: Mapped[str] = Column(
        String(100), nullable=False
    )  # "Pod", "VM", "Namespace", "Host"
    connector_type: Mapped[str] = Column(
        String(50), nullable=False
    )  # "kubernetes", "vmware", "gcp"

    # Scoped identity (NEW in TASK-156)
    scope: Mapped[dict[str, Any] | None] = Column(
        JSONB, nullable=True, default=dict
    )  # {"namespace": "prod"} or {"datacenter": "dc1"}
    canonical_id: Mapped[str] = Column(
        String(500), nullable=False
    )  # "prod/nginx" or moref for VMware

    # Rich description for embedding generation
    description: Mapped[str] = Column(Text, nullable=False)

    # Raw attributes from the source system
    raw_attributes: Mapped[dict[str, Any] | None] = Column(
        JSONB, nullable=True, default=dict
    )  # {"ip": "192.168.1.10", "namespace": "prod"}

    # Lifecycle management
    discovered_at: Mapped[datetime] = Column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    last_verified_at: Mapped[datetime | None] = Column(TIMESTAMP(timezone=True), nullable=True)
    stale_at: Mapped[datetime | None] = Column(TIMESTAMP(timezone=True), nullable=True)

    # Multi-tenancy
    tenant_id: Mapped[str] = Column(String(100), nullable=False, index=True)

    # Relationships
    embedding: Mapped["TopologyEmbeddingModel | None"] = relationship(
        "TopologyEmbeddingModel",
        back_populates="entity",
        uselist=False,
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_topology_entities_connector", "connector_id"),
        Index("idx_topology_entities_name", "name"),
        Index("idx_topology_entities_type", "entity_type"),
        Index("idx_topology_entities_connector_type", "connector_type"),
        # Unique identity: (tenant_id, connector_id, entity_type, canonical_id)
        Index(
            "idx_topology_entity_identity",
            "tenant_id",
            "connector_id",
            "entity_type",
            "canonical_id",
            unique=True,
        ),
    )

    def __repr__(self) -> str:
        return f"<TopologyEntity(id={self.id}, name={self.name})>"


class TopologyEmbeddingModel(Base):
    """
    Embeddings for entities, enabling similarity search.

    Used to find potentially related entities across connectors.
    """

    __tablename__ = "topology_embeddings"

    entity_id: Mapped[uuid.UUID] = Column(
        UUID(as_uuid=True), ForeignKey(FK_TOPOLOGY_ENTITY_ID, ondelete="CASCADE"), primary_key=True
    )
    embedding: Mapped[Any] = Column(Vector(1024), nullable=True)  # Voyage AI voyage-4-large

    # Relationship back to entity
    entity: Mapped["TopologyEntityModel"] = relationship(
        "TopologyEntityModel", back_populates="embedding"
    )

    def __repr__(self) -> str:
        return f"<TopologyEmbedding(entity_id={self.entity_id})>"


class TopologyRelationshipModel(Base):
    """
    Relationships between entities.

    Examples: routes_to, runs_on, uses_storage, resolves_to
    """

    __tablename__ = "topology_relationships"

    id: Mapped[uuid.UUID] = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_entity_id: Mapped[uuid.UUID] = Column(
        UUID(as_uuid=True), ForeignKey(FK_TOPOLOGY_ENTITY_ID, ondelete="CASCADE"), nullable=False
    )
    to_entity_id: Mapped[uuid.UUID] = Column(
        UUID(as_uuid=True), ForeignKey(FK_TOPOLOGY_ENTITY_ID, ondelete="CASCADE"), nullable=False
    )
    relationship_type: Mapped[str] = Column(
        String(100), nullable=False
    )  # "routes_to", "runs_on", "uses_storage"

    # Lifecycle
    discovered_at: Mapped[datetime] = Column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    last_verified_at: Mapped[datetime | None] = Column(TIMESTAMP(timezone=True), nullable=True)

    # Relationships
    from_entity: Mapped["TopologyEntityModel"] = relationship(
        "TopologyEntityModel", foreign_keys=[from_entity_id]
    )
    to_entity: Mapped["TopologyEntityModel"] = relationship(
        "TopologyEntityModel", foreign_keys=[to_entity_id]
    )

    __table_args__ = (
        Index("idx_topology_relationships_from", "from_entity_id"),
        Index("idx_topology_relationships_to", "to_entity_id"),
        Index(
            "idx_topology_relationships_unique",
            "from_entity_id",
            "to_entity_id",
            "relationship_type",
            unique=True,
        ),
    )

    def __repr__(self) -> str:
        return f"<TopologyRelationship(from={self.from_entity_id}, to={self.to_entity_id}, type={self.relationship_type})>"


class TopologySameAsModel(Base):
    """
    SAME_AS relationships - cross-connector correlation.

    When entities from different connectors represent the same real-world thing:
    - K8s Node "node-01" <-> Proxmox VM "k8s-worker-01"
    - DNS record "shop.example.com" <-> K8s Ingress "shop-ingress"

    Discovered via embedding similarity and verified via live API calls.

    Invariant: tenant_id MUST equal entity_a.tenant_id and entity_b.tenant_id.
    Enforced at the service layer in store_same_as() and store_discovery().
    A SAME_AS relationship cannot cross tenant boundaries.
    """

    __tablename__ = "topology_same_as"

    id: Mapped[uuid.UUID] = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_a_id: Mapped[uuid.UUID] = Column(
        UUID(as_uuid=True), ForeignKey(FK_TOPOLOGY_ENTITY_ID, ondelete="CASCADE"), nullable=False
    )
    entity_b_id: Mapped[uuid.UUID] = Column(
        UUID(as_uuid=True), ForeignKey(FK_TOPOLOGY_ENTITY_ID, ondelete="CASCADE"), nullable=False
    )

    # Multi-tenancy (must match both entity_a and entity_b tenant_id)
    tenant_id: Mapped[str] = Column(String(100), nullable=False, index=True)

    # How it was discovered
    similarity_score: Mapped[float] = Column(
        Float, nullable=False
    )  # Embedding similarity that triggered investigation
    verified_via: Mapped[list[str]] = Column(
        ARRAY(Text), nullable=False
    )  # ["IP: 192.168.1.10", "Both exist in APIs"]

    # Provenance
    discovered_at: Mapped[datetime] = Column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    last_verified_at: Mapped[datetime | None] = Column(TIMESTAMP(timezone=True), nullable=True)

    # Relationships
    entity_a: Mapped["TopologyEntityModel"] = relationship(
        "TopologyEntityModel", foreign_keys=[entity_a_id]
    )
    entity_b: Mapped["TopologyEntityModel"] = relationship(
        "TopologyEntityModel", foreign_keys=[entity_b_id]
    )

    __table_args__ = (
        Index("idx_topology_same_as_tenant", "tenant_id"),
        Index(
            "idx_topology_same_as_unique", "entity_a_id", "entity_b_id", "tenant_id", unique=True
        ),
    )

    def __repr__(self) -> str:
        return f"<TopologySameAs(a={self.entity_a_id}, b={self.entity_b_id}, tenant={self.tenant_id}, score={self.similarity_score})>"


class TopologySameAsSuggestionModel(Base):
    """
    Pending SAME_AS suggestions for manual review.

    Created automatically when:
    - K8s Ingress hostname matches a connector's target_host
    - VMware VM IP/hostname matches a connector's target_host
    - GCP Instance IP matches a connector's target_host

    Users can approve (creates SAME_AS) or reject these suggestions.
    """

    __tablename__ = "topology_same_as_suggestion"

    id: Mapped[uuid.UUID] = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_a_id: Mapped[uuid.UUID] = Column(
        UUID(as_uuid=True), ForeignKey(FK_TOPOLOGY_ENTITY_ID, ondelete="CASCADE"), nullable=False
    )
    entity_b_id: Mapped[uuid.UUID] = Column(
        UUID(as_uuid=True), ForeignKey(FK_TOPOLOGY_ENTITY_ID, ondelete="CASCADE"), nullable=False
    )

    # Confidence and match details
    confidence: Mapped[float] = Column(Float, nullable=False)  # 0.0 to 1.0
    match_type: Mapped[str] = Column(String(50), nullable=False)  # "hostname_match", "ip_match"
    match_details: Mapped[str | None] = Column(
        Text, nullable=True
    )  # Details about how match was detected

    # Workflow status
    status: Mapped[str] = Column(
        String(20), nullable=False, default="pending"
    )  # "pending", "approved", "rejected"
    suggested_at: Mapped[datetime] = Column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    resolved_at: Mapped[datetime | None] = Column(TIMESTAMP(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = Column(
        String(255), nullable=True
    )  # user who approved/rejected

    # LLM verification (Phase 3)
    llm_verification_attempted: Mapped[bool] = Column(Boolean, default=False, nullable=False)
    llm_verification_result: Mapped[dict[str, Any] | None] = Column(
        JSONB, nullable=True
    )  # {is_same, confidence, reasoning, matching_identifiers}

    # Multi-tenancy
    tenant_id: Mapped[str] = Column(String(100), nullable=False, index=True)

    # Relationships
    entity_a: Mapped["TopologyEntityModel"] = relationship(
        "TopologyEntityModel", foreign_keys=[entity_a_id]
    )
    entity_b: Mapped["TopologyEntityModel"] = relationship(
        "TopologyEntityModel", foreign_keys=[entity_b_id]
    )

    __table_args__ = (
        Index("idx_topology_suggestion_tenant", "tenant_id"),
        Index("idx_topology_suggestion_status", "status"),
        Index("idx_topology_suggestion_unique", "entity_a_id", "entity_b_id", unique=True),
    )

    def __repr__(self) -> str:
        return f"<TopologySameAsSuggestion(a={self.entity_a_id}, b={self.entity_b_id}, status={self.status})>"
