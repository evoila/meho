# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for ReactAgentState dataclass."""

from __future__ import annotations

from meho_app.modules.agents.react_agent.state import ReactAgentState


class TestReactAgentStateCreation:
    """Tests for creating ReactAgentState."""

    def test_create_state_with_goal(self) -> None:
        """Test creating state with user goal."""
        state = ReactAgentState(user_goal="List all VMs")
        assert state.user_goal == "List all VMs"

    def test_default_values(self) -> None:
        """Test default field values."""
        state = ReactAgentState(user_goal="test")
        assert state.scratchpad == []
        assert state.step_count == 0
        assert state.pending_tool is None
        assert state.pending_args is None
        assert state.last_observation is None
        assert state.final_answer is None
        assert state.error_message is None

    def test_state_is_dataclass(self) -> None:
        """Test that ReactAgentState is a dataclass."""
        from dataclasses import is_dataclass

        assert is_dataclass(ReactAgentState)


class TestReactAgentStateScratchpad:
    """Tests for scratchpad functionality."""

    def test_add_to_scratchpad(self) -> None:
        """Test adding entries to scratchpad."""
        state = ReactAgentState(user_goal="test")
        state.add_to_scratchpad("Thought: testing")
        assert len(state.scratchpad) == 1
        assert state.scratchpad[0] == "Thought: testing"

    def test_add_multiple_entries(self) -> None:
        """Test adding multiple scratchpad entries."""
        state = ReactAgentState(user_goal="test")
        state.add_to_scratchpad("Thought: first")
        state.add_to_scratchpad("Action: list_connectors")
        state.add_to_scratchpad("Observation: found 3 connectors")
        assert len(state.scratchpad) == 3

    def test_get_scratchpad_text_empty(self) -> None:
        """Test getting scratchpad text when empty."""
        state = ReactAgentState(user_goal="test")
        assert state.get_scratchpad_text() == ""

    def test_get_scratchpad_text_single(self) -> None:
        """Test getting scratchpad text with single entry."""
        state = ReactAgentState(user_goal="test")
        state.add_to_scratchpad("Thought: testing")
        assert state.get_scratchpad_text() == "Thought: testing"

    def test_get_scratchpad_text_multiple(self) -> None:
        """Test getting scratchpad text with multiple entries."""
        state = ReactAgentState(user_goal="test")
        state.add_to_scratchpad("Thought: first")
        state.add_to_scratchpad("Action: test")
        expected = "Thought: first\nAction: test"
        assert state.get_scratchpad_text() == expected


class TestReactAgentStatePendingAction:
    """Tests for pending action functionality."""

    def test_set_pending_action(self) -> None:
        """Test setting pending tool and args."""
        state = ReactAgentState(user_goal="test")
        state.pending_tool = "list_connectors"
        state.pending_args = {"limit": 10}
        assert state.pending_tool == "list_connectors"
        assert state.pending_args == {"limit": 10}

    def test_clear_pending_action(self) -> None:
        """Test clearing pending action."""
        state = ReactAgentState(user_goal="test")
        state.pending_tool = "search_operations"
        state.pending_args = {"query": "vms"}
        state.clear_pending_action()
        assert state.pending_tool is None
        assert state.pending_args is None


class TestReactAgentStateCompletion:
    """Tests for completion status."""

    def test_is_complete_false_initially(self) -> None:
        """Test that state is not complete initially."""
        state = ReactAgentState(user_goal="test")
        assert state.is_complete() is False

    def test_is_complete_with_final_answer(self) -> None:
        """Test completion with final answer."""
        state = ReactAgentState(user_goal="test")
        state.final_answer = "Here are the results"
        assert state.is_complete() is True

    def test_is_complete_with_error(self) -> None:
        """Test completion with error."""
        state = ReactAgentState(user_goal="test")
        state.error_message = "Something went wrong"
        assert state.is_complete() is True

    def test_is_complete_with_both(self) -> None:
        """Test completion with both answer and error."""
        state = ReactAgentState(user_goal="test")
        state.final_answer = "Results"
        state.error_message = "Warning"
        assert state.is_complete() is True
