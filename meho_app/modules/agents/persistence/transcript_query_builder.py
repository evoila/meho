# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Query builder for transcript searches.

This module provides a builder class for constructing SQLAlchemy queries
for transcript and event searches. Separating query building from service
logic makes it easier to:
1. Test query construction independently
2. Reuse common query patterns
3. Add complex query features without bloating the service

Example:
    >>> from meho_app.modules.agents.persistence.transcript_query_builder import (
    ...     TranscriptQueryBuilder,
    ... )
    >>> stmt = (
    ...     TranscriptQueryBuilder.events_query()
    ...     .by_session(session_id)
    ...     .with_types(["thought", "action"])
    ...     .paginate(limit=50, offset=0)
    ...     .build()
    ... )
    >>> result = await session.execute(stmt)
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, select

from meho_app.modules.agents.persistence.transcript_models import (
    SessionTranscriptModel,
    TranscriptEventModel,
)


class EventQueryBuilder:
    """Builder for transcript event queries.

    Provides a fluent interface for constructing event queries
    with various filters and pagination.
    """

    def __init__(self) -> None:
        """Initialize the query builder with a base events query."""
        self._stmt: Select = select(TranscriptEventModel)
        self._ordered = False

    def by_transcript(self, transcript_id: UUID) -> EventQueryBuilder:
        """Filter events by transcript ID.

        Args:
            transcript_id: The transcript ID to filter by.

        Returns:
            Self for method chaining.
        """
        self._stmt = self._stmt.where(TranscriptEventModel.transcript_id == transcript_id)
        return self

    def by_session(self, session_id: UUID) -> EventQueryBuilder:
        """Filter events by session ID (denormalized).

        Args:
            session_id: The session ID to filter by.

        Returns:
            Self for method chaining.
        """
        self._stmt = self._stmt.where(TranscriptEventModel.session_id == session_id)
        return self

    def with_types(self, event_types: list[str]) -> EventQueryBuilder:
        """Filter events by type.

        Args:
            event_types: List of event types to include.

        Returns:
            Self for method chaining.
        """
        if event_types:
            self._stmt = self._stmt.where(TranscriptEventModel.type.in_(event_types))
        return self

    def order_by_timestamp(self, descending: bool = False) -> EventQueryBuilder:
        """Order events by timestamp.

        Args:
            descending: If True, order descending (newest first).

        Returns:
            Self for method chaining.
        """
        if descending:
            self._stmt = self._stmt.order_by(TranscriptEventModel.timestamp.desc())
        else:
            self._stmt = self._stmt.order_by(TranscriptEventModel.timestamp)
        self._ordered = True
        return self

    def paginate(
        self,
        limit: int | None = None,
        offset: int | None = None,
    ) -> EventQueryBuilder:
        """Apply pagination to the query.

        Args:
            limit: Maximum number of results.
            offset: Number of results to skip.

        Returns:
            Self for method chaining.
        """
        if offset:
            self._stmt = self._stmt.offset(offset)
        if limit:
            self._stmt = self._stmt.limit(limit)
        return self

    def build(self) -> Select:
        """Build and return the final query.

        Applies default ordering if not already set.

        Returns:
            SQLAlchemy Select statement.
        """
        if not self._ordered:
            self._stmt = self._stmt.order_by(TranscriptEventModel.timestamp)
        return self._stmt


class TranscriptQueryBuilder:
    """Builder for session transcript queries.

    Provides a fluent interface for constructing transcript queries
    with various filters and pagination.
    """

    def __init__(self) -> None:
        """Initialize the query builder with a base transcripts query."""
        self._stmt: Select = select(SessionTranscriptModel)
        self._ordered = False

    def by_session(self, session_id: UUID) -> TranscriptQueryBuilder:
        """Filter transcripts by session ID.

        Args:
            session_id: The session ID to filter by.

        Returns:
            Self for method chaining.
        """
        self._stmt = self._stmt.where(SessionTranscriptModel.session_id == session_id)
        return self

    def by_id(self, transcript_id: UUID) -> TranscriptQueryBuilder:
        """Filter transcripts by ID.

        Args:
            transcript_id: The transcript ID to filter by.

        Returns:
            Self for method chaining.
        """
        self._stmt = self._stmt.where(SessionTranscriptModel.id == transcript_id)
        return self

    def with_status(self, status: str | list[str]) -> TranscriptQueryBuilder:
        """Filter transcripts by status.

        Args:
            status: Status or list of statuses to include.

        Returns:
            Self for method chaining.
        """
        if isinstance(status, str):
            self._stmt = self._stmt.where(SessionTranscriptModel.status == status)
        else:
            self._stmt = self._stmt.where(SessionTranscriptModel.status.in_(status))
        return self

    def not_deleted(self) -> TranscriptQueryBuilder:
        """Exclude soft-deleted transcripts.

        Returns:
            Self for method chaining.
        """
        self._stmt = self._stmt.where(SessionTranscriptModel.deleted_at.is_(None))
        return self

    def deleted_before(self, before: datetime) -> TranscriptQueryBuilder:
        """Filter for transcripts deleted before a date.

        Args:
            before: Only include transcripts deleted before this time.

        Returns:
            Self for method chaining.
        """
        self._stmt = self._stmt.where(
            SessionTranscriptModel.deleted_at.isnot(None),
            SessionTranscriptModel.deleted_at < before,
        )
        return self

    def completed_before(self, before: datetime) -> TranscriptQueryBuilder:
        """Filter for transcripts completed before a date.

        Args:
            before: Only include transcripts completed before this time.

        Returns:
            Self for method chaining.
        """
        self._stmt = self._stmt.where(
            SessionTranscriptModel.completed_at.isnot(None),
            SessionTranscriptModel.completed_at < before,
        )
        return self

    def order_by_created(self, descending: bool = True) -> TranscriptQueryBuilder:
        """Order transcripts by creation date.

        Args:
            descending: If True, order descending (newest first).

        Returns:
            Self for method chaining.
        """
        if descending:
            self._stmt = self._stmt.order_by(SessionTranscriptModel.created_at.desc())
        else:
            self._stmt = self._stmt.order_by(SessionTranscriptModel.created_at)
        self._ordered = True
        return self

    def paginate(
        self,
        limit: int | None = None,
        offset: int | None = None,
    ) -> TranscriptQueryBuilder:
        """Apply pagination to the query.

        Args:
            limit: Maximum number of results.
            offset: Number of results to skip.

        Returns:
            Self for method chaining.
        """
        if offset:
            self._stmt = self._stmt.offset(offset)
        if limit:
            self._stmt = self._stmt.limit(limit)
        return self

    def build(self) -> Select:
        """Build and return the final query.

        Applies default ordering if not already set.

        Returns:
            SQLAlchemy Select statement.
        """
        if not self._ordered:
            self._stmt = self._stmt.order_by(SessionTranscriptModel.created_at.desc())
        return self._stmt

    @staticmethod
    def events() -> EventQueryBuilder:
        """Create an event query builder.

        Returns:
            EventQueryBuilder instance.
        """
        return EventQueryBuilder()
