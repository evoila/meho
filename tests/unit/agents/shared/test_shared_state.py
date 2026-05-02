# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for shared state classes.

Tests WorkflowState, WorkflowResult, and BaseAgentState classes
that are shared across all agent types.
"""

from meho_app.modules.agents.shared import (
    BaseAgentState,
    WorkflowResult,
    WorkflowState,
)


class TestWorkflowState:
    """Tests for WorkflowState dataclass."""

    def test_minimal_creation(self):
        """WorkflowState can be created with just user_goal."""
        state = WorkflowState(user_goal="List all VMs")
        assert state.user_goal == "List all VMs"
        assert state.connector_id == ""
        assert state.connector_name == ""
        assert state.steps_executed == []
        assert state.session_state is None
        assert state.session_id is None
        assert state.cached_tables == {}

    def test_full_creation(self):
        """WorkflowState can be created with all fields."""
        cached = {"vms": {"row_count": 10, "columns": ["name", "id"]}}
        state = WorkflowState(
            user_goal="List all VMs",
            connector_id="conn-123",
            connector_name="vSphere",
            steps_executed=["step1", "step2"],
            session_id="session-456",
            cached_tables=cached,
        )
        assert state.user_goal == "List all VMs"
        assert state.connector_id == "conn-123"
        assert state.connector_name == "vSphere"
        assert state.steps_executed == ["step1", "step2"]
        assert state.session_id == "session-456"
        assert state.cached_tables == cached

    def test_steps_executed_is_mutable(self):
        """steps_executed list can be appended to."""
        state = WorkflowState(user_goal="Test")
        state.steps_executed.append("New step")
        assert "New step" in state.steps_executed


class TestWorkflowResult:
    """Tests for WorkflowResult dataclass."""

    def test_success_result(self):
        """WorkflowResult for successful workflow."""
        result = WorkflowResult(
            success=True,
            findings='{"vms": []}',
            steps_executed=["search", "execute"],
        )
        assert result.success is True
        assert result.findings == '{"vms": []}'
        assert result.steps_executed == ["search", "execute"]
        assert result.error is None

    def test_error_result(self):
        """WorkflowResult for failed workflow."""
        result = WorkflowResult(
            success=False,
            findings="",
            steps_executed=["search"],
            error="Connection timeout",
        )
        assert result.success is False
        assert result.findings == ""
        assert result.error == "Connection timeout"


class TestBaseAgentState:
    """Tests for BaseAgentState dataclass."""

    def test_minimal_creation(self):
        """BaseAgentState can be created with required fields."""
        state = BaseAgentState(
            user_goal="List pods",
            connector_id="k8s-123",
        )
        assert state.user_goal == "List pods"
        assert state.connector_id == "k8s-123"
        assert state.connector_name == ""
        assert state.connector_type == ""
        assert state.iteration == 1
        assert state.prior_findings == []
        assert state.scratchpad == []
        assert state.step_count == 0
        assert state.pending_tool is None
        assert state.pending_args is None
        assert state.last_observation is None
        assert state.final_answer is None
        assert state.error_message is None
        assert state.approval_granted is False

    def test_add_to_scratchpad(self):
        """add_to_scratchpad appends entries."""
        state = BaseAgentState(user_goal="Test", connector_id="test")
        state.add_to_scratchpad("Thought: I should list pods")
        state.add_to_scratchpad("Action: get_pods")
        assert len(state.scratchpad) == 2
        assert "Thought: I should list pods" in state.scratchpad
        assert "Action: get_pods" in state.scratchpad

    def test_get_scratchpad_text(self):
        """get_scratchpad_text formats entries with newlines."""
        state = BaseAgentState(user_goal="Test", connector_id="test")
        state.add_to_scratchpad("Line 1")
        state.add_to_scratchpad("Line 2")
        text = state.get_scratchpad_text()
        assert text == "Line 1\nLine 2"

    def test_get_scratchpad_text_empty(self):
        """get_scratchpad_text returns empty string when empty."""
        state = BaseAgentState(user_goal="Test", connector_id="test")
        assert state.get_scratchpad_text() == ""

    def test_clear_pending_action(self):
        """clear_pending_action resets pending_tool and pending_args."""
        state = BaseAgentState(user_goal="Test", connector_id="test")
        state.pending_tool = "get_pods"
        state.pending_args = {"namespace": "default"}
        state.clear_pending_action()
        assert state.pending_tool is None
        assert state.pending_args is None

    def test_is_complete_with_final_answer(self):
        """is_complete returns True when final_answer is set."""
        state = BaseAgentState(user_goal="Test", connector_id="test")
        assert state.is_complete() is False
        state.final_answer = "Here are your pods"
        assert state.is_complete() is True

    def test_is_complete_with_error(self):
        """is_complete returns True when error_message is set."""
        state = BaseAgentState(user_goal="Test", connector_id="test")
        assert state.is_complete() is False
        state.error_message = "Connection failed"
        assert state.is_complete() is True

    def test_get_prior_findings_text_empty(self):
        """get_prior_findings_text returns default message when empty."""
        state = BaseAgentState(user_goal="Test", connector_id="test")
        text = state.get_prior_findings_text()
        assert text == "No prior findings from previous iterations."

    def test_get_prior_findings_text_with_findings(self):
        """get_prior_findings_text formats findings with labels."""
        state = BaseAgentState(user_goal="Test", connector_id="test")
        state.prior_findings = ["Found 5 pods", "Pod nginx is unhealthy"]
        text = state.get_prior_findings_text()
        assert "[Previous Finding 1]: Found 5 pods" in text
        assert "[Previous Finding 2]: Pod nginx is unhealthy" in text
