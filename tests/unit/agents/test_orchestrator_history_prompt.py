# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for format_history_for_prompt() function.

Tests formatting of conversation history for inclusion in LLM prompts.
"""

from meho_app.modules.agents.orchestrator.history import format_history_for_prompt


class TestFormatHistoryForPrompt:
    """Tests for format_history_for_prompt() function."""

    def test_none_history_returns_no_conversation_message(self) -> None:
        """None history should return 'no previous conversation' message."""
        result = format_history_for_prompt(None)
        assert "No previous conversation" in result

    def test_empty_history_returns_no_conversation_message(self) -> None:
        """Empty history list should return 'no previous conversation' message."""
        result = format_history_for_prompt([])
        assert "No previous conversation" in result

    def test_single_message_formatted(self) -> None:
        """Single message should be formatted with role and content."""
        history = [{"role": "user", "content": "List all namespaces"}]
        result = format_history_for_prompt(history)

        assert "User" in result
        assert "List all namespaces" in result
        assert "Recent Conversation" in result

    def test_user_and_assistant_formatted(self) -> None:
        """Both user and assistant messages should be formatted."""
        history = [
            {"role": "user", "content": "List all namespaces"},
            {"role": "assistant", "content": "Found 30 namespaces..."},
        ]
        result = format_history_for_prompt(history)

        assert "User" in result
        assert "Assistant" in result
        assert "List all namespaces" in result
        assert "30 namespaces" in result

    def test_limits_to_last_3_messages(self) -> None:
        """Should only include last 3 messages by default."""
        history = [
            {"role": "user", "content": "Message 1"},
            {"role": "assistant", "content": "Reply 1"},
            {"role": "user", "content": "Message 2"},
            {"role": "assistant", "content": "Reply 2"},
            {"role": "user", "content": "Message 3"},
        ]
        result = format_history_for_prompt(history)

        # First two messages should not be included
        assert "Message 1" not in result
        assert "Reply 1" not in result

        # Last three should be included
        assert "Message 2" in result
        assert "Reply 2" in result
        assert "Message 3" in result

    def test_truncates_long_messages(self) -> None:
        """Messages longer than 150 chars should be truncated."""
        long_content = "x" * 200
        history = [{"role": "user", "content": long_content}]
        result = format_history_for_prompt(history)

        # Should be truncated with ellipsis
        assert "..." in result
        # Full content should not be present
        assert "x" * 200 not in result
        # First 150 chars should be present
        assert "x" * 150 in result

    def test_exactly_150_chars_not_truncated(self) -> None:
        """Message of exactly 150 chars should not be truncated."""
        content = "x" * 150
        history = [{"role": "user", "content": content}]
        result = format_history_for_prompt(history)

        # Should not have truncation ellipsis at end of content
        assert content in result
        # Content should not end with "..."
        assert f"{content}..." not in result

    def test_handles_missing_content_key(self) -> None:
        """Missing content key should be handled gracefully."""
        history = [{"role": "user"}]
        result = format_history_for_prompt(history)

        # Should not crash, should produce some output
        assert "User" in result

    def test_handles_missing_role_key(self) -> None:
        """Missing role key should be handled gracefully."""
        history = [{"content": "Some message"}]
        result = format_history_for_prompt(history)

        # Should default to Assistant role
        assert "Assistant" in result
        assert "Some message" in result

    def test_output_format_is_markdown(self) -> None:
        """Output should be markdown formatted."""
        history = [{"role": "user", "content": "Test message"}]
        result = format_history_for_prompt(history)

        # Should have markdown formatting
        assert "**" in result  # Bold markers
        assert "-" in result  # List markers

    def test_multiline_content_preserved_but_truncated(self) -> None:
        """Multiline content should be preserved but still truncated if too long."""
        content = "Line 1\nLine 2\nLine 3\n" + "x" * 200
        history = [{"role": "user", "content": content}]
        result = format_history_for_prompt(history)

        # Should truncate after 150 chars total
        assert "..." in result

    def test_realistic_follow_up_scenario(self) -> None:
        """Test with realistic follow-up query scenario."""
        history = [
            {"role": "user", "content": "List all namespaces in production"},
            {"role": "assistant", "content": "Found 30 namespaces in production cluster..."},
            {"role": "user", "content": "Show the other 15"},
        ]
        result = format_history_for_prompt(history)

        # All context should be included (3 messages = limit)
        assert "List all namespaces" in result
        assert "30 namespaces" in result
        assert "Show the other 15" in result

        # Markdown format
        assert "**Recent Conversation" in result
        assert "**User:**" in result
        assert "**Assistant:**" in result
