# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
SQLAlchemy models for memory service.
"""

# mypy: disable-error-code="valid-type,misc"
import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import TIMESTAMP, Column, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from meho_app.database import Base


class MemoryType(StrEnum):
    """Classification of memory content.

    Three types are produced by auto-extraction (entity, pattern, outcome).
    The fourth type (config) is intentionally unreachable via auto-extraction --
    config memories are only created manually by operators via the "remember this"
    command or programmatically via the API. This is by design: auto-extraction
    should not capture connector configuration details (credentials, endpoints,
    thresholds) as memories.
    """

    ENTITY = "entity"
    PATTERN = "pattern"
    OUTCOME = "outcome"
    CONFIG = "config"  # Operator-curated only; excluded from auto-extraction by design


class ConfidenceLevel(StrEnum):
    """Three-tier confidence hierarchy: operator > confirmed_outcome > auto_extracted."""

    OPERATOR = "operator"
    CONFIRMED_OUTCOME = "confirmed_outcome"
    AUTO_EXTRACTED = "auto_extracted"


class ConnectorMemoryModel(Base):
    """Connector-scoped memory with confidence scoring and provenance tracking."""

    __tablename__ = "connector_memory"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Scoping - memories are always tenant-scoped
    tenant_id = Column(String, nullable=False, index=True)
    connector_id = Column(
        UUID(as_uuid=True),
        ForeignKey("connector.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Relationship back to ConnectorModel
    connector = relationship("ConnectorModel", back_populates="connector_memories")

    # Content
    title = Column(String(500), nullable=False)
    body = Column(Text, nullable=False)

    # Classification
    memory_type = Column(String(50), nullable=False, index=True)
    tags = Column(JSONB, nullable=False, default=list)

    # Confidence & provenance
    confidence_level = Column(
        String(50), nullable=False, default=ConfidenceLevel.AUTO_EXTRACTED.value
    )
    source_type = Column(String(50), nullable=False, default="extraction")
    created_by = Column(String, nullable=True)
    provenance_trail = Column(JSONB, nullable=False, default=list)

    # Occurrence & staleness tracking
    occurrence_count = Column(Integer, nullable=False, default=1)
    last_accessed = Column(TIMESTAMP(timezone=True), nullable=True)
    last_seen = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    # Vector embedding for semantic search (1024 dimensions for Voyage AI voyage-4-large)
    embedding = Column(Vector(1024), nullable=True)

    # Timestamps
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Composite indexes for common query patterns
    __table_args__ = (
        Index("ix_connector_memory_tenant_connector", "tenant_id", "connector_id"),
        Index("ix_connector_memory_connector_type", "connector_id", "memory_type"),
    )

    def __repr__(self) -> str:
        return (
            f"<ConnectorMemory(id={self.id}, connector={self.connector_id}, "
            f"type={self.memory_type}, confidence={self.confidence_level}, "
            f"title={self.title[:50]}...)>"
        )
