# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for topology discovery queue.

Tests DiscoveryQueue with both Redis and in-memory fallback modes.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from meho_app.modules.topology.auto_discovery.base import (
    ExtractedEntity,
    ExtractedRelationship,
)
from meho_app.modules.topology.auto_discovery.queue import (
    DiscoveryMessage,
    DiscoveryQueue,
    get_discovery_queue,
    reset_discovery_queue,
)


class TestDiscoveryMessage:
    """Tests for DiscoveryMessage dataclass."""

    def test_create_minimal(self):
        """Test creating message with minimal fields."""
        msg = DiscoveryMessage(
            entities=[],
            relationships=[],
            tenant_id="tenant-1",
        )

        assert msg.entities == []
        assert msg.relationships == []
        assert msg.tenant_id == "tenant-1"
        assert isinstance(msg.timestamp, datetime)

    def test_create_with_data(self):
        """Test creating message with entities and relationships."""
        entity = ExtractedEntity(
            name="web-01",
            description="A VM",
            connector_id="conn-123",
        )
        rel = ExtractedRelationship(
            from_entity_name="web-01",
            to_entity_name="esxi-01",
            relationship_type="runs_on",
        )

        msg = DiscoveryMessage(
            entities=[entity],
            relationships=[rel],
            tenant_id="tenant-1",
        )

        assert len(msg.entities) == 1
        assert len(msg.relationships) == 1
        assert msg.entities[0].name == "web-01"
        assert msg.relationships[0].relationship_type == "runs_on"

    def test_to_dict(self):
        """Test serialization to dictionary."""
        entity = ExtractedEntity(
            name="web-01",
            description="A VM",
            connector_id="conn-123",
        )
        msg = DiscoveryMessage(
            entities=[entity],
            relationships=[],
            tenant_id="tenant-1",
        )

        data = msg.to_dict()

        assert data["tenant_id"] == "tenant-1"
        assert len(data["entities"]) == 1
        assert data["entities"][0]["name"] == "web-01"
        assert "timestamp" in data

    def test_to_json(self):
        """Test serialization to JSON."""
        msg = DiscoveryMessage(
            entities=[],
            relationships=[],
            tenant_id="tenant-1",
        )

        json_str = msg.to_json()

        assert "tenant-1" in json_str
        assert "entities" in json_str

    def test_from_dict(self):
        """Test deserialization from dictionary."""
        data = {
            "entities": [
                {
                    "name": "web-01",
                    "description": "A VM",
                    "connector_id": "conn-123",
                }
            ],
            "relationships": [
                {
                    "from_entity_name": "web-01",
                    "to_entity_name": "esxi-01",
                    "relationship_type": "runs_on",
                }
            ],
            "tenant_id": "tenant-1",
            "timestamp": "2024-01-15T10:30:00",
        }

        msg = DiscoveryMessage.from_dict(data)

        assert msg.tenant_id == "tenant-1"
        assert len(msg.entities) == 1
        assert msg.entities[0].name == "web-01"
        assert len(msg.relationships) == 1
        assert msg.relationships[0].relationship_type == "runs_on"

    def test_from_json(self):
        """Test deserialization from JSON."""
        json_str = """
        {
            "entities": [],
            "relationships": [],
            "tenant_id": "tenant-1",
            "timestamp": "2024-01-15T10:30:00"
        }
        """

        msg = DiscoveryMessage.from_json(json_str)

        assert msg.tenant_id == "tenant-1"

    def test_roundtrip(self):
        """Test serialization/deserialization roundtrip."""
        original = DiscoveryMessage(
            entities=[
                ExtractedEntity(
                    name="web-01",
                    description="A VM",
                    connector_id="conn-123",
                    raw_attributes={"cpu": 4},
                )
            ],
            relationships=[
                ExtractedRelationship(
                    from_entity_name="web-01",
                    to_entity_name="esxi-01",
                    relationship_type="runs_on",
                )
            ],
            tenant_id="tenant-1",
        )

        json_str = original.to_json()
        restored = DiscoveryMessage.from_json(json_str)

        assert restored.tenant_id == original.tenant_id
        assert len(restored.entities) == len(original.entities)
        assert restored.entities[0].name == original.entities[0].name
        assert len(restored.relationships) == len(original.relationships)


class TestDiscoveryQueueInMemory:
    """Tests for DiscoveryQueue with in-memory fallback."""

    @pytest.fixture
    def queue(self):
        """Create in-memory queue."""
        reset_discovery_queue()
        return DiscoveryQueue()  # No Redis client

    @pytest.fixture
    def sample_message(self):
        """Create sample discovery message."""
        return DiscoveryMessage(
            entities=[
                ExtractedEntity(
                    name="web-01",
                    description="A VM",
                    connector_id="conn-123",
                )
            ],
            relationships=[],
            tenant_id="tenant-1",
        )

    def test_init_without_redis(self, queue):
        """Test initializing queue without Redis."""
        assert queue.is_redis_backed is False
        assert queue.redis is None

    @pytest.mark.asyncio
    async def test_push(self, queue, sample_message):
        """Test pushing message to queue."""
        result = await queue.push(sample_message)

        assert result is True
        assert await queue.size() == 1

    @pytest.mark.asyncio
    async def test_pop(self, queue, sample_message):
        """Test popping message from queue."""
        await queue.push(sample_message)

        msg = await queue.pop()

        assert msg is not None
        assert msg.tenant_id == "tenant-1"
        assert len(msg.entities) == 1
        assert msg.entities[0].name == "web-01"

    @pytest.mark.asyncio
    async def test_pop_empty(self, queue):
        """Test popping from empty queue."""
        msg = await queue.pop()
        assert msg is None

    @pytest.mark.asyncio
    async def test_pop_batch(self, queue):
        """Test popping multiple messages."""
        for i in range(5):
            await queue.push(
                DiscoveryMessage(
                    entities=[],
                    relationships=[],
                    tenant_id=f"tenant-{i}",
                )
            )

        messages = await queue.pop_batch(3)

        assert len(messages) == 3
        assert messages[0].tenant_id == "tenant-0"
        assert messages[1].tenant_id == "tenant-1"
        assert messages[2].tenant_id == "tenant-2"

        # Should have 2 remaining
        assert await queue.size() == 2

    @pytest.mark.asyncio
    async def test_pop_batch_more_than_available(self, queue, sample_message):
        """Test popping more messages than available."""
        await queue.push(sample_message)

        messages = await queue.pop_batch(100)

        assert len(messages) == 1

    @pytest.mark.asyncio
    async def test_size(self, queue, sample_message):
        """Test getting queue size."""
        assert await queue.size() == 0

        await queue.push(sample_message)
        assert await queue.size() == 1

        await queue.push(sample_message)
        assert await queue.size() == 2

    @pytest.mark.asyncio
    async def test_clear(self, queue, sample_message):
        """Test clearing queue."""
        await queue.push(sample_message)
        await queue.push(sample_message)

        count = await queue.clear()

        assert count == 2
        assert await queue.size() == 0

    @pytest.mark.asyncio
    async def test_peek(self, queue, sample_message):
        """Test peeking at queue without removing."""
        await queue.push(sample_message)

        messages = await queue.peek(1)

        assert len(messages) == 1
        assert messages[0].tenant_id == "tenant-1"

        # Should still be in queue
        assert await queue.size() == 1

    @pytest.mark.asyncio
    async def test_health_check(self, queue):
        """Test health check for in-memory queue."""
        result = await queue.health_check()
        assert result is True


class TestDiscoveryQueueRedis:
    """Tests for DiscoveryQueue with Redis backend."""

    @pytest.fixture
    def mock_redis(self):
        """Create mock Redis client."""
        redis = MagicMock()
        redis.rpush = AsyncMock(return_value=1)
        redis.lpop = AsyncMock(return_value=None)
        redis.llen = AsyncMock(return_value=0)
        redis.delete = AsyncMock(return_value=1)
        redis.lrange = AsyncMock(return_value=[])
        redis.ping = AsyncMock(return_value=True)
        # pipeline() is a sync method that returns an async context manager
        redis.pipeline = MagicMock()
        return redis

    @pytest.fixture
    def queue(self, mock_redis):
        """Create Redis-backed queue."""
        reset_discovery_queue()
        return DiscoveryQueue(redis_client=mock_redis)

    @pytest.fixture
    def sample_message(self):
        """Create sample discovery message."""
        return DiscoveryMessage(
            entities=[
                ExtractedEntity(
                    name="web-01",
                    description="A VM",
                    connector_id="conn-123",
                )
            ],
            relationships=[],
            tenant_id="tenant-1",
        )

    def test_init_with_redis(self, queue, mock_redis):
        """Test initializing queue with Redis."""
        assert queue.is_redis_backed is True
        assert queue.redis is mock_redis

    @pytest.mark.asyncio
    async def test_push_to_redis(self, queue, mock_redis, sample_message):
        """Test pushing message to Redis."""
        result = await queue.push(sample_message)

        assert result is True
        mock_redis.rpush.assert_called_once()

    @pytest.mark.asyncio
    async def test_pop_from_redis(self, queue, mock_redis, sample_message):
        """Test popping message from Redis."""
        # Set up mock to return a message
        mock_redis.lpop.return_value = sample_message.to_json()

        msg = await queue.pop()

        assert msg is not None
        assert msg.tenant_id == "tenant-1"
        mock_redis.lpop.assert_called_once()

    @pytest.mark.asyncio
    async def test_pop_batch_from_redis(self, queue, mock_redis):
        """Test batch pop from Redis using pipeline."""
        # redis-py's pipeline() returns an object that is both the pipeline
        # and an async context manager. Create a proper mock for this pattern.
        mock_pipeline = MagicMock()
        mock_pipeline.lpop = MagicMock(return_value=mock_pipeline)  # Returns self for chaining
        mock_pipeline.execute = AsyncMock(
            return_value=[
                '{"entities": [], "relationships": [], "tenant_id": "t1", "timestamp": "2024-01-01T00:00:00"}',
                '{"entities": [], "relationships": [], "tenant_id": "t2", "timestamp": "2024-01-01T00:00:00"}',
                None,
            ]
        )
        mock_pipeline.__aenter__ = AsyncMock(return_value=mock_pipeline)
        mock_pipeline.__aexit__ = AsyncMock(return_value=None)

        mock_redis.pipeline.return_value = mock_pipeline

        messages = await queue.pop_batch(3)

        assert len(messages) == 2
        assert messages[0].tenant_id == "t1"
        assert messages[1].tenant_id == "t2"

    @pytest.mark.asyncio
    async def test_size_from_redis(self, queue, mock_redis):
        """Test getting size from Redis."""
        mock_redis.llen.return_value = 5

        size = await queue.size()

        assert size == 5
        mock_redis.llen.assert_called_once()

    @pytest.mark.asyncio
    async def test_clear_redis(self, queue, mock_redis):
        """Test clearing Redis queue."""
        mock_redis.llen.return_value = 3

        count = await queue.clear()

        assert count == 3
        mock_redis.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_health_check_redis(self, queue, mock_redis):
        """Test health check with Redis."""
        result = await queue.health_check()

        assert result is True
        mock_redis.ping.assert_called_once()

    @pytest.mark.asyncio
    async def test_fallback_on_redis_error(self, mock_redis, sample_message):
        """Test fallback to in-memory on Redis error."""
        mock_redis.rpush.side_effect = Exception("Redis connection failed")

        queue = DiscoveryQueue(redis_client=mock_redis)
        result = await queue.push(sample_message)

        # Should fall back to in-memory
        assert result is True


class TestDiscoveryQueueSingleton:
    """Tests for discovery queue singleton management."""

    def setup_method(self):
        """Reset singleton before each test."""
        reset_discovery_queue()

    @pytest.mark.asyncio
    async def test_get_queue_creates_instance(self):
        """Test that get_discovery_queue creates instance."""
        queue = await get_discovery_queue()

        assert queue is not None
        assert isinstance(queue, DiscoveryQueue)

    @pytest.mark.asyncio
    async def test_get_queue_returns_same_instance(self):
        """Test that get_discovery_queue returns singleton."""
        queue1 = await get_discovery_queue()
        queue2 = await get_discovery_queue()

        assert queue1 is queue2

    @pytest.mark.asyncio
    async def test_reset_queue(self):
        """Test resetting queue singleton."""
        queue1 = await get_discovery_queue()
        reset_discovery_queue()
        queue2 = await get_discovery_queue()

        assert queue1 is not queue2
