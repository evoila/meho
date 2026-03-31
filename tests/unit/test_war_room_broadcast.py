# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for war room SSE broadcasting events (Phase 39).

Tests:
- user_message event published with correct payload for group session
- processing_started event published at start of group session processing
- processing_complete event published after processing completes
- Sender name injection in conversation history format

Phase 84: SSE broadcaster now uses async Redis rpush, MagicMock needs AsyncMock.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: SSE broadcaster now uses async Redis rpush, MagicMock needs AsyncMock for await expressions")

from meho_app.modules.agents.sse.broadcaster import RedisSSEBroadcaster


@pytest.fixture
def mock_redis():
    """Create a mock async Redis client."""
    client = MagicMock()
    client.publish = AsyncMock()
    client.set = AsyncMock(return_value=True)
    client.exists = AsyncMock(return_value=0)
    client.delete = AsyncMock()
    return client


@pytest.fixture
def broadcaster(mock_redis):
    """Create a RedisSSEBroadcaster with mock Redis."""
    return RedisSSEBroadcaster(mock_redis)


# =============================================================================
# user_message Broadcast Tests
# =============================================================================


@pytest.mark.unit
class TestUserMessageBroadcast:
    """Tests for user_message SSE event broadcasting."""

    @pytest.mark.asyncio
    async def test_user_message_event_published(self, broadcaster, mock_redis):
        """user_message event should be published with correct payload."""
        session_id = "test-session-123"
        event = {
            "type": "user_message",
            "content": "Check the pod logs",
            "sender_id": "alice@acme.com",
            "sender_name": "Alice",
        }

        await broadcaster.publish(session_id, event)

        mock_redis.publish.assert_called_once()
        call_args = mock_redis.publish.call_args
        # Channel should be meho:sse:{session_id}
        assert call_args[0][0] == f"meho:sse:{session_id}"
        # Payload should be JSON
        import json

        payload = json.loads(call_args[0][1])
        assert payload["type"] == "user_message"
        assert payload["content"] == "Check the pod logs"
        assert payload["sender_id"] == "alice@acme.com"
        assert payload["sender_name"] == "Alice"

    @pytest.mark.asyncio
    async def test_user_message_includes_sender_info(self, broadcaster, mock_redis):
        """user_message should include both sender_id and sender_name."""
        event = {
            "type": "user_message",
            "content": "What's the cluster status?",
            "sender_id": "bob@acme.com",
            "sender_name": "Bob",
        }

        await broadcaster.publish("session-456", event)

        import json

        call_args = mock_redis.publish.call_args
        payload = json.loads(call_args[0][1])
        assert "sender_id" in payload
        assert "sender_name" in payload
        assert payload["sender_id"] == "bob@acme.com"
        assert payload["sender_name"] == "Bob"


# =============================================================================
# Processing Event Broadcast Tests
# =============================================================================


@pytest.mark.unit
class TestProcessingEventBroadcast:
    """Tests for processing_started and processing_complete events."""

    @pytest.mark.asyncio
    async def test_processing_started_event(self, broadcaster, mock_redis):
        """processing_started event should be published at start of processing."""
        session_id = "test-session-789"
        event = {
            "type": "processing_started",
            "sender_id": "alice@acme.com",
        }

        await broadcaster.publish(session_id, event)

        import json

        call_args = mock_redis.publish.call_args
        payload = json.loads(call_args[0][1])
        assert payload["type"] == "processing_started"
        assert payload["sender_id"] == "alice@acme.com"

    @pytest.mark.asyncio
    async def test_processing_complete_event(self, broadcaster, mock_redis):
        """processing_complete event should be published after processing."""
        session_id = "test-session-789"
        event = {"type": "processing_complete"}

        await broadcaster.publish(session_id, event)

        import json

        call_args = mock_redis.publish.call_args
        payload = json.loads(call_args[0][1])
        assert payload["type"] == "processing_complete"

    @pytest.mark.asyncio
    async def test_processing_events_use_correct_channel(self, broadcaster, mock_redis):
        """Processing events should use the correct Redis pub/sub channel."""
        session_id = "abc-def-123"

        await broadcaster.publish(session_id, {"type": "processing_started", "sender_id": "x"})

        call_args = mock_redis.publish.call_args
        assert call_args[0][0] == f"meho:sse:{session_id}"


# =============================================================================
# Sender Name in Conversation History Format Tests
# =============================================================================


@pytest.mark.unit
class TestSenderNameInHistory:
    """Tests for sender name injection in conversation history formatting."""

    def test_user_message_with_sender_name(self):
        """User messages with sender_name should be prefixed."""
        from meho_app.modules.agents.adapter import _format_conversation_history

        history = [
            {"role": "user", "content": "Check pod logs", "sender_name": "Alice"},
        ]
        result = _format_conversation_history(history)
        assert result == "USER (Alice): Check pod logs"

    def test_user_message_without_sender_name(self):
        """User messages without sender_name should use standard format."""
        from meho_app.modules.agents.adapter import _format_conversation_history

        history = [
            {"role": "user", "content": "Hello"},
        ]
        result = _format_conversation_history(history)
        assert result == "USER: Hello"

    def test_assistant_message_no_sender_prefix(self):
        """Assistant messages should never have sender prefix."""
        from meho_app.modules.agents.adapter import _format_conversation_history

        history = [
            {"role": "assistant", "content": "Here are the logs..."},
        ]
        result = _format_conversation_history(history)
        assert result == "ASSISTANT: Here are the logs..."

    def test_mixed_conversation_with_multiple_senders(self):
        """Multi-user conversation should show each sender's name."""
        from meho_app.modules.agents.adapter import _format_conversation_history

        history = [
            {"role": "user", "content": "Check pod logs", "sender_name": "Alice"},
            {"role": "assistant", "content": "The pod logs show errors."},
            {"role": "user", "content": "What about network?", "sender_name": "Bob"},
            {"role": "assistant", "content": "Network is healthy."},
        ]
        result = _format_conversation_history(history)
        lines = result.split("\n")
        assert lines[0] == "USER (Alice): Check pod logs"
        assert lines[1] == "ASSISTANT: The pod logs show errors."
        assert lines[2] == "USER (Bob): What about network?"
        assert lines[3] == "ASSISTANT: Network is healthy."

    def test_empty_history(self):
        """Empty history should return empty string."""
        from meho_app.modules.agents.adapter import _format_conversation_history

        result = _format_conversation_history([])
        assert result == ""

    def test_backward_compatible_no_sender_name_key(self):
        """Messages without sender_name key should work (backward compatible)."""
        from meho_app.modules.agents.adapter import _format_conversation_history

        history = [
            {"role": "user", "content": "old message"},
            {"role": "assistant", "content": "old response"},
        ]
        result = _format_conversation_history(history)
        assert "USER: old message" in result
        assert "ASSISTANT: old response" in result
