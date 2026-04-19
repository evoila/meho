# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Database models for ingestion job tracking.

Tracks document/text ingestion jobs with progress and status.
"""

# mypy: disable-error-code="return-value"
import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import TIMESTAMP, Column, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from meho_app.modules.knowledge.models import Base


class IngestionStage(StrEnum):
    """Detailed ingestion stages for progress tracking"""

    UPLOADING = "uploading"  # File upload to object storage
    EXTRACTING = "extracting"  # Extracting text from file
    CHUNKING = "chunking"  # Splitting into chunks
    METADATA_EXTRACTION = "metadata"  # Extracting metadata
    EMBEDDING = "embedding"  # Generating embeddings (slowest!)
    STORING = "storing"  # Storing in vector DB
    COMPLETED = "completed"
    FAILED = "failed"


class DeletionStage(StrEnum):
    """Deletion stages for progress tracking (Session 30)"""

    PREPARING = "preparing"  # Counting chunks to delete
    DELETING_CHUNKS = "deleting_chunks"  # Deleting from PostgreSQL + pgvector
    UPDATING_INDEX = "updating_index"  # Rebuilding BM25 index
    CLEANUP_STORAGE = "cleanup_storage"  # Deleting from MinIO/S3
    COMPLETED = "completed"
    FAILED = "failed"


class IngestionJob(Base):
    """
    Tracks ingestion job progress and status.

    Enables:
    - User visibility (progress bars)
    - Error reporting
    - Reliable testing (poll for completion)
    - Monitoring/observability
    """

    __tablename__ = "ingestion_jobs"

    # Identity
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Job metadata
    job_type = Column(String(50), nullable=False)  # 'document', 'text', 'webhook'
    status = Column(
        String(50), nullable=False, index=True
    )  # 'pending', 'processing', 'completed', 'failed'

    # Connector scope (nullable for backward compat with historical jobs)
    connector_id = Column(
        UUID(as_uuid=True), ForeignKey("connector.id", ondelete="CASCADE"), nullable=True
    )

    # Input metadata
    tenant_id = Column(String(255), nullable=False, index=True)
    filename = Column(String(512), nullable=True)
    file_size = Column(Integer, nullable=True)  # bytes
    knowledge_type = Column(String(50), nullable=False, default="documentation")
    tags = Column(JSONB, nullable=False, default=list)

    # Progress tracking (basic)
    total_chunks = Column(Integer, nullable=True)  # Set when chunking complete
    chunks_processed = Column(Integer, nullable=False, default=0)  # Chunks processed so far
    chunks_created = Column(Integer, nullable=False, default=0)  # Chunks successfully created

    # Detailed progress tracking (Session 30)
    current_stage = Column(String(50), nullable=True)  # IngestionStage value
    stage_progress = Column(Float, default=0.0)  # Progress within current stage (0.0-1.0)
    overall_progress = Column(Float, default=0.0)  # Overall progress (0.0-1.0)
    status_message = Column(Text, nullable=True)  # Human-readable status

    # Timing and estimation
    stage_started_at = Column(TIMESTAMP(timezone=True), nullable=True)
    estimated_completion = Column(TIMESTAMP(timezone=True), nullable=True)

    # Enhanced error tracking
    error_stage = Column(String(50), nullable=True)  # Which stage failed
    error_chunk_index = Column(Integer, nullable=True)  # Which chunk failed
    error_details = Column(JSONB, nullable=True)  # Structured error info

    # Job retention (auto-cleanup)
    retention_until = Column(TIMESTAMP(timezone=True), nullable=True)

    # Results
    chunk_ids = Column(JSONB, nullable=False, default=list)  # List of created chunk IDs
    error = Column(Text, nullable=True)  # Error message if failed

    # Timing
    started_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    completed_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # Indexes for efficient queries
    __table_args__ = (
        Index("idx_ingestion_jobs_status", "status"),
        Index("idx_ingestion_jobs_tenant", "tenant_id"),
        Index("idx_ingestion_jobs_tenant_status", "tenant_id", "status"),
        Index("idx_ingestion_jobs_retention", "retention_until"),  # For cleanup task
    )

    def __repr__(self) -> str:
        return f"<IngestionJob(id={self.id}, type={self.job_type}, status={self.status}, progress={self.chunks_processed}/{self.total_chunks})>"

    @property
    def progress_percent(self) -> float:
        """Calculate progress percentage"""
        if not self.total_chunks or self.total_chunks == 0:
            return 0.0
        return (self.chunks_processed / self.total_chunks) * 100.0

    @property
    def is_complete(self) -> bool:
        """Check if job is in terminal state"""
        return self.status in ["completed", "failed"]
