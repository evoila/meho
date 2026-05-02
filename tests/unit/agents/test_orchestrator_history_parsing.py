# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for parse_history_text() function.

Tests parsing of conversation history text format back to structured list.
"""

from meho_app.modules.agents.orchestrator.history import parse_history_text


class TestParseHistoryText:
    """Tests for parse_history_text() function."""

    def test_empty_string_returns_empty_list(self) -> None:
        """Empty string should return empty list."""
        result = parse_history_text("")
        assert result == []

    def test_none_like_empty_returns_empty_list(self) -> None:
        """None-like values should return empty list."""
        # Empty string
        assert parse_history_text("") == []

    def test_single_user_message(self) -> None:
        """Single USER message should be parsed correctly."""
        history_text = "USER: List all namespaces"
        result = parse_history_text(history_text)

        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "List all namespaces"

    def test_single_assistant_message(self) -> None:
        """Single ASSISTANT message should be parsed correctly."""
        history_text = "ASSISTANT: Here are the namespaces..."
        result = parse_history_text(history_text)

        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        assert result[0]["content"] == "Here are the namespaces..."

    def test_user_and_assistant_exchange(self) -> None:
        """User and assistant exchange should be parsed correctly."""
        history_text = "USER: List all namespaces\nASSISTANT: Found 30 namespaces"
        result = parse_history_text(history_text)

        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "List all namespaces"
        assert result[1]["role"] == "assistant"
        assert result[1]["content"] == "Found 30 namespaces"

    def test_multi_turn_conversation(self) -> None:
        """Multi-turn conversation should be parsed correctly."""
        history_text = (
            "USER: List all namespaces\n"
            "ASSISTANT: Found 30 namespaces\n"
            "USER: Show the other 15\n"
            "ASSISTANT: Here are the remaining 15..."
        )
        result = parse_history_text(history_text)

        assert len(result) == 4
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[2]["role"] == "user"
        assert result[3]["role"] == "assistant"

    def test_multiline_message(self) -> None:
        """Multiline messages should preserve newlines."""
        history_text = (
            "USER: List all namespaces\n"
            "with their labels\n"
            "and annotations\n"
            "ASSISTANT: Here are the results..."
        )
        result = parse_history_text(history_text)

        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert "with their labels" in result[0]["content"]
        assert "and annotations" in result[0]["content"]

    def test_empty_content_after_prefix(self) -> None:
        """Message with empty content after prefix should work."""
        history_text = "USER: \nASSISTANT: Response"
        result = parse_history_text(history_text)

        assert len(result) == 2
        assert result[0]["content"] == ""
        assert result[1]["content"] == "Response"

    def test_preserves_whitespace_in_content(self) -> None:
        """Whitespace in message content should be preserved."""
        history_text = "USER:   spaced content   "
        result = parse_history_text(history_text)

        assert len(result) == 1
        assert result[0]["content"] == "  spaced content   "

    def test_lines_without_prefix_ignored_before_first_message(self) -> None:
        """Lines without USER:/ASSISTANT: prefix before first message are ignored."""
        history_text = "Some random text\nMore random text\nUSER: First real message"
        result = parse_history_text(history_text)

        assert len(result) == 1
        assert result[0]["content"] == "First real message"

    def test_real_adapter_format(self) -> None:
        """Test parsing of realistic adapter output format."""
        history_text = (
            "USER: What pods are running in production?\n"
            "ASSISTANT: I found 42 pods running in the production namespace. Here's a summary:\n"
            "\n"
            "| Pod Name | Status |\n"
            "|----------|--------|\n"
            "| api-server-1 | Running |\n"
            "| web-frontend-2 | Running |\n"
            "\n"
            "USER: Which ones are using more than 1GB memory?"
        )
        result = parse_history_text(history_text)

        assert len(result) == 3
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[2]["role"] == "user"
        # Check multiline assistant response
        assert "| Pod Name | Status |" in result[1]["content"]
