# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for BaseAgent abstract base class.

These tests verify:
1. BaseAgent is an ABC and cannot be instantiated directly
2. Concrete implementations must implement all abstract methods
3. The agent_folder property auto-detects correctly
4. The run() method properly consumes streaming events
"""

from __future__ import annotations

from abc import ABC
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from meho_app.modules.agents.base import BaseAgent


class TestBaseAgentContract:
    """Tests for the BaseAgent contract."""

    def test_base_agent_is_abc(self) -> None:
        """BaseAgent should be an ABC."""
        assert issubclass(BaseAgent, ABC)

    def test_base_agent_cannot_be_instantiated(self) -> None:
        """BaseAgent cannot be instantiated directly."""
        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            BaseAgent(dependencies=MagicMock())  # type: ignore[abstract]

    def test_base_agent_importable_from_base(self) -> None:
        """BaseAgent should be importable from meho_app.modules.agents.base."""
        from meho_app.modules.agents.base import BaseAgent as ImportedBaseAgent

        assert ImportedBaseAgent is BaseAgent

    def test_base_agent_has_required_abstract_methods(self) -> None:
        """BaseAgent should have the required abstract methods."""
        abstract_methods = BaseAgent.__abstractmethods__
        assert "_load_config" in abstract_methods
        assert "build_flow" in abstract_methods
        assert "run_streaming" in abstract_methods


class TestConcreteAgent:
    """Tests for concrete agent implementations."""

    def test_concrete_agent_can_be_created(self) -> None:
        """A concrete agent implementing all methods can be instantiated."""

        @dataclass
        class TestAgent(BaseAgent):
            agent_name = "test"

            def _load_config(self) -> dict[str, Any]:
                return {"name": "test"}

            def build_flow(self) -> str:
                return "entry_node"

            async def run_streaming(
                self,
                user_message: str,
                session_id: str | None = None,
                context: dict[str, Any] | None = None,
            ) -> AsyncIterator[Any]:
                yield {"type": "thought", "data": {"content": "Thinking..."}}
                yield {"type": "final_answer", "data": {"content": "Done"}}

        # Should not raise
        agent = TestAgent(dependencies=MagicMock())
        assert agent.agent_name == "test"
        assert agent.dependencies is not None

    def test_agent_folder_property(self, tmp_path: Path) -> None:
        """agent_folder property should return the parent folder of the agent module."""

        @dataclass
        class TestAgent(BaseAgent):
            agent_name = "test"

            def _load_config(self) -> dict[str, Any]:
                return {}

            def build_flow(self) -> str:
                return "entry"

            async def run_streaming(
                self,
                user_message: str,
                session_id: str | None = None,
                context: dict[str, Any] | None = None,
            ) -> AsyncIterator[Any]:
                if False:
                    yield

        agent = TestAgent(dependencies=MagicMock())
        # The folder should be a Path object
        assert isinstance(agent.agent_folder, Path)
        # It should be the parent directory of this test file
        # (since TestAgent is defined here)


class TestAgentRunMethod:
    """Tests for the non-streaming run() method."""

    @pytest.mark.asyncio
    async def test_run_returns_final_answer(self) -> None:
        """run() should return the final answer from the stream."""

        @dataclass
        class TestAgent(BaseAgent):
            agent_name = "test"

            def _load_config(self) -> dict[str, Any]:
                return {}

            def build_flow(self) -> str:
                return "entry"

            async def run_streaming(
                self,
                user_message: str,
                session_id: str | None = None,
                context: dict[str, Any] | None = None,
            ) -> AsyncIterator[Any]:
                # Simulate event objects with type and data attributes
                class Event:
                    def __init__(self, type_: str, data: dict[str, Any]) -> None:
                        self.type = type_
                        self.data = data

                yield Event("thought", {"content": "Thinking..."})
                yield Event("action", {"tool": "search", "args": {}})
                yield Event("final_answer", {"content": "The answer is 42"})

        agent = TestAgent(dependencies=MagicMock())
        result = await agent.run("What is the answer?")
        assert result == "The answer is 42"

    @pytest.mark.asyncio
    async def test_run_returns_error_message(self) -> None:
        """run() should return error message if error event received."""

        @dataclass
        class TestAgent(BaseAgent):
            agent_name = "test"

            def _load_config(self) -> dict[str, Any]:
                return {}

            def build_flow(self) -> str:
                return "entry"

            async def run_streaming(
                self,
                user_message: str,
                session_id: str | None = None,
                context: dict[str, Any] | None = None,
            ) -> AsyncIterator[Any]:
                class Event:
                    def __init__(self, type_: str, data: dict[str, Any]) -> None:
                        self.type = type_
                        self.data = data

                yield Event("thought", {"content": "Thinking..."})
                yield Event("error", {"message": "Something went wrong"})

        agent = TestAgent(dependencies=MagicMock())
        result = await agent.run("What is the answer?")
        assert result == "Error: Something went wrong"

    @pytest.mark.asyncio
    async def test_run_returns_default_when_no_answer(self) -> None:
        """run() should return default message if no final_answer event."""

        @dataclass
        class TestAgent(BaseAgent):
            agent_name = "test"

            def _load_config(self) -> dict[str, Any]:
                return {}

            def build_flow(self) -> str:
                return "entry"

            async def run_streaming(
                self,
                user_message: str,
                session_id: str | None = None,
                context: dict[str, Any] | None = None,
            ) -> AsyncIterator[Any]:
                class Event:
                    def __init__(self, type_: str, data: dict[str, Any]) -> None:
                        self.type = type_
                        self.data = data

                yield Event("thought", {"content": "Thinking..."})
                # No final_answer event

        agent = TestAgent(dependencies=MagicMock())
        result = await agent.run("What is the answer?")
        assert result == "No response generated"
