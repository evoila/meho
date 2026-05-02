# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for Redis state persistence in chat flow

Tests that state persists across multiple requests and enables:
- No redundant connector discovery
- Cached endpoints reused
- Entity context preserved
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.api.dependencies import create_state_store
from meho_app.modules.agents.session_state import AgentSessionState
from meho_app.modules.agents.state_store import RedisStateStore


@pytest.fixture
def mock_redis_client():
    """Mock Redis client for integration tests"""
    redis = AsyncMock()
    redis.ping = AsyncMock(return_value=True)
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=1)
    redis.exists = AsyncMock(return_value=False)
    redis.ttl = AsyncMock(return_value=-2)
    redis.expire = AsyncMock(return_value=True)
    return redis


@pytest.fixture
async def state_store_with_mock(mock_redis_client):
    """Create RedisStateStore with mocked Redis"""
    return RedisStateStore(mock_redis_client)


class TestStatePersistenceIntegration:
    """Integration tests for state persistence across requests"""

    async def test_state_persists_across_requests(self, state_store_with_mock, mock_redis_client):
        """Test that state is saved after request and loaded on next request"""
        session_id = "test-session-123"

        # ===== REQUEST 1: Create and save state =====
        state1 = AgentSessionState()
        connector = state1.get_or_create_connector(
            connector_id="conn-456", connector_name="vCenter", connector_type="vcenter"
        )
        connector.add_endpoint("/api/vcenter/vm", "endpoint-789", "GET")

        # Save state (simulates end of request 1)
        saved = await state_store_with_mock.save_state(session_id, state1)
        assert saved is True

        # Get the saved JSON
        call_args = mock_redis_client.setex.call_args
        saved_json = call_args[0][2]

        # ===== REQUEST 2: Load saved state =====
        # Mock Redis returning the saved data
        mock_redis_client.get.return_value = saved_json

        # Load state (simulates start of request 2)
        state2 = await state_store_with_mock.load_state(session_id)

        # Verify state was restored
        assert state2 is not None
        assert len(state2.connectors) == 1
        assert "conn-456" in state2.connectors

        # Verify connector details
        loaded_connector = state2.connectors["conn-456"]
        assert loaded_connector.connector_name == "vCenter"
        assert loaded_connector.connector_type == "vcenter"
        assert "GET:/api/vcenter/vm" in loaded_connector.known_endpoints
        assert loaded_connector.known_endpoints["GET:/api/vcenter/vm"] == "endpoint-789"

    async def test_no_redundant_connector_discovery(self, state_store_with_mock, mock_redis_client):
        """Test that connector_id is remembered across turns"""
        session_id = "test-session-456"

        # Turn 1: Agent discovers connector
        state1 = AgentSessionState()
        state1.get_or_create_connector(
            connector_id="hetzner-conn-123",
            connector_name="Hetzner Cloud",
            connector_type="hetzner",
        )
        state1.primary_connector_id = "hetzner-conn-123"

        # Save
        await state_store_with_mock.save_state(session_id, state1)

        # Get saved data
        saved_json = mock_redis_client.setex.call_args[0][2]
        mock_redis_client.get.return_value = saved_json

        # Turn 2: Load state
        state2 = await state_store_with_mock.load_state(session_id)

        # Verify primary connector is remembered
        assert state2.primary_connector_id == "hetzner-conn-123"
        assert "hetzner-conn-123" in state2.connectors

        # Agent can now auto-fill connector_id without redundant determine_connector call!
        active_connector = state2.get_active_connector()
        assert active_connector is not None
        assert active_connector.connector_id == "hetzner-conn-123"
        assert active_connector.connector_name == "Hetzner Cloud"

    async def test_cached_endpoints_reused(self, state_store_with_mock, mock_redis_client):
        """Test that discovered endpoints are cached and reused"""
        session_id = "test-session-789"

        # Turn 1: Agent discovers endpoints
        state1 = AgentSessionState()
        connector = state1.get_or_create_connector(
            connector_id="k8s-conn-456", connector_name="Kubernetes", connector_type="kubernetes"
        )

        # Cache multiple endpoints
        connector.add_endpoint("/api/v1/pods", "endpoint-1", "GET")
        connector.add_endpoint("/api/v1/pods/{name}", "endpoint-2", "GET")
        connector.add_endpoint("/api/v1/pods", "endpoint-3", "POST")

        await state_store_with_mock.save_state(session_id, state1)

        # Turn 2: Load and verify endpoints
        saved_json = mock_redis_client.setex.call_args[0][2]
        mock_redis_client.get.return_value = saved_json

        state2 = await state_store_with_mock.load_state(session_id)
        loaded_connector = state2.connectors["k8s-conn-456"]

        # Verify all endpoints are cached
        assert len(loaded_connector.known_endpoints) == 3
        assert loaded_connector.get_endpoint("/api/v1/pods", "GET") == "endpoint-1"
        assert loaded_connector.get_endpoint("/api/v1/pods/{name}", "GET") == "endpoint-2"
        assert loaded_connector.get_endpoint("/api/v1/pods", "POST") == "endpoint-3"

        # Agent can now reuse these endpoints without redundant search_endpoints!

    async def test_state_ttl_expiration(self, state_store_with_mock, mock_redis_client):
        """Test that state expires after TTL"""
        session_id = "test-session-ttl"

        state = AgentSessionState()
        await state_store_with_mock.save_state(session_id, state)

        # Verify TTL was set (24 hours = 86400 seconds)
        call_args = mock_redis_client.setex.call_args
        assert call_args[0][1] == 86400

    async def test_state_not_found_creates_new(self, state_store_with_mock, mock_redis_client):
        """Test that missing state results in new AgentSessionState"""
        session_id = "new-session"

        # Mock Redis returning None (no state exists)
        mock_redis_client.get.return_value = None

        loaded_state = await state_store_with_mock.load_state(session_id)

        # Should return None (caller creates new state)
        assert loaded_state is None

    async def test_corrupted_state_recovery(self, state_store_with_mock, mock_redis_client):
        """Test that corrupted state is handled gracefully"""
        session_id = "corrupted-session"

        # Mock Redis returning invalid JSON
        mock_redis_client.get.return_value = "invalid json {"

        loaded_state = await state_store_with_mock.load_state(session_id)

        # Should return None and delete corrupted state
        assert loaded_state is None
        mock_redis_client.delete.assert_called_once()


class TestStatePersistenceCreateDependencies:
    """Test state store creation in dependencies"""

    @patch("meho_agent.state_store.redis.from_url")
    @patch("meho_api.dependencies.get_api_config")
    def test_create_state_store_uses_config(self, mock_get_config, mock_redis_from_url):
        """Test that create_state_store uses config.redis_url"""
        # Mock config
        mock_config = MagicMock()
        mock_config.redis_url = "redis://test-redis:6379/0"
        mock_get_config.return_value = mock_config

        # Mock Redis client
        mock_redis_client = AsyncMock()
        mock_redis_from_url.return_value = mock_redis_client

        # Create state store
        state_store = create_state_store()

        # Verify Redis was created with correct URL
        mock_redis_from_url.assert_called_once_with(
            "redis://test-redis:6379/0",
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )

        # Verify state store was created
        assert isinstance(state_store, RedisStateStore)
        assert state_store.redis == mock_redis_client


class TestMultiTurnConversationSimulation:
    """Simulate multi-turn conversation with state persistence"""

    async def test_three_turn_conversation(self, state_store_with_mock, mock_redis_client):
        """
        Simulate a 3-turn conversation with state persistence:
        Turn 1: "Get VMs from Hetzner" - discovers connector
        Turn 2: "approve" - reuses connector_id from state
        Turn 3: "Get IP addresses" - reuses entities from state
        """

        session_id = "multi-turn-session"

        # ===== TURN 1: Initial discovery =====
        state_turn1 = AgentSessionState()

        # Agent calls determine_connector
        connector = state_turn1.get_or_create_connector(
            connector_id="hetzner-12345", connector_name="Hetzner Cloud", connector_type="hetzner"
        )
        state_turn1.primary_connector_id = "hetzner-12345"

        # Agent calls search_endpoints
        connector.add_endpoint("/servers", "endpoint-get-servers", "GET")

        # Save state at end of Turn 1
        await state_store_with_mock.save_state(session_id, state_turn1)
        saved_json_turn1 = mock_redis_client.setex.call_args[0][2]

        # ===== TURN 2: Reuse connector (no redundant discovery!) =====
        mock_redis_client.get.return_value = saved_json_turn1
        state_turn2 = await state_store_with_mock.load_state(session_id)

        # Verify state was loaded
        assert state_turn2 is not None
        assert state_turn2.primary_connector_id == "hetzner-12345"

        # Agent can now skip determine_connector!
        # Agent can reuse cached endpoint!
        cached_endpoint = state_turn2.connectors["hetzner-12345"].get_endpoint("/servers", "GET")
        assert cached_endpoint == "endpoint-get-servers"

        # Save state at end of Turn 2
        await state_store_with_mock.save_state(session_id, state_turn2)
        saved_json_turn2 = mock_redis_client.setex.call_args[0][2]

        # ===== TURN 3: Reuse state from memory =====
        mock_redis_client.get.return_value = saved_json_turn2
        state_turn3 = await state_store_with_mock.load_state(session_id)

        # Verify state is loaded
        assert state_turn3 is not None
        assert "hetzner-12345" in state_turn3.connectors


class TestStatePersistenceErrorHandling:
    """Test error handling in state persistence"""

    async def test_save_failure_does_not_crash_request(
        self, state_store_with_mock, mock_redis_client
    ):
        """Test that save failure doesn't crash the request"""
        mock_redis_client.setex.side_effect = Exception("Redis connection failed")

        state = AgentSessionState()
        result = await state_store_with_mock.save_state("session-123", state)

        # Should return False but not raise exception
        assert result is False

    async def test_load_failure_returns_none(self, state_store_with_mock, mock_redis_client):
        """Test that load failure returns None gracefully"""
        mock_redis_client.get.side_effect = Exception("Redis connection failed")

        loaded_state = await state_store_with_mock.load_state("session-123")

        # Should return None, not raise exception
        assert loaded_state is None
