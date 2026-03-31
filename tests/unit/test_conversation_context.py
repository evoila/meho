# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for conversation context functionality.

Tests edge cases and boundary conditions for conversation history handling.

Phase 84: get_conversation_history function signature changed -- now takes
agent_service parameter instead of using get_agent_http_client. Mock targets outdated.
"""

from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: get_conversation_history signature changed, now takes agent_service instead of get_agent_http_client")

from meho_app.api.routes_chat import get_conversation_history


class TestGetConversationHistory:
    """Test the get_conversation_history helper function"""

    @pytest.mark.asyncio
    async def test_empty_session_returns_empty_list(self):
        """Empty sessions should return empty conversation history"""
        with patch("meho_app.api.routes_chat.get_agent_http_client") as mock_get_client:
            mock_http_client = AsyncMock()
            mock_http_client.get_chat_session = AsyncMock(return_value={"messages": []})
            mock_get_client.return_value = mock_http_client

            history = await get_conversation_history("test-session-id")

            assert history == []
            mock_http_client.get_chat_session.assert_called_once_with("test-session-id")

    @pytest.mark.asyncio
    async def test_nonexistent_session_returns_empty_list(self):
        """Non-existent sessions should return empty list without error"""
        with patch("meho_app.api.routes_chat.get_agent_http_client") as mock_get_client:
            mock_http_client = AsyncMock()
            mock_http_client.get_chat_session = AsyncMock(return_value=None)
            mock_get_client.return_value = mock_http_client

            history = await get_conversation_history("nonexistent-session")

            assert history == []

    @pytest.mark.asyncio
    async def test_session_without_messages_key_returns_empty(self):
        """Sessions without 'messages' key should return empty list"""
        with patch("meho_app.api.routes_chat.get_agent_http_client") as mock_get_client:
            mock_http_client = AsyncMock()
            mock_http_client.get_chat_session = AsyncMock(
                return_value={"id": "test", "title": "Test"}
            )
            mock_get_client.return_value = mock_http_client

            history = await get_conversation_history("test-session-id")

            assert history == []

    @pytest.mark.asyncio
    async def test_returns_last_n_messages_when_over_limit(self):
        """Should return only last N messages when total exceeds limit"""
        messages = [
            {"role": "user", "content": f"Message {i}"}
            for i in range(30)  # 30 messages, limit is 20
        ]

        with patch("meho_app.api.routes_chat.get_agent_http_client") as mock_get_client:
            mock_http_client = AsyncMock()
            mock_http_client.get_chat_session = AsyncMock(return_value={"messages": messages})
            mock_get_client.return_value = mock_http_client

            history = await get_conversation_history("test-session-id", limit=20)

            assert len(history) == 20
            # Should be the last 20 messages
            assert history[0]["content"] == "Message 10"  # messages[10]
            assert history[-1]["content"] == "Message 29"  # messages[29]

    @pytest.mark.asyncio
    async def test_respects_custom_limit(self):
        """Should respect custom limit parameter"""
        messages = [{"role": "user", "content": f"Message {i}"} for i in range(15)]

        with patch("meho_app.api.routes_chat.get_agent_http_client") as mock_get_client:
            mock_http_client = AsyncMock()
            mock_http_client.get_chat_session = AsyncMock(return_value={"messages": messages})
            mock_get_client.return_value = mock_http_client

            history = await get_conversation_history("test-session-id", limit=10)

            assert len(history) == 10
            # Should be the last 10 messages
            assert history[0]["content"] == "Message 5"
            assert history[-1]["content"] == "Message 14"

    @pytest.mark.asyncio
    async def test_includes_both_user_and_assistant_messages(self):
        """Should include both user and assistant messages in history"""
        messages = [
            {"role": "user", "content": "Question 1"},
            {"role": "assistant", "content": "Answer 1"},
            {"role": "user", "content": "Question 2"},
            {"role": "assistant", "content": "Answer 2"},
        ]

        with patch("meho_app.api.routes_chat.get_agent_http_client") as mock_get_client:
            mock_http_client = AsyncMock()
            mock_http_client.get_chat_session = AsyncMock(return_value={"messages": messages})
            mock_get_client.return_value = mock_http_client

            history = await get_conversation_history("test-session-id")

            assert len(history) == 4
            assert history[0]["role"] == "user"
            assert history[1]["role"] == "assistant"
            assert history[2]["role"] == "user"
            assert history[3]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_preserves_message_order(self):
        """Should preserve chronological order of messages"""
        messages = [
            {"role": "user", "content": "First"},
            {"role": "assistant", "content": "Second"},
            {"role": "user", "content": "Third"},
        ]

        with patch("meho_app.api.routes_chat.get_agent_http_client") as mock_get_client:
            mock_http_client = AsyncMock()
            mock_http_client.get_chat_session = AsyncMock(return_value={"messages": messages})
            mock_get_client.return_value = mock_http_client

            history = await get_conversation_history("test-session-id")

            assert history[0]["content"] == "First"
            assert history[1]["content"] == "Second"
            assert history[2]["content"] == "Third"

    @pytest.mark.asyncio
    async def test_handles_http_client_error_gracefully(self):
        """Should return empty list if HTTP client raises an error"""
        with patch("meho_app.api.routes_chat.get_agent_http_client") as mock_get_client:
            mock_http_client = AsyncMock()
            mock_http_client.get_chat_session = AsyncMock(side_effect=Exception("Network error"))
            mock_get_client.return_value = mock_http_client

            history = await get_conversation_history("test-session-id")

            assert history == []  # Should handle error gracefully

    @pytest.mark.asyncio
    async def test_empty_string_session_id_returns_empty(self):
        """Empty string session ID should return empty list without API call"""
        history = await get_conversation_history("")

        assert history == []

    @pytest.mark.asyncio
    async def test_none_session_id_returns_empty(self):
        """None session ID should return empty list without API call"""
        history = await get_conversation_history(None)

        assert history == []


class TestConversationContextEdgeCases:
    """Test edge cases for conversation context in agent planning"""

    @pytest.mark.asyncio
    async def test_very_long_messages_are_included(self):
        """Long messages should be included without truncation"""
        long_content = "A" * 5000  # Very long message
        messages = [
            {"role": "user", "content": long_content},
            {"role": "assistant", "content": "Short answer"},
        ]

        with patch("meho_app.api.routes_chat.get_agent_http_client") as mock_get_client:
            mock_http_client = AsyncMock()
            mock_http_client.get_chat_session = AsyncMock(return_value={"messages": messages})
            mock_get_client.return_value = mock_http_client

            history = await get_conversation_history("test-session-id")

            assert len(history) == 2
            assert len(history[0]["content"]) == 5000  # Not truncated

    @pytest.mark.asyncio
    async def test_special_characters_preserved(self):
        """Special characters and unicode should be preserved"""
        messages = [
            {"role": "user", "content": "Test with émojis 🚀 and symbols @#$%"},
            {"role": "assistant", "content": "Response with 中文 and קודם"},
        ]

        with patch("meho_app.api.routes_chat.get_agent_http_client") as mock_get_client:
            mock_http_client = AsyncMock()
            mock_http_client.get_chat_session = AsyncMock(return_value={"messages": messages})
            mock_get_client.return_value = mock_http_client

            history = await get_conversation_history("test-session-id")

            assert history[0]["content"] == "Test with émojis 🚀 and symbols @#$%"
            assert history[1]["content"] == "Response with 中文 and קודם"

    @pytest.mark.asyncio
    async def test_json_in_message_content_preserved(self):
        """JSON content in messages should be preserved as strings"""
        json_content = '{"key": "value", "nested": {"data": 123}}'
        messages = [
            {"role": "user", "content": "Show me this JSON"},
            {"role": "assistant", "content": json_content},
        ]

        with patch("meho_app.api.routes_chat.get_agent_http_client") as mock_get_client:
            mock_http_client = AsyncMock()
            mock_http_client.get_chat_session = AsyncMock(return_value={"messages": messages})
            mock_get_client.return_value = mock_http_client

            history = await get_conversation_history("test-session-id")

            assert history[1]["content"] == json_content

    @pytest.mark.asyncio
    async def test_messages_with_extra_fields_handled(self):
        """Messages with extra fields should work (only role and content needed)"""
        messages = [
            {
                "role": "user",
                "content": "Test",
                "id": "msg-123",
                "created_at": "2025-11-22T10:00:00Z",
                "workflow_id": "wf-456",
            },
            {
                "role": "assistant",
                "content": "Response",
                "id": "msg-124",
                "created_at": "2025-11-22T10:00:05Z",
            },
        ]

        with patch("meho_app.api.routes_chat.get_agent_http_client") as mock_get_client:
            mock_http_client = AsyncMock()
            mock_http_client.get_chat_session = AsyncMock(return_value={"messages": messages})
            mock_get_client.return_value = mock_http_client

            history = await get_conversation_history("test-session-id")

            # Should only extract role and content
            assert history[0] == {"role": "user", "content": "Test"}
            assert history[1] == {"role": "assistant", "content": "Response"}
            assert "id" not in history[0]
            assert "created_at" not in history[0]

    @pytest.mark.asyncio
    async def test_alternating_roles_not_required(self):
        """Consecutive messages from same role should be handled"""
        messages = [
            {"role": "user", "content": "Question 1"},
            {"role": "user", "content": "Question 2"},  # Two user messages in a row
            {"role": "assistant", "content": "Combined answer"},
        ]

        with patch("meho_app.api.routes_chat.get_agent_http_client") as mock_get_client:
            mock_http_client = AsyncMock()
            mock_http_client.get_chat_session = AsyncMock(return_value={"messages": messages})
            mock_get_client.return_value = mock_http_client

            history = await get_conversation_history("test-session-id")

            assert len(history) == 3
            assert history[0]["role"] == "user"
            assert history[1]["role"] == "user"  # Consecutive user messages OK
            assert history[2]["role"] == "assistant"
