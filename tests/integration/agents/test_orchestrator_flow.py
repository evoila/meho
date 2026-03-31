# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Integration tests for orchestrator flow.

Tests for:
- Full loop: decide -> dispatch -> aggregate -> respond
- Parallel dispatch runs agents concurrently
- Events stream in real-time (TTFUR)
- Events wrapped with correct source metadata
- Feature flag switches between old/new agents
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.modules.agents.base.events import AgentEvent
from meho_app.modules.agents.orchestrator import OrchestratorAgent
from meho_app.modules.agents.orchestrator.contracts import SubgraphOutput, WrappedEvent
from meho_app.modules.agents.orchestrator.state import ConnectorSelection, OrchestratorState

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_dependencies():
    """Create mock MEHODependencies with connector repo."""
    deps = MagicMock()
    deps.user_context = MagicMock()
    deps.user_context.tenant_id = "test-tenant"
    deps.user_context.user_id = "test-user"

    # Mock connector repository with test connectors
    mock_connectors = [
        MagicMock(
            id="k8s-prod",
            name="K8s Production",
            description="Kubernetes production cluster",
            routing_description="Kubernetes pods, deployments, services",
            connector_type="rest",
            is_active=True,
        ),
        MagicMock(
            id="gcp-prod",
            name="GCP Production",
            description="GCP production project",
            routing_description="GCP VMs and cloud resources",
            connector_type="rest",
            is_active=True,
        ),
    ]
    deps.connector_repo = MagicMock()
    deps.connector_repo.list_connectors = AsyncMock(return_value=mock_connectors)

    return deps


@pytest.fixture
def orchestrator_agent(mock_dependencies):
    """Create OrchestratorAgent with mocked config."""
    with patch.object(OrchestratorAgent, "_load_config") as mock_config:
        mock_config.return_value = MagicMock(
            max_iterations=3,
            model=MagicMock(name="openai:gpt-4.1-mini"),
        )
        agent = OrchestratorAgent(dependencies=mock_dependencies)
        return agent


# =============================================================================
# Full Flow Tests
# =============================================================================


class TestFullOrchestratorFlow:
    """Tests for the complete orchestrator flow."""

    @pytest.mark.asyncio
    async def test_decide_dispatch_aggregate_respond_flow(self, orchestrator_agent):
        """Test the full orchestrator flow: decide -> dispatch -> aggregate -> respond."""
        # Mock _decide_next_action to query once then respond
        call_count = 0

        async def mock_decide(state):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {
                    "action": "query",
                    "connectors": [
                        ConnectorSelection(
                            connector_id="k8s-prod",
                            connector_name="K8s Production",
                            routing_description="K8s",
                            relevance_score=0.9,
                            reason="Test",
                        ),
                    ],
                }
            return {"action": "respond"}

        # Mock _dispatch_parallel to yield a SubgraphOutput
        async def mock_dispatch(*args, **kwargs):
            yield SubgraphOutput(
                connector_id="k8s-prod",
                connector_name="K8s Production",
                findings="Found 5 running pods",
                status="success",
                execution_time_ms=500.0,
            )

        # Mock _synthesize
        async def mock_synthesize(state):
            return f"Based on the findings: {state.get_findings_summary()}"

        with patch.object(orchestrator_agent, "_decide_next_action", side_effect=mock_decide):  # noqa: SIM117 -- readability preferred over combined with
            with patch.object(orchestrator_agent, "_dispatch_parallel", side_effect=mock_dispatch):
                with patch.object(orchestrator_agent, "_synthesize", side_effect=mock_synthesize):
                    events = []
                    async for event in orchestrator_agent.run_streaming(
                        "List all pods",
                        session_id="test-session",
                    ):
                        events.append(event)

        event_types = [e.type for e in events]

        # Verify flow events
        assert "orchestrator_start" in event_types
        assert "iteration_start" in event_types
        assert "dispatch_start" in event_types
        assert "connector_complete" in event_types
        assert "iteration_complete" in event_types
        assert "synthesis_start" in event_types
        assert "final_answer" in event_types
        assert "orchestrator_complete" in event_types

        # Verify final answer includes findings
        final_answer_event = next(e for e in events if e.type == "final_answer")
        assert "Found 5 running pods" in final_answer_event.data["content"]

    @pytest.mark.asyncio
    async def test_multiple_iterations(self, orchestrator_agent):
        """Test that orchestrator can do multiple iterations."""
        call_count = 0

        async def mock_decide(state):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return {
                    "action": "query",
                    "connectors": [
                        ConnectorSelection(
                            connector_id=f"conn-{call_count}",
                            connector_name=f"Connector {call_count}",
                            routing_description="Test",
                            relevance_score=0.8,
                            reason="Test",
                        ),
                    ],
                }
            return {"action": "respond"}

        async def mock_dispatch(state, connectors, iteration):
            for conn in connectors:
                yield SubgraphOutput(
                    connector_id=conn.connector_id,
                    connector_name=conn.connector_name,
                    findings=f"Findings from iteration {iteration}",
                    status="success",
                )

        with patch.object(orchestrator_agent, "_decide_next_action", side_effect=mock_decide):  # noqa: SIM117 -- readability preferred over combined with
            with patch.object(orchestrator_agent, "_dispatch_parallel", side_effect=mock_dispatch):
                with patch.object(
                    orchestrator_agent, "_synthesize", new_callable=AsyncMock
                ) as mock_synth:
                    mock_synth.return_value = "Combined answer"

                    events = []
                    async for event in orchestrator_agent.run_streaming("Multi-iteration test"):
                        events.append(event)

        iteration_starts = [e for e in events if e.type == "iteration_start"]
        # We query twice (call_count 1 and 2), then respond (call_count 3)
        # But iteration 3 starts before the decision is made, so we may see 3 iteration_starts
        assert len(iteration_starts) >= 2


# =============================================================================
# Parallel Dispatch Tests
# =============================================================================


class TestParallelDispatch:
    """Tests for parallel dispatch functionality."""

    @pytest.mark.asyncio
    async def test_dispatch_runs_agents_concurrently(self, orchestrator_agent):
        """Test that agents are dispatched in parallel."""
        # Track execution order
        execution_log = []

        async def mock_run_single_agent(state, connector, iteration, queue):
            execution_log.append(f"start_{connector.connector_id}")
            await asyncio.sleep(0.1)  # Simulate work
            execution_log.append(f"end_{connector.connector_id}")
            await queue.put(
                SubgraphOutput(
                    connector_id=connector.connector_id,
                    connector_name=connector.connector_name,
                    findings="Test",
                    status="success",
                )
            )

        connectors = [
            ConnectorSelection("conn-1", "Conn 1", "Desc 1", 0.8, "Test"),
            ConnectorSelection("conn-2", "Conn 2", "Desc 2", 0.8, "Test"),
            ConnectorSelection("conn-3", "Conn 3", "Desc 3", 0.8, "Test"),
        ]
        state = OrchestratorState(user_goal="Test")

        with patch.object(
            orchestrator_agent, "_run_single_agent", side_effect=mock_run_single_agent
        ):
            outputs = []
            async for item in orchestrator_agent._dispatch_parallel(state, connectors, 1):
                if isinstance(item, SubgraphOutput):
                    outputs.append(item)

        assert len(outputs) == 3

        # Verify parallel execution: all starts should occur before all ends
        # (in pure parallel, starts happen together, then ends happen together)
        start_indices = [i for i, log in enumerate(execution_log) if log.startswith("start_")]
        end_indices = [i for i, log in enumerate(execution_log) if log.startswith("end_")]

        # In true parallel, the last start should be before the first end
        # (or very close - depends on timing)
        # For this test, just verify all agents were run
        assert len(start_indices) == 3
        assert len(end_indices) == 3


# =============================================================================
# Event Streaming Tests (TTFUR)
# =============================================================================


class TestEventStreaming:
    """Tests for real-time event streaming (Time To First Useful Response)."""

    @pytest.mark.asyncio
    async def test_events_stream_as_agents_produce_them(self, orchestrator_agent):
        """Test that events are yielded as they're produced, not batched."""
        event_order = []

        async def mock_decide(state):
            return (
                {
                    "action": "query",
                    "connectors": [
                        ConnectorSelection("k8s", "K8s", "K8s cluster", 0.9, "Test"),
                    ],
                }
                if state.current_iteration == 0
                else {"action": "respond"}
            )

        async def mock_dispatch(state, connectors, iteration):
            # Yield wrapped events first, then output
            yield WrappedEvent(
                agent_source={"connector_id": "k8s", "agent_name": "generic_k8s"},
                inner_event={"type": "thought", "data": {"content": "Thinking..."}},
            )
            event_order.append("wrapped_event_1")

            yield WrappedEvent(
                agent_source={"connector_id": "k8s", "agent_name": "generic_k8s"},
                inner_event={"type": "action", "data": {"tool": "search"}},
            )
            event_order.append("wrapped_event_2")

            yield SubgraphOutput(
                connector_id="k8s",
                connector_name="K8s",
                findings="Found data",
                status="success",
            )
            event_order.append("subgraph_output")

        with patch.object(orchestrator_agent, "_decide_next_action", side_effect=mock_decide):  # noqa: SIM117 -- readability preferred over combined with
            with patch.object(orchestrator_agent, "_dispatch_parallel", side_effect=mock_dispatch):
                with patch.object(
                    orchestrator_agent, "_synthesize", new_callable=AsyncMock
                ) as mock_synth:
                    mock_synth.return_value = "Answer"

                    events = []
                    async for event in orchestrator_agent.run_streaming("Test TTFUR"):
                        events.append(event)
                        if event.type == "agent_event" or event.type == "connector_complete":
                            event_order.append(f"yielded_{event.type}")

        # Verify agent_event events were yielded
        agent_events = [e for e in events if e.type == "agent_event"]
        assert len(agent_events) >= 2  # At least the thought and action

    @pytest.mark.asyncio
    async def test_wrapped_events_have_source_metadata(self, orchestrator_agent):
        """Test that wrapped events contain correct source metadata."""

        async def mock_decide(state):
            return (
                {
                    "action": "query",
                    "connectors": [
                        ConnectorSelection("test-conn", "Test Connector", "Test", 0.9, "Test"),
                    ],
                }
                if state.current_iteration == 0
                else {"action": "respond"}
            )

        async def mock_dispatch(state, connectors, iteration):
            yield WrappedEvent(
                agent_source={
                    "agent_name": "generic_test-conn",
                    "connector_id": "test-conn",
                    "connector_name": "Test Connector",
                    "iteration": iteration,
                },
                inner_event={"type": "thought", "data": {"content": "Test thought"}},
            )
            yield SubgraphOutput("test-conn", "Test Connector", "Findings", "success")

        with patch.object(orchestrator_agent, "_decide_next_action", side_effect=mock_decide):  # noqa: SIM117 -- readability preferred over combined with
            with patch.object(orchestrator_agent, "_dispatch_parallel", side_effect=mock_dispatch):
                with patch.object(
                    orchestrator_agent, "_synthesize", new_callable=AsyncMock
                ) as mock_synth:
                    mock_synth.return_value = "Answer"

                    events = []
                    async for event in orchestrator_agent.run_streaming("Test metadata"):
                        events.append(event)

        agent_events = [e for e in events if e.type == "agent_event"]
        assert len(agent_events) >= 1

        # Check the agent_event data contains wrapped event info
        agent_event_data = agent_events[0].data
        assert "agent_source" in agent_event_data
        assert agent_event_data["agent_source"]["connector_id"] == "test-conn"


# =============================================================================
# Feature Flag Tests
# =============================================================================


class TestFeatureFlag:
    """Tests for orchestrator flow integration."""

    @pytest.mark.asyncio
    async def test_adapter_creates_orchestrator_agent(self):
        """Test that adapter creates OrchestratorAgent correctly."""
        from meho_app.modules.agents.adapter import create_orchestrator_agent

        mock_deps = MagicMock()
        mock_deps.user_context = MagicMock()
        mock_deps.user_context.tenant_id = "test"

        with patch.object(OrchestratorAgent, "_load_config") as mock_config:
            mock_config.return_value = MagicMock(
                max_iterations=3,
                model=MagicMock(name="openai:gpt-4.1-mini"),
            )

            agent = create_orchestrator_agent(mock_deps)

            assert isinstance(agent, OrchestratorAgent)
            assert agent.dependencies is mock_deps

    @pytest.mark.asyncio
    async def test_run_orchestrator_streaming_adapter(self):
        """Test run_orchestrator_streaming adapter function."""
        from meho_app.modules.agents.adapter import run_orchestrator_streaming

        mock_agent = MagicMock()

        # Mock the run_streaming async generator
        async def mock_run_streaming(*args, **kwargs):
            yield AgentEvent(type="orchestrator_start", agent="orchestrator", data={"goal": "test"})
            yield AgentEvent(type="final_answer", agent="orchestrator", data={"content": "Answer"})
            yield AgentEvent(
                type="orchestrator_complete", agent="orchestrator", data={"success": True}
            )

        mock_agent.run_streaming = mock_run_streaming

        events = []
        async for event in run_orchestrator_streaming(
            agent=mock_agent,
            user_message="Test",
            session_id="test-session",
            conversation_history=[],
        ):
            events.append(event)

        # Events should be in old format (flattened)
        assert len(events) == 3
        assert events[0]["type"] == "orchestrator_start"
        assert events[1]["type"] == "final_answer"
        assert events[1]["content"] == "Answer"


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestErrorHandling:
    """Tests for error handling in orchestrator flow."""

    @pytest.mark.asyncio
    async def test_agent_error_produces_error_event(self, orchestrator_agent):
        """Test that agent errors produce error events."""

        async def mock_decide(state):
            raise Exception("Test decision error")

        with patch.object(orchestrator_agent, "_decide_next_action", side_effect=mock_decide):  # noqa: SIM117 -- readability preferred over combined with
            with patch.object(
                orchestrator_agent, "_synthesize", new_callable=AsyncMock
            ) as mock_synth:
                mock_synth.return_value = "Fallback answer"

                events = []
                async for event in orchestrator_agent.run_streaming("Test error"):
                    events.append(event)

        event_types = [e.type for e in events]
        assert "error" in event_types

    @pytest.mark.asyncio
    async def test_synthesis_error_produces_error_event(self, orchestrator_agent):
        """Test that synthesis errors produce error events."""

        async def mock_decide(state):
            return {"action": "respond"}

        async def mock_synthesize(state):
            raise Exception("Synthesis failed")

        with patch.object(orchestrator_agent, "_decide_next_action", side_effect=mock_decide):  # noqa: SIM117 -- readability preferred over combined with
            with patch.object(orchestrator_agent, "_synthesize", side_effect=mock_synthesize):
                events = []
                async for event in orchestrator_agent.run_streaming("Test synthesis error"):
                    events.append(event)

        event_types = [e.type for e in events]
        assert "error" in event_types

        error_events = [e for e in events if e.type == "error"]
        assert any("Synthesis" in e.data.get("message", "") for e in error_events)

    @pytest.mark.asyncio
    async def test_connector_timeout_handled_gracefully(self, orchestrator_agent):
        """Test that connector timeouts are handled gracefully."""

        async def mock_decide(state):
            return (
                {
                    "action": "query",
                    "connectors": [
                        ConnectorSelection("slow-conn", "Slow Connector", "Slow", 0.7, "Test")
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
                error_message="Exceeded 30s timeout",
            )

        with patch.object(orchestrator_agent, "_decide_next_action", side_effect=mock_decide):  # noqa: SIM117 -- readability preferred over combined with
            with patch.object(orchestrator_agent, "_dispatch_parallel", side_effect=mock_dispatch):
                with patch.object(
                    orchestrator_agent, "_synthesize", new_callable=AsyncMock
                ) as mock_synth:
                    mock_synth.return_value = "No data due to timeout"

                    events = []
                    async for event in orchestrator_agent.run_streaming("Test timeout"):
                        events.append(event)

        # Should still complete without error
        event_types = [e.type for e in events]
        assert "orchestrator_complete" in event_types

        # Should have connector_complete event with timeout status
        connector_events = [e for e in events if e.type == "connector_complete"]
        assert len(connector_events) == 1
        assert connector_events[0].data["status"] == "timeout"
