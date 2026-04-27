# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
SQLAlchemy models for knowledge service.
"""

# mypy: disable-error-code="valid-type,misc"
import enum
import uuid
from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import TIMESTAMP, CheckConstraint, Column, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from meho_app.database import Base


class ScopeType(enum.StrEnum):
    """Three-tier knowledge scoping.

    - GLOBAL: Org-wide knowledge (not tied to any connector or type)
    - TYPE: Shared across all instances of a connector type (e.g., all Kubernetes)
    - INSTANCE: Specific to one connector instance (existing behavior)
    """

    GLOBAL = "global"
    TYPE = "type"
    INSTANCE = "instance"


class KnowledgeChunkModel(Base):
    """Knowledge chunk with ACL metadata and three-tier scoping"""

    __tablename__ = "knowledge_chunk"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # ACL fields - control who can access this knowledge
    tenant_id = Column(String, nullable=True, index=True)
    connector_id = Column(
        UUID(as_uuid=True),
        ForeignKey("connector.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    user_id = Column(String, nullable=True, index=True)
    roles = Column(JSONB, nullable=False, default=list)  # List of role strings
    groups = Column(JSONB, nullable=False, default=list)  # List of group strings

    # Three-tier scoping (Phase 65)
    # - global: org-wide, connector_id=NULL, connector_type_scope=NULL
    # - type: shared across connector type, connector_id=NULL, connector_type_scope='kubernetes'
    # - instance: per-connector, connector_id set, connector_type_scope=NULL
    scope_type = Column(String(20), nullable=False, server_default="instance", index=True)
    connector_type_scope = Column(String(50), nullable=True)  # e.g., "kubernetes", "vmware"

    # Content
    text = Column(Text, nullable=False)
    tags = Column(JSONB, nullable=False, default=list)  # List of tag strings
    source_uri = Column(Text, nullable=True)  # e.g., s3://bucket/doc.pdf#page=3

    # Vector embedding for semantic search (1024 dimensions for Voyage AI voyage-4-large)
    embedding = Column(Vector(1024), nullable=True)  # Populated during ingestion

    # Rich metadata for enhanced retrieval
    search_metadata = Column(
        JSONB, nullable=True, default=dict
    )  # Structured metadata (ChunkMetadata schema)

    # Documentation versioning (e.g. "v8", "v9", "2026.1")
    doc_version = Column(String(100), nullable=True)

    # Family grouping: all versions of the same logical document share a family_id
    family_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_document_family.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Lifecycle management (for events vs documentation)
    expires_at = Column(
        TIMESTAMP(timezone=True), nullable=True, index=True
    )  # Auto-delete after this time (NULL = never expires)
    knowledge_type = Column(
        String(50), nullable=False, default="documentation", index=True
    )  # documentation, procedure, event, trend
    priority = Column(
        Integer, nullable=False, default=0
    )  # For search ranking (higher = more important)

    # Metadata
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Indexes and constraints for common query patterns
    __table_args__ = (
        Index("ix_knowledge_chunk_tenant_connector", "tenant_id", "connector_id"),
        Index("ix_knowledge_chunk_tenant_user", "tenant_id", "user_id"),
        # Scope-aware indexes (Phase 65)
        Index("ix_knowledge_chunk_scope", "tenant_id", "scope_type"),
        Index(
            "ix_knowledge_chunk_type_scope",
            "tenant_id",
            "connector_type_scope",
            postgresql_where="scope_type = 'type'",
        ),
        # Data integrity: instance rows MUST have connector_id, global/type rows MUST NOT
        CheckConstraint(
            "(scope_type = 'instance' AND connector_id IS NOT NULL) OR "
            "(scope_type IN ('global', 'type') AND connector_id IS NULL)",
            name="ck_knowledge_chunk_scope_connector",
        ),
    )

    def __repr__(self) -> str:
        return f"<KnowledgeChunk(id={self.id}, tenant={self.tenant_id}, scope={self.scope_type}, connector={self.connector_id}, text={self.text[:50]}...)>"


class DocumentFamilyModel(Base):
    """A logical document that can have multiple versions.

    Each upload attaches to exactly one family. The first upload creates
    the family (name derived from the file stem), and subsequent uploads
    attach as additional versions. Uniqueness constraints on
    (family_id, doc_version) and (family_id, file_hash) live on
    ``ingestion_jobs`` and prevent duplicate versions or identical file
    uploads within a single family.
    """

    __tablename__ = "knowledge_document_family"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    tenant_id = Column(String(255), nullable=False, index=True)
    name = Column(String(512), nullable=False)

    # Scope info mirrors IngestionJob / KnowledgeChunk so we can filter families by scope
    scope_type = Column(String(20), nullable=False)
    connector_id = Column(
        UUID(as_uuid=True),
        ForeignKey("connector.id", ondelete="CASCADE"),
        nullable=True,
    )
    connector_type_scope = Column(String(100), nullable=True)

    # Inherited defaults for new versions
    knowledge_type = Column(String(50), nullable=False, default="documentation")
    tags = Column(JSONB, nullable=False, default=list)

    created_by_user_id = Column(String(255), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        Index(
            "ix_document_family_scope",
            "tenant_id",
            "scope_type",
            "connector_type_scope",
            "connector_id",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<DocumentFamily(id={self.id}, name={self.name}, "
            f"scope={self.scope_type}, tenant={self.tenant_id})>"
        )
