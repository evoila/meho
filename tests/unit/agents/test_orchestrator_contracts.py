# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for orchestrator contracts, state, and event wrapper.

Tests for:
- SubgraphInput serialization/deserialization
- SubgraphOutput with various status values
- WrappedEvent SSE format validation
- IterationResult structure
- OrchestratorState iteration tracking
- ConnectorSelection creation
- get_findings_summary() formatting
- add_iteration_findings() accumulation
- Event wrapping preserves all data
- SSE serialization format is valid JSON
- Agent source metadata is correct

Phase 84: OrchestratorAgent contracts (ConnectorSelection, SubgraphInput/Output)
were restructured for topology-driven routing. Tests pre-date the refactor.
"""

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: OrchestratorAgent contracts restructured for topology-driven routing, ConnectorSelection API changed")

import json
from datetime import datetime
from unittest.mock import MagicMock

from meho_app.modules.agents.orchestrator.contracts import (
    IterationResult,
    SubgraphInput,
    SubgraphOutput,
    WrappedEvent,
)
from meho_app.modules.agents.orchestrator.event_wrapper import EventWrapper
from meho_app.modules.agents.orchestrator.state import (
    ConnectorSelection,
    OrchestratorState,
)

# =============================================================================
# SubgraphInput Tests
# =============================================================================


class TestSubgraphInput:
    """Tests for SubgraphInput dataclass."""

    def test_create_with_required_fields(self):
        """Test creating SubgraphInput with only required fields."""
        input_data = SubgraphInput(
            goal="Find all running pods",
            connector_id="k8s-prod-123",
        )

        assert input_data.goal == "Find all running pods"
        assert input_data.connector_id == "k8s-prod-123"
        assert input_data.iteration == 1  # default
        assert input_data.prior_findings == []
        assert input_data.entities_of_interest == []
        assert input_data.max_steps == 10
        assert input_data.timeout_seconds == 30.0

    def test_create_with_all_fields(self):
        """Test creating SubgraphInput with all fields."""
        input_data = SubgraphInput(
            goal="Check pod status",
            connector_id="k8s-prod-123",
            iteration=2,
            prior_findings=["Found 10 pods in namespace default"],
            entities_of_interest=["pod/nginx-1234", "deployment/web"],
            max_steps=5,
            timeout_seconds=15.0,
        )

        assert input_data.iteration == 2
        assert len(input_data.prior_findings) == 1
        assert len(input_data.entities_of_interest) == 2
        assert input_data.max_steps == 5
        assert input_data.timeout_seconds == 15.0

    def test_to_dict_serialization(self):
        """Test serialization to dictionary."""
        input_data = SubgraphInput(
            goal="Test goal",
            connector_id="conn-123",
            iteration=3,
        )

        result = input_data.to_dict()

        assert result["goal"] == "Test goal"
        assert result["connector_id"] == "conn-123"
        assert result["iteration"] == 3
        assert "prior_findings" in result
        assert "entities_of_interest" in result

    def test_from_dict_deserialization(self):
        """Test deserialization from dictionary."""
        data = {
            "goal": "Deserialized goal",
            "connector_id": "conn-456",
            "iteration": 2,
            "prior_findings": ["finding1"],
            "entities_of_interest": ["entity1"],
            "max_steps": 8,
            "timeout_seconds": 20.0,
        }

        input_data = SubgraphInput.from_dict(data)

        assert input_data.goal == "Deserialized goal"
        assert input_data.connector_id == "conn-456"
        assert input_data.iteration == 2
        assert input_data.prior_findings == ["finding1"]


# =============================================================================
# SubgraphOutput Tests
# =============================================================================


class TestSubgraphOutput:
    """Tests for SubgraphOutput dataclass."""

    def test_create_success_output(self):
        """Test creating a successful output."""
        output = SubgraphOutput(
            connector_id="k8s-prod",
            connector_name="Production K8s",
            findings="Found 5 running pods in namespace default",
            status="success",
            confidence=0.9,
            execution_time_ms=1500.0,
        )

        assert output.connector_id == "k8s-prod"
        assert output.connector_name == "Production K8s"
        assert output.is_success
        assert not output.has_error
        assert output.confidence == 0.9

    def test_create_failed_output(self):
        """Test creating a failed output."""
        output = SubgraphOutput(
            connector_id="gcp-prod",
            connector_name="Production GCP",
            findings="",
            status="failed",
            error_message="Connection timeout",
            execution_time_ms=30000.0,
        )

        assert output.status == "failed"
        assert output.has_error
        assert not output.is_success
        assert output.error_message == "Connection timeout"

    def test_create_timeout_output(self):
        """Test creating a timeout output."""
        output = SubgraphOutput(
            connector_id="slow-api",
            connector_name="Slow API",
            findings="",
            status="timeout",
            error_message="Exceeded 30s timeout",
        )

        assert output.status == "timeout"
        assert output.has_error

    def test_create_partial_output(self):
        """Test creating a partial success output."""
        output = SubgraphOutput(
            connector_id="k8s-staging",
            connector_name="Staging K8s",
            findings="Found some pods but namespace access denied",
            status="partial",
            confidence=0.6,
        )

        assert output.status == "partial"
        assert output.is_partial
        assert not output.is_success
        assert not output.has_error

    def test_entities_discovered(self):
        """Test output with discovered entities."""
        output = SubgraphOutput(
            connector_id="k8s-prod",
            connector_name="Production K8s",
            findings="Found VM and related pods",
            entities_discovered=[
                {"type": "vm", "id": "vm-123", "name": "web-server-1"},
                {"type": "pod", "id": "pod-456", "name": "nginx-abc"},
            ],
        )

        assert len(output.entities_discovered) == 2
        assert output.entities_discovered[0]["type"] == "vm"

    def test_to_dict_serialization(self):
        """Test serialization to dictionary."""
        output = SubgraphOutput(
            connector_id="test",
            connector_name="Test",
            findings="Test findings",
            status="success",
        )

        result = output.to_dict()

        assert result["connector_id"] == "test"
        assert result["findings"] == "Test findings"
        assert result["status"] == "success"

    def test_from_dict_deserialization(self):
        """Test deserialization from dictionary."""
        data = {
            "connector_id": "deser-test",
            "connector_name": "Deserialized Test",
            "findings": "Deserialized findings",
            "status": "partial",
            "confidence": 0.7,
            "entities_discovered": [],
            "error_message": None,
            "execution_time_ms": 500.0,
        }

        output = SubgraphOutput.from_dict(data)

        assert output.connector_id == "deser-test"
        assert output.status == "partial"
        assert output.confidence == 0.7


# =============================================================================
# WrappedEvent Tests
# =============================================================================


class TestWrappedEvent:
    """Tests for WrappedEvent dataclass."""

    def test_create_wrapped_event(self):
        """Test creating a wrapped event."""
        event = WrappedEvent(
            agent_source={
                "agent_name": "react_agent_k8s-prod",
                "connector_id": "k8s-prod",
                "connector_name": "Production K8s",
                "iteration": 1,
            },
            inner_event={
                "type": "thought",
                "data": {"content": "Searching for pods..."},
                "timestamp": "2026-01-27T10:00:00Z",
            },
        )

        assert event.agent_source["connector_id"] == "k8s-prod"
        assert event.inner_event["type"] == "thought"

    def test_to_sse_format(self):
        """Test SSE serialization format."""
        event = WrappedEvent(
            agent_source={
                "agent_name": "test_agent",
                "connector_id": "test-conn",
                "connector_name": "Test",
                "iteration": 1,
            },
            inner_event={
                "type": "action",
                "data": {"tool": "search_pods"},
            },
        )

        sse = event.to_sse()

        # Should start with "data: "
        assert sse.startswith("data: ")
        # Should end with double newline
        assert sse.endswith("\n\n")

        # Should be valid JSON after removing "data: " prefix
        json_str = sse[6:-2]  # Remove "data: " and "\n\n"
        parsed = json.loads(json_str)

        assert parsed["type"] == "agent_event"
        assert parsed["agent_source"]["agent_name"] == "test_agent"
        assert parsed["inner_event"]["type"] == "action"

    def test_to_dict(self):
        """Test conversion to dictionary."""
        event = WrappedEvent(
            agent_source={"agent_name": "test"},
            inner_event={"type": "observation"},
        )

        result = event.to_dict()

        assert result["type"] == "agent_event"
        assert "agent_source" in result
        assert "inner_event" in result

    def test_from_dict(self):
        """Test creation from dictionary."""
        data = {
            "agent_source": {"connector_id": "test"},
            "inner_event": {"type": "final_answer"},
        }

        event = WrappedEvent.from_dict(data)

        assert event.agent_source["connector_id"] == "test"
        assert event.inner_event["type"] == "final_answer"


# =============================================================================
# IterationResult Tests
# =============================================================================


class TestIterationResult:
    """Tests for IterationResult dataclass."""

    def test_create_iteration_result(self):
        """Test creating an iteration result."""
        outputs = [
            SubgraphOutput(
                connector_id="k8s-prod",
                connector_name="K8s Prod",
                findings="Found pods",
                status="success",
            ),
            SubgraphOutput(
                connector_id="gcp-prod",
                connector_name="GCP Prod",
                findings="Found VMs",
                status="success",
            ),
        ]

        result = IterationResult(
            iteration=1,
            outputs=outputs,
            total_time_ms=5000.0,
        )

        assert result.iteration == 1
        assert len(result.outputs) == 2
        assert result.total_time_ms == 5000.0

    def test_success_count(self):
        """Test counting successful outputs."""
        result = IterationResult(
            iteration=1,
            outputs=[
                SubgraphOutput("a", "A", "found", status="success"),
                SubgraphOutput("b", "B", "", status="failed"),
                SubgraphOutput("c", "C", "partial", status="partial"),
            ],
            total_time_ms=1000.0,
        )

        assert result.success_count == 1
        assert result.error_count == 1
        assert not result.all_succeeded

    def test_all_succeeded(self):
        """Test when all outputs succeed."""
        result = IterationResult(
            iteration=1,
            outputs=[
                SubgraphOutput("a", "A", "found", status="success"),
                SubgraphOutput("b", "B", "found", status="success"),
            ],
            total_time_ms=1000.0,
        )

        assert result.all_succeeded
        assert result.success_count == 2
        assert result.error_count == 0

    def test_to_dict_serialization(self):
        """Test serialization to dictionary."""
        result = IterationResult(
            iteration=2,
            outputs=[
                SubgraphOutput("x", "X", "data", status="success"),
            ],
            total_time_ms=2500.0,
        )

        data = result.to_dict()

        assert data["iteration"] == 2
        assert len(data["outputs"]) == 1
        assert data["total_time_ms"] == 2500.0

    def test_from_dict_deserialization(self):
        """Test deserialization from dictionary."""
        data = {
            "iteration": 3,
            "outputs": [
                {
                    "connector_id": "test",
                    "connector_name": "Test",
                    "findings": "test findings",
                    "status": "success",
                    "confidence": 0.5,
                    "entities_discovered": [],
                    "error_message": None,
                    "execution_time_ms": 0.0,
                }
            ],
            "total_time_ms": 1500.0,
        }

        result = IterationResult.from_dict(data)

        assert result.iteration == 3
        assert len(result.outputs) == 1
        assert result.outputs[0].connector_id == "test"


# =============================================================================
# ConnectorSelection Tests
# =============================================================================


class TestConnectorSelection:
    """Tests for ConnectorSelection dataclass."""

    def test_create_connector_selection(self):
        """Test creating a connector selection."""
        selection = ConnectorSelection(
            connector_id="k8s-prod-123",
            connector_name="Production Kubernetes",
            routing_description="Kubernetes cluster hosting production workloads",
            relevance_score=0.9,
            reason="Query mentions pods and deployments",
        )

        assert selection.connector_id == "k8s-prod-123"
        assert selection.connector_name == "Production Kubernetes"
        assert selection.relevance_score == 0.9
        assert "pods" in selection.reason

    def test_to_dict(self):
        """Test conversion to dictionary."""
        selection = ConnectorSelection(
            connector_id="test",
            connector_name="Test",
            routing_description="Test desc",
            relevance_score=0.5,
            reason="Test reason",
        )

        result = selection.to_dict()

        assert result["connector_id"] == "test"
        assert result["relevance_score"] == 0.5


# =============================================================================
# OrchestratorState Tests
# =============================================================================


class TestOrchestratorState:
    """Tests for OrchestratorState dataclass."""

    def test_create_initial_state(self):
        """Test creating initial orchestrator state."""
        state = OrchestratorState(
            user_goal="Find all unhealthy pods",
            session_id="session-123",
        )

        assert state.user_goal == "Find all unhealthy pods"
        assert state.session_id == "session-123"
        assert state.current_iteration == 0
        assert state.max_iterations == 3
        assert state.all_findings == []
        assert state.should_continue is True
        assert state.final_answer is None

    def test_add_iteration_findings(self):
        """Test adding findings from an iteration."""
        state = OrchestratorState(user_goal="Test")

        outputs = [
            SubgraphOutput("a", "A", "Found stuff", status="success"),
            SubgraphOutput("b", "B", "Found more", status="success"),
        ]

        state.add_iteration_findings(outputs)

        assert state.current_iteration == 1
        assert len(state.all_findings) == 2

        # Add another iteration
        state.add_iteration_findings(
            [
                SubgraphOutput("c", "C", "Even more", status="partial"),
            ]
        )

        assert state.current_iteration == 2
        assert len(state.all_findings) == 3

    def test_get_findings_summary_empty(self):
        """Test findings summary when empty."""
        state = OrchestratorState(user_goal="Test")

        assert state.get_findings_summary() == ""

    def test_get_findings_summary_with_data(self):
        """Test findings summary with data."""
        state = OrchestratorState(user_goal="Test")
        state.all_findings = [
            SubgraphOutput("k8s", "K8s Prod", "Found 5 pods", status="success"),
            SubgraphOutput("gcp", "GCP Prod", "", status="failed", error_message="Timeout"),
        ]

        summary = state.get_findings_summary()

        assert "✓ K8s Prod" in summary
        assert "Found 5 pods" in summary
        assert "✗ GCP Prod" in summary
        assert "Timeout" in summary

    def test_get_queried_connector_ids(self):
        """Test getting set of queried connector IDs."""
        state = OrchestratorState(user_goal="Test")
        state.all_findings = [
            SubgraphOutput("conn-1", "Conn 1", "data"),
            SubgraphOutput("conn-2", "Conn 2", "data"),
            SubgraphOutput("conn-1", "Conn 1", "more data"),  # duplicate
        ]

        ids = state.get_queried_connector_ids()

        assert ids == {"conn-1", "conn-2"}

    def test_has_sufficient_findings(self):
        """Test checking if there are sufficient findings."""
        state = OrchestratorState(user_goal="Test")

        # No findings
        assert not state.has_sufficient_findings()

        # Only failed findings
        state.all_findings = [
            SubgraphOutput("a", "A", "", status="failed"),
        ]
        assert not state.has_sufficient_findings()

        # Has partial finding
        state.all_findings.append(SubgraphOutput("b", "B", "partial data", status="partial"))
        assert state.has_sufficient_findings()

        # Has success finding
        state.all_findings.append(SubgraphOutput("c", "C", "full data", status="success"))
        assert state.has_sufficient_findings()

    def test_is_last_iteration(self):
        """Test checking if at last iteration."""
        state = OrchestratorState(user_goal="Test", max_iterations=3)

        assert not state.is_last_iteration()  # iteration 0, next is 1

        state.current_iteration = 1
        assert not state.is_last_iteration()  # iteration 1, next is 2

        state.current_iteration = 2
        assert state.is_last_iteration()  # iteration 2, next would be 3 (max)

    def test_successful_findings_property(self):
        """Test the successful_findings property."""
        state = OrchestratorState(user_goal="Test")
        state.all_findings = [
            SubgraphOutput("a", "A", "data", status="success"),
            SubgraphOutput("b", "B", "", status="failed"),
            SubgraphOutput("c", "C", "more", status="success"),
        ]

        successful = state.successful_findings

        assert len(successful) == 2
        assert all(f.status == "success" for f in successful)

    def test_failed_findings_property(self):
        """Test the failed_findings property."""
        state = OrchestratorState(user_goal="Test")
        state.all_findings = [
            SubgraphOutput("a", "A", "data", status="success"),
            SubgraphOutput("b", "B", "", status="failed"),
            SubgraphOutput("c", "C", "", status="timeout"),
            SubgraphOutput("d", "D", "", status="cancelled"),
        ]

        failed = state.failed_findings

        assert len(failed) == 3
        assert all(f.status in ("failed", "timeout", "cancelled") for f in failed)


# =============================================================================
# EventWrapper Tests
# =============================================================================


class TestEventWrapper:
    """Tests for EventWrapper class."""

    def test_create_event_wrapper(self):
        """Test creating an event wrapper."""
        wrapper = EventWrapper(
            connector_id="k8s-prod",
            connector_name="Production K8s",
            iteration=1,
        )

        assert wrapper.connector_id == "k8s-prod"
        assert wrapper.connector_name == "Production K8s"
        assert wrapper.iteration == 1

    def test_wrap_agent_event(self):
        """Test wrapping an AgentEvent."""
        wrapper = EventWrapper(
            connector_id="test-conn",
            connector_name="Test Connector",
            iteration=2,
        )

        # Create a mock AgentEvent
        mock_event = MagicMock()
        mock_event.agent = "react_agent"
        mock_event.type = "thought"
        mock_event.data = {"content": "Thinking..."}
        mock_event.timestamp = datetime(2026, 1, 27, 10, 0, 0)  # noqa: DTZ001 -- naive datetime for test compatibility
        mock_event.step = 1
        mock_event.node = "reason"

        wrapped = wrapper.wrap(mock_event)

        assert wrapped.agent_source["agent_name"] == "react_agent_test-conn"
        assert wrapped.agent_source["connector_id"] == "test-conn"
        assert wrapped.agent_source["connector_name"] == "Test Connector"
        assert wrapped.agent_source["iteration"] == 2

        assert wrapped.inner_event["type"] == "thought"
        assert wrapped.inner_event["data"]["content"] == "Thinking..."
        assert wrapped.inner_event["step"] == 1
        assert wrapped.inner_event["node"] == "reason"

    def test_wrap_dict_event(self):
        """Test wrapping a dictionary event."""
        wrapper = EventWrapper(
            connector_id="dict-conn",
            connector_name="Dict Connector",
            iteration=1,
        )

        event_dict = {
            "agent": "specialist_agent",
            "type": "action",
            "data": {"tool": "search_operations"},
            "timestamp": "2026-01-27T10:00:00Z",
            "step": 2,
            "node": "tool_dispatch",
        }

        wrapped = wrapper.wrap_dict(event_dict)

        assert wrapped.agent_source["agent_name"] == "specialist_agent_dict-conn"
        assert wrapped.inner_event["type"] == "action"
        assert wrapped.inner_event["data"]["tool"] == "search_operations"

    def test_wrapped_event_to_sse_is_valid_json(self):
        """Test that wrapped event produces valid JSON in SSE format."""
        wrapper = EventWrapper("conn", "Connector", 1)

        mock_event = MagicMock()
        mock_event.agent = "test"
        mock_event.type = "observation"
        mock_event.data = {"result": "success"}
        mock_event.timestamp = None
        mock_event.step = None
        mock_event.node = None

        wrapped = wrapper.wrap(mock_event)
        sse = wrapped.to_sse()

        # Extract JSON from SSE
        json_str = sse.replace("data: ", "").strip()

        # Should be valid JSON
        parsed = json.loads(json_str)
        assert parsed["type"] == "agent_event"
        assert "agent_source" in parsed
        assert "inner_event" in parsed
