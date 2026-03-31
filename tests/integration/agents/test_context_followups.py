# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Integration tests for contextual follow-ups (Phase 5 - TASK-185).

Tests end-to-end operation context tracking across multiple turns:
- Context extraction after each turn
- Context persistence via Redis
- Context usage in subsequent routing decisions
- Ambiguous query resolution using prior context
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.modules.agents.adapter import run_orchestrator_streaming
from meho_app.modules.agents.orchestrator.agent import OrchestratorAgent
from meho_app.modules.agents.orchestrator.contracts import SubgraphOutput
from meho_app.modules.agents.persistence import (
    AgentStateStore,
    OrchestratorSessionState,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_dependencies():
    """Create mock MEHODependencies for testing."""
    deps = MagicMock()
    deps.user_context = MagicMock()
    deps.user_context.tenant_id = "test-tenant"
    deps.user_context.user_id = "test-user"
    deps.connector_repo = MagicMock()
    deps.connector_repo.list_connectors = AsyncMock(return_value=[])
    return deps


@pytest.fixture
def mock_state_store():
    """Create mock AgentStateStore for testing."""
    store = MagicMock(spec=AgentStateStore)
    store.load_state = AsyncMock(return_value=None)
    store.save_state = AsyncMock(return_value=True)
    return store


@pytest.fixture
def orchestrator_agent(mock_dependencies):
    """Create OrchestratorAgent with mocked dependencies."""
    with patch.object(OrchestratorAgent, "_load_config") as mock_config:
        mock_config.return_value = MagicMock(
            max_iterations=3,
            model=MagicMock(name="openai:gpt-4.1-mini"),
        )
        agent = OrchestratorAgent(dependencies=mock_dependencies)
        return agent


@pytest.fixture
def session_state_with_context():
    """Create session state with prior operation context."""
    state = OrchestratorSessionState()
    state.turn_count = 1
    state.remember_connector(
        connector_id="k8s-prod-123",
        connector_name="K8s Production",
        connector_type="kubernetes",
        query="list pods in production namespace",
        status="success",
    )
    state.set_operation_context(
        "Investigating pod issues in production namespace",
        ["nginx-pod", "api-pod", "production"],
    )
    state.register_cached_data("pods", "k8s-prod-123", 15)
    return state


# =============================================================================
# Tests for Context Persistence
# =============================================================================


class TestContextPersistence:
    """Test that operation context persists across turns via Redis."""

    @pytest.mark.asyncio
    async def test_context_saved_after_turn(self, orchestrator_agent, mock_state_store):
        """Test that context is saved to state store after turn completes."""
        # Mock orchestrator to produce findings and extract context
        orchestrator_agent._decide_next_action = AsyncMock(
            side_effect=[
                {
                    "action": "query",
                    "connectors": [
                        MagicMock(
                            connector_id="k8s-1",
                            connector_name="K8s",
                            connector_type="kubernetes",
                            routing_description="K8s cluster",
                        )
                    ],
                },
                {"action": "respond"},
            ]
        )
        orchestrator_agent._synthesize = AsyncMock(return_value="Found 10 pods")
        orchestrator_agent._extract_operation_context = AsyncMock(
            return_value=("Listing pods in cluster", ["nginx", "production"])
        )
        orchestrator_agent._get_available_connectors = AsyncMock(return_value=[])

        async def mock_dispatch(state, connectors, iteration):
            yield SubgraphOutput(
                connector_id="k8s-1",
                connector_name="K8s",
                findings="Found pods: nginx, api-server",
                status="success",
            )

        orchestrator_agent._dispatch_parallel = mock_dispatch

        # Run through adapter
        events = []
        async for event in run_orchestrator_streaming(
            agent=orchestrator_agent,
            user_message="List pods in production",
            session_id="test-session-123",
            conversation_history=[],
            state_store=mock_state_store,
        ):
            events.append(event)

        # Verify state was saved
        mock_state_store.save_state.assert_called_once()
        saved_state = mock_state_store.save_state.call_args[0][1]

        # Verify context was set
        assert saved_state.current_operation == "Listing pods in cluster"
        assert "nginx" in saved_state.operation_entities
        assert "production" in saved_state.operation_entities

    @pytest.mark.asyncio
    async def test_context_loaded_on_next_turn(
        self, orchestrator_agent, mock_state_store, session_state_with_context
    ):
        """Test that context is loaded from state store at turn start."""
        # Configure store to return prior state
        mock_state_store.load_state = AsyncMock(return_value=session_state_with_context)

        orchestrator_agent._decide_next_action = AsyncMock(return_value={"action": "respond"})
        orchestrator_agent._synthesize = AsyncMock(return_value="Here are the details...")
        orchestrator_agent._get_available_connectors = AsyncMock(return_value=[])

        # Run through adapter
        events = []
        async for event in run_orchestrator_streaming(
            agent=orchestrator_agent,
            user_message="Show me more details",
            session_id="test-session-123",
            conversation_history=[],
            state_store=mock_state_store,
        ):
            events.append(event)

        # Verify state was loaded
        mock_state_store.load_state.assert_called_once_with("test-session-123")


# =============================================================================
# Tests for Context Usage in Decisions
# =============================================================================


class TestContextUsageInDecisions:
    """Test that prior context influences routing decisions."""

    @pytest.mark.asyncio
    async def test_ambiguous_query_uses_prior_context(
        self, orchestrator_agent, session_state_with_context
    ):
        """Test that 'show me more' uses prior operation context."""
        # Set up state with prior context
        orchestrator_agent._get_available_connectors = AsyncMock(
            return_value=[
                {
                    "id": "k8s-prod-123",
                    "name": "K8s Production",
                    "connector_type": "kubernetes",
                    "routing_description": "Production K8s cluster",
                    "description": "K8s cluster",
                }
            ]
        )

        # Capture the prompt sent to LLM
        captured_prompts = []

        async def capture_llm(prompt):
            captured_prompts.append(prompt)
            return '{"action": "respond"}'

        orchestrator_agent._call_llm = capture_llm

        # Run with ambiguous query and prior context
        events = []
        async for event in orchestrator_agent.run_streaming(
            user_message="Show me more details",
            session_id="test-session",
            context={"session_state": session_state_with_context},
        ):
            events.append(event)

        # Verify prior context was included in routing prompt
        assert len(captured_prompts) > 0
        routing_prompt = captured_prompts[0]
        assert "Investigating pod issues" in routing_prompt
        assert "nginx-pod" in routing_prompt or "production" in routing_prompt

    @pytest.mark.asyncio
    async def test_filter_those_references_cached_data(
        self, orchestrator_agent, session_state_with_context
    ):
        """Test that 'filter those' references cached data tables."""
        orchestrator_agent._get_available_connectors = AsyncMock(
            return_value=[
                {
                    "id": "k8s-1",
                    "name": "K8s Prod",
                    "connector_type": "kubernetes",
                    "routing_description": "K8s cluster",
                    "description": "K8s",
                }
            ]
        )

        captured_prompts = []

        async def capture_llm(prompt):
            captured_prompts.append(prompt)
            return '{"action": "respond"}'

        orchestrator_agent._call_llm = capture_llm

        events = []
        async for event in orchestrator_agent.run_streaming(
            user_message="Filter those to show only unhealthy",
            session_id="test-session",
            context={"session_state": session_state_with_context},
        ):
            events.append(event)

        # Verify cached data is mentioned
        assert len(captured_prompts) > 0
        # Cached data should be mentioned in session context
        full_prompts = " ".join(captured_prompts)
        assert "pods" in full_prompts.lower() or "Cached" in full_prompts


# =============================================================================
# Tests for Multi-Turn Scenarios
# =============================================================================


class TestMultiTurnScenarios:
    """Test complete multi-turn conversation scenarios."""

    @pytest.mark.asyncio
    async def test_two_turn_conversation_flow(self, orchestrator_agent, mock_state_store):
        """Test context flows correctly across two turns."""
        # TURN 1: Initial query
        OrchestratorSessionState()

        async def mock_dispatch_turn1(state, connectors, iteration):
            yield SubgraphOutput(
                connector_id="k8s-1",
                connector_name="K8s",
                findings="Found pods: nginx-pod, api-pod in production",
                status="success",
            )

        orchestrator_agent._decide_next_action = AsyncMock(
            side_effect=[
                {
                    "action": "query",
                    "connectors": [
                        MagicMock(
                            connector_id="k8s-1",
                            connector_name="K8s",
                            connector_type="kubernetes",
                            routing_description="",
                        )
                    ],
                },
                {"action": "respond"},
            ]
        )
        orchestrator_agent._synthesize = AsyncMock(
            return_value="Found nginx-pod and api-pod in production namespace"
        )
        orchestrator_agent._extract_operation_context = AsyncMock(
            return_value=("Listing pods in production", ["nginx-pod", "api-pod"])
        )
        orchestrator_agent._dispatch_parallel = mock_dispatch_turn1
        orchestrator_agent._get_available_connectors = AsyncMock(return_value=[])

        mock_state_store.load_state = AsyncMock(return_value=None)

        # Execute turn 1
        events1 = []
        async for event in run_orchestrator_streaming(
            agent=orchestrator_agent,
            user_message="List pods in production",
            session_id="session-abc",
            conversation_history=[],
            state_store=mock_state_store,
        ):
            events1.append(event)

        # Capture the saved state from turn 1
        assert mock_state_store.save_state.called
        saved_state_turn1 = mock_state_store.save_state.call_args[0][1]
        assert saved_state_turn1.current_operation == "Listing pods in production"
        assert "nginx-pod" in saved_state_turn1.operation_entities

        # TURN 2: Follow-up query
        # Load the state from turn 1
        mock_state_store.load_state = AsyncMock(return_value=saved_state_turn1)
        mock_state_store.save_state.reset_mock()

        orchestrator_agent._decide_next_action = AsyncMock(return_value={"action": "respond"})
        orchestrator_agent._synthesize = AsyncMock(
            return_value="The nginx-pod is healthy with 3/3 replicas ready"
        )

        # Execute turn 2 with ambiguous follow-up
        events2 = []
        async for event in run_orchestrator_streaming(
            agent=orchestrator_agent,
            user_message="What's the status of the first one?",
            session_id="session-abc",
            conversation_history=[
                {"role": "user", "content": "List pods in production"},
                {
                    "role": "assistant",
                    "content": "Found nginx-pod and api-pod in production namespace",
                },
            ],
            state_store=mock_state_store,
        ):
            events2.append(event)

        # Verify context was loaded
        mock_state_store.load_state.assert_called_with("session-abc")

        # Verify state was saved after turn 2
        assert mock_state_store.save_state.called

    @pytest.mark.asyncio
    async def test_context_updated_each_turn(self, orchestrator_agent, mock_state_store):
        """Test that operation context updates with each turn."""
        # Initial state with turn 1 context
        initial_state = OrchestratorSessionState()
        initial_state.turn_count = 1
        initial_state.set_operation_context("Listing pods", ["pod-1"])

        mock_state_store.load_state = AsyncMock(return_value=initial_state)

        # Turn 2: Different operation
        async def mock_dispatch(state, connectors, iteration):
            yield SubgraphOutput(
                connector_id="vmware-1",
                connector_name="VMware",
                findings="Found VMs: web-server, db-master",
                status="success",
            )

        orchestrator_agent._decide_next_action = AsyncMock(
            side_effect=[
                {
                    "action": "query",
                    "connectors": [
                        MagicMock(
                            connector_id="vmware-1",
                            connector_name="VMware",
                            connector_type="vmware",
                            routing_description="",
                        )
                    ],
                },
                {"action": "respond"},
            ]
        )
        orchestrator_agent._synthesize = AsyncMock(return_value="Found VMs")
        orchestrator_agent._extract_operation_context = AsyncMock(
            return_value=("Listing VMs in datacenter", ["web-server", "db-master"])
        )
        orchestrator_agent._dispatch_parallel = mock_dispatch
        orchestrator_agent._get_available_connectors = AsyncMock(return_value=[])

        events = []
        async for event in run_orchestrator_streaming(
            agent=orchestrator_agent,
            user_message="Now show me VMs",
            session_id="session-xyz",
            conversation_history=[],
            state_store=mock_state_store,
        ):
            events.append(event)

        # Verify context was updated to new operation
        saved_state = mock_state_store.save_state.call_args[0][1]
        assert saved_state.current_operation == "Listing VMs in datacenter"
        assert "web-server" in saved_state.operation_entities


# =============================================================================
# Tests for Graceful Degradation
# =============================================================================


class TestGracefulDegradation:
    """Test graceful handling of failures."""

    @pytest.mark.asyncio
    async def test_works_without_state_store(self, orchestrator_agent):
        """Test that orchestrator works without state store."""
        orchestrator_agent._decide_next_action = AsyncMock(return_value={"action": "respond"})
        orchestrator_agent._synthesize = AsyncMock(return_value="Response without state")
        orchestrator_agent._get_available_connectors = AsyncMock(return_value=[])

        # Run without state store
        events = []
        async for event in run_orchestrator_streaming(
            agent=orchestrator_agent,
            user_message="Test query",
            session_id="test-session",
            conversation_history=[],
            state_store=None,  # No state store
        ):
            events.append(event)

        # Should complete successfully
        event_types = [e["type"] for e in events]
        assert "final_answer" in event_types

    @pytest.mark.asyncio
    async def test_works_with_state_store_failure(self, orchestrator_agent, mock_state_store):
        """Test graceful handling of state store failures."""
        # State store fails on load
        mock_state_store.load_state = AsyncMock(side_effect=Exception("Redis connection error"))

        orchestrator_agent._decide_next_action = AsyncMock(return_value={"action": "respond"})
        orchestrator_agent._synthesize = AsyncMock(return_value="Response")
        orchestrator_agent._get_available_connectors = AsyncMock(return_value=[])

        # Should still work
        events = []
        async for event in run_orchestrator_streaming(
            agent=orchestrator_agent,
            user_message="Test query",
            session_id="test-session",
            conversation_history=[],
            state_store=mock_state_store,
        ):
            events.append(event)

        event_types = [e["type"] for e in events]
        assert "final_answer" in event_types

    @pytest.mark.asyncio
    async def test_context_extraction_failure_non_fatal(self, orchestrator_agent, mock_state_store):
        """Test that context extraction failure doesn't break the flow."""

        async def mock_dispatch(state, connectors, iteration):
            yield SubgraphOutput(
                connector_id="k8s-1",
                connector_name="K8s",
                findings="Found data",
                status="success",
            )

        orchestrator_agent._decide_next_action = AsyncMock(
            side_effect=[
                {
                    "action": "query",
                    "connectors": [
                        MagicMock(
                            connector_id="k8s-1",
                            connector_name="K8s",
                            connector_type="kubernetes",
                            routing_description="",
                        )
                    ],
                },
                {"action": "respond"},
            ]
        )
        orchestrator_agent._synthesize = AsyncMock(return_value="Success")
        orchestrator_agent._extract_operation_context = AsyncMock(
            side_effect=Exception("LLM API error")
        )
        orchestrator_agent._dispatch_parallel = mock_dispatch
        orchestrator_agent._get_available_connectors = AsyncMock(return_value=[])

        mock_state_store.load_state = AsyncMock(return_value=None)

        events = []
        async for event in run_orchestrator_streaming(
            agent=orchestrator_agent,
            user_message="Test query",
            session_id="test-session",
            conversation_history=[],
            state_store=mock_state_store,
        ):
            events.append(event)

        # Should complete successfully despite extraction failure
        event_types = [e["type"] for e in events]
        assert "final_answer" in event_types

        # State should still be saved (without context)
        assert mock_state_store.save_state.called
