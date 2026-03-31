# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for AgentStateStore Redis-backed state persistence."""

import json
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from meho_app.modules.agents.persistence import (
    AgentStateStore,
    OrchestratorSessionState,
)


@pytest.fixture
def mock_redis():
    """Create a mock Redis client."""
    mock = MagicMock()
    mock.get = AsyncMock(return_value=None)
    mock.setex = AsyncMock(return_value=True)
    mock.delete = AsyncMock(return_value=1)
    mock.exists = AsyncMock(return_value=0)
    mock.ping = AsyncMock(return_value=True)
    return mock


@pytest.fixture
def state_store(mock_redis):
    """Create an AgentStateStore with mocked Redis."""
    return AgentStateStore(
        redis_client=mock_redis,
        ttl=timedelta(hours=24),
        key_prefix="meho:agents:state",
    )


class TestAgentStateStoreInit:
    """Test AgentStateStore initialization."""

    def test_default_ttl(self, mock_redis):
        """Test default TTL is 24 hours."""
        store = AgentStateStore(mock_redis)
        assert store.ttl == timedelta(hours=24)

    def test_default_key_prefix(self, mock_redis):
        """Test default key prefix."""
        store = AgentStateStore(mock_redis)
        assert store.key_prefix == "meho:agents:state"

    def test_custom_ttl(self, mock_redis):
        """Test custom TTL."""
        store = AgentStateStore(mock_redis, ttl=timedelta(hours=1))
        assert store.ttl == timedelta(hours=1)

    def test_custom_key_prefix(self, mock_redis):
        """Test custom key prefix."""
        store = AgentStateStore(mock_redis, key_prefix="custom:prefix")
        assert store.key_prefix == "custom:prefix"

    def test_make_key(self, state_store):
        """Test key generation."""
        key = state_store._make_key("session-123")
        assert key == "meho:agents:state:session-123"


class TestAgentStateStoreSave:
    """Test AgentStateStore.save_state()."""

    @pytest.mark.asyncio
    async def test_save_state_success(self, state_store, mock_redis):
        """Test successful state save."""
        state = OrchestratorSessionState()
        state.remember_connector("c1", "K8s", "kubernetes", status="success")

        result = await state_store.save_state("session-123", state)

        assert result is True
        mock_redis.setex.assert_called_once()

        # Verify the key
        call_args = mock_redis.setex.call_args
        assert call_args[0][0] == "meho:agents:state:session-123"

        # Verify TTL (24 hours = 86400 seconds)
        assert call_args[0][1] == 86400

        # Verify JSON is valid
        json_data = call_args[0][2]
        parsed = json.loads(json_data)
        assert "connectors" in parsed
        assert "c1" in parsed["connectors"]

    @pytest.mark.asyncio
    async def test_save_state_increments_turn_count(self, state_store, mock_redis):
        """Test that save_state increments turn_count."""
        state = OrchestratorSessionState()
        initial_turn = state.turn_count

        await state_store.save_state("session-123", state)

        assert state.turn_count == initial_turn + 1

    @pytest.mark.asyncio
    async def test_save_state_updates_last_updated(self, state_store, mock_redis):
        """Test that save_state updates last_updated."""
        state = OrchestratorSessionState()
        original_time = state.last_updated

        await state_store.save_state("session-123", state)

        assert state.last_updated >= original_time

    @pytest.mark.asyncio
    async def test_save_state_custom_ttl(self, state_store, mock_redis):
        """Test save_state with custom TTL."""
        state = OrchestratorSessionState()

        await state_store.save_state("session-123", state, ttl=timedelta(hours=1))

        call_args = mock_redis.setex.call_args
        assert call_args[0][1] == 3600  # 1 hour

    @pytest.mark.asyncio
    async def test_save_state_failure(self, state_store, mock_redis):
        """Test save_state handles Redis failure."""
        mock_redis.setex = AsyncMock(side_effect=Exception("Redis error"))
        state = OrchestratorSessionState()

        result = await state_store.save_state("session-123", state)

        assert result is False


class TestAgentStateStoreLoad:
    """Test AgentStateStore.load_state()."""

    @pytest.mark.asyncio
    async def test_load_state_not_found(self, state_store, mock_redis):
        """Test load_state returns None when not found."""
        mock_redis.get = AsyncMock(return_value=None)

        result = await state_store.load_state("session-123")

        assert result is None
        mock_redis.get.assert_called_once_with("meho:agents:state:session-123")

    @pytest.mark.asyncio
    async def test_load_state_success(self, state_store, mock_redis):
        """Test successful state load."""
        state = OrchestratorSessionState()
        state.remember_connector("c1", "K8s", "kubernetes", status="success")
        state.turn_count = 5

        mock_redis.get = AsyncMock(return_value=json.dumps(state.to_dict()))

        result = await state_store.load_state("session-123")

        assert result is not None
        assert len(result.connectors) == 1
        assert "c1" in result.connectors
        assert result.turn_count == 5

    @pytest.mark.asyncio
    async def test_load_state_corrupted_json(self, state_store, mock_redis):
        """Test load_state handles corrupted JSON."""
        mock_redis.get = AsyncMock(return_value="not valid json")
        mock_redis.delete = AsyncMock(return_value=1)

        result = await state_store.load_state("session-123")

        assert result is None
        # Should delete corrupted state
        mock_redis.delete.assert_called_once_with("meho:agents:state:session-123")

    @pytest.mark.asyncio
    async def test_load_state_redis_failure(self, state_store, mock_redis):
        """Test load_state handles Redis failure."""
        mock_redis.get = AsyncMock(side_effect=Exception("Redis error"))

        result = await state_store.load_state("session-123")

        assert result is None


class TestAgentStateStoreDelete:
    """Test AgentStateStore.delete_state()."""

    @pytest.mark.asyncio
    async def test_delete_state_success(self, state_store, mock_redis):
        """Test successful state deletion."""
        mock_redis.delete = AsyncMock(return_value=1)

        result = await state_store.delete_state("session-123")

        assert result is True
        mock_redis.delete.assert_called_once_with("meho:agents:state:session-123")

    @pytest.mark.asyncio
    async def test_delete_state_not_found(self, state_store, mock_redis):
        """Test delete_state returns False when not found."""
        mock_redis.delete = AsyncMock(return_value=0)

        result = await state_store.delete_state("session-123")

        assert result is False

    @pytest.mark.asyncio
    async def test_delete_state_redis_failure(self, state_store, mock_redis):
        """Test delete_state handles Redis failure."""
        mock_redis.delete = AsyncMock(side_effect=Exception("Redis error"))

        result = await state_store.delete_state("session-123")

        assert result is False


class TestAgentStateStoreExists:
    """Test AgentStateStore.exists()."""

    @pytest.mark.asyncio
    async def test_exists_true(self, state_store, mock_redis):
        """Test exists returns True when state exists."""
        mock_redis.exists = AsyncMock(return_value=1)

        result = await state_store.exists("session-123")

        assert result is True
        mock_redis.exists.assert_called_once_with("meho:agents:state:session-123")

    @pytest.mark.asyncio
    async def test_exists_false(self, state_store, mock_redis):
        """Test exists returns False when state doesn't exist."""
        mock_redis.exists = AsyncMock(return_value=0)

        result = await state_store.exists("session-123")

        assert result is False

    @pytest.mark.asyncio
    async def test_exists_redis_failure(self, state_store, mock_redis):
        """Test exists handles Redis failure."""
        mock_redis.exists = AsyncMock(side_effect=Exception("Redis error"))

        result = await state_store.exists("session-123")

        assert result is False


class TestAgentStateStorePing:
    """Test AgentStateStore.ping()."""

    @pytest.mark.asyncio
    async def test_ping_success(self, state_store, mock_redis):
        """Test ping returns True when Redis is available."""
        mock_redis.ping = AsyncMock(return_value=True)

        result = await state_store.ping()

        assert result is True

    @pytest.mark.asyncio
    async def test_ping_failure(self, state_store, mock_redis):
        """Test ping returns False when Redis is unavailable."""
        mock_redis.ping = AsyncMock(side_effect=Exception("Connection refused"))

        result = await state_store.ping()

        assert result is False


class TestAgentStateStoreIntegration:
    """Integration tests for save/load roundtrip."""

    @pytest.mark.asyncio
    async def test_save_load_roundtrip(self, mock_redis):
        """Test that saved state can be loaded correctly."""
        store = AgentStateStore(mock_redis)

        # Create state with various data
        state = OrchestratorSessionState()
        state.remember_connector("c1", "K8s Prod", "kubernetes", "list pods", "success")
        state.set_operation_context("Debug pod crashes", ["nginx-pod"])
        state.register_cached_data("pods", "c1", 50)
        state.record_error("c2", "timeout", "Request timed out")

        # Capture the JSON that would be saved
        saved_json = None

        async def capture_setex(key, ttl, data):
            nonlocal saved_json
            saved_json = data
            return True

        mock_redis.setex = AsyncMock(side_effect=capture_setex)

        # Save state
        await store.save_state("session-123", state)

        # Setup load to return saved JSON
        mock_redis.get = AsyncMock(return_value=saved_json)

        # Load state
        loaded = await store.load_state("session-123")

        # Verify
        assert loaded is not None
        assert "c1" in loaded.connectors
        assert loaded.connectors["c1"].connector_name == "K8s Prod"
        assert loaded.current_operation == "Debug pod crashes"
        assert "pods" in loaded.cached_tables
        assert len(loaded.recent_errors) == 1
        assert loaded.turn_count == 1  # Incremented on save
