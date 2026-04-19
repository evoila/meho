# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for SpecialistAgent ReAct loop state, models, and events.

Tests cover:
- SpecialistReActState: scratchpad, budget, circuit breaker, loop detection
- ReActStep: structured output model validation
- step_progress: new event type registration
"""

from __future__ import annotations

from meho_app.modules.agents.base.events import AgentEvent
from meho_app.modules.agents.specialist_agent.models import ReActStep
from meho_app.modules.agents.specialist_agent.state import SpecialistReActState

# ──────────────────────────────────────────────────────────────────────────────
# SpecialistReActState tests
# ──────────────────────────────────────────────────────────────────────────────


class TestSpecialistReActState:
    """Tests for the SpecialistReActState dataclass."""

    def test_default_max_steps_is_8(self) -> None:
        """Verify default budget is 8 steps (Phase 36: reduced from 15)."""
        state = SpecialistReActState(user_goal="test")
        assert state.max_steps == 8

    def test_add_observation_passes_full_result(self) -> None:
        """Pass a 50,000-char string -- verify scratchpad contains full result."""
        state = SpecialistReActState(user_goal="test")
        big_result = "x" * 50_000
        state.add_observation("reduce_data", big_result)

        # Should have Action and Observation entries
        assert len(state.scratchpad) == 2
        assert state.scratchpad[0] == "Action: reduce_data"
        # Full result preserved -- no truncation for 50k chars
        assert state.scratchpad[1] == f"Observation: {big_result}"
        assert len(state.scratchpad[1]) == 50_000 + len("Observation: ")

    def test_add_observation_circuit_breaker_at_200k(self) -> None:
        """Pass a 300,000-char string -- verify truncated to 200k (circuit breaker)."""
        state = SpecialistReActState(user_goal="test")
        huge_result = "y" * 300_000
        state.add_observation("call_operation", huge_result)

        assert len(state.scratchpad) == 2
        observation = state.scratchpad[1]
        assert observation.startswith("Observation: ")
        # The observation text should contain exactly 200k chars of 'y'
        # followed by the truncation notice
        obs_text = observation.replace("Observation: ", "", 1)
        assert "TRUNCATED" in obs_text
        # First 200k chars should be preserved
        assert obs_text[:200_000] == "y" * 200_000

    def test_is_complete_true_when_final_answer_set(self) -> None:
        """Set final_answer -- verify is_complete returns True."""
        state = SpecialistReActState(user_goal="test")
        state.final_answer = "Found 5 pods"
        assert state.is_complete() is True

    def test_is_complete_true_when_error_set(self) -> None:
        """Set error_message -- verify is_complete returns True."""
        state = SpecialistReActState(user_goal="test")
        state.error_message = "Connection timeout"
        assert state.is_complete() is True

    def test_is_complete_false_when_neither_set(self) -> None:
        """Verify is_complete returns False on fresh state."""
        state = SpecialistReActState(user_goal="test")
        assert state.is_complete() is False

    def test_has_duplicate_action_detects_repeat(self) -> None:
        """Call record_action then has_duplicate_action with same args -- True."""
        state = SpecialistReActState(user_goal="test")
        args = {"query": "list pods"}
        state.record_action("search_operations", args)
        assert state.has_duplicate_action("search_operations", args) is True

    def test_has_duplicate_action_allows_different_args(self) -> None:
        """Verify has_duplicate_action returns False for different args."""
        state = SpecialistReActState(user_goal="test")
        state.record_action("search_operations", {"query": "list pods"})
        assert (
            state.has_duplicate_action("search_operations", {"query": "list namespaces"}) is False
        )

    def test_get_observations_summary_formats_steps(self) -> None:
        """Add 2 observations -- verify summary format."""
        state = SpecialistReActState(user_goal="test")
        state.add_observation("search_operations", "Found 3 operations")
        state.add_observation("call_operation", '{"data_available": true}')

        summary = state.get_observations_summary()
        assert "### search_operations" in summary
        assert "Found 3 operations" in summary
        assert "### call_operation" in summary
        assert '{"data_available": true}' in summary


# ──────────────────────────────────────────────────────────────────────────────
# ReActStep model tests
# ──────────────────────────────────────────────────────────────────────────────


class TestReActStep:
    """Tests for the ReActStep Pydantic model."""

    def test_action_step_valid(self) -> None:
        """Create ReActStep with response_type='action' -- verify fields."""
        from meho_app.modules.agents.specialist_agent.models import SearchOperationsAction

        step = ReActStep(
            thought="I need to search for operations related to pods",
            response_type="action",
            action_input=SearchOperationsAction(query="list pods"),
        )
        assert step.response_type == "action"
        assert step.action == "search_operations"
        assert step.action_input.query == "list pods"
        assert step.final_answer is None

    def test_final_answer_step_valid(self) -> None:
        """Create ReActStep with response_type='final_answer' -- verify."""
        step = ReActStep(
            thought="I have enough information to answer",
            response_type="final_answer",
            final_answer="Found 5 pods in the default namespace.",
        )
        assert step.response_type == "final_answer"
        assert step.final_answer == "Found 5 pods in the default namespace."
        assert step.action is None
        assert step.action_input is None

    def test_react_step_serializes_to_json(self) -> None:
        """Verify model_dump() produces valid dict."""
        from meho_app.modules.agents.specialist_agent.models import LookupTopologyAction

        step = ReActStep(
            thought="Checking topology",
            response_type="action",
            action_input=LookupTopologyAction(query="pods"),
        )
        dumped = step.model_dump()
        assert isinstance(dumped, dict)
        assert dumped["thought"] == "Checking topology"
        assert dumped["response_type"] == "action"
        assert dumped["action_input"]["tool"] == "lookup_topology"
        assert dumped["action_input"]["query"] == "pods"


# ──────────────────────────────────────────────────────────────────────────────
# step_progress event type tests
# ──────────────────────────────────────────────────────────────────────────────


class TestStepProgressEventType:
    """Tests for the step_progress event type."""

    def test_step_progress_in_event_type(self) -> None:
        """Verify 'step_progress' is valid for EventType via AgentEvent."""
        event = AgentEvent(
            type="step_progress",
            agent="specialist_k8s-prod",
            data={"step": 1, "max_steps": 15},
        )
        assert event.type == "step_progress"
        assert event.data["step"] == 1
        assert event.data["max_steps"] == 15
