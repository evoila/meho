# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
API request/response schemas for Knowledge HTTP Service.

These are HTTP-specific schemas separate from the domain schemas.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from meho_app.modules.knowledge.schemas import KnowledgeType

# ============================================================================
# Chunk Schemas
# ============================================================================


class ChunkCreateRequest(BaseModel):
    """Request to create a knowledge chunk"""

    text: str = Field(..., min_length=1, max_length=100000)
    tenant_id: str | None = None
    connector_id: str | None = None
    user_id: str | None = None
    roles: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    source_uri: str | None = None

    # Lifecycle fields
    knowledge_type: KnowledgeType = Field(
        default=KnowledgeType.DOCUMENTATION,
        description="Type of knowledge (DOCUMENTATION, PROCEDURE, EVENT)",
    )
    priority: int = Field(default=0, ge=-100, le=100, description="Search ranking priority")
    expires_at: datetime | None = Field(
        default=None,
        description="Expiration timestamp for temporary knowledge (e.g., events, notices)",
    )


class ChunkResponse(BaseModel):
    """Response with chunk data"""

    id: str
    text: str
    tenant_id: str | None
    connector_id: str | None = None
    user_id: str | None
    roles: list[str]
    groups: list[str]
    tags: list[str]
    source_uri: str | None
    knowledge_type: str
    priority: int = 0
    expires_at: datetime | None = None
    scope_type: str = "instance"
    connector_type_scope: str | None = None
    search_metadata: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


# ============================================================================
# Search Schemas
# ============================================================================


class SearchRequest(BaseModel):
    """Request to search knowledge base"""

    query: str = Field(..., min_length=1, max_length=1000)
    tenant_id: str | None = None
    system_id: str | None = None
    user_id: str | None = None
    roles: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    top_k: int = Field(default=10, ge=1, le=100)
    score_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    metadata_filters: dict[str, Any] | None = Field(
        default=None,
        description="Optional metadata filters for enhanced retrieval. Examples: "
        "{'resource_type': 'roles'}, {'content_type': 'example_json'}, "
        "{'chapter': 'Roles Management'}, {'has_json_example': True}",
    )


class SearchResult(BaseModel):
    """Single search result"""

    id: str
    text: str
    score: float
    tags: list[str]
    source_uri: str | None


class SearchResponse(BaseModel):
    """Response with search results"""

    results: list[SearchResult]
    query: str
    count: int


# ============================================================================
# Ingestion Schemas
# ============================================================================


class IngestTextRequest(BaseModel):
    """
    Request to ingest raw text.

    Examples:
    - Documentation (permanent): Architecture docs, reference guides
    - Procedure (permanent): Runbooks, troubleshooting guides, lessons learned
    - Event (temporary): Temporary notices, maintenance windows, time-sensitive info
    """

    text: str = Field(..., min_length=1)
    tenant_id: str | None = None
    system_id: str | None = None
    user_id: str | None = None
    roles: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    source_uri: str | None = None

    # Lifecycle fields
    knowledge_type: KnowledgeType = Field(
        default=KnowledgeType.DOCUMENTATION,
        description="Type: DOCUMENTATION (permanent), PROCEDURE (permanent), EVENT (temporary)",
    )
    priority: int = Field(default=0, ge=-100, le=100, description="Search ranking priority")
    expires_at: datetime | None = Field(
        default=None,
        description="Expiration time for temporary knowledge (EVENT type). Example: Marathon notice expires tomorrow.",
    )


class IngestTextResponse(BaseModel):
    """Response from text ingestion"""

    chunk_ids: list[str]
    count: int


class IngestDocumentResponse(BaseModel):
    """Response from document ingestion"""

    chunk_ids: list[str]
    count: int
    document_uri: str


# ============================================================================
# Health Schema
# ============================================================================


class HealthResponse(BaseModel):
    """Health check response"""

    status: str
    version: str
    database: str
    vector_store: str
