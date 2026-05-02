# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for TranscriptService.

Tests the service layer with mocked database session.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from meho_app.modules.agents.base.detailed_events import DetailedEvent, EventDetails


@pytest.fixture
def mock_session():
    """Create a mock database session."""
    session = MagicMock()
    session.add = MagicMock()
    session.add_all = MagicMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock()
    session.delete = AsyncMock()
    return session


@pytest.fixture
def transcript_service(mock_session):
    """Create a TranscriptService with mocked session."""
    from meho_app.modules.agents.persistence.transcript_service import TranscriptService

    return TranscriptService(mock_session)


class TestTranscriptServiceCreate:
    """Tests for transcript creation."""

    @pytest.mark.asyncio
    async def test_create_transcript(self, transcript_service, mock_session):
        """Test creating a new transcript."""
        session_id = uuid4()
        user_query = "List all VMs"
        agent_type = "react"

        result = await transcript_service.create_transcript(
            session_id=session_id,
            user_query=user_query,
            agent_type=agent_type,
        )

        # Verify session.add was called
        mock_session.add.assert_called_once()
        mock_session.flush.assert_called_once()

        # Verify transcript properties
        assert result.session_id == session_id
        assert result.user_query == user_query
        assert result.agent_type == agent_type
        assert result.status == "running"

    @pytest.mark.asyncio
    async def test_create_transcript_with_connectors(self, transcript_service, mock_session):
        """Test creating a transcript with connector IDs."""
        session_id = uuid4()
        connector_ids = [uuid4(), uuid4()]

        result = await transcript_service.create_transcript(
            session_id=session_id,
            user_query="Query connectors",
            agent_type="orchestrator",
            connector_ids=connector_ids,
        )

        assert result.connector_ids == connector_ids


class TestTranscriptServiceAddEvent:
    """Tests for adding events to transcripts."""

    @pytest.mark.asyncio
    async def test_add_event(self, transcript_service, mock_session):
        """Test adding a single event."""
        transcript_id = uuid4()
        details = EventDetails(tool_name="test_tool", tool_duration_ms=100.0)
        event = DetailedEvent.create(
            event_type="observation",
            summary="Test observation",
            details=details,
            session_id=str(uuid4()),
        )

        result = await transcript_service.add_event(
            transcript_id=transcript_id,
            event=event,
        )

        mock_session.add.assert_called_once()
        mock_session.flush.assert_called_once()
        assert result.type == "observation"
        assert result.summary == "Test observation"

    @pytest.mark.asyncio
    async def test_add_event_extracts_duration(self, transcript_service, mock_session):
        """Test that duration is extracted from details."""
        transcript_id = uuid4()

        # Test LLM duration
        llm_details = EventDetails(llm_duration_ms=1234.5)
        llm_event = DetailedEvent.create(
            event_type="thought",
            summary="LLM call",
            details=llm_details,
        )

        result = await transcript_service.add_event(transcript_id, llm_event)
        assert result.duration_ms == pytest.approx(1234.5)


class TestTranscriptServiceBatchAdd:
    """Tests for batch adding events."""

    @pytest.mark.asyncio
    async def test_batch_add_events(self, transcript_service, mock_session):
        """Test batch adding multiple events."""
        transcript_id = uuid4()
        events = [
            DetailedEvent.create(event_type="thought", summary=f"Thought {i}") for i in range(5)
        ]

        await transcript_service.batch_add_events(transcript_id, events)

        mock_session.add_all.assert_called_once()
        mock_session.flush.assert_called_once()

        # Verify all 5 events were added
        added_events = mock_session.add_all.call_args[0][0]
        assert len(added_events) == 5

    @pytest.mark.asyncio
    async def test_batch_add_empty_list(self, transcript_service, mock_session):
        """Test batch add with empty list does nothing."""
        await transcript_service.batch_add_events(uuid4(), [])

        mock_session.add_all.assert_not_called()
        mock_session.flush.assert_not_called()


class TestTranscriptServiceComplete:
    """Tests for completing transcripts."""

    @pytest.mark.asyncio
    async def test_complete_transcript(self, transcript_service, mock_session):
        """Test completing a transcript."""
        transcript_id = uuid4()

        await transcript_service.complete_transcript(transcript_id)

        mock_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_complete_transcript_with_failed_status(self, transcript_service, mock_session):
        """Test completing a transcript with failed status."""
        transcript_id = uuid4()

        await transcript_service.complete_transcript(transcript_id, status="failed")

        mock_session.execute.assert_called_once()


class TestTranscriptServiceQuery:
    """Tests for querying transcripts and events."""

    @pytest.mark.asyncio
    async def test_get_transcript(self, transcript_service, mock_session):
        """Test getting a transcript by session ID."""
        session_id = uuid4()

        # Mock the query result
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = MagicMock(
            id=uuid4(),
            session_id=session_id,
            status="completed",
        )
        mock_session.execute.return_value = mock_result

        result = await transcript_service.get_transcript(session_id)

        assert result is not None
        assert result.session_id == session_id

    @pytest.mark.asyncio
    async def test_get_transcript_not_found(self, transcript_service, mock_session):
        """Test getting a non-existent transcript."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        result = await transcript_service.get_transcript(uuid4())

        assert result is None

    @pytest.mark.asyncio
    async def test_get_events(self, transcript_service, mock_session):
        """Test getting events for a transcript."""
        transcript_id = uuid4()

        # Mock the query result
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            MagicMock(type="thought", summary="Test"),
        ]
        mock_session.execute.return_value = mock_result

        result = await transcript_service.get_events(transcript_id)

        assert len(result) == 1
        assert result[0].type == "thought"

    @pytest.mark.asyncio
    async def test_get_events_with_type_filter(self, transcript_service, mock_session):
        """Test getting events with type filter."""
        transcript_id = uuid4()

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        await transcript_service.get_events(
            transcript_id,
            event_types=["thought", "action"],
        )

        mock_session.execute.assert_called_once()
