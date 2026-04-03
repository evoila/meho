# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Observability API package.

Provides deep introspection into agent execution through:
- Session listing and filtering
- Transcript retrieval with event details
- Event type filtering (LLM, HTTP, SQL)
- Search across sessions
- Human-readable explanations
- Data retention management
- Export functionality (JSON/CSV)

Part of TASK-186: Deep Observability & Introspection System.
"""

from meho_app.api.observability.router import router

# Re-export schemas for external use
from meho_app.api.observability.schemas import (
    BulkExportRequest,
    BulkExportResponse,
    CleanupResultResponse,
    EventDetailsResponse,
    EventResponse,
    ExportFormat,
    MultiTranscriptResponse,
    RetentionStatsResponse,
    SearchResponse,
    SearchResultItem,
    SessionExplanationResponse,
    SessionListItem,
    SessionListResponse,
    SessionSummaryResponse,
    TokenUsageResponse,
    TranscriptItemResponse,
    TranscriptResponse,
)

__all__ = [
    # Schemas
    "BulkExportRequest",
    "BulkExportResponse",
    "CleanupResultResponse",
    "EventDetailsResponse",
    "EventResponse",
    "ExportFormat",
    "MultiTranscriptResponse",
    "RetentionStatsResponse",
    "SearchResponse",
    "SearchResultItem",
    "SessionExplanationResponse",
    "SessionListItem",
    "SessionListResponse",
    "SessionSummaryResponse",
    "TokenUsageResponse",
    "TranscriptItemResponse",
    "TranscriptResponse",
    # Router
    "router",
]
