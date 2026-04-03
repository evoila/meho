# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Integration tests for ReactAgent execution.

Tests the full ReAct loop with mocked LLM and services.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.modules.agents.config.loader import AgentConfig
from meho_app.modules.agents.config.models import ModelConfig
from meho_app.modules.agents.react_agent.agent import ReactAgent


@dataclass
class MockExternalDeps:
    """Mock external dependencies."""

    topology_service: Any = None
    connector_service: Any = None


@pytest.fixture
def mock_deps() -> MockExternalDeps:
    """Create mock dependencies for the agent."""
    return MockExternalDeps(
        topology_service=MagicMock(),
        connector_service=MagicMock(),
    )


@pytest.fixture
def agent_with_mocked_config(mock_deps: MockExternalDeps) -> ReactAgent:
    """Create a ReactAgent with mocked config loading."""
    with patch.object(ReactAgent, "_load_config") as mock_load:
        mock_load.return_value = AgentConfig(
            name="react",
            description="Test agent",
            model=ModelConfig(name="openai:gpt-4.1-mini", temperature=0.0),
            system_prompt="You are a test agent.\n{{user_goal}}\n{{tool_list}}\n{{scratchpad}}\n{{tables_context}}\n{{topology_context}}\n{{history_context}}\n{{request_guidance}}",
            max_steps=10,
            tools={
                "call_operation": {"require_approval_for_dangerous": True},
            },
        )
        agent = ReactAgent(dependencies=mock_deps)
    return agent


class TestReactAgentExecution:
    """Tests for ReactAgent execution flow."""

    @pytest.mark.asyncio
    async def test_agent_emits_start_and_complete_events(
        self,
        agent_with_mocked_config: ReactAgent,
    ) -> None:
        """Test that agent emits agent_start and agent_complete events."""
        with patch("meho_app.modules.agents.base.inference.infer") as mock_infer:
            mock_infer.return_value = (
                "Thought: I can answer this directly.\nFinal Answer: Hello, I'm MEHO!"
            )

            events = []
            async for event in agent_with_mocked_config.run_streaming(
                "Hello", session_id="test-session"
            ):
                events.append(event)

            # Should have agent_start and agent_complete
            event_types = [e.type for e in events]
            assert "agent_start" in event_types
            assert "agent_complete" in event_types

            # agent_start should be first
            assert events[0].type == "agent_start"
            assert events[0].data["user_message"] == "Hello"

            # agent_complete should be last
            assert events[-1].type == "agent_complete"

    @pytest.mark.asyncio
    async def test_agent_completes_on_final_answer(
        self,
        agent_with_mocked_config: ReactAgent,
    ) -> None:
        """Test that agent completes when LLM returns Final Answer."""
        with patch("meho_app.modules.agents.base.inference.infer") as mock_infer:
            mock_infer.return_value = (
                "Thought: The user wants a greeting.\n"
                "Final Answer: Hello! How can I help you today?"
            )

            events = []
            async for event in agent_with_mocked_config.run_streaming("Hi"):
                events.append(event)

            # Should have final_answer event
            event_types = [e.type for e in events]
            assert "final_answer" in event_types

            # Should complete successfully
            complete_event = next(e for e in events if e.type == "agent_complete")
            assert complete_event.data["success"] is True

    @pytest.mark.asyncio
    async def test_agent_executes_tool_and_returns_to_reason(
        self,
        agent_with_mocked_config: ReactAgent,
    ) -> None:
        """Test that agent executes tools and returns to reasoning."""
        call_count = [0]

        def mock_infer_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call - request tool
                return (
                    "Thought: I need to list connectors first.\n"
                    "Action: list_connectors\n"
                    "Action Input: {}"
                )
            else:
                # Second call - final answer
                return (
                    "Thought: I have the connector list.\n"
                    "Final Answer: Here are the connectors: none found."
                )

        with patch("meho_app.modules.agents.base.inference.infer") as mock_infer:
            mock_infer.side_effect = mock_infer_side_effect

            # Mock the tool execution
            with patch("meho_app.modules.agents.react_agent.tools.TOOL_REGISTRY") as mock_registry:
                mock_tool_class = MagicMock()
                mock_tool = MagicMock()
                mock_tool.InputSchema = MagicMock(return_value=MagicMock())
                mock_tool.execute = AsyncMock(return_value={"connectors": []})
                mock_tool_class.return_value = mock_tool
                mock_registry.__contains__ = MagicMock(return_value=True)
                mock_registry.__getitem__ = MagicMock(return_value=mock_tool_class)

                events = []
                async for event in agent_with_mocked_config.run_streaming("List connectors"):
                    events.append(event)

                event_types = [e.type for e in events]

                # Should have action and observation events
                assert "action" in event_types
                assert "observation" in event_types
                assert "final_answer" in event_types

    @pytest.mark.asyncio
    async def test_max_steps_enforcement(
        self,
        mock_deps: MockExternalDeps,
    ) -> None:
        """Test that agent stops after max_steps is reached."""
        # Create agent with very low max_steps
        with patch.object(ReactAgent, "_load_config") as mock_load:
            mock_load.return_value = AgentConfig(
                name="react",
                description="Test agent",
                model=ModelConfig(name="openai:gpt-4.1-mini"),
                system_prompt="Test\n{{user_goal}}\n{{tool_list}}\n{{scratchpad}}\n{{tables_context}}\n{{topology_context}}\n{{history_context}}\n{{request_guidance}}",
                max_steps=2,  # Very low limit
                tools={},
            )
            agent = ReactAgent(dependencies=mock_deps)

        # Mock infer to always return action (never final answer)
        with patch("meho_app.modules.agents.base.inference.infer") as mock_infer:
            mock_infer.return_value = (
                "Thought: Still working...\nAction: list_connectors\nAction Input: {}"
            )

            with patch("meho_app.modules.agents.react_agent.tools.TOOL_REGISTRY") as mock_registry:
                mock_tool_class = MagicMock()
                mock_tool = MagicMock()
                mock_tool.InputSchema = MagicMock(return_value=MagicMock())
                mock_tool.execute = AsyncMock(return_value={})
                mock_tool_class.return_value = mock_tool
                mock_registry.__contains__ = MagicMock(return_value=True)
                mock_registry.__getitem__ = MagicMock(return_value=mock_tool_class)

                events = []
                async for event in agent.run_streaming("Loop forever"):
                    events.append(event)

                # Should have error about max steps
                error_events = [e for e in events if e.type == "error"]
                assert len(error_events) > 0
                assert any("Max steps" in e.data.get("message", "") for e in error_events)

    @pytest.mark.asyncio
    async def test_error_handling(
        self,
        agent_with_mocked_config: ReactAgent,
    ) -> None:
        """Test that agent handles errors gracefully."""
        with patch("meho_app.modules.agents.base.inference.infer") as mock_infer:
            mock_infer.side_effect = Exception("LLM API error")

            events = []
            async for event in agent_with_mocked_config.run_streaming("Test"):
                events.append(event)

            # Should have error event
            error_events = [e for e in events if e.type == "error"]
            assert len(error_events) > 0

            # Should still complete
            complete_events = [e for e in events if e.type == "agent_complete"]
            assert len(complete_events) == 1


class TestApprovalFlow:
    """Tests for approval flow in ReactAgent."""

    @pytest.mark.asyncio
    async def test_dangerous_tool_triggers_approval(
        self,
        agent_with_mocked_config: ReactAgent,
    ) -> None:
        """Test that dangerous tools trigger approval_required event."""
        with patch("meho_app.modules.agents.base.inference.infer") as mock_infer:
            mock_infer.return_value = (
                "Thought: I need to execute this operation.\n"
                "Action: call_operation\n"
                'Action Input: {"operation_id": "delete_vm", "connector_id": "abc"}'
            )

            events = []
            async for event in agent_with_mocked_config.run_streaming("Delete VM"):
                events.append(event)

            # Should have approval_required event
            event_types = [e.type for e in events]
            assert "approval_required" in event_types

            # Check approval event details
            approval_event = next(e for e in events if e.type == "approval_required")
            assert approval_event.data["tool"] == "call_operation"
            assert approval_event.data["danger_level"] in ["dangerous", "critical"]


class TestSSEEventSequence:
    """Tests for SSE event sequence."""

    @pytest.mark.asyncio
    async def test_event_sequence_for_simple_query(
        self,
        agent_with_mocked_config: ReactAgent,
    ) -> None:
        """Test that events are emitted in correct sequence."""
        with patch("meho_app.modules.agents.base.inference.infer") as mock_infer:
            mock_infer.return_value = (
                "Thought: Simple question, simple answer.\nFinal Answer: The answer is 42."
            )

            events = []
            async for event in agent_with_mocked_config.run_streaming("What is the answer?"):
                events.append(event)

            event_types = [e.type for e in events]

            # Expected sequence: agent_start -> ... -> agent_complete
            assert event_types[0] == "agent_start"
            assert event_types[-1] == "agent_complete"

            # Should have thought and final_answer somewhere in between
            assert "thought" in event_types
            assert "final_answer" in event_types

    @pytest.mark.asyncio
    async def test_all_events_have_agent_name(
        self,
        agent_with_mocked_config: ReactAgent,
    ) -> None:
        """Test that all events have the agent name."""
        with patch("meho_app.modules.agents.base.inference.infer") as mock_infer:
            mock_infer.return_value = "Thought: Test.\nFinal Answer: Done."

            async for event in agent_with_mocked_config.run_streaming("Test"):
                assert event.agent == "react"

    @pytest.mark.asyncio
    async def test_session_id_propagated(
        self,
        agent_with_mocked_config: ReactAgent,
    ) -> None:
        """Test that session_id is propagated to all events."""
        with patch("meho_app.modules.agents.base.inference.infer") as mock_infer:
            mock_infer.return_value = "Thought: Test.\nFinal Answer: Done."

            session_id = "test-session-123"
            async for event in agent_with_mocked_config.run_streaming(
                "Test", session_id=session_id
            ):
                # Main events should have session_id
                if event.type in ["agent_start", "agent_complete"]:
                    assert event.session_id == session_id
