# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Persistence layer for agent state and transcripts.

This module provides:
- Database models for storing execution transcripts
- TranscriptService for CRUD operations
- TranscriptCollector for async event buffering
- TranscriptRetentionService for data lifecycle management
- Scrubbing utilities for sensitive data
- Redis-backed session state for multi-turn conversations
- Query builders for complex transcript searches
- Event factory for creating DetailedEvent instances

Example:
    >>> from meho_app.modules.agents.persistence import (
    ...     TranscriptService,
    ...     TranscriptCollector,
    ...     TranscriptRetentionService,
    ...     SessionTranscriptModel,
    ...     TranscriptEventModel,
    ...     AgentStateStore,
    ...     OrchestratorSessionState,
    ...     EventFactory,
    ...     ScrubPatterns,
    ... )
"""

# Transcript models and services
# Context utilities
from meho_app.modules.agents.persistence.event_context import (
    get_transcript_collector,
    has_transcript_collector,
    set_transcript_collector,
)

# Event factory
from meho_app.modules.agents.persistence.event_factory import EventFactory

# Helper functions
from meho_app.modules.agents.persistence.helpers import create_transcript_collector
from meho_app.modules.agents.persistence.retention_service import (
    CleanupResult,
    RetentionStats,
    TranscriptRetentionService,
)

# Scrubbing utilities
from meho_app.modules.agents.persistence.scrubber import (
    create_sanitized_event_details,
    sanitize_headers,
    sanitize_http_body,
    sanitize_tool_output,
    scrub_sensitive_data,
    truncate_payload,
)
from meho_app.modules.agents.persistence.scrubber_patterns import ScrubPatterns

# Session state for multi-turn conversations
from meho_app.modules.agents.persistence.session_state import (
    ConnectorMemory,
    OrchestratorSessionState,
)
from meho_app.modules.agents.persistence.state_store import AgentStateStore
from meho_app.modules.agents.persistence.transcript_collector import TranscriptCollector
from meho_app.modules.agents.persistence.transcript_models import (
    SessionTranscriptModel,
    TranscriptEventModel,
)

# Query builders
from meho_app.modules.agents.persistence.transcript_query_builder import (
    EventQueryBuilder,
    TranscriptQueryBuilder,
)
from meho_app.modules.agents.persistence.transcript_service import TranscriptService

__all__ = [
    "AgentStateStore",
    "CleanupResult",
    # Session State
    "ConnectorMemory",
    # Event factory
    "EventFactory",
    # Query builders
    "EventQueryBuilder",
    "OrchestratorSessionState",
    # Retention types
    "RetentionStats",
    "ScrubPatterns",
    # Transcript Models
    "SessionTranscriptModel",
    # Collector
    "TranscriptCollector",
    "TranscriptEventModel",
    "TranscriptQueryBuilder",
    "TranscriptRetentionService",
    # Transcript Services
    "TranscriptService",
    "create_sanitized_event_details",
    # Helper functions
    "create_transcript_collector",
    "get_transcript_collector",
    "has_transcript_collector",
    "sanitize_headers",
    "sanitize_http_body",
    "sanitize_tool_output",
    # Scrubbing utilities
    "scrub_sensitive_data",
    # Context utilities
    "set_transcript_collector",
    "truncate_payload",
]
