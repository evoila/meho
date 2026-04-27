# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Conversation history parsing and formatting for the Orchestrator Agent.

This module provides functions for parsing conversation history text from the
adapter and formatting it for inclusion in LLM prompts.
"""

from __future__ import annotations


def parse_history_text(history_text: str) -> list[dict[str, str]]:
    """Parse history text 'USER: ...\\nASSISTANT: ...' back to structured list.

    The adapter formats conversation history as text with USER:/ASSISTANT: prefixes.
    This function parses it back into a list of message dicts.

    Args:
        history_text: Formatted history text from adapter.

    Returns:
        List of message dicts with 'role' and 'content' keys.

    Example:
        >>> text = "USER: Hello\\nASSISTANT: Hi there!\\nUSER: How are you?"
        >>> parse_history_text(text)
        [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Hi there!'},
            {'role': 'user', 'content': 'How are you?'}
        ]
    """
    if not history_text:
        return []

    messages: list[dict[str, str]] = []
    current_role: str | None = None
    current_content: list[str] = []

    for line in history_text.split("\n"):
        if line.startswith("USER: "):
            # Save previous message if exists
            if current_role:
                messages.append(
                    {
                        "role": current_role,
                        "content": "\n".join(current_content),
                    }
                )
            current_role = "user"
            current_content = [line[6:]]  # Skip "USER: " prefix
        elif line.startswith("ASSISTANT: "):
            # Save previous message if exists
            if current_role:
                messages.append(
                    {
                        "role": current_role,
                        "content": "\n".join(current_content),
                    }
                )
            current_role = "assistant"
            current_content = [line[11:]]  # Skip "ASSISTANT: " prefix
        elif current_role:
            # Continuation of current message
            current_content.append(line)

    # Don't forget the last message
    if current_role:
        messages.append(
            {
                "role": current_role,
                "content": "\n".join(current_content),
            }
        )

    return messages


def format_history_for_prompt(
    history: list[dict[str, str]] | None,
    max_messages: int = 3,
    max_content_length: int = 150,
) -> str:
    """Format recent conversation history for routing prompt.

    Truncates long messages and limits to recent exchanges for context.

    Args:
        history: List of message dicts with 'role' and 'content' keys.
        max_messages: Maximum number of recent messages to include (default: 3).
        max_content_length: Maximum length of each message before truncation (default: 150).

    Returns:
        Formatted history string for inclusion in prompts.

    Example:
        >>> history = [
        ...     {'role': 'user', 'content': 'Hello'},
        ...     {'role': 'assistant', 'content': 'Hi there!'},
        ... ]
        >>> format_history_for_prompt(history)
        '**Recent Conversation (for context):**\\n- **User:** Hello\\n- **Assistant:** Hi there!'
    """
    if not history:
        return "No previous conversation in this session."

    lines = ["**Recent Conversation (for context):**"]
    # Take last N messages for context
    for msg in history[-max_messages:]:
        role = "User" if msg.get("role") == "user" else "Assistant"
        content = msg.get("content", "")
        # Truncate long messages
        if len(content) > max_content_length:
            content = content[:max_content_length] + "..."
        lines.append(f"- **{role}:** {content}")

    return "\n".join(lines)
