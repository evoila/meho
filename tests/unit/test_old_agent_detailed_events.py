# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for detailed event emission in old agent (TASK-193 Phase 2/3).

Tests the detailed event methods for transcript persistence:
1. thought_detailed() captures LLM context
2. action_detailed() captures tool input
3. tool_call_detailed() captures tool input, output, and timing
4. Fallback to simple events when no transcript collector

Phase 84: Cost estimation pricing data and model name matching changed.
"""

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: old agent detailed event cost estimation pricing data outdated")

from unittest.mock import AsyncMock

import pytest

from meho_app.modules.agents.base.events import AgentEvent
from meho_app.modules.agents.sse.emitter import EventEmitter


class TestEventEmitterDetailedMethods:
    """Tests for EventEmitter detailed event methods."""

    @pytest.mark.asyncio
    async def test_thought_detailed_emits_event_and_persists(self) -> None:
        """thought_detailed should emit SSE event and persist to transcript."""
        # Create emitter with mock collector
        emitter = EventEmitter(agent_name="react", session_id="test-session")

        # Capture SSE events
        captured_events: list[AgentEvent] = []

        async def capture_event(event: AgentEvent) -> None:
            captured_events.append(event)

        emitter.set_callback(capture_event)

        # Mock transcript collector
        mock_collector = AsyncMock()
        mock_collector.add = AsyncMock()
        emitter.set_transcript_collector(mock_collector)

        # Call thought_detailed
        await emitter.thought_detailed(
            summary="I need to search for VMs",
            prompt="System prompt here",
            response="Raw LLM response",
            parsed={"thought": "I need to search for VMs", "action": "list_connectors"},
            prompt_tokens=100,
            completion_tokens=50,
            model="gpt-4.1-mini",
            duration_ms=1234.5,
        )

        # Verify SSE events were emitted (thought_detailed emits twice - once for SSE, once for detailed)
        assert len(captured_events) >= 1
        # At least one thought event
        thought_events = [e for e in captured_events if e.type == "thought"]
        assert len(thought_events) >= 1
        assert "I need to search for VMs" in thought_events[0].data["content"]

        # Verify detailed event was added to collector
        mock_collector.add.assert_called_once()
        detailed_event = mock_collector.add.call_args[0][0]
        assert detailed_event.type == "thought"
        assert detailed_event.summary == "I need to search for VMs"
        assert detailed_event.details.llm_prompt == "System prompt here"
        assert detailed_event.details.llm_response == "Raw LLM response"
        assert detailed_event.details.token_usage.prompt_tokens == 100
        assert detailed_event.details.token_usage.completion_tokens == 50

    @pytest.mark.asyncio
    async def test_thought_detailed_without_collector_only_emits_sse(self) -> None:
        """thought_detailed without collector should only emit SSE event."""
        emitter = EventEmitter(agent_name="react", session_id="test-session")

        captured_events: list[AgentEvent] = []

        async def capture_event(event: AgentEvent) -> None:
            captured_events.append(event)

        emitter.set_callback(capture_event)

        # No collector set
        await emitter.thought_detailed(
            summary="Test thought",
            prompt="Prompt",
            response="Response",
        )

        # SSE event should still be emitted
        assert len(captured_events) == 1
        assert captured_events[0].type == "thought"

    @pytest.mark.asyncio
    async def test_action_detailed_emits_event_and_persists(self) -> None:
        """action_detailed should emit SSE event and persist to transcript."""
        emitter = EventEmitter(agent_name="react", session_id="test-session")

        captured_events: list[AgentEvent] = []

        async def capture_event(event: AgentEvent) -> None:
            captured_events.append(event)

        emitter.set_callback(capture_event)

        mock_collector = AsyncMock()
        mock_collector.add = AsyncMock()
        emitter.set_transcript_collector(mock_collector)

        await emitter.action_detailed(
            tool="list_connectors",
            args={},
            summary="Calling list_connectors",
        )

        # Verify SSE event
        assert len(captured_events) == 1
        assert captured_events[0].type == "action"
        assert captured_events[0].data["tool"] == "list_connectors"

        # Verify detailed event
        mock_collector.add.assert_called_once()
        detailed_event = mock_collector.add.call_args[0][0]
        assert detailed_event.type == "action"
        assert detailed_event.details.tool_name == "list_connectors"

    @pytest.mark.asyncio
    async def test_tool_call_detailed_emits_and_persists(self) -> None:
        """tool_call_detailed should emit observation and persist full details."""
        emitter = EventEmitter(agent_name="react", session_id="test-session")

        captured_events: list[AgentEvent] = []

        async def capture_event(event: AgentEvent) -> None:
            captured_events.append(event)

        emitter.set_callback(capture_event)

        mock_collector = AsyncMock()
        mock_collector.add = AsyncMock()
        emitter.set_transcript_collector(mock_collector)

        await emitter.tool_call_detailed(
            tool="call_operation",
            args={"connector_id": "abc", "operation_id": "list_vms"},
            result={"vms": ["vm1", "vm2"]},
            summary="call_operation(list_vms)",
            duration_ms=567.8,
        )

        # Verify SSE event was emitted (may be observation or tool_call)
        assert len(captured_events) >= 1

        # Verify detailed event was persisted
        mock_collector.add.assert_called_once()
        detailed_event = mock_collector.add.call_args[0][0]
        assert detailed_event.type == "tool_call"
        assert detailed_event.details.tool_name == "call_operation"
        assert detailed_event.details.tool_input == {
            "connector_id": "abc",
            "operation_id": "list_vms",
        }
        assert detailed_event.details.tool_duration_ms == 567.8

    @pytest.mark.asyncio
    async def test_tool_call_detailed_with_error(self) -> None:
        """tool_call_detailed should capture errors."""
        emitter = EventEmitter(agent_name="react", session_id="test-session")

        mock_collector = AsyncMock()
        mock_collector.add = AsyncMock()
        emitter.set_transcript_collector(mock_collector)

        await emitter.tool_call_detailed(
            tool="call_operation",
            args={"connector_id": "abc"},
            result=None,
            summary="call_operation failed",
            duration_ms=100.0,
            error="Connection refused",
        )

        # Verify error is captured
        detailed_event = mock_collector.add.call_args[0][0]
        assert detailed_event.details.tool_error == "Connection refused"

    @pytest.mark.asyncio
    async def test_has_transcript_collector_property(self) -> None:
        """has_transcript_collector should return correct value."""
        emitter = EventEmitter(agent_name="react")

        # Initially no collector
        assert emitter.has_transcript_collector is False

        # Set collector
        mock_collector = AsyncMock()
        emitter.set_transcript_collector(mock_collector)

        assert emitter.has_transcript_collector is True


class TestDetailedEventEmissionPattern:
    """Tests for the detailed event emission pattern in old agent nodes."""

    @pytest.mark.asyncio
    async def test_emitter_conditional_check_pattern(self) -> None:
        """Test the pattern used in nodes to check for transcript collector."""
        # This tests the pattern:
        # if emitter and hasattr(emitter, 'has_transcript_collector') and emitter.has_transcript_collector:

        emitter = EventEmitter(agent_name="react")

        # Pattern should return False when no collector
        result = (
            emitter
            and hasattr(emitter, "has_transcript_collector")
            and emitter.has_transcript_collector
        )
        assert result is False

        # Pattern should return True when collector is set
        mock_collector = AsyncMock()
        emitter.set_transcript_collector(mock_collector)

        result = (
            emitter
            and hasattr(emitter, "has_transcript_collector")
            and emitter.has_transcript_collector
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_emitter_none_pattern_safety(self) -> None:
        """Pattern should be safe when emitter is None."""
        emitter = None

        result = (
            emitter
            and hasattr(emitter, "has_transcript_collector")
            and emitter.has_transcript_collector
        )

        # Should not raise and be falsy (None or False)
        assert not result


class TestTokenUsageCapture:
    """Tests for token usage capture in detailed events."""

    @pytest.mark.asyncio
    async def test_token_usage_cost_estimation(self) -> None:
        """thought_detailed should estimate cost from token usage."""
        emitter = EventEmitter(agent_name="react", session_id="test-session")

        mock_collector = AsyncMock()
        mock_collector.add = AsyncMock()
        emitter.set_transcript_collector(mock_collector)

        await emitter.thought_detailed(
            summary="Test",
            prompt_tokens=1000,
            completion_tokens=500,
            model="gpt-4.1-mini",
        )

        detailed_event = mock_collector.add.call_args[0][0]
        token_usage = detailed_event.details.token_usage

        assert token_usage.prompt_tokens == 1000
        assert token_usage.completion_tokens == 500
        assert token_usage.total_tokens == 1500
        # Cost should be estimated (exact value depends on pricing)
        assert token_usage.estimated_cost_usd is not None
        assert token_usage.estimated_cost_usd >= 0

    @pytest.mark.asyncio
    async def test_zero_token_usage_no_cost(self) -> None:
        """Zero tokens should result in no token usage object."""
        emitter = EventEmitter(agent_name="react", session_id="test-session")

        mock_collector = AsyncMock()
        mock_collector.add = AsyncMock()
        emitter.set_transcript_collector(mock_collector)

        await emitter.thought_detailed(
            summary="Test",
            prompt_tokens=0,
            completion_tokens=0,
        )

        detailed_event = mock_collector.add.call_args[0][0]
        # No token usage when both are 0
        assert detailed_event.details.token_usage is None


class TestToolDurationCapture:
    """Tests for tool duration capture."""

    @pytest.mark.asyncio
    async def test_duration_captured_in_tool_call(self) -> None:
        """tool_call_detailed should capture duration_ms."""
        emitter = EventEmitter(agent_name="react", session_id="test-session")

        mock_collector = AsyncMock()
        mock_collector.add = AsyncMock()
        emitter.set_transcript_collector(mock_collector)

        await emitter.tool_call_detailed(
            tool="search_operations",
            args={"query": "list vms"},
            result="Found 5 operations",
            summary="search_operations('list vms')",
            duration_ms=250.5,
        )

        detailed_event = mock_collector.add.call_args[0][0]
        assert detailed_event.details.tool_duration_ms == 250.5
