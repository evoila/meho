# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Transcript Service for persisting and querying execution transcripts.

This service provides CRUD operations for session transcripts and events,
enabling deep observability into agent execution.

Example:
    >>> service = TranscriptService(session)
    >>> transcript = await service.create_transcript(
    ...     session_id=uuid4(),
    ...     user_query="List all VMs",
    ...     agent_type="react",
    ... )
    >>> await service.add_event(
    ...     transcript_id=transcript.id,
    ...     event=detailed_event,
    ... )
    >>> await service.complete_transcript(transcript.id)
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.otel import get_logger
from meho_app.modules.agents.base.detailed_events import DetailedEvent
from meho_app.modules.agents.persistence.transcript_models import (
    SessionTranscriptModel,
    TranscriptEventModel,
)
from meho_app.modules.agents.persistence.transcript_query_builder import (
    EventQueryBuilder,
)

logger = get_logger(__name__)


class TranscriptService:
    """Service for managing session transcripts.

    Provides CRUD operations for transcripts and events,
    with support for batch operations and filtering.

    Attributes:
        session: SQLAlchemy async session.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Initialize the service with a database session.

        Args:
            session: SQLAlchemy async session for database operations.
        """
        self.session = session

    async def create_transcript(
        self,
        session_id: UUID,
        user_query: str,
        agent_type: str,
        connector_ids: list[UUID] | None = None,
    ) -> SessionTranscriptModel:
        """Create a new session transcript.

        Args:
            session_id: The chat session ID this transcript belongs to.
            user_query: The original user query.
            agent_type: Type of agent (orchestrator, react, generic, k8).
            connector_ids: Optional list of connector IDs involved.

        Returns:
            The created SessionTranscriptModel.
        """
        transcript = SessionTranscriptModel(
            session_id=session_id,
            user_query=user_query,
            agent_type=agent_type,
            connector_ids=connector_ids or [],
            status="running",
            created_at=datetime.now(tz=UTC),
        )
        self.session.add(transcript)
        await self.session.flush()
        logger.debug(f"Created transcript {transcript.id} for session {session_id}")
        return transcript

    async def add_event(
        self,
        transcript_id: UUID,
        event: DetailedEvent,
    ) -> TranscriptEventModel:
        """Add a single event to a transcript.

        Args:
            transcript_id: The transcript to add the event to.
            event: The DetailedEvent to persist.

        Returns:
            The created TranscriptEventModel.
        """
        # Extract duration from details if present
        duration_ms = None
        if event.details.llm_duration_ms is not None:
            duration_ms = event.details.llm_duration_ms
        elif event.details.tool_duration_ms is not None:
            duration_ms = event.details.tool_duration_ms
        elif event.details.http_duration_ms is not None:
            duration_ms = event.details.http_duration_ms
        elif event.details.sql_duration_ms is not None:
            duration_ms = event.details.sql_duration_ms

        db_event = TranscriptEventModel(
            id=UUID(event.id) if isinstance(event.id, str) else event.id,
            transcript_id=transcript_id,
            session_id=UUID(event.session_id) if event.session_id else None,
            timestamp=event.timestamp,
            type=event.type,
            summary=event.summary,
            details=event.details.to_dict(),
            parent_event_id=(UUID(event.parent_event_id) if event.parent_event_id else None),
            step_number=event.step_number,
            node_name=event.node_name,
            agent_name=event.agent_name,
            duration_ms=duration_ms,
        )
        self.session.add(db_event)
        await self.session.flush()
        return db_event

    async def batch_add_events(
        self,
        transcript_id: UUID,
        events: list[DetailedEvent],
    ) -> None:
        """Add multiple events to a transcript in a single batch.

        This is more efficient than adding events one by one when
        processing a buffer of events.

        Args:
            transcript_id: The transcript to add events to.
            events: List of DetailedEvents to persist.
        """
        if not events:
            return

        db_events = []
        for event in events:
            # Extract duration from details if present
            duration_ms = None
            if event.details.llm_duration_ms is not None:
                duration_ms = event.details.llm_duration_ms
            elif event.details.tool_duration_ms is not None:
                duration_ms = event.details.tool_duration_ms
            elif event.details.http_duration_ms is not None:
                duration_ms = event.details.http_duration_ms
            elif event.details.sql_duration_ms is not None:
                duration_ms = event.details.sql_duration_ms

            db_event = TranscriptEventModel(
                id=UUID(event.id) if isinstance(event.id, str) else event.id,
                transcript_id=transcript_id,
                session_id=UUID(event.session_id) if event.session_id else None,
                timestamp=event.timestamp,
                type=event.type,
                summary=event.summary,
                details=event.details.to_dict(),
                parent_event_id=(UUID(event.parent_event_id) if event.parent_event_id else None),
                step_number=event.step_number,
                node_name=event.node_name,
                agent_name=event.agent_name,
                duration_ms=duration_ms,
            )
            db_events.append(db_event)

        self.session.add_all(db_events)
        await self.session.flush()
        logger.debug(f"Batch added {len(db_events)} events to transcript {transcript_id}")

    async def update_stats(
        self,
        transcript_id: UUID,
        llm_calls: int = 0,
        sql_queries: int = 0,
        http_calls: int = 0,  # operation calls (param name kept for caller compat)
        tool_calls: int = 0,
        tokens: int = 0,
        cost_usd: float | None = None,
        duration_ms: float = 0,
    ) -> None:
        """Increment transcript statistics.

        Uses a single UPDATE statement for efficiency. Cost is handled
        using COALESCE to properly accumulate from None initial value.

        Args:
            transcript_id: The transcript to update.
            llm_calls: Number of LLM calls to add.
            sql_queries: Number of SQL queries to add.
            http_calls: Number of operation calls to add.
            tool_calls: Number of tool calls to add.
            tokens: Number of tokens to add.
            cost_usd: Cost to add (will be accumulated).
            duration_ms: Duration to add.
        """
        from sqlalchemy import func

        # Build values dict for single UPDATE
        values = {
            "total_llm_calls": SessionTranscriptModel.total_llm_calls + llm_calls,
            "total_sql_queries": SessionTranscriptModel.total_sql_queries + sql_queries,
            "total_operation_calls": SessionTranscriptModel.total_operation_calls + http_calls,
            "total_tool_calls": SessionTranscriptModel.total_tool_calls + tool_calls,
            "total_tokens": SessionTranscriptModel.total_tokens + tokens,
            "total_duration_ms": SessionTranscriptModel.total_duration_ms + duration_ms,
        }

        # Include cost in same UPDATE using COALESCE to handle NULL
        if cost_usd is not None:
            values["total_cost_usd"] = (
                func.coalesce(SessionTranscriptModel.total_cost_usd, 0.0) + cost_usd
            )

        stmt = (
            update(SessionTranscriptModel)
            .where(SessionTranscriptModel.id == transcript_id)
            .values(**values)
        )
        await self.session.execute(stmt)

    async def complete_transcript(
        self,
        transcript_id: UUID,
        status: str = "completed",
    ) -> None:
        """Mark a transcript as completed.

        Calculates final duration and updates status.

        Args:
            transcript_id: The transcript to complete.
            status: Final status (completed, failed).
        """
        now = datetime.now(tz=UTC)
        stmt = (
            update(SessionTranscriptModel)
            .where(SessionTranscriptModel.id == transcript_id)
            .values(
                completed_at=now,
                status=status,
            )
        )
        await self.session.execute(stmt)
        logger.debug(f"Completed transcript {transcript_id} with status {status}")

    async def get_transcript(
        self,
        session_id: UUID,
    ) -> SessionTranscriptModel | None:
        """Get the transcript for a session.

        Args:
            session_id: The chat session ID.

        Returns:
            The SessionTranscriptModel if found, None otherwise.
            If multiple transcripts exist (data integrity issue), returns the most recent.
        """
        stmt = (
            select(SessionTranscriptModel)
            .where(SessionTranscriptModel.session_id == session_id)
            .order_by(SessionTranscriptModel.created_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_transcript_by_id(
        self,
        transcript_id: UUID,
    ) -> SessionTranscriptModel | None:
        """Get a transcript by its ID.

        Args:
            transcript_id: The transcript ID.

        Returns:
            The SessionTranscriptModel if found, None otherwise.
        """
        stmt = select(SessionTranscriptModel).where(SessionTranscriptModel.id == transcript_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_transcripts_for_session(
        self,
        session_id: UUID,
    ) -> list[SessionTranscriptModel]:
        """Get all transcripts for a session, ordered by created_at DESC.

        Args:
            session_id: The chat session ID.

        Returns:
            List of SessionTranscriptModel for the session.
        """
        stmt = (
            select(SessionTranscriptModel)
            .where(SessionTranscriptModel.session_id == session_id)
            .order_by(SessionTranscriptModel.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_events(
        self,
        transcript_id: UUID,
        event_types: list[str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[TranscriptEventModel]:
        """Get events for a transcript.

        Args:
            transcript_id: The transcript ID.
            event_types: Optional filter for specific event types.
            limit: Maximum number of events to return.
            offset: Number of events to skip (for pagination).

        Returns:
            List of TranscriptEventModel ordered by timestamp.
        """
        builder = EventQueryBuilder().by_transcript(transcript_id).order_by_timestamp()

        if event_types:
            builder = builder.with_types(event_types)

        builder = builder.paginate(limit=limit, offset=offset)
        stmt = builder.build()

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_events_by_session(
        self,
        session_id: UUID,
        event_types: list[str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[TranscriptEventModel]:
        """Get events for a session (via denormalized session_id).

        Args:
            session_id: The chat session ID.
            event_types: Optional filter for specific event types.
            limit: Maximum number of events to return.
            offset: Number of events to skip (for pagination).

        Returns:
            List of TranscriptEventModel ordered by timestamp.
        """
        builder = EventQueryBuilder().by_session(session_id).order_by_timestamp()

        if event_types:
            builder = builder.with_types(event_types)

        builder = builder.paginate(limit=limit, offset=offset)
        stmt = builder.build()

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_event_by_id(
        self,
        event_id: UUID,
    ) -> TranscriptEventModel | None:
        """Get a single event by ID.

        Args:
            event_id: The event ID.

        Returns:
            The TranscriptEventModel if found, None otherwise.
        """
        stmt = select(TranscriptEventModel).where(TranscriptEventModel.id == event_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_llm_calls(
        self,
        session_id: UUID,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[TranscriptEventModel]:
        """Get all LLM call events for a session.

        Args:
            session_id: The chat session ID.
            limit: Maximum number of events to return.
            offset: Number of events to skip (for pagination).

        Returns:
            List of events with LLM call details.
        """
        return await self.get_events_by_session(
            session_id,
            event_types=["thought", "llm_call"],
            limit=limit,
            offset=offset,
        )

    async def get_operation_calls(
        self,
        session_id: UUID,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[TranscriptEventModel]:
        """Get all operation call events for a session.

        Args:
            session_id: The chat session ID.
            limit: Maximum number of events to return.
            offset: Number of events to skip (for pagination).

        Returns:
            List of events with operation call details.
        """
        return await self.get_events_by_session(
            session_id,
            event_types=["operation_call", "observation"],
            limit=limit,
            offset=offset,
        )

    async def get_sql_queries(
        self,
        session_id: UUID,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[TranscriptEventModel]:
        """Get all SQL query events for a session.

        Args:
            session_id: The chat session ID.
            limit: Maximum number of events to return.
            offset: Number of events to skip (for pagination).

        Returns:
            List of events with SQL query details.
        """
        return await self.get_events_by_session(
            session_id,
            event_types=["sql_query"],
            limit=limit,
            offset=offset,
        )

    async def delete_transcript(
        self,
        transcript_id: UUID,
    ) -> bool:
        """Delete a transcript and all its events.

        Args:
            transcript_id: The transcript to delete.

        Returns:
            True if deleted, False if not found.
        """
        transcript = await self.get_transcript_by_id(transcript_id)
        if transcript is None:
            return False

        await self.session.delete(transcript)
        await self.session.flush()
        logger.debug(f"Deleted transcript {transcript_id}")
        return True
