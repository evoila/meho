# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Pydantic schemas for memory service.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from meho_app.modules.memory.models import ConfidenceLevel, MemoryType


class MemoryCreate(BaseModel):
    """Schema for creating a memory."""

    title: str = Field(..., min_length=1, max_length=500, description="Short title for scanning")
    body: str = Field(..., min_length=1, description="Full context body text")
    memory_type: MemoryType = Field(
        ..., description="Classification: entity, pattern, outcome, config"
    )
    tags: list[str] = Field(default_factory=list, description="Free-form tags")
    confidence_level: ConfidenceLevel = Field(
        default=ConfidenceLevel.AUTO_EXTRACTED,
        description="Confidence tier: operator, confirmed_outcome, auto_extracted",
    )
    source_type: str = Field(
        default="extraction", description="Source: operator, extraction, merge"
    )
    created_by: str | None = Field(default=None, description="User ID or 'system'")
    provenance_trail: list[dict] = Field(
        default_factory=list,
        description="Array of {conversation_id, timestamp, source} entries",
    )
    connector_id: str = Field(..., description="Connector this memory belongs to")
    tenant_id: str = Field(..., description="Tenant scope")
    conversation_id: str | None = Field(
        default=None,
        description="Convenience field for extraction pipeline — auto-appended to provenance_trail",
    )


class MemoryUpdate(BaseModel):
    """Schema for updating a memory (PATCH semantics — all fields optional)."""

    title: str | None = Field(default=None, min_length=1, max_length=500)
    body: str | None = Field(default=None, min_length=1)
    memory_type: MemoryType | None = None
    tags: list[str] | None = None
    confidence_level: ConfidenceLevel | None = None


class MemoryResponse(BaseModel):
    """Schema for memory API responses."""

    id: str
    tenant_id: str
    connector_id: str
    title: str
    body: str
    memory_type: str
    tags: list[str]
    confidence_level: str
    source_type: str
    created_by: str | None
    provenance_trail: list[dict]
    occurrence_count: int
    last_accessed: datetime | None
    last_seen: datetime
    created_at: datetime
    updated_at: datetime
    merged: bool = Field(
        default=False, description="Whether this memory was merged with an existing one"
    )

    model_config = ConfigDict(from_attributes=True)


class MemoryFilter(BaseModel):
    """Schema for filtering/listing memories."""

    connector_id: str = Field(..., description="Required — memories are connector-scoped")
    tenant_id: str | None = Field(default=None, description="Filter by tenant")
    memory_type: MemoryType | None = Field(default=None, description="Filter by memory type")
    confidence_level: ConfidenceLevel | None = Field(
        default=None, description="Filter by confidence"
    )
    created_after: datetime | None = Field(
        default=None, description="Filter by creation date lower bound"
    )
    created_before: datetime | None = Field(
        default=None, description="Filter by creation date upper bound"
    )
    tags: list[str] | None = Field(default=None, description="Filter by tags (AND logic)")
    limit: int = Field(default=100, ge=1, le=1000, description="Max results")
    offset: int = Field(default=0, ge=0, description="Pagination offset")


class MemorySearchRequest(BaseModel):
    """Schema for semantic search across memories."""

    query: str = Field(..., min_length=1, description="Natural language search query")
    memory_type: MemoryType | None = Field(default=None, description="Filter by memory type")
    confidence_level: ConfidenceLevel | None = Field(
        default=None, description="Filter by confidence"
    )
    top_k: int = Field(default=10, ge=1, le=100, description="Number of results to return")
    score_threshold: float = Field(
        default=0.7, ge=0.0, le=1.0, description="Minimum similarity score"
    )


class MemorySearchResult(BaseModel):
    """Single result from a semantic memory search."""

    memory: MemoryResponse
    similarity: float = Field(description="Raw cosine similarity score")
    final_score: float = Field(description="Confidence-weighted final score")


class BulkCreateMemoriesRequest(BaseModel):
    """Schema for batch memory creation (used by extraction pipeline)."""

    memories: list[MemoryCreate] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Batch of memories to create/merge",
    )


class BulkCreateMemoriesResponse(BaseModel):
    """Response for bulk memory creation."""

    created: int = Field(description="Number of new memories created")
    merged: int = Field(description="Number of memories merged with existing")
    memories: list[MemoryResponse] = Field(description="All resulting memories")
