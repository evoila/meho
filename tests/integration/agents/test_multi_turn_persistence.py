# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
End-to-end tests for multi-turn state persistence (TASK-185 Phase 6).

These tests verify that the complete multi-turn conversation flow works correctly:
- Connectors are remembered across turns
- Cached data is queryable in follow-up turns
- Operation context is preserved for ambiguous queries

Tests use real Redis when available, simulating the complete conversation flow.
"""

import os
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
import redis.asyncio as redis

from meho_app.modules.agents.adapter import run_orchestrator_streaming
from meho_app.modules.agents.persistence import (
    AgentStateStore,
    OrchestratorSessionState,
)

pytestmark = pytest.mark.asyncio


# =============================================================================
# Fixtures
# =============================================================================


def get_test_redis_client() -> redis.Redis:
    """Create a Redis client for testing."""
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    return redis.from_url(
        redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )


@pytest.fixture
async def real_redis_client():
    """Create a real Redis client for integration tests."""
    client = get_test_redis_client()
    try:
        await client.ping()
        yield client
    except Exception:
        pytest.skip("Redis is not available")
    finally:
        await client.aclose()


@pytest.fixture
async def state_store(real_redis_client):
    """Create an AgentStateStore with a test-specific prefix."""
    test_prefix = f"meho:test:multi-turn:{uuid.uuid4().hex[:8]}"
    store = AgentStateStore(
        redis_client=real_redis_client,
        ttl=timedelta(minutes=5),
        key_prefix=test_prefix,
    )
    yield store
    # Cleanup
    keys = await real_redis_client.keys(f"{test_prefix}:*")
    if keys:
        await real_redis_client.delete(*keys)


class FakeAgentEvent:
    """Fake AgentEvent for testing."""

    def __init__(self, event_type: str, data: dict):
        self.type = event_type
        self.data = data


def create_mock_orchestrator_agent(
    connector_id: str = "k8s-prod-123",
    connector_name: str = "K8s Production",
    connector_type: str = "kubernetes",
    findings: str = "Found data",
    final_answer: str = "Here is the result",
):
    """Create a mock orchestrator agent that produces realistic events."""
    mock_agent = MagicMock()

    async def mock_stream(user_message, session_id=None, context=None):
        # Emit connector_complete event
        yield FakeAgentEvent(
            "connector_complete",
            {
                "connector_id": connector_id,
                "connector_name": connector_name,
                "connector_type": connector_type,
                "status": "success",
                "query": user_message,
            },
        )
        # Emit final_answer
        yield FakeAgentEvent("final_answer", {"content": final_answer})

    mock_agent.run_streaming = mock_stream
    return mock_agent


# =============================================================================
# Test 1: Connector Remembered Across Turns
# =============================================================================


class TestConnectorRememberedAcrossTurns:
    """
    Verify that connectors discovered in one turn are available in subsequent turns.

    User experience:
    - Turn 1: "List pods in production cluster"
    - Turn 2: "Show me more from that cluster"
    - Expected: Uses the same K8s connector without re-discovery
    """

    async def test_connector_remembered_across_turns(self, state_store):
        """Test that connector from turn 1 is available in turn 2."""
        session_id = f"test-connector-{uuid.uuid4().hex[:8]}"

        # ===== TURN 1: Initial query discovers K8s connector =====
        agent_turn1 = create_mock_orchestrator_agent(
            connector_id="k8s-prod-123",
            connector_name="K8s Production",
            connector_type="kubernetes",
            final_answer="Found 25 pods in production namespace",
        )

        events_turn1 = []
        async for event in run_orchestrator_streaming(
            agent=agent_turn1,
            user_message="List all pods in production namespace",
            session_id=session_id,
            conversation_history=[],
            state_store=state_store,
        ):
            events_turn1.append(event)

        # Verify turn 1 completed
        assert any(e["type"] == "final_answer" for e in events_turn1)

        # ===== TURN 2: Follow-up query =====
        # Load state to verify connector was remembered
        state_turn2 = await state_store.load_state(session_id)

        assert state_turn2 is not None
        assert "k8s-prod-123" in state_turn2.connectors
        assert state_turn2.connectors["k8s-prod-123"].connector_name == "K8s Production"
        assert state_turn2.primary_connector_id == "k8s-prod-123"

        # The orchestrator can now use this context for routing
        primary = state_turn2.get_primary_connector()
        assert primary is not None
        assert primary.connector_type == "kubernetes"

    async def test_multiple_connectors_remembered(self, state_store):
        """Test that multiple connectors from different turns are all remembered."""
        session_id = f"test-multi-conn-{uuid.uuid4().hex[:8]}"

        # ===== TURN 1: Query K8s =====
        agent_turn1 = create_mock_orchestrator_agent(
            connector_id="k8s-001",
            connector_name="K8s Prod",
            connector_type="kubernetes",
        )

        async for _ in run_orchestrator_streaming(
            agent=agent_turn1,
            user_message="List pods",
            session_id=session_id,
            conversation_history=[],
            state_store=state_store,
        ):
            pass  # Context manager runs the agent; side effects are asserted below

        # ===== TURN 2: Query VMware =====
        agent_turn2 = create_mock_orchestrator_agent(
            connector_id="vmware-001",
            connector_name="vCenter",
            connector_type="vmware",
        )

        async for _ in run_orchestrator_streaming(
            agent=agent_turn2,
            user_message="List VMs",
            session_id=session_id,
            conversation_history=[],
            state_store=state_store,
        ):
            pass  # Context manager runs the agent; side effects are asserted below

        # ===== Verify both connectors remembered =====
        state = await state_store.load_state(session_id)
        assert len(state.connectors) == 2
        assert "k8s-001" in state.connectors
        assert "vmware-001" in state.connectors

        # Primary should be most recently used
        assert state.primary_connector_id == "vmware-001"

    async def test_connector_query_history_preserved(self, state_store):
        """Test that the last query for each connector is preserved."""
        session_id = f"test-query-history-{uuid.uuid4().hex[:8]}"

        # Turn 1: First K8s query
        agent1 = create_mock_orchestrator_agent(connector_id="k8s-001")
        async for _ in run_orchestrator_streaming(
            agent=agent1,
            user_message="List pods in default namespace",
            session_id=session_id,
            conversation_history=[],
            state_store=state_store,
        ):
            pass  # Context manager runs the agent; side effects are asserted below

        # Turn 2: Different K8s query
        agent2 = create_mock_orchestrator_agent(connector_id="k8s-001")
        async for _ in run_orchestrator_streaming(
            agent=agent2,
            user_message="Show deployment status",
            session_id=session_id,
            conversation_history=[],
            state_store=state_store,
        ):
            pass  # Context manager runs the agent; side effects are asserted below

        # Verify last query is preserved
        state = await state_store.load_state(session_id)
        assert state.connectors["k8s-001"].last_query == "Show deployment status"


# =============================================================================
# Test 2: Cached Data Queryable
# =============================================================================


class TestCachedDataQueryable:
    """
    Verify that data cached in one turn is available for queries in subsequent turns.

    User experience:
    - Turn 1: "Get all pods" -> data cached
    - Turn 2: "Filter to unhealthy pods" -> uses cached data
    - Expected: No re-fetch, uses cached table for SQL query
    """

    async def test_cached_data_queryable(self, state_store):
        """Test that cached data from turn 1 is available in turn 2."""
        session_id = f"test-cached-{uuid.uuid4().hex[:8]}"

        # ===== TURN 1: Initial query caches data =====
        state_turn1 = OrchestratorSessionState()
        state_turn1.remember_connector(
            connector_id="k8s-prod-123",
            connector_name="K8s Production",
            connector_type="kubernetes",
            query="list all pods",
            status="success",
        )
        # Simulate data caching (normally done by CallOperationNode)
        state_turn1.register_cached_data("pods_abc123", "k8s-prod-123", 150)

        await state_store.save_state(session_id, state_turn1)

        # ===== TURN 2: Follow-up query =====
        state_turn2 = await state_store.load_state(session_id)
        assert state_turn2 is not None

        # Verify cached data is available
        available_tables = state_turn2.get_available_tables()
        assert "pods_abc123" in available_tables

        # Verify table metadata
        cache_entry = state_turn2.cached_tables["pods_abc123"]
        assert cache_entry["connector_id"] == "k8s-prod-123"
        assert cache_entry["row_count"] == 150

    async def test_multiple_cached_tables_across_turns(self, state_store):
        """Test that multiple cached tables accumulate across turns."""
        session_id = f"test-multi-cache-{uuid.uuid4().hex[:8]}"

        # ===== TURN 1: Cache pods =====
        state1 = OrchestratorSessionState()
        state1.register_cached_data("pods", "k8s-001", 100)
        await state_store.save_state(session_id, state1)

        # ===== TURN 2: Cache deployments =====
        state2 = await state_store.load_state(session_id)
        state2.register_cached_data("deployments", "k8s-001", 25)
        await state_store.save_state(session_id, state2)

        # ===== TURN 3: Cache services =====
        state3 = await state_store.load_state(session_id)
        state3.register_cached_data("services", "k8s-001", 15)
        await state_store.save_state(session_id, state3)

        # ===== Verify all tables available =====
        final_state = await state_store.load_state(session_id)
        tables = final_state.get_available_tables()

        assert len(tables) == 3
        assert "pods" in tables
        assert "deployments" in tables
        assert "services" in tables

    async def test_cached_tables_from_multiple_connectors(self, state_store):
        """Test that cached tables from different connectors are tracked."""
        session_id = f"test-multi-conn-cache-{uuid.uuid4().hex[:8]}"

        state = OrchestratorSessionState()
        state.register_cached_data("pods", "k8s-001", 100)
        state.register_cached_data("vms", "vmware-001", 200)
        state.register_cached_data("hosts", "vmware-001", 10)
        await state_store.save_state(session_id, state)

        # Verify all tracked
        loaded = await state_store.load_state(session_id)
        tables = loaded.get_available_tables()

        assert len(tables) == 3
        assert loaded.cached_tables["pods"]["connector_id"] == "k8s-001"
        assert loaded.cached_tables["vms"]["connector_id"] == "vmware-001"

    async def test_context_summary_mentions_cached_data(self, state_store):
        """Test that context summary includes cached data for LLM prompts."""
        session_id = f"test-context-cache-{uuid.uuid4().hex[:8]}"

        state = OrchestratorSessionState()
        state.register_cached_data("pods", "k8s-001", 100)
        state.register_cached_data("services", "k8s-001", 20)
        await state_store.save_state(session_id, state)

        loaded = await state_store.load_state(session_id)
        summary = loaded.get_context_summary()

        # Context summary should mention cached data
        assert "Cached data:" in summary
        assert "pods" in summary
        assert "services" in summary


# =============================================================================
# Test 3: Operation Context Preserved
# =============================================================================


class TestOperationContextPreserved:
    """
    Verify that operation context is preserved for resolving ambiguous follow-ups.

    User experience:
    - Turn 1: "Debug why pod nginx is crashing"
    - Turn 2: "What are its logs?"
    - Expected: "its" resolves to nginx pod from prior context
    """

    async def test_operation_context_preserved(self, state_store):
        """Test that operation context from turn 1 is available in turn 2."""
        session_id = f"test-context-{uuid.uuid4().hex[:8]}"

        # ===== TURN 1: Set operation context =====
        state_turn1 = OrchestratorSessionState()
        state_turn1.remember_connector(
            connector_id="k8s-prod-123",
            connector_name="K8s Production",
            connector_type="kubernetes",
            status="success",
        )
        state_turn1.set_operation_context(
            "Debugging pod crashes in production",
            ["nginx-pod", "api-pod", "production"],
        )
        await state_store.save_state(session_id, state_turn1)

        # ===== TURN 2: Ambiguous follow-up =====
        state_turn2 = await state_store.load_state(session_id)
        assert state_turn2 is not None

        # Verify operation context is preserved
        assert state_turn2.current_operation == "Debugging pod crashes in production"
        assert "nginx-pod" in state_turn2.operation_entities
        assert "api-pod" in state_turn2.operation_entities
        assert "production" in state_turn2.operation_entities

    async def test_operation_context_updated_each_turn(self, state_store):
        """Test that operation context updates with each turn."""
        session_id = f"test-context-update-{uuid.uuid4().hex[:8]}"

        # ===== TURN 1: Initial context =====
        state1 = OrchestratorSessionState()
        state1.set_operation_context("Listing pods", ["pod-1", "pod-2"])
        await state_store.save_state(session_id, state1)

        # ===== TURN 2: New operation =====
        state2 = await state_store.load_state(session_id)
        state2.set_operation_context("Investigating VM performance", ["web-server", "db-master"])
        await state_store.save_state(session_id, state2)

        # ===== Verify context updated =====
        state3 = await state_store.load_state(session_id)
        assert state3.current_operation == "Investigating VM performance"
        assert "web-server" in state3.operation_entities
        assert "db-master" in state3.operation_entities

    async def test_context_summary_includes_operation(self, state_store):
        """Test that context summary includes operation for LLM prompts."""
        session_id = f"test-summary-op-{uuid.uuid4().hex[:8]}"

        state = OrchestratorSessionState()
        state.set_operation_context("Debug pod crashes", ["nginx-pod"])
        state.remember_connector("k8s-001", "K8s Prod", "kubernetes", status="success")
        await state_store.save_state(session_id, state)

        loaded = await state_store.load_state(session_id)
        summary = loaded.get_context_summary()

        # Context summary should include operation info
        assert "Debug pod crashes" in summary
        assert "nginx-pod" in summary
        assert "K8s Prod" in summary

    async def test_entities_preserved_for_pronoun_resolution(self, state_store):
        """Test that entity names are preserved for resolving pronouns like 'its'."""
        session_id = f"test-pronouns-{uuid.uuid4().hex[:8]}"

        # Turn 1: User asks about specific pod
        state1 = OrchestratorSessionState()
        state1.set_operation_context(
            "Investigating nginx-pod restart issues",
            ["nginx-pod", "default-namespace"],
        )
        await state_store.save_state(session_id, state1)

        # Turn 2: User asks "What are its logs?"
        # The orchestrator should be able to resolve "its" to "nginx-pod"
        state2 = await state_store.load_state(session_id)

        # Verify entity context is available
        assert len(state2.operation_entities) == 2
        assert state2.operation_entities[0] == "nginx-pod"

        # The first entity is typically the primary focus
        primary_entity = state2.operation_entities[0]
        assert primary_entity == "nginx-pod"


# =============================================================================
# Combined Multi-Turn Scenarios
# =============================================================================


class TestCompleteMultiTurnConversation:
    """Test complete multi-turn conversation scenarios."""

    async def test_three_turn_conversation(self, state_store):
        """
        Complete 3-turn conversation:
        Turn 1: Query K8s for pods
        Turn 2: Filter to unhealthy (uses cached data)
        Turn 3: Show logs for specific pod (uses context)
        """
        session_id = f"test-3-turn-{uuid.uuid4().hex[:8]}"

        # ===== TURN 1 =====
        agent1 = create_mock_orchestrator_agent(
            connector_id="k8s-prod",
            connector_name="K8s Production",
            connector_type="kubernetes",
            final_answer="Found 50 pods in production",
        )

        async for _ in run_orchestrator_streaming(
            agent=agent1,
            user_message="List all pods in production",
            session_id=session_id,
            conversation_history=[],
            state_store=state_store,
        ):
            pass  # Context manager runs the agent; side effects are asserted below

        # Simulate cache registration
        state1 = await state_store.load_state(session_id)
        state1.register_cached_data("pods", "k8s-prod", 50)
        state1.set_operation_context("Listing pods in production", ["production"])
        await state_store.save_state(session_id, state1)

        # ===== TURN 2 =====
        agent2 = create_mock_orchestrator_agent(
            connector_id="k8s-prod",
            connector_name="K8s Production",
            connector_type="kubernetes",
            final_answer="Found 3 unhealthy pods",
        )

        async for _ in run_orchestrator_streaming(
            agent=agent2,
            user_message="Filter to unhealthy pods",
            session_id=session_id,
            conversation_history=[
                {"role": "user", "content": "List all pods in production"},
                {"role": "assistant", "content": "Found 50 pods in production"},
            ],
            state_store=state_store,
        ):
            pass  # Context manager runs the agent; side effects are asserted below

        state2 = await state_store.load_state(session_id)
        state2.set_operation_context(
            "Filtering unhealthy pods", ["nginx-pod", "api-pod", "worker-pod"]
        )
        await state_store.save_state(session_id, state2)

        # ===== TURN 3 =====
        state3 = await state_store.load_state(session_id)

        # Verify all context is available
        assert "k8s-prod" in state3.connectors
        assert "pods" in state3.cached_tables
        assert "nginx-pod" in state3.operation_entities
        assert state3.turn_count >= 2

    async def test_error_tracking_across_turns(self, state_store):
        """Test that errors are tracked and can influence routing."""
        session_id = f"test-errors-{uuid.uuid4().hex[:8]}"

        # Turn 1: Record an error
        state1 = OrchestratorSessionState()
        state1.record_error("vmware-001", "timeout", "Request timed out")
        await state_store.save_state(session_id, state1)

        # Turn 2: Check error is available
        state2 = await state_store.load_state(session_id)
        assert state2.has_recent_error("vmware-001", "timeout") is True

        # Orchestrator can use this to avoid retrying failed connectors
        assert len(state2.recent_errors) == 1


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Test edge cases and graceful degradation."""

    async def test_works_without_state_store(self):
        """Test that streaming works when no state store is provided."""
        agent = create_mock_orchestrator_agent()

        events = []
        async for event in run_orchestrator_streaming(
            agent=agent,
            user_message="test",
            session_id="test-123",
            conversation_history=[],
            state_store=None,
        ):
            events.append(event)

        assert any(e["type"] == "final_answer" for e in events)

    async def test_works_without_session_id(self):
        """Test that streaming works when no session_id is provided."""
        mock_store = AsyncMock(spec=AgentStateStore)
        agent = create_mock_orchestrator_agent()

        events = []
        async for event in run_orchestrator_streaming(
            agent=agent,
            user_message="test",
            session_id=None,
            conversation_history=[],
            state_store=mock_store,
        ):
            events.append(event)

        # Should complete without persistence
        assert any(e["type"] == "final_answer" for e in events)
        mock_store.load_state.assert_not_called()
        mock_store.save_state.assert_not_called()

    async def test_empty_state_produces_valid_summary(self, state_store):
        """Test that new session state produces valid context summary."""
        session_id = f"test-empty-{uuid.uuid4().hex[:8]}"

        state = OrchestratorSessionState()
        await state_store.save_state(session_id, state)

        loaded = await state_store.load_state(session_id)
        summary = loaded.get_context_summary()

        assert summary == "New conversation"
