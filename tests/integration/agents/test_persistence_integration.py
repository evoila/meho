# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for agents persistence module with real Redis.

These tests require Redis to be running (via ./scripts/dev-env.sh local or up).
Tests use a separate key prefix to avoid interfering with application state.
"""

import os
import uuid
from datetime import timedelta

import pytest
import redis.asyncio as redis

from meho_app.modules.agents.persistence import (
    AgentStateStore,
    OrchestratorSessionState,
)

# Skip all tests in this module if Redis is not available
pytestmark = pytest.mark.asyncio


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
        # Test connection
        await client.ping()
        yield client
    except Exception:
        pytest.skip("Redis is not available")
    finally:
        await client.aclose()


@pytest.fixture
async def test_state_store(real_redis_client):
    """Create an AgentStateStore with a test-specific prefix."""
    # Use a unique prefix for each test run to avoid collisions
    test_prefix = f"meho:test:agents:state:{uuid.uuid4().hex[:8]}"
    store = AgentStateStore(
        redis_client=real_redis_client,
        ttl=timedelta(minutes=5),  # Short TTL for tests
        key_prefix=test_prefix,
    )
    yield store
    # Cleanup: delete all keys with this prefix
    keys = await real_redis_client.keys(f"{test_prefix}:*")
    if keys:
        await real_redis_client.delete(*keys)


class TestAgentStateStoreWithRealRedis:
    """Integration tests for AgentStateStore with real Redis."""

    async def test_save_and_load_state(self, test_state_store):
        """Test saving and loading state with real Redis."""
        session_id = f"test-session-{uuid.uuid4().hex[:8]}"

        # Create state with connector memory
        state = OrchestratorSessionState()
        state.remember_connector(
            connector_id="k8s-prod",
            connector_name="Kubernetes Production",
            connector_type="kubernetes",
            query="list pods in namespace default",
            status="success",
        )
        state.set_operation_context("Debugging pod restarts", ["nginx-pod", "api-pod"])
        state.register_cached_data("pods", "k8s-prod", 42)

        # Save state
        save_result = await test_state_store.save_state(session_id, state)
        assert save_result is True

        # Load state
        loaded = await test_state_store.load_state(session_id)

        # Verify
        assert loaded is not None
        assert len(loaded.connectors) == 1
        assert "k8s-prod" in loaded.connectors
        assert loaded.connectors["k8s-prod"].connector_name == "Kubernetes Production"
        assert loaded.connectors["k8s-prod"].last_query == "list pods in namespace default"
        assert loaded.primary_connector_id == "k8s-prod"
        assert loaded.current_operation == "Debugging pod restarts"
        assert loaded.operation_entities == ["nginx-pod", "api-pod"]
        assert "pods" in loaded.cached_tables
        assert loaded.turn_count == 1  # Incremented on save

    async def test_state_not_found(self, test_state_store):
        """Test that loading non-existent state returns None."""
        result = await test_state_store.load_state("non-existent-session")
        assert result is None

    async def test_delete_state(self, test_state_store):
        """Test deleting state from Redis."""
        session_id = f"test-session-{uuid.uuid4().hex[:8]}"

        # Create and save state
        state = OrchestratorSessionState()
        await test_state_store.save_state(session_id, state)

        # Verify it exists
        assert await test_state_store.exists(session_id) is True

        # Delete
        deleted = await test_state_store.delete_state(session_id)
        assert deleted is True

        # Verify it's gone
        assert await test_state_store.exists(session_id) is False

    async def test_exists(self, test_state_store):
        """Test checking if state exists."""
        session_id = f"test-session-{uuid.uuid4().hex[:8]}"

        # Should not exist initially
        assert await test_state_store.exists(session_id) is False

        # Create and save
        state = OrchestratorSessionState()
        await test_state_store.save_state(session_id, state)

        # Should exist now
        assert await test_state_store.exists(session_id) is True

    async def test_ping(self, test_state_store):
        """Test Redis ping."""
        result = await test_state_store.ping()
        assert result is True

    async def test_turn_count_increments(self, test_state_store):
        """Test that turn_count increments on each save."""
        session_id = f"test-session-{uuid.uuid4().hex[:8]}"

        state = OrchestratorSessionState()
        assert state.turn_count == 0

        # First save
        await test_state_store.save_state(session_id, state)
        loaded1 = await test_state_store.load_state(session_id)
        assert loaded1.turn_count == 1

        # Second save
        await test_state_store.save_state(session_id, loaded1)
        loaded2 = await test_state_store.load_state(session_id)
        assert loaded2.turn_count == 2

        # Third save
        await test_state_store.save_state(session_id, loaded2)
        loaded3 = await test_state_store.load_state(session_id)
        assert loaded3.turn_count == 3


class TestMultiTurnConversationWithRealRedis:
    """Test multi-turn conversation scenarios with real Redis."""

    async def test_three_turn_conversation(self, test_state_store):
        """
        Simulate a 3-turn conversation:
        Turn 1: Query K8s for pods - discover connector
        Turn 2: "Filter to unhealthy pods" - use cached data
        Turn 3: "Show logs" - use operation context
        """
        session_id = f"test-multi-turn-{uuid.uuid4().hex[:8]}"

        # ===== TURN 1: Initial query =====
        state1 = OrchestratorSessionState()
        state1.remember_connector(
            connector_id="k8s-123",
            connector_name="K8s Production",
            connector_type="kubernetes",
            query="list all pods",
            status="success",
        )
        state1.register_cached_data("pods", "k8s-123", 150)
        state1.set_operation_context("Investigating pod health", ["nginx", "api"])

        await test_state_store.save_state(session_id, state1)

        # ===== TURN 2: Follow-up query =====
        state2 = await test_state_store.load_state(session_id)
        assert state2 is not None

        # Verify connector is remembered (no need to rediscover)
        primary = state2.get_primary_connector()
        assert primary is not None
        assert primary.connector_id == "k8s-123"

        # Verify cached data is available
        assert "pods" in state2.cached_tables

        # Add more data
        state2.register_cached_data("unhealthy_pods", "k8s-123", 5)
        await test_state_store.save_state(session_id, state2)

        # ===== TURN 3: Another follow-up =====
        state3 = await test_state_store.load_state(session_id)
        assert state3 is not None

        # All context is preserved
        assert state3.current_operation == "Investigating pod health"
        assert state3.operation_entities == ["nginx", "api"]
        assert "pods" in state3.cached_tables
        assert "unhealthy_pods" in state3.cached_tables
        assert state3.turn_count == 2  # Two saves completed

    async def test_error_tracking_persists(self, test_state_store):
        """Test that errors are tracked across turns."""
        session_id = f"test-errors-{uuid.uuid4().hex[:8]}"

        # Turn 1: Record an error
        state1 = OrchestratorSessionState()
        state1.record_error("vmware-001", "timeout", "Request timed out after 30s")
        state1.record_error("vmware-001", "auth_failed", "Invalid credentials")

        await test_state_store.save_state(session_id, state1)

        # Turn 2: Check errors persist
        state2 = await test_state_store.load_state(session_id)
        assert state2 is not None
        assert len(state2.recent_errors) == 2
        assert state2.has_recent_error("vmware-001", "timeout") is True
        assert state2.has_recent_error("vmware-001", "auth_failed") is True
        assert state2.has_recent_error("other-conn", "timeout") is False

    async def test_multiple_connectors_tracked(self, test_state_store):
        """Test tracking multiple connectors across turns."""
        session_id = f"test-multi-conn-{uuid.uuid4().hex[:8]}"

        # Turn 1: Use K8s connector
        state1 = OrchestratorSessionState()
        state1.remember_connector(
            connector_id="k8s-001",
            connector_name="K8s Prod",
            connector_type="kubernetes",
            status="success",
        )
        await test_state_store.save_state(session_id, state1)

        # Turn 2: Use VMware connector
        state2 = await test_state_store.load_state(session_id)
        state2.remember_connector(
            connector_id="vmware-001",
            connector_name="vCenter",
            connector_type="vmware",
            status="success",
        )
        await test_state_store.save_state(session_id, state2)

        # Turn 3: Verify both connectors remembered
        state3 = await test_state_store.load_state(session_id)
        assert len(state3.connectors) == 2
        assert "k8s-001" in state3.connectors
        assert "vmware-001" in state3.connectors

        # Primary should be the most recently successful
        assert state3.primary_connector_id == "vmware-001"


class TestContextSummaryWithRealRedis:
    """Test context summary generation with persisted state."""

    async def test_context_summary_round_trip(self, test_state_store):
        """Test that context summary works with loaded state."""
        session_id = f"test-summary-{uuid.uuid4().hex[:8]}"

        # Create rich state
        state = OrchestratorSessionState()
        state.remember_connector("k8s-001", "K8s Prod", "kubernetes", status="success")
        state.set_operation_context("Debug pod crashes", ["nginx-pod"])
        state.register_cached_data("pods", "k8s-001", 50)

        await test_state_store.save_state(session_id, state)

        # Load and generate summary
        loaded = await test_state_store.load_state(session_id)
        summary = loaded.get_context_summary()

        # Verify summary contains key information
        assert "K8s Prod" in summary
        assert "Debug pod crashes" in summary
        assert "nginx-pod" in summary
        assert "pods" in summary
