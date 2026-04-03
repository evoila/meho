# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Contract tests for Orchestrator Agent API (TASK-181).

Verifies that the OrchestratorAgent provides the APIs expected by the BFF (meho-api).
Tests event schemas, method signatures, and configuration contracts.
"""

import inspect
from dataclasses import fields


class TestOrchestratorAgentContract:
    """Test OrchestratorAgent API contract."""

    def test_orchestrator_agent_class_exists(self):
        """Verify OrchestratorAgent class exists."""
        from meho_app.modules.agents.orchestrator.agent import OrchestratorAgent

        assert OrchestratorAgent is not None  # NOSONAR -- intentional identity check

    def test_orchestrator_agent_has_run_streaming_method(self):
        """Verify run_streaming method exists."""
        from meho_app.modules.agents.orchestrator.agent import OrchestratorAgent

        assert hasattr(OrchestratorAgent, "run_streaming")

    def test_orchestrator_run_streaming_signature(self):
        """Verify run_streaming has expected parameters."""
        from meho_app.modules.agents.orchestrator.agent import OrchestratorAgent

        sig = inspect.signature(OrchestratorAgent.run_streaming)
        params = list(sig.parameters.keys())

        # Expected parameters
        assert "self" in params
        assert "user_message" in params
        assert "session_id" in params
        assert "context" in params

    def test_orchestrator_has_agent_name(self):
        """Verify orchestrator has correct agent_name class var."""
        from meho_app.modules.agents.orchestrator.agent import OrchestratorAgent

        assert hasattr(OrchestratorAgent, "agent_name")
        assert OrchestratorAgent.agent_name == "orchestrator"


class TestOrchestratorStateContract:
    """Test OrchestratorState contract."""

    def test_orchestrator_state_exists(self):
        """Verify OrchestratorState dataclass exists."""
        from meho_app.modules.agents.orchestrator.state import OrchestratorState

        assert OrchestratorState is not None  # NOSONAR -- intentional identity check

    def test_orchestrator_state_fields(self):
        """Verify OrchestratorState has expected fields."""
        from meho_app.modules.agents.orchestrator.state import OrchestratorState

        field_names = {f.name for f in fields(OrchestratorState)}

        # Required fields
        assert "user_goal" in field_names
        assert "session_id" in field_names
        assert "current_iteration" in field_names
        assert "max_iterations" in field_names
        assert "all_findings" in field_names
        assert "final_answer" in field_names
        assert "should_continue" in field_names

    def test_orchestrator_state_methods(self):
        """Verify OrchestratorState has expected methods."""
        from meho_app.modules.agents.orchestrator.state import OrchestratorState

        assert hasattr(OrchestratorState, "add_iteration_findings")
        assert hasattr(OrchestratorState, "get_findings_summary")
        assert hasattr(OrchestratorState, "get_queried_connector_ids")
        assert hasattr(OrchestratorState, "has_sufficient_findings")
        assert hasattr(OrchestratorState, "is_last_iteration")


class TestConnectorSelectionContract:
    """Test ConnectorSelection contract."""

    def test_connector_selection_exists(self):
        """Verify ConnectorSelection dataclass exists."""
        from meho_app.modules.agents.orchestrator.state import ConnectorSelection

        assert ConnectorSelection is not None  # NOSONAR -- intentional identity check

    def test_connector_selection_fields(self):
        """Verify ConnectorSelection has expected fields."""
        from meho_app.modules.agents.orchestrator.state import ConnectorSelection

        field_names = {f.name for f in fields(ConnectorSelection)}

        assert "connector_id" in field_names
        assert "connector_name" in field_names
        assert "routing_description" in field_names
        assert "relevance_score" in field_names
        assert "reason" in field_names


class TestSubgraphOutputContract:
    """Test SubgraphOutput contract."""

    def test_subgraph_output_exists(self):
        """Verify SubgraphOutput dataclass exists."""
        from meho_app.modules.agents.orchestrator.contracts import SubgraphOutput

        assert SubgraphOutput is not None  # NOSONAR -- intentional identity check

    def test_subgraph_output_fields(self):
        """Verify SubgraphOutput has expected fields."""
        from meho_app.modules.agents.orchestrator.contracts import SubgraphOutput

        field_names = {f.name for f in fields(SubgraphOutput)}

        assert "connector_id" in field_names
        assert "connector_name" in field_names
        assert "findings" in field_names
        assert "status" in field_names
        assert "error_message" in field_names
        assert "execution_time_ms" in field_names

    def test_subgraph_output_status_values(self):
        """Verify SubgraphOutput supports expected status values."""
        from meho_app.modules.agents.orchestrator.contracts import SubgraphOutput

        # Test each status type
        statuses = ["success", "partial", "failed", "timeout", "cancelled"]
        for status in statuses:
            output = SubgraphOutput(
                connector_id="test-id",
                connector_name="Test",
                findings="",
                status=status,
            )
            assert output.status == status


class TestWrappedEventContract:
    """Test WrappedEvent contract."""

    def test_wrapped_event_exists(self):
        """Verify WrappedEvent dataclass exists."""
        from meho_app.modules.agents.orchestrator.contracts import WrappedEvent

        assert WrappedEvent is not None  # NOSONAR -- intentional identity check

    def test_wrapped_event_fields(self):
        """Verify WrappedEvent has expected fields."""
        from meho_app.modules.agents.orchestrator.contracts import WrappedEvent

        field_names = {f.name for f in fields(WrappedEvent)}

        assert "agent_source" in field_names
        assert "inner_event" in field_names

    def test_wrapped_event_to_sse(self):
        """Verify WrappedEvent.to_sse() returns valid SSE format."""
        from meho_app.modules.agents.orchestrator.contracts import WrappedEvent

        event = WrappedEvent(
            agent_source={
                "agent_name": "test_agent",
                "connector_id": "conn-1",
                "connector_name": "Test",
                "iteration": 1,
            },
            inner_event={
                "type": "thought",
                "data": {"content": "Thinking..."},
            },
        )

        sse_string = event.to_sse()

        assert sse_string.startswith("data: ")
        assert sse_string.endswith("\n\n")
        assert "agent_event" in sse_string

    def test_wrapped_event_to_dict(self):
        """Verify WrappedEvent.to_dict() includes type field."""
        from meho_app.modules.agents.orchestrator.contracts import WrappedEvent

        event = WrappedEvent(
            agent_source={"agent_name": "test"},
            inner_event={"type": "thought", "data": {}},
        )

        result = event.to_dict()

        assert result["type"] == "agent_event"
        assert "agent_source" in result
        assert "inner_event" in result


class TestEventWrapperContract:
    """Test EventWrapper contract."""

    def test_event_wrapper_exists(self):
        """Verify EventWrapper class exists."""
        from meho_app.modules.agents.orchestrator.event_wrapper import EventWrapper

        assert EventWrapper is not None  # NOSONAR -- intentional identity check

    def test_event_wrapper_wrap_method(self):
        """Verify EventWrapper.wrap() method exists."""
        from meho_app.modules.agents.orchestrator.event_wrapper import EventWrapper

        assert hasattr(EventWrapper, "wrap")


class TestOrchestratorConfigContract:
    """Test orchestrator configuration contract."""

    def test_config_loader_exists(self):
        """Verify config loader works for orchestrator."""
        from pathlib import Path

        from meho_app.modules.agents.config.loader import load_yaml_config

        config_path = (
            Path(__file__).parent.parent.parent
            / "meho_app"
            / "modules"
            / "agents"
            / "orchestrator"
            / "config.yaml"
        )

        # Should not raise
        config = load_yaml_config(config_path)
        assert config is not None

    def test_orchestrator_config_has_required_sections(self):
        """Verify config.yaml has required sections."""
        from pathlib import Path

        import yaml

        config_path = (
            Path(__file__).parent.parent.parent
            / "meho_app"
            / "modules"
            / "agents"
            / "orchestrator"
            / "config.yaml"
        )

        with open(config_path) as f:
            config = yaml.safe_load(f)

        assert "orchestrator" in config
        orch = config["orchestrator"]

        # Check required keys
        assert "max_iterations" in orch
        assert "agent_timeout" in orch
        assert "total_timeout" in orch

    def test_orchestrator_config_values_valid(self):
        """Verify config.yaml values are valid."""
        from pathlib import Path

        import yaml

        config_path = (
            Path(__file__).parent.parent.parent
            / "meho_app"
            / "modules"
            / "agents"
            / "orchestrator"
            / "config.yaml"
        )

        with open(config_path) as f:
            config = yaml.safe_load(f)

        orch = config["orchestrator"]

        # Validate values
        assert orch["max_iterations"] >= 1
        assert orch["agent_timeout"] > 0
        assert orch["total_timeout"] > 0


class TestCoreConfigOrchestratorOverrides:
    """Test core config has orchestrator overrides."""

    def test_core_config_has_orchestrator_fields(self):
        """Verify core Config has orchestrator override fields."""
        from meho_app.core.config import Config

        fields = Config.model_fields

        assert "orchestrator_max_iterations" in fields
        assert "orchestrator_agent_timeout" in fields
        assert "orchestrator_total_timeout" in fields
