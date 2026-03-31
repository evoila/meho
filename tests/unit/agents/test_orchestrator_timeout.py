# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for orchestrator timeout handling.

Tests for:
- Single agent timeout (others complete normally)
- All agents timeout
- Total timeout exceeded (cancel remaining)
- Timeout events emitted correctly

Phase 84: OrchestratorAgent rewritten -- timeout handling uses investigation budget now.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: OrchestratorAgent rewritten, timeout handling replaced by investigation budget system")

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
                "agent_timeout": 1.0,  # Short timeout for tests
                "total_timeout": 5.0,
            }
        }
        mock_config.return_value = config
        agent = OrchestratorAgent(dependencies=mock_dependencies)
        return agent


# =============================================================================
# Timeout Configuration Tests
# =============================================================================


class TestTimeoutConfiguration:
    """Tests for timeout configuration getters."""

    def test_get_agent_timeout_from_config(self, orchestrator_agent):
        """Test that agent timeout is read from config."""
        timeout = orchestrator_agent._get_agent_timeout()
        assert timeout == 1.0

    def test_get_total_timeout_from_config(self, orchestrator_agent):
        """Test that total timeout is read from config."""
        timeout = orchestrator_agent._get_total_timeout()
        assert timeout == 5.0

    def test_get_agent_timeout_default(self, mock_dependencies):
        """Test default agent timeout when not in config."""
        with patch.object(OrchestratorAgent, "_load_config") as mock_config:
            config = MagicMock()
            config.raw = {}  # No orchestrator config
            mock_config.return_value = config
            agent = OrchestratorAgent(dependencies=mock_dependencies)

            timeout = agent._get_agent_timeout()
            assert timeout == 30.0  # Default

    def test_get_total_timeout_default(self, mock_dependencies):
        """Test default total timeout when not in config."""
        with patch.object(OrchestratorAgent, "_load_config") as mock_config:
            config = MagicMock()
            config.raw = {}
            mock_config.return_value = config
            agent = OrchestratorAgent(dependencies=mock_dependencies)

            timeout = agent._get_total_timeout()
            assert timeout == 120.0  # Default


# =============================================================================
# Single Agent Timeout Tests
# =============================================================================


class TestSingleAgentTimeout:
    """Tests for when a single agent times out."""

    @pytest.mark.asyncio
    async def test_single_agent_timeout_others_complete(self, orchestrator_agent):
        """Test that one agent can timeout while others complete."""
        connectors = [
            ConnectorSelection("fast-conn", "Fast Connector", "Fast", 0.9, "Test"),
            ConnectorSelection("slow-conn", "Slow Connector", "Slow", 0.8, "Test"),
        ]
        state = OrchestratorState(user_goal="Test")

        call_count = {"fast": 0, "slow": 0}

        async def mock_run_single_agent(state, conn, iteration, queue):
            if conn.connector_id == "slow-conn":
                call_count["slow"] += 1
                # Simulate slow agent that will be cancelled by timeout
                await asyncio.sleep(10)  # Will timeout
            else:
                call_count["fast"] += 1
                await queue.put(
                    SubgraphOutput(
                        connector_id=conn.connector_id,
                        connector_name=conn.connector_name,
                        findings="Fast result",
                        status="success",
                    )
                )

        with patch.object(
            orchestrator_agent, "_run_single_agent", side_effect=mock_run_single_agent
        ):
            outputs = []
            async for item in orchestrator_agent._dispatch_parallel(state, connectors, 1):
                if isinstance(item, SubgraphOutput):
                    outputs.append(item)

        assert len(outputs) == 2

        # Find results
        fast_output = next((o for o in outputs if o.connector_id == "fast-conn"), None)
        slow_output = next((o for o in outputs if o.connector_id == "slow-conn"), None)

        assert fast_output is not None
        assert fast_output.status == "success"
        assert fast_output.findings == "Fast result"

        assert slow_output is not None
        assert slow_output.status == "timeout"
        assert "timeout" in slow_output.error_message.lower()


# =============================================================================
# All Agents Timeout Tests
# =============================================================================


class TestAllAgentsTimeout:
    """Tests for when all agents timeout."""

    @pytest.mark.asyncio
    async def test_all_agents_timeout(self, orchestrator_agent):
        """Test that all agents can timeout."""
        connectors = [
            ConnectorSelection("slow-1", "Slow 1", "Desc", 0.9, "Test"),
            ConnectorSelection("slow-2", "Slow 2", "Desc", 0.8, "Test"),
        ]
        state = OrchestratorState(user_goal="Test")

        async def mock_run_slow(state, conn, iteration, queue):
            await asyncio.sleep(10)  # Will timeout

        with patch.object(orchestrator_agent, "_run_single_agent", side_effect=mock_run_slow):
            outputs = []
            async for item in orchestrator_agent._dispatch_parallel(state, connectors, 1):
                if isinstance(item, SubgraphOutput):
                    outputs.append(item)

        assert len(outputs) == 2
        assert all(o.status == "timeout" for o in outputs)


# =============================================================================
# Total Timeout Tests
# =============================================================================


class TestTotalTimeout:
    """Tests for total iteration timeout."""

    @pytest.mark.asyncio
    async def test_total_timeout_cancels_remaining(self, mock_dependencies):
        """Test that total timeout cancels remaining agents."""
        # Create agent with very short total timeout
        with patch.object(OrchestratorAgent, "_load_config") as mock_config:
            config = MagicMock()
            config.max_iterations = 3
            config.model = MagicMock(name="openai:gpt-4.1-mini")
            config.raw = {
                "orchestrator": {
                    "agent_timeout": 10.0,  # Long agent timeout
                    "total_timeout": 0.5,  # Very short total timeout
                }
            }
            mock_config.return_value = config
            agent = OrchestratorAgent(dependencies=mock_dependencies)

        connectors = [
            ConnectorSelection("conn-1", "Conn 1", "Desc", 0.9, "Test"),
            ConnectorSelection("conn-2", "Conn 2", "Desc", 0.8, "Test"),
            ConnectorSelection("conn-3", "Conn 3", "Desc", 0.7, "Test"),
        ]
        state = OrchestratorState(user_goal="Test")

        async def mock_run_medium(state, conn, iteration, queue):
            # All agents take 2 seconds, but total timeout is 0.5s
            await asyncio.sleep(2)

        with patch.object(agent, "_run_single_agent", side_effect=mock_run_medium):
            outputs = []
            async for item in agent._dispatch_parallel(state, connectors, 1):
                if isinstance(item, SubgraphOutput):
                    outputs.append(item)

        # All should timeout due to total timeout
        assert len(outputs) == 3
        assert all(o.status == "timeout" for o in outputs)
        # Check that error message mentions total timeout
        assert any("Total timeout" in (o.error_message or "") for o in outputs)


# =============================================================================
# Cancellation Handling Tests
# =============================================================================


class TestCancellationHandling:
    """Tests for CancelledError handling."""

    @pytest.mark.asyncio
    async def test_cancelled_error_produces_cancelled_status(self, orchestrator_agent):
        """Test that CancelledError produces cancelled status."""
        connectors = [
            ConnectorSelection("cancel-conn", "Cancel Connector", "Desc", 0.9, "Test"),
        ]
        state = OrchestratorState(user_goal="Test")

        async def mock_run_cancelled(state, conn, iteration, queue):
            raise asyncio.CancelledError()

        with patch.object(orchestrator_agent, "_run_single_agent", side_effect=mock_run_cancelled):
            outputs = []
            async for item in orchestrator_agent._dispatch_parallel(state, connectors, 1):
                if isinstance(item, SubgraphOutput):
                    outputs.append(item)

        assert len(outputs) == 1
        assert outputs[0].status == "cancelled"
        assert "cancelled" in outputs[0].error_message.lower()


# =============================================================================
# Timeout Event Emission Tests
# =============================================================================


class TestTimeoutEventEmission:
    """Tests for timeout event emission in run_streaming."""

    @pytest.mark.asyncio
    async def test_timeout_produces_connector_complete_event(self, orchestrator_agent):
        """Test that timeout produces connector_complete event with timeout status."""

        async def mock_decide(state):
            return (
                {
                    "action": "query",
                    "connectors": [
                        ConnectorSelection("slow-conn", "Slow Connector", "Desc", 0.9, "Test"),
                    ],
                }
                if state.current_iteration == 0
                else {"action": "respond"}
            )

        async def mock_dispatch(state, connectors, iteration):
            yield SubgraphOutput(
                connector_id="slow-conn",
                connector_name="Slow Connector",
                findings="",
                status="timeout",
                error_message="Agent exceeded 30s timeout",
            )

        with patch.object(orchestrator_agent, "_decide_next_action", side_effect=mock_decide):  # noqa: SIM117 -- readability preferred over combined with
            with patch.object(orchestrator_agent, "_dispatch_parallel", side_effect=mock_dispatch):
                with patch.object(
                    orchestrator_agent, "_synthesize", new_callable=AsyncMock
                ) as mock_synth:
                    mock_synth.return_value = "Partial answer due to timeout"

                    events = []
                    async for event in orchestrator_agent.run_streaming("Test timeout event"):
                        events.append(event)

        # Should have early_findings and connector_complete events with timeout status
        connector_events = [e for e in events if e.type == "connector_complete"]
        assert len(connector_events) == 1
        assert connector_events[0].data["status"] == "timeout"

        early_findings = [e for e in events if e.type == "early_findings"]
        assert len(early_findings) == 1
        assert early_findings[0].data["status"] == "timeout"
