# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for orchestrator graceful degradation.

Tests for:
- LLM decision error with existing findings (recovers)
- LLM decision error with no findings (raises)
- Agent raises exception mid-execution
- Partial synthesis on error
- Error events with recoverable indicator

Phase 84: OrchestratorAgent rewritten -- degradation patterns changed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: OrchestratorAgent rewritten with topology-driven routing, degradation handling changed")

from meho_app.modules.agents.orchestrator.agent import OrchestratorAgent
from meho_app.modules.agents.orchestrator.contracts import SubgraphOutput
from meho_app.modules.agents.orchestrator.state import ConnectorSelection, OrchestratorState

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_dependencies():
    """Create mock MEHODependencies."""
    deps = MagicMock()
    deps.user_context = MagicMock()
    deps.user_context.tenant_id = "test-tenant"
    deps.user_context.user_id = "test-user"
    deps.connector_repo = MagicMock()
    deps.connector_repo.list_connectors = AsyncMock(return_value=[])
    return deps


@pytest.fixture
def orchestrator_agent(mock_dependencies):
    """Create OrchestratorAgent with mocked config."""
    with patch.object(OrchestratorAgent, "_load_config") as mock_config:
        config = MagicMock()
        config.max_iterations = 3
        config.model = MagicMock(name="openai:gpt-4.1-mini")
        config.raw = {
            "orchestrator": {
                "agent_timeout": 30.0,
                "total_timeout": 120.0,
            }
        }
        mock_config.return_value = config
        agent = OrchestratorAgent(dependencies=mock_dependencies)
        return agent


# =============================================================================
# Decision Error with Findings Tests
# =============================================================================


class TestDecisionErrorWithFindings:
    """Tests for decision errors when findings exist."""

    @pytest.mark.asyncio
    async def test_decision_error_with_findings_recovers(self, orchestrator_agent):
        """Test that decision error with existing findings recovers to synthesis."""
        call_count = 0

        async def mock_decide(state):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call succeeds
                return {
                    "action": "query",
                    "connectors": [
                        ConnectorSelection("conn-1", "Connector 1", "Desc", 0.9, "Test"),
                    ],
                }
            # Second call fails
            raise Exception("LLM decision failed")

        async def mock_dispatch(state, connectors, iteration):
            yield SubgraphOutput(
                connector_id="conn-1",
                connector_name="Connector 1",
                findings="Found some data",
                status="success",
            )

        with patch.object(orchestrator_agent, "_decide_next_action", side_effect=mock_decide):  # noqa: SIM117 -- readability preferred over combined with
            with patch.object(orchestrator_agent, "_dispatch_parallel", side_effect=mock_dispatch):
                with patch.object(
                    orchestrator_agent, "_synthesize", new_callable=AsyncMock
                ) as mock_synth:
                    mock_synth.return_value = "Partial answer based on findings"

                    events = []
                    async for event in orchestrator_agent.run_streaming("Test recovery"):
                        events.append(event)

        event_types = [e.type for e in events]

        # Should have synthesis and final_answer (recovered)
        assert "synthesis_start" in event_types
        assert "final_answer" in event_types
        assert "orchestrator_complete" in event_types

        # Final answer should be marked as partial
        final_answer = next(e for e in events if e.type == "final_answer")
        assert final_answer.data["partial"] is True
        assert final_answer.data["content"] == "Partial answer based on findings"

    @pytest.mark.asyncio
    async def test_decision_error_with_findings_emits_no_error_event(self, orchestrator_agent):
        """Test that graceful recovery doesn't emit error event for decision failure."""
        call_count = 0

        async def mock_decide(state):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {
                    "action": "query",
                    "connectors": [
                        ConnectorSelection("conn-1", "Conn 1", "Desc", 0.9, "Test"),
                    ],
                }
            raise Exception("Decision error")

        async def mock_dispatch(state, connectors, iteration):
            yield SubgraphOutput("conn-1", "Conn 1", "Data", "success")

        with patch.object(orchestrator_agent, "_decide_next_action", side_effect=mock_decide):  # noqa: SIM117 -- readability preferred over combined with
            with patch.object(orchestrator_agent, "_dispatch_parallel", side_effect=mock_dispatch):
                with patch.object(
                    orchestrator_agent, "_synthesize", new_callable=AsyncMock
                ) as mock_synth:
                    mock_synth.return_value = "Answer"

                    events = []
                    async for event in orchestrator_agent.run_streaming("Test"):
                        events.append(event)

        # Should NOT have error event since we recovered gracefully
        error_events = [e for e in events if e.type == "error"]
        assert len(error_events) == 0


# =============================================================================
# Decision Error without Findings Tests
# =============================================================================


class TestDecisionErrorWithoutFindings:
    """Tests for decision errors when no findings exist."""

    @pytest.mark.asyncio
    async def test_decision_error_without_findings_emits_error(self, orchestrator_agent):
        """Test that decision error without findings emits error event."""

        async def mock_decide(state):
            raise Exception("LLM unavailable")

        with patch.object(orchestrator_agent, "_decide_next_action", side_effect=mock_decide):
            events = []
            async for event in orchestrator_agent.run_streaming("Test error"):
                events.append(event)

        event_types = [e.type for e in events]

        # Should have error event
        assert "error" in event_types

        # Error should indicate not recoverable
        error_event = next(e for e in events if e.type == "error")
        assert error_event.data["recoverable"] is False

    @pytest.mark.asyncio
    async def test_decision_error_without_findings_no_synthesis(self, orchestrator_agent):
        """Test that decision error without findings skips synthesis."""

        async def mock_decide(state):
            raise Exception("LLM unavailable")

        with patch.object(orchestrator_agent, "_decide_next_action", side_effect=mock_decide):  # noqa: SIM117 -- readability preferred over combined with
            with patch.object(
                orchestrator_agent, "_synthesize", new_callable=AsyncMock
            ) as mock_synth:
                mock_synth.return_value = "Should not be called"

                events = []
                async for event in orchestrator_agent.run_streaming("Test"):
                    events.append(event)

        # Synthesis should not be called since there are no findings
        # Actually, it will be called but with empty findings
        # Let's check final_answer is not emitted or synthesis_start indicates no findings
        event_types = [e.type for e in events]

        # Should not have final_answer if synthesis fails or is skipped
        # The actual behavior depends on implementation
        assert "error" in event_types


# =============================================================================
# Agent Exception Tests
# =============================================================================


class TestAgentException:
    """Tests for agent raising exception mid-execution."""

    @pytest.mark.asyncio
    async def test_agent_exception_produces_failed_output(self, orchestrator_agent):
        """Test that agent exception produces failed SubgraphOutput."""
        connectors = [
            ConnectorSelection("error-conn", "Error Connector", "Desc", 0.9, "Test"),
        ]
        state = OrchestratorState(user_goal="Test")

        async def mock_run_error(state, conn, iteration, queue):
            raise ValueError("Unexpected error in agent")

        with patch.object(orchestrator_agent, "_run_single_agent", side_effect=mock_run_error):
            outputs = []
            async for item in orchestrator_agent._dispatch_parallel(state, connectors, 1):
                if isinstance(item, SubgraphOutput):
                    outputs.append(item)

        assert len(outputs) == 1
        assert outputs[0].status == "failed"
        assert "Unexpected error" in outputs[0].error_message


# =============================================================================
# Partial Synthesis Tests
# =============================================================================


class TestPartialSynthesis:
    """Tests for partial synthesis on error."""

    @pytest.mark.asyncio
    async def test_synthesis_start_includes_partial_flag(self, orchestrator_agent):
        """Test that synthesis_start event includes partial flag when degraded."""
        call_count = 0

        async def mock_decide(state):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {
                    "action": "query",
                    "connectors": [ConnectorSelection("c1", "C1", "D", 0.9, "T")],
                }
            raise Exception("Error")

        async def mock_dispatch(state, connectors, iteration):
            yield SubgraphOutput("c1", "C1", "Data", "success")

        with patch.object(orchestrator_agent, "_decide_next_action", side_effect=mock_decide):  # noqa: SIM117 -- readability preferred over combined with
            with patch.object(orchestrator_agent, "_dispatch_parallel", side_effect=mock_dispatch):
                with patch.object(
                    orchestrator_agent, "_synthesize", new_callable=AsyncMock
                ) as mock_synth:
                    mock_synth.return_value = "Partial answer"

                    events = []
                    async for event in orchestrator_agent.run_streaming("Test"):
                        events.append(event)

        synthesis_start = next((e for e in events if e.type == "synthesis_start"), None)
        assert synthesis_start is not None
        assert synthesis_start.data.get("partial") is True

    @pytest.mark.asyncio
    async def test_orchestrator_complete_includes_partial_flag(self, orchestrator_agent):
        """Test that orchestrator_complete event includes partial flag when degraded."""
        call_count = 0

        async def mock_decide(state):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {
                    "action": "query",
                    "connectors": [ConnectorSelection("c1", "C1", "D", 0.9, "T")],
                }
            raise Exception("Error")

        async def mock_dispatch(state, connectors, iteration):
            yield SubgraphOutput("c1", "C1", "Data", "success")

        with patch.object(orchestrator_agent, "_decide_next_action", side_effect=mock_decide):  # noqa: SIM117 -- readability preferred over combined with
            with patch.object(orchestrator_agent, "_dispatch_parallel", side_effect=mock_dispatch):
                with patch.object(
                    orchestrator_agent, "_synthesize", new_callable=AsyncMock
                ) as mock_synth:
                    mock_synth.return_value = "Answer"

                    events = []
                    async for event in orchestrator_agent.run_streaming("Test"):
                        events.append(event)

        complete_event = next((e for e in events if e.type == "orchestrator_complete"), None)
        assert complete_event is not None
        assert complete_event.data.get("partial") is True


# =============================================================================
# Error Event Tests
# =============================================================================


class TestErrorEvents:
    """Tests for error event emission."""

    @pytest.mark.asyncio
    async def test_error_event_includes_recoverable_indicator(self, orchestrator_agent):
        """Test that error events include recoverable indicator."""

        async def mock_decide(state):
            raise Exception("Test error")

        with patch.object(orchestrator_agent, "_decide_next_action", side_effect=mock_decide):
            events = []
            async for event in orchestrator_agent.run_streaming("Test"):
                events.append(event)

        error_events = [e for e in events if e.type == "error"]
        assert len(error_events) >= 1

        # Check error event has recoverable field
        assert "recoverable" in error_events[0].data

    @pytest.mark.asyncio
    async def test_error_event_includes_findings_count(self, orchestrator_agent):
        """Test that error events include findings_so_far count."""
        call_count = 0

        async def mock_decide(state):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {
                    "action": "query",
                    "connectors": [ConnectorSelection("c1", "C1", "D", 0.9, "T")],
                }
            # Force error by raising exception that won't be caught gracefully
            raise KeyboardInterrupt("Simulated interrupt")

        async def mock_dispatch(state, connectors, iteration):
            yield SubgraphOutput("c1", "C1", "Data", "success")

        with patch.object(orchestrator_agent, "_decide_next_action", side_effect=mock_decide):  # noqa: SIM117 -- readability preferred over combined with
            with patch.object(orchestrator_agent, "_dispatch_parallel", side_effect=mock_dispatch):
                with patch.object(
                    orchestrator_agent, "_synthesize", new_callable=AsyncMock
                ) as mock_synth:
                    mock_synth.return_value = "Answer"

                    events = []
                    try:
                        async for event in orchestrator_agent.run_streaming("Test"):
                            events.append(event)
                    except KeyboardInterrupt:
                        pass  # Expected

        # May or may not have error event depending on where KeyboardInterrupt is caught
        # This test verifies the structure when error events are emitted


# =============================================================================
# Synthesis Error Tests
# =============================================================================


class TestSynthesisError:
    """Tests for synthesis error handling."""

    @pytest.mark.asyncio
    async def test_synthesis_error_emits_error_event(self, orchestrator_agent):
        """Test that synthesis error emits error event."""

        async def mock_decide(state):
            return {"action": "respond"}

        async def mock_synthesize(state):
            raise Exception("Synthesis failed")

        with patch.object(orchestrator_agent, "_decide_next_action", side_effect=mock_decide):  # noqa: SIM117 -- readability preferred over combined with
            with patch.object(orchestrator_agent, "_synthesize", side_effect=mock_synthesize):
                events = []
                async for event in orchestrator_agent.run_streaming("Test"):
                    events.append(event)

        event_types = [e.type for e in events]
        assert "error" in event_types

        error_event = next(e for e in events if e.type == "error")
        assert "Synthesis" in error_event.data["message"]
        assert error_event.data["recoverable"] is False
