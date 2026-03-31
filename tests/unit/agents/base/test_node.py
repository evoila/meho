# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for BaseNode abstract base class and NodeResult.

These tests verify:
1. BaseNode is an ABC and cannot be instantiated directly
2. Concrete implementations must implement run()
3. NodeResult correctly represents flow transitions
4. NodeResult.is_terminal() works correctly
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from meho_app.modules.agents.base import BaseNode, NodeResult


class TestNodeResult:
    """Tests for the NodeResult dataclass."""

    def test_node_result_with_next_node(self) -> None:
        """NodeResult should store next_node correctly."""
        result = NodeResult(next_node="tool_dispatch")
        assert result.next_node == "tool_dispatch"
        assert result.data == {}

    def test_node_result_with_data(self) -> None:
        """NodeResult should store data correctly."""
        result = NodeResult(
            next_node="tool_dispatch",
            data={"tool": "search", "args": {"query": "test"}},
        )
        assert result.next_node == "tool_dispatch"
        assert result.data["tool"] == "search"
        assert result.data["args"]["query"] == "test"

    def test_node_result_terminal(self) -> None:
        """NodeResult with None next_node should be terminal."""
        result = NodeResult(next_node=None)
        assert result.next_node is None
        assert result.is_terminal() is True

    def test_node_result_not_terminal(self) -> None:
        """NodeResult with next_node should not be terminal."""
        result = NodeResult(next_node="reason")
        assert result.is_terminal() is False

    def test_node_result_empty_string_is_not_terminal(self) -> None:
        """NodeResult with empty string next_node is not terminal."""
        result = NodeResult(next_node="")
        # Empty string is falsy but not None
        assert result.is_terminal() is False


class TestBaseNodeContract:
    """Tests for the BaseNode contract."""

    def test_base_node_is_abc(self) -> None:
        """BaseNode should be an ABC."""
        assert issubclass(BaseNode, ABC)

    def test_base_node_cannot_be_instantiated(self) -> None:
        """BaseNode cannot be instantiated directly without implementing run."""

        @dataclass
        class IncompleteNode(BaseNode[dict]):
            NODE_NAME = "incomplete"
            # Missing run() implementation

        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            IncompleteNode()  # type: ignore[abstract]

    def test_base_node_importable_from_base(self) -> None:
        """BaseNode should be importable from meho_app.modules.agents.base."""
        from meho_app.modules.agents.base import BaseNode as ImportedBaseNode

        assert ImportedBaseNode is BaseNode

    def test_node_result_importable_from_base(self) -> None:
        """NodeResult should be importable from meho_app.modules.agents.base."""
        from meho_app.modules.agents.base import NodeResult as ImportedNodeResult

        assert ImportedNodeResult is NodeResult

    def test_base_node_has_required_abstract_methods(self) -> None:
        """BaseNode should have the run abstract method."""
        abstract_methods = BaseNode.__abstractmethods__
        assert "run" in abstract_methods


class TestConcreteNode:
    """Tests for concrete node implementations."""

    def test_concrete_node_can_be_created(self) -> None:
        """A concrete node implementing run can be instantiated."""

        @dataclass
        class TestNode(BaseNode[dict]):
            NODE_NAME = "test_node"

            async def run(
                self,
                state: dict,
                deps: Any,
                emitter: Any,
            ) -> NodeResult:
                return NodeResult(next_node="next", data={"key": "value"})

        # Should not raise
        node = TestNode()
        assert node.NODE_NAME == "test_node"

    @pytest.mark.asyncio
    async def test_concrete_node_run_returns_next_node(self) -> None:
        """Concrete node's run method should return NodeResult."""

        @dataclass
        class TestNode(BaseNode[dict]):
            NODE_NAME = "test"

            async def run(
                self,
                state: dict,
                deps: Any,
                emitter: Any,
            ) -> NodeResult:
                state["processed"] = True
                return NodeResult(next_node="next_node", data={"result": "done"})

        node = TestNode()
        state: dict[str, Any] = {}
        result = await node.run(state, MagicMock(), MagicMock())

        assert result.next_node == "next_node"
        assert result.data["result"] == "done"
        assert state["processed"] is True  # State was mutated

    @pytest.mark.asyncio
    async def test_concrete_node_run_returns_terminal(self) -> None:
        """Concrete node can return terminal NodeResult."""

        @dataclass
        class FinalNode(BaseNode[dict]):
            NODE_NAME = "final"

            async def run(
                self,
                state: dict,
                deps: Any,
                emitter: Any,
            ) -> NodeResult:
                return NodeResult(next_node=None, data={"final_answer": "Done!"})

        node = FinalNode()
        result = await node.run({}, MagicMock(), MagicMock())

        assert result.is_terminal() is True
        assert result.data["final_answer"] == "Done!"


class TestNodeGenericTyping:
    """Tests for BaseNode generic typing."""

    def test_node_with_typed_state(self) -> None:
        """Node should work with typed state classes."""

        @dataclass
        class MyState:
            counter: int = 0
            message: str = ""

        @dataclass
        class CounterNode(BaseNode[MyState]):
            NODE_NAME = "counter"

            async def run(
                self,
                state: MyState,
                deps: Any,
                emitter: Any,
            ) -> NodeResult:
                state.counter += 1
                state.message = f"Count: {state.counter}"
                return NodeResult(next_node=None)

        # Should not raise any type errors at runtime
        node = CounterNode()
        assert node.NODE_NAME == "counter"

    @pytest.mark.asyncio
    async def test_node_mutates_typed_state(self) -> None:
        """Node should be able to mutate typed state."""

        @dataclass
        class AgentState:
            step: int = 0
            observations: list[str] | None = None

            def __post_init__(self) -> None:
                if self.observations is None:
                    self.observations = []

        @dataclass
        class ObserverNode(BaseNode[AgentState]):
            NODE_NAME = "observer"

            async def run(
                self,
                state: AgentState,
                deps: Any,
                emitter: Any,
            ) -> NodeResult:
                state.step += 1
                if state.observations is not None:
                    state.observations.append(f"Step {state.step}")
                return NodeResult(next_node="next" if state.step < 3 else None)

        node = ObserverNode()
        state = AgentState()

        # Run multiple times
        result = await node.run(state, MagicMock(), MagicMock())
        assert result.next_node == "next"
        assert state.step == 1

        result = await node.run(state, MagicMock(), MagicMock())
        assert result.next_node == "next"
        assert state.step == 2

        result = await node.run(state, MagicMock(), MagicMock())
        assert result.is_terminal()
        assert state.step == 3
        assert state.observations == ["Step 1", "Step 2", "Step 3"]
