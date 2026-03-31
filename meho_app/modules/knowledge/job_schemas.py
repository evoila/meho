# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Pydantic schemas for ingestion jobs.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class IngestionJobCreate(BaseModel):
    """Schema for creating an ingestion job"""

    job_type: str = Field(..., description="Type of job: 'document', 'text', 'webhook'")
    tenant_id: str = Field(..., description="Tenant ID")
    connector_id: str | None = Field(
        None, description="Connector ID — required for new uploads, nullable for historical jobs"
    )
    filename: str | None = Field(None, description="Filename (for document uploads)")
    file_size: int | None = Field(None, description="File size in bytes")
    knowledge_type: str = Field(default="documentation", description="Knowledge type")
    tags: list[str] = Field(default_factory=list, description="Document-level tags")


class IngestionJobProgress(BaseModel):
    """Progress information for an ingestion job"""

    # Basic progress
    total_chunks: int | None = Field(None, description="Total chunks to process")
    chunks_processed: int = Field(..., description="Chunks processed so far")
    chunks_created: int = Field(..., description="Chunks successfully created")
    percent: float = Field(..., description="Progress percentage (0-100)")

    # Detailed progress (Session 30)
    current_stage: str | None = Field(
        None, description="Current stage (extracting, chunking, embedding, storing)"
    )
    stage_progress: float = Field(
        default=0.0, description="Progress within current stage (0.0-1.0)"
    )
    overall_progress: float = Field(default=0.0, description="Overall progress (0.0-1.0)")
    status_message: str | None = Field(None, description="Human-readable status message")
    estimated_completion: datetime | None = Field(None, description="Estimated completion time")


class IngestionJob(BaseModel):
    """Complete ingestion job with status and progress"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    job_type: str
    status: str  # 'pending', 'processing', 'completed', 'failed'

    @field_serializer("id")
    def serialize_id(self, value: UUID) -> str:
        """Serialize UUID to string for JSON"""
        return str(value)

    # Metadata
    tenant_id: str
    connector_id: str | None = None
    filename: str | None
    file_size: int | None
    knowledge_type: str
    tags: list[str] = Field(default_factory=list)

    # Progress (basic)
    total_chunks: int | None
    chunks_processed: int
    chunks_created: int

    # Detailed progress (Session 30)
    current_stage: str | None = None
    stage_progress: float = 0.0
    overall_progress: float = 0.0
    status_message: str | None = None
    stage_started_at: datetime | None = None
    estimated_completion: datetime | None = None

    # Enhanced error tracking
    error_stage: str | None = None
    error_chunk_index: int | None = None
    error_details: dict | None = None

    # Job retention
    retention_until: datetime | None = None

    # Results
    chunk_ids: list[str]
    error: str | None

    # Timing
    started_at: datetime
    completed_at: datetime | None

    @property
    def progress_percent(self) -> float:
        """Calculate progress percentage"""
        if not self.total_chunks or self.total_chunks == 0:
            return 0.0
        return (self.chunks_processed / self.total_chunks) * 100.0


class IngestionJobStatus(BaseModel):
    """Job status response for API"""

    id: str
    status: str
    progress: IngestionJobProgress
    started_at: datetime
    completed_at: datetime | None
    error: str | None

    # Optional: ETA calculation
    estimated_completion: datetime | None = Field(None, description="Estimated completion time")


class IngestionJobFilter(BaseModel):
    """Filter for listing ingestion jobs"""

    tenant_id: str | None = None
    connector_id: str | None = None  # Filter by connector
    status: str | None = None  # pending, processing, completed, failed
    job_type: str | None = None  # document, text, webhook
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)
