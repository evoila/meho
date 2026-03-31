# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for TranscriptCollector.

Tests the async buffering and event collection logic.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from meho_app.modules.agents.base.detailed_events import DetailedEvent, EventDetails


@pytest.fixture
def mock_service():
    """Create a mock TranscriptService."""
    service = MagicMock()
    service.batch_add_events = AsyncMock()
    service.update_stats = AsyncMock()
    service.complete_transcript = AsyncMock()
    return service


@pytest.fixture
def collector(mock_service):
    """Create a TranscriptCollector with mocked service."""
    from meho_app.modules.agents.persistence.transcript_collector import TranscriptCollector

    return TranscriptCollector(
        transcript_id=uuid4(),
        session_id=uuid4(),
        service=mock_service,
        buffer_size=5,  # Small buffer for testing
    )


class TestTranscriptCollectorAdd:
    """Tests for adding events to the collector."""

    @pytest.mark.asyncio
    async def test_add_event_buffers(self, collector, mock_service):
        """Test that events are buffered before flush."""
        event = DetailedEvent.create(
            event_type="thought",
            summary="Test thought",
        )

        await collector.add(event)

        # Event should be buffered, not yet flushed
        mock_service.batch_add_events.assert_not_called()
        assert len(collector._buffer) == 1

    @pytest.mark.asyncio
    async def test_add_event_auto_flush(self, collector, mock_service):
        """Test that buffer is flushed when full."""
        # Add events up to buffer size (5)
        for i in range(5):
            event = DetailedEvent.create(
                event_type="thought",
                summary=f"Thought {i}",
            )
            await collector.add(event)

        # Should have flushed
        mock_service.batch_add_events.assert_called_once()
        assert len(collector._buffer) == 0

    @pytest.mark.asyncio
    async def test_add_sets_session_id(self, collector):
        """Test that session_id is set on events."""
        event = DetailedEvent.create(
            event_type="action",
            summary="Test action",
            session_id=None,  # No session_id
        )

        await collector.add(event)

        # Session ID should be set from collector
        assert event.session_id == str(collector.session_id)


class TestTranscriptCollectorStats:
    """Tests for statistics tracking."""

    @pytest.mark.asyncio
    async def test_llm_call_stats(self, collector):
        """Test LLM call stats are tracked."""
        from meho_app.modules.agents.base.detailed_events import TokenUsage

        event = DetailedEvent.create(
            event_type="thought",
            summary="LLM reasoning",
            details=EventDetails(
                llm_prompt="System prompt",
                llm_response="Response",
                token_usage=TokenUsage(
                    prompt_tokens=100,
                    completion_tokens=50,
                    total_tokens=150,
                    estimated_cost_usd=0.001,
                ),
            ),
        )

        await collector.add(event)

        assert collector._llm_calls == 1
        assert collector._total_tokens == 150
        assert collector._total_cost_usd == 0.001

    @pytest.mark.asyncio
    async def test_operation_call_stats(self, collector):
        """Test operation call stats are tracked."""
        event = DetailedEvent.create(
            event_type="operation_call",
            summary="API call",
            details=EventDetails(
                http_url="https://api.example.com",
                http_method="GET",
            ),
        )

        await collector.add(event)

        assert collector._operation_calls == 1

    @pytest.mark.asyncio
    async def test_sql_query_stats(self, collector):
        """Test SQL query stats are tracked."""
        event = DetailedEvent.create(
            event_type="sql_query",
            summary="Database query",
            details=EventDetails(
                sql_query="SELECT * FROM users",
            ),
        )

        await collector.add(event)

        assert collector._sql_queries == 1

    @pytest.mark.asyncio
    async def test_tool_call_stats(self, collector):
        """Test tool call stats are tracked."""
        event = DetailedEvent.create(
            event_type="observation",
            summary="Tool result",
            details=EventDetails(
                tool_name="search_operations",
            ),
        )

        await collector.add(event)

        assert collector._tool_calls == 1


class TestTranscriptCollectorFlush:
    """Tests for manual flush."""

    @pytest.mark.asyncio
    async def test_manual_flush(self, collector, mock_service):
        """Test manual flush persists buffered events."""
        # Add some events
        for i in range(3):
            event = DetailedEvent.create(
                event_type="thought",
                summary=f"Thought {i}",
            )
            await collector.add(event)

        # Manually flush
        await collector.flush()

        mock_service.batch_add_events.assert_called_once()
        assert len(collector._buffer) == 0

    @pytest.mark.asyncio
    async def test_flush_empty_buffer(self, collector, mock_service):
        """Test flush with empty buffer does nothing."""
        await collector.flush()

        mock_service.batch_add_events.assert_not_called()


class TestTranscriptCollectorClose:
    """Tests for closing the collector."""

    @pytest.mark.asyncio
    async def test_close_flushes_buffer(self, collector, mock_service):
        """Test close flushes remaining events."""
        event = DetailedEvent.create(
            event_type="thought",
            summary="Test thought",
        )
        await collector.add(event)

        await collector.close()

        mock_service.batch_add_events.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_updates_stats(self, collector, mock_service):
        """Test close updates transcript stats."""
        await collector.close()

        mock_service.update_stats.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_completes_transcript(self, collector, mock_service):
        """Test close marks transcript as complete."""
        await collector.close()

        mock_service.complete_transcript.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_with_failed_status(self, collector, mock_service):
        """Test close with failed status."""
        await collector.close(status="failed")

        mock_service.complete_transcript.assert_called_once_with(
            collector.transcript_id,
            status="failed",
        )

    @pytest.mark.asyncio
    async def test_close_idempotent(self, collector, mock_service):
        """Test close can be called multiple times."""
        await collector.close()
        await collector.close()

        # Should only complete once
        assert mock_service.complete_transcript.call_count == 1

    @pytest.mark.asyncio
    async def test_add_after_close_ignored(self, collector, mock_service):
        """Test adding events after close is ignored."""
        await collector.close()

        event = DetailedEvent.create(
            event_type="thought",
            summary="Late event",
        )
        await collector.add(event)

        # Should not be added to buffer
        assert len(collector._buffer) == 0


class TestTranscriptCollectorContextManager:
    """Tests for async context manager usage."""

    @pytest.mark.asyncio
    async def test_context_manager_success(self, mock_service):
        """Test context manager closes collector on success."""
        from meho_app.modules.agents.persistence.transcript_collector import TranscriptCollector

        collector = TranscriptCollector(
            transcript_id=uuid4(),
            session_id=uuid4(),
            service=mock_service,
        )

        async with collector:
            event = DetailedEvent.create(
                event_type="thought",
                summary="Test",
            )
            await collector.add(event)

        mock_service.complete_transcript.assert_called_once_with(
            collector.transcript_id,
            status="completed",
        )

    @pytest.mark.asyncio
    async def test_context_manager_failure(self, mock_service):
        """Test context manager sets failed status on exception."""
        from meho_app.modules.agents.persistence.transcript_collector import TranscriptCollector

        collector = TranscriptCollector(
            transcript_id=uuid4(),
            session_id=uuid4(),
            service=mock_service,
        )

        with pytest.raises(ValueError):  # noqa: PT011 -- test validates exception type is sufficient
            async with collector:
                raise ValueError("Test error")

        mock_service.complete_transcript.assert_called_once_with(
            collector.transcript_id,
            status="failed",
        )


class TestTranscriptCollectorEventHelpers:
    """Tests for convenience event creation methods."""

    @pytest.mark.asyncio
    async def test_create_llm_event(self, collector):
        """Test create_llm_event helper."""
        event = collector.create_llm_event(
            summary="LLM reasoning",
            prompt="System prompt",
            response="LLM response",
            prompt_tokens=100,
            completion_tokens=50,
            model="gpt-4.1-mini",
            duration_ms=1234.5,
        )

        assert event.type == "thought"
        assert event.summary == "LLM reasoning"
        assert event.details.llm_prompt == "System prompt"
        assert event.details.llm_response == "LLM response"
        assert event.details.token_usage is not None
        assert event.details.token_usage.total_tokens == 150
        assert event.details.model == "gpt-4.1-mini"
        assert event.details.llm_duration_ms == 1234.5

    @pytest.mark.asyncio
    async def test_create_tool_event(self, collector):
        """Test create_tool_event helper."""
        event = collector.create_tool_event(
            summary="Tool result",
            tool_name="search_operations",
            tool_input={"connector_id": "abc"},
            tool_output={"results": []},
            duration_ms=100.0,
        )

        assert event.type == "observation"
        assert event.details.tool_name == "search_operations"
        assert event.details.tool_input["connector_id"] == "abc"
        assert event.details.tool_output["results"] == []
        assert event.details.tool_duration_ms == 100.0

    @pytest.mark.asyncio
    async def test_create_operation_event(self, collector):
        """Test create_operation_event helper."""
        event = collector.create_operation_event(
            summary="API call",
            method="GET",
            url="https://api.example.com/resource",
            status_code=200,
            duration_ms=342.1,
        )

        assert event.type == "operation_call"
        assert event.details.http_method == "GET"
        assert event.details.http_url == "https://api.example.com/resource"
        assert event.details.http_status_code == 200
        assert event.details.http_duration_ms == 342.1

    @pytest.mark.asyncio
    async def test_create_sql_event(self, collector):
        """Test create_sql_event helper."""
        event = collector.create_sql_event(
            summary="Database query",
            query="SELECT * FROM users WHERE id = :id",
            parameters={"id": 123},
            row_count=5,
            duration_ms=10.5,
        )

        assert event.type == "sql_query"
        assert "SELECT * FROM users" in event.details.sql_query
        assert event.details.sql_parameters["id"] == 123
        assert event.details.sql_row_count == 5
        assert event.details.sql_duration_ms == 10.5


class TestTranscriptCollectorErrorEvent:
    """Tests for add_error_event method (TASK-188)."""

    @pytest.mark.asyncio
    async def test_add_error_event_basic(self, collector, mock_service):
        """Test basic error event creation."""
        await collector.add_error_event("Client disconnected")

        # Event should be buffered
        assert len(collector._buffer) == 1
        event = collector._buffer[0]

        assert event.type == "error"
        assert "Client disconnected" in event.summary
        assert event.details.tool_error == "Client disconnected"

    @pytest.mark.asyncio
    async def test_add_error_event_with_traceback(self, collector, mock_service):
        """Test error event with traceback capture."""
        try:
            raise ValueError("Test error")
        except ValueError:
            await collector.add_error_event("Test error", include_traceback=True)

        event = collector._buffer[0]
        assert event.type == "error"
        assert event.details.tool_output is not None
        assert "traceback" in event.details.tool_output
        assert "ValueError" in event.details.tool_output["traceback"]

    @pytest.mark.asyncio
    async def test_add_error_event_sets_session_id(self, collector):
        """Test that error event has session_id set."""
        await collector.add_error_event("Test error")

        event = collector._buffer[0]
        assert event.session_id == str(collector.session_id)

    @pytest.mark.asyncio
    async def test_add_error_event_truncates_long_message(self, collector):
        """Test that long error messages are truncated in summary."""
        long_message = "x" * 200
        await collector.add_error_event(long_message)

        event = collector._buffer[0]
        # Summary should be truncated to 100 chars + "Fatal: " prefix
        assert len(event.summary) <= 108  # "Fatal: " + 100 chars + potential "..."
        # But full message is in tool_error
        assert event.details.tool_error == long_message
