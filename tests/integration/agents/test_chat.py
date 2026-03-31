# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Integration tests for /chat/stream endpoint.

Tests the chat streaming endpoint with the orchestrator agent implementation.

Verifies:
- Adapter creates ReactAgent correctly
- Adapter streams events in correct format
- Event format compatibility (old SSE format maintained for frontend)
- Conversation history formatting
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestChatStreamWithNewAgent:
    """Tests for /chat/stream using OrchestratorAgent."""

    @pytest.mark.asyncio
    async def test_adapter_creates_agent(
        self,
    ) -> None:
        """Test that adapter can create ReactAgent."""
        mock_dependencies = MagicMock()
        mock_dependencies.session_state = MagicMock()

        with patch(
            "meho_app.modules.agents.react_agent.agent.ReactAgent._load_config"
        ) as mock_load:
            from meho_app.modules.agents.config.loader import AgentConfig
            from meho_app.modules.agents.config.models import ModelConfig

            mock_load.return_value = AgentConfig(
                name="react",
                description="Test",
                model=ModelConfig(name="anthropic:claude-sonnet-4-6"),
                system_prompt="Test {{user_goal}} {{tool_list}} {{scratchpad}} {{tables_context}} {{topology_context}} {{history_context}} {{request_guidance}}",
                max_steps=5,
                tools={},
            )

            from meho_app.modules.agents.adapter import create_react_agent

            agent = create_react_agent(mock_dependencies)
            assert agent is not None
            assert agent.agent_name == "react"

    @pytest.mark.asyncio
    async def test_adapter_streams_events(
        self,
    ) -> None:
        """Test that adapter streams events in correct format."""
        mock_dependencies = MagicMock()
        mock_dependencies.session_state = MagicMock()

        with (
            patch("meho_app.modules.agents.react_agent.agent.ReactAgent._load_config") as mock_load,
            patch("meho_app.modules.agents.base.inference.infer") as mock_infer,
        ):
            from meho_app.modules.agents.config.loader import AgentConfig
            from meho_app.modules.agents.config.models import ModelConfig

            mock_load.return_value = AgentConfig(
                name="react",
                description="Test",
                model=ModelConfig(name="anthropic:claude-sonnet-4-6"),
                system_prompt="Test {{user_goal}} {{tool_list}} {{scratchpad}} {{tables_context}} {{topology_context}} {{history_context}} {{request_guidance}}",
                max_steps=5,
                tools={},
            )

            mock_infer.return_value = "Thought: Direct answer.\nFinal Answer: Hello from new agent!"

            from meho_app.modules.agents.adapter import (
                create_react_agent,
                run_agent_streaming,
            )

            agent = create_react_agent(mock_dependencies)
            events = []

            async for event in run_agent_streaming(
                agent=agent,
                user_message="Hello",
                session_id="test-123",
                conversation_history=[],
            ):
                events.append(event)

            # Should have events
            assert len(events) > 0

            # Events should be in old format (flat dict with type + data fields)
            for event in events:
                assert "type" in event


class TestEventFormatCompatibility:
    """Tests for event format compatibility between old and new agents."""

    @pytest.mark.asyncio
    async def test_event_format_matches(self) -> None:
        """Test that new agent events match expected SSE format."""
        from meho_app.modules.agents.adapter import _convert_event_to_old_format
        from meho_app.modules.agents.base.events import AgentEvent

        # Create new agent events and verify they convert correctly
        test_cases = [
            (
                AgentEvent(type="thought", agent="react", data={"content": "Thinking..."}),
                {"type": "thought", "content": "Thinking..."},
            ),
            (
                AgentEvent(
                    type="action",
                    agent="react",
                    data={"tool": "search_knowledge", "args": {"query": "test"}},
                ),
                {"type": "action", "tool": "search_knowledge", "args": {"query": "test"}},
            ),
            (
                AgentEvent(
                    type="observation",
                    agent="react",
                    data={"tool": "search_knowledge", "result": "Found 3 results"},
                ),
                {"type": "observation", "tool": "search_knowledge", "result": "Found 3 results"},
            ),
            (
                AgentEvent(
                    type="final_answer",
                    agent="react",
                    data={"content": "The answer is 42."},
                ),
                {"type": "final_answer", "content": "The answer is 42."},
            ),
            (
                AgentEvent(
                    type="approval_required",
                    agent="react",
                    data={
                        "tool": "call_operation",
                        "args": {"op": "delete"},
                        "danger_level": "critical",
                        "description": "Delete operation",
                    },
                ),
                {
                    "type": "approval_required",
                    "tool": "call_operation",
                    "args": {"op": "delete"},
                    "danger_level": "critical",
                    "description": "Delete operation",
                },
            ),
            (
                AgentEvent(
                    type="error",
                    agent="react",
                    data={"message": "Something went wrong"},
                ),
                {"type": "error", "message": "Something went wrong"},
            ),
        ]

        for new_event, expected_old_format in test_cases:
            converted = _convert_event_to_old_format(new_event)
            assert converted == expected_old_format, f"Mismatch for {new_event.type}"


class TestConversationHistoryFormatting:
    """Tests for conversation history formatting in adapter."""

    def test_format_empty_history(self) -> None:
        """Test formatting empty conversation history."""
        from meho_app.modules.agents.adapter import _format_conversation_history

        result = _format_conversation_history([])
        assert result == ""

    def test_format_single_message(self) -> None:
        """Test formatting single message."""
        from meho_app.modules.agents.adapter import _format_conversation_history

        history = [{"role": "user", "content": "Hello"}]
        result = _format_conversation_history(history)
        assert "USER: Hello" in result

    def test_format_conversation(self) -> None:
        """Test formatting multi-turn conversation."""
        from meho_app.modules.agents.adapter import _format_conversation_history

        history = [
            {"role": "user", "content": "What is MEHO?"},
            {"role": "assistant", "content": "MEHO is a multi-system diagnostic agent."},
            {"role": "user", "content": "What can it do?"},
        ]
        result = _format_conversation_history(history)
        assert "USER: What is MEHO?" in result
        assert "ASSISTANT: MEHO is a multi-system diagnostic agent." in result
        assert "USER: What can it do?" in result
