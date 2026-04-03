# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""SQLAlchemy models for session transcript persistence.

This module defines the database schema for storing execution transcripts,
enabling deep observability and historical access to agent behavior.

Tables:
- session_transcripts: Summary of a session's execution
- transcript_events: Individual events with full details
"""

# mypy: disable-error-code="valid-type,misc,var-annotated"
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import relationship

from meho_app.database import Base


class SessionTranscriptModel(Base):
    """Persistent storage for session execution details.

    Each session can have one transcript that captures all execution events
    with their full details for later retrieval and analysis.

    Attributes:
        id: Unique transcript identifier.
        session_id: Reference to the chat session.
        created_at: When the transcript was created.
        completed_at: When the session finished (null if still running).
        deleted_at: Soft-delete timestamp for retention (null if active).
        total_llm_calls: Count of LLM calls made.
        total_sql_queries: Count of SQL queries executed.
        total_operation_calls: Count of operation calls made (REST/SOAP/VMware).
        total_tool_calls: Count of tool invocations.
        total_tokens: Total tokens used (prompt + completion).
        total_cost_usd: Estimated cost in USD.
        total_duration_ms: Total execution time in milliseconds.
        agent_type: Type of agent (orchestrator, react, generic, k8).
        connector_ids: List of connector IDs involved.
        user_query: The original user query.
        status: Execution status (running, completed, failed).
    """

    __tablename__ = "session_transcripts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("chat_session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    completed_at = Column(DateTime(timezone=True), nullable=True, index=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)

    # Summary statistics
    total_llm_calls = Column(Integer, nullable=False, default=0)
    total_sql_queries = Column(Integer, nullable=False, default=0)
    total_operation_calls = Column(Integer, nullable=False, default=0)
    total_tool_calls = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    total_cost_usd = Column(Float, nullable=True)
    total_duration_ms = Column(Float, nullable=False, default=0)

    # Metadata
    agent_type = Column(String(50), nullable=True)
    connector_ids = Column(ARRAY(UUID(as_uuid=True)), nullable=True)
    user_query = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="running")

    # Relationships
    events = relationship(
        "TranscriptEventModel",
        back_populates="transcript",
        cascade="all, delete-orphan",
        order_by="TranscriptEventModel.timestamp",
    )

    def __repr__(self) -> str:
        return (
            f"<SessionTranscript(id={self.id}, session_id={self.session_id}, status={self.status})>"
        )


class TranscriptEventModel(Base):
    """Individual event in a session transcript.

    Each event captures a specific moment in the execution with full details
    stored in the JSONB `details` column.

    Attributes:
        id: Unique event identifier.
        transcript_id: Reference to parent transcript.
        session_id: Reference to chat session (denormalized for efficient queries).
        timestamp: When the event occurred.
        type: Event type (thought, action, observation, etc.).
        summary: Brief human-readable summary.
        details: Full event details as JSONB.
        parent_event_id: ID of parent event (for nested events).
        step_number: ReAct step number.
        node_name: Current graph node name.
        agent_name: Name of the agent that emitted this event.
        duration_ms: Duration of the operation (if applicable).
    """

    __tablename__ = "transcript_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transcript_id = Column(
        UUID(as_uuid=True),
        ForeignKey("session_transcripts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id = Column(UUID(as_uuid=True), nullable=False, index=True)

    # Event data
    timestamp = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    type = Column(String(50), nullable=False, index=True)
    summary = Column(Text, nullable=False)
    details = Column(JSONB, nullable=False, default=dict)

    # Linkage and context
    parent_event_id = Column(UUID(as_uuid=True), nullable=True)
    step_number = Column(Integer, nullable=True)
    node_name = Column(String(100), nullable=True)
    agent_name = Column(String(50), nullable=True)
    duration_ms = Column(Float, nullable=True)

    # Relationships
    transcript = relationship("SessionTranscriptModel", back_populates="events")

    # Composite indexes for efficient queries
    __table_args__ = (
        # Fast session timeline queries
        Index("ix_transcript_events_session_timestamp", "session_id", "timestamp"),
        # Fast type filtering within a session
        Index("ix_transcript_events_session_type", "session_id", "type"),
        # GIN index for JSONB full-text search (created in migration)
    )

    def __repr__(self) -> str:
        return f"<TranscriptEvent(id={self.id}, type={self.type}, summary={self.summary[:50]}...)>"
