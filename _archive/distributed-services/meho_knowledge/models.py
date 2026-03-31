"""
SQLAlchemy models for knowledge service.
"""
# mypy: disable-error-code="valid-type,misc"
from sqlalchemy import Column, String, Text, TIMESTAMP, Integer, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import declarative_base
from pgvector.sqlalchemy import Vector
from datetime import datetime
import uuid

Base = declarative_base()


class KnowledgeChunkModel(Base):
    """Knowledge chunk with ACL metadata"""
    
    __tablename__ = "knowledge_chunk"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # ACL fields - control who can access this knowledge
    tenant_id = Column(String, nullable=True, index=True)
    system_id = Column(String, nullable=True, index=True)
    user_id = Column(String, nullable=True, index=True)
    roles = Column(JSONB, nullable=False, default=list)  # List of role strings
    groups = Column(JSONB, nullable=False, default=list)  # List of group strings
    
    # Content
    text = Column(Text, nullable=False)
    tags = Column(JSONB, nullable=False, default=list)  # List of tag strings
    source_uri = Column(Text, nullable=True)  # e.g., s3://bucket/doc.pdf#page=3
    
    # Vector embedding for semantic search (1536 dimensions for OpenAI)
    embedding = Column(Vector(1536), nullable=True)  # Populated during ingestion
    
    # Rich metadata for enhanced retrieval
    search_metadata = Column(JSONB, nullable=True, default=dict)  # Structured metadata (ChunkMetadata schema)
    
    # Lifecycle management (for events vs documentation)
    expires_at = Column(TIMESTAMP, nullable=True, index=True)  # Auto-delete after this time (NULL = never expires)
    knowledge_type = Column(String(50), nullable=False, default="documentation", index=True)  # documentation, procedure, event, trend
    priority = Column(Integer, nullable=False, default=0)  # For search ranking (higher = more important)
    
    # Metadata
    created_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        TIMESTAMP,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow
    )
    
    # Indexes for common query patterns
    __table_args__ = (
        Index('ix_knowledge_chunk_tenant_system', 'tenant_id', 'system_id'),
        Index('ix_knowledge_chunk_tenant_user', 'tenant_id', 'user_id'),
    )
    
    def __repr__(self) -> str:
        return f"<KnowledgeChunk(id={self.id}, tenant={self.tenant_id}, text={self.text[:50]}...)>"

