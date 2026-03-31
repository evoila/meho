"""
Pydantic schemas for ingestion jobs.
"""
from pydantic import BaseModel, Field, ConfigDict, field_serializer
from typing import Optional, List
from datetime import datetime
from uuid import UUID


class IngestionJobCreate(BaseModel):
    """Schema for creating an ingestion job"""
    job_type: str = Field(..., description="Type of job: 'document', 'text', 'webhook'")
    tenant_id: str = Field(..., description="Tenant ID")
    filename: Optional[str] = Field(None, description="Filename (for document uploads)")
    file_size: Optional[int] = Field(None, description="File size in bytes")
    knowledge_type: str = Field(default="documentation", description="Knowledge type")
    tags: List[str] = Field(default_factory=list, description="Document-level tags")


class IngestionJobProgress(BaseModel):
    """Progress information for an ingestion job"""
    # Basic progress
    total_chunks: Optional[int] = Field(None, description="Total chunks to process")
    chunks_processed: int = Field(..., description="Chunks processed so far")
    chunks_created: int = Field(..., description="Chunks successfully created")
    percent: float = Field(..., description="Progress percentage (0-100)")
    
    # Detailed progress (Session 30)
    current_stage: Optional[str] = Field(None, description="Current stage (extracting, chunking, embedding, storing)")
    stage_progress: float = Field(default=0.0, description="Progress within current stage (0.0-1.0)")
    overall_progress: float = Field(default=0.0, description="Overall progress (0.0-1.0)")
    status_message: Optional[str] = Field(None, description="Human-readable status message")
    estimated_completion: Optional[datetime] = Field(None, description="Estimated completion time")


class IngestionJob(BaseModel):
    """Complete ingestion job with status and progress"""
    model_config = ConfigDict(from_attributes=True)
    
    id: UUID
    job_type: str
    status: str  # 'pending', 'processing', 'completed', 'failed'
    
    @field_serializer('id')
    def serialize_id(self, value: UUID) -> str:
        """Serialize UUID to string for JSON"""
        return str(value)
    
    # Metadata
    tenant_id: str
    filename: Optional[str]
    file_size: Optional[int]
    knowledge_type: str
    tags: List[str] = Field(default_factory=list)
    
    # Progress (basic)
    total_chunks: Optional[int]
    chunks_processed: int
    chunks_created: int
    
    # Detailed progress (Session 30)
    current_stage: Optional[str] = None
    stage_progress: float = 0.0
    overall_progress: float = 0.0
    status_message: Optional[str] = None
    stage_started_at: Optional[datetime] = None
    estimated_completion: Optional[datetime] = None
    
    # Enhanced error tracking
    error_stage: Optional[str] = None
    error_chunk_index: Optional[int] = None
    error_details: Optional[dict] = None
    
    # Job retention
    retention_until: Optional[datetime] = None
    
    # Results
    chunk_ids: List[str]
    error: Optional[str]
    
    # Timing
    started_at: datetime
    completed_at: Optional[datetime]
    
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
    completed_at: Optional[datetime]
    error: Optional[str]
    
    # Optional: ETA calculation
    estimated_completion: Optional[datetime] = Field(None, description="Estimated completion time")


class IngestionJobFilter(BaseModel):
    """Filter for listing ingestion jobs"""
    tenant_id: Optional[str] = None
    status: Optional[str] = None  # pending, processing, completed, failed
    job_type: Optional[str] = None  # document, text, webhook
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)

