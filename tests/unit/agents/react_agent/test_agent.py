# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for ReactAgent class shell."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from meho_app.modules.agents.base.agent import BaseAgent
from meho_app.modules.agents.base.events import AgentEvent
from meho_app.modules.agents.react_agent import ReactAgent


class TestReactAgentImport:
    """Tests for ReactAgent import."""

    def test_import_from_module(self) -> None:
        """Test importing ReactAgent from module."""
        from meho_app.modules.agents.react_agent import ReactAgent

        assert ReactAgent is not None

    def test_is_base_agent_subclass(self) -> None:
        """Test ReactAgent inherits from BaseAgent."""
        assert issubclass(ReactAgent, BaseAgent)


class TestReactAgentClassAttributes:
    """Tests for ReactAgent class attributes."""

    def test_has_agent_name(self) -> None:
        """Test ReactAgent has agent_name class variable."""
        assert hasattr(ReactAgent, "agent_name")
        assert ReactAgent.agent_name == "react"

    def test_agent_name_is_class_var(self) -> None:
        """Test agent_name is defined at class level."""
        # Should be accessible without instantiation
        assert ReactAgent.agent_name == "react"


class TestReactAgentInstantiation:
    """Tests for ReactAgent instantiation."""

    @pytest.fixture
    def mock_deps(self) -> MagicMock:
        """Create mock dependencies."""
        return MagicMock()

    def test_instantiate_with_dependencies(self, mock_deps: MagicMock) -> None:
        """Test creating ReactAgent instance."""
        agent = ReactAgent(dependencies=mock_deps)
        assert agent is not None
        assert agent.dependencies is mock_deps

    def test_agent_folder_property(self, mock_deps: MagicMock) -> None:
        """Test agent_folder property returns correct path."""
        agent = ReactAgent(dependencies=mock_deps)
        folder = agent.agent_folder
        assert isinstance(folder, Path)
        assert folder.name == "react_agent"

    def test_config_loads_on_init(self, mock_deps: MagicMock) -> None:
        """Test that config is loaded during initialization."""
        agent = ReactAgent(dependencies=mock_deps)
        assert agent._config is not None
        assert agent._config.name == "react"


class TestReactAgentMethods:
    """Tests for ReactAgent methods."""

    @pytest.fixture
    def agent(self) -> ReactAgent:
        """Create ReactAgent instance."""
        return ReactAgent(dependencies=MagicMock())

    def test_build_flow_returns_entry_node(self, agent: ReactAgent) -> None:
        """Test build_flow returns entry node name."""
        entry = agent.build_flow()
        assert entry == "topology_lookup"

    def test_load_config_returns_agent_config(self, agent: ReactAgent) -> None:
        """Test _load_config returns AgentConfig."""
        from meho_app.modules.agents.config.loader import AgentConfig

        config = agent._load_config()
        assert isinstance(config, AgentConfig)


class TestReactAgentRunStreaming:
    """Tests for run_streaming method."""

    @pytest.fixture
    def agent(self) -> ReactAgent:
        """Create ReactAgent instance."""
        return ReactAgent(dependencies=MagicMock())

    @pytest.mark.asyncio
    async def test_run_streaming_yields_events(self, agent: ReactAgent) -> None:
        """Test run_streaming yields AgentEvent objects."""
        events = []
        async for event in agent.run_streaming("test message"):
            events.append(event)
        assert len(events) > 0
        for event in events:
            assert isinstance(event, AgentEvent)

    @pytest.mark.asyncio
    async def test_run_streaming_includes_agent_start(self, agent: ReactAgent) -> None:
        """Test run_streaming yields agent_start event."""
        events = []
        async for event in agent.run_streaming("test message"):
            events.append(event)
        event_types = [e.type for e in events]
        assert "agent_start" in event_types

    @pytest.mark.asyncio
    async def test_run_streaming_includes_agent_complete(self, agent: ReactAgent) -> None:
        """Test run_streaming yields agent_complete event."""
        events = []
        async for event in agent.run_streaming("test message"):
            events.append(event)
        event_types = [e.type for e in events]
        assert "agent_complete" in event_types

    @pytest.mark.asyncio
    async def test_run_streaming_events_have_agent_name(self, agent: ReactAgent) -> None:
        """Test all events have correct agent name."""
        async for event in agent.run_streaming("test"):
            assert event.agent == "react"

    @pytest.mark.asyncio
    async def test_run_streaming_with_session_id(self, agent: ReactAgent) -> None:
        """Test run_streaming passes session_id to events."""
        events = []
        async for event in agent.run_streaming("test", session_id="test-session"):
            events.append(event)
        for event in events:
            assert event.session_id == "test-session"


class TestReactAgentRun:
    """Tests for non-streaming run method."""

    @pytest.fixture
    def agent(self) -> ReactAgent:
        """Create ReactAgent instance."""
        return ReactAgent(dependencies=MagicMock())

    @pytest.mark.asyncio
    async def test_run_returns_string(self, agent: ReactAgent) -> None:
        """Test run returns string result."""
        result = await agent.run("test message")
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_run_returns_response(self, agent: ReactAgent) -> None:
        """Test run returns a response (working implementation)."""
        from unittest.mock import patch

        # Mock the LLM to return a predictable response
        with patch("meho_app.modules.agents.base.inference.infer") as mock_infer:
            mock_infer.return_value = (
                "Thought: Simple test query.\nFinal Answer: This is a test response."
            )
            result = await agent.run("test message")

        # Should return the final answer or a meaningful response
        assert isinstance(result, str)
        assert len(result) > 0
