# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Pydantic response models for the Observability API.

Part of TASK-186: Deep Observability & Introspection System.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# ============================================================================
# Token and Event Details
# ============================================================================


class TokenUsageResponse(BaseModel):
    """Token usage metrics for an LLM call."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float | None = None


class EventDetailsResponse(BaseModel):
    """Full details for an event, supporting multiple event types."""

    # LLM call details
    llm_prompt: str | None = None
    llm_messages: list[dict] | None = None
    llm_response: str | None = None
    llm_parsed: dict | None = None
    token_usage: TokenUsageResponse | None = None
    llm_duration_ms: float | None = None
    model: str | None = None

    # HTTP call details
    http_method: str | None = None
    http_url: str | None = None
    http_headers: dict | None = None
    http_request_body: str | None = None
    http_response_body: str | None = None
    http_status_code: int | None = None
    http_duration_ms: float | None = None

    # SQL query details
    sql_query: str | None = None
    sql_parameters: dict | None = None
    sql_row_count: int | None = None
    sql_result_sample: list[dict] | None = None
    sql_duration_ms: float | None = None

    # Tool call details
    tool_name: str | None = None
    tool_input: dict | None = None
    tool_output: Any | None = None
    tool_duration_ms: float | None = None
    tool_error: str | None = None

    # Knowledge search details
    search_query: str | None = None
    search_type: str | None = None
    search_results: list[dict] | None = None
    search_scores: list[float] | None = None

    # Topology details
    entities_extracted: list[str] | None = None
    entities_found: list[dict] | None = None
    context_injected: str | None = None


class EventResponse(BaseModel):
    """Individual event in a transcript."""

    id: str
    timestamp: datetime
    type: str
    summary: str
    details: EventDetailsResponse
    parent_event_id: str | None = None
    step_number: int | None = None
    node_name: str | None = None
    agent_name: str | None = None
    duration_ms: float | None = None
    tags: dict | None = None  # Factual metadata tags extracted from event details


# ============================================================================
# Session and Transcript Models
# ============================================================================


class SessionSummaryResponse(BaseModel):
    """Summary statistics for a session transcript."""

    session_id: str
    status: str
    created_at: datetime
    completed_at: datetime | None = None
    total_llm_calls: int
    total_operation_calls: int
    total_sql_queries: int
    total_tool_calls: int
    total_tokens: int
    total_cost_usd: float | None = None
    total_duration_ms: float
    user_query: str | None = None
    agent_type: str | None = None


class TranscriptResponse(BaseModel):
    """Full transcript with summary and events."""

    session_id: str
    summary: SessionSummaryResponse
    events: list[EventResponse]
    total_events: int


class TranscriptItemResponse(BaseModel):
    """Individual transcript in a multi-transcript session view."""

    transcript_id: str
    user_query: str | None = None
    created_at: datetime
    status: str
    summary: SessionSummaryResponse
    events: list[EventResponse]


class MultiTranscriptResponse(BaseModel):
    """Multiple transcripts for a session (multi-turn conversation view)."""

    session_id: str
    transcripts: list[TranscriptItemResponse]
    total_transcripts: int


# ============================================================================
# Session List Models
# ============================================================================


class SessionListItem(BaseModel):
    """Session item in list view."""

    session_id: str
    created_at: datetime
    status: str
    user_query: str | None = None
    total_llm_calls: int
    total_tokens: int
    total_duration_ms: float


class SessionListResponse(BaseModel):
    """Paginated list of sessions."""

    sessions: list[SessionListItem]
    total: int
    offset: int
    limit: int


# ============================================================================
# Search Models
# ============================================================================


class SearchResultItem(BaseModel):
    """Search result item."""

    event_id: str
    session_id: str
    event_type: str
    summary: str
    timestamp: datetime
    match_context: str | None = None


class SearchResponse(BaseModel):
    """Search results across sessions."""

    query: str
    results: list[SearchResultItem]
    total: int


# ============================================================================
# Explanation Models
# ============================================================================


class SessionExplanationResponse(BaseModel):
    """Human-readable explanation of session execution."""

    session_id: str
    focus: str
    explanation: str
    summary: SessionSummaryResponse
    key_events: list[dict] = Field(default_factory=list)


# ============================================================================
# Retention Models
# ============================================================================


class RetentionStatsResponse(BaseModel):
    """Retention statistics for monitoring."""

    total_transcripts: int
    active_transcripts: int
    soft_deleted_transcripts: int
    pending_hard_delete: int
    oldest_active_timestamp: datetime | None = None
    oldest_soft_deleted_timestamp: datetime | None = None
    retention_days: int
    grace_days: int


class CleanupResultResponse(BaseModel):
    """Result of a cleanup operation."""

    soft_deleted_count: int
    hard_deleted_count: int
    errors: list[str] = Field(default_factory=list)
    message: str


# ============================================================================
# Export Models
# ============================================================================


class ExportFormat(StrEnum):
    """Supported export formats."""

    JSON = "json"
    CSV = "csv"


class BulkExportRequest(BaseModel):
    """Request for bulk transcript export."""

    session_ids: list[str] | None = Field(
        default=None, description="Session IDs to export (None = recent sessions)"
    )
    since: datetime | None = Field(
        default=None, description="Export sessions created after this time"
    )
    until: datetime | None = Field(
        default=None, description="Export sessions created before this time"
    )
    event_types: list[str] | None = Field(default=None, description="Filter by event types")
    include_details: bool = Field(default=True, description="Include full event details")
    max_sessions: int = Field(default=10, ge=1, le=50, description="Maximum sessions to export")


class BulkExportResponse(BaseModel):
    """Response with exported transcript data."""

    sessions_exported: int
    total_events: int
    format: str
    filename: str
