# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Discovery queue for topology auto-discovery.

Provides Redis-backed queue with in-memory fallback for storing
discovered entities before batch processing.

Uses existing Redis patterns from meho_app/modules/agent/state_store.py
"""

import json
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as redis

from meho_app.core.otel import get_logger

from .base import ExtractedEntity, ExtractedRelationship

logger = get_logger(__name__)


@dataclass
class DiscoveryMessage:
    """
    Message containing discovered entities and relationships.

    Queued for background processing by the BatchProcessor.

    Attributes:
        entities: List of entities to store
        relationships: List of relationships to create
        tenant_id: Tenant ID for multi-tenancy
        connector_type: Type of connector (kubernetes, vmware, etc.)
        timestamp: When the discovery was made
    """

    entities: list[ExtractedEntity]
    relationships: list[ExtractedRelationship]
    tenant_id: str
    connector_type: str = "unknown"  # Required for StoreDiscoveryInput
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "entities": [e.to_dict() for e in self.entities],
            "relationships": [r.to_dict() for r in self.relationships],
            "tenant_id": self.tenant_id,
            "connector_type": self.connector_type,
            "timestamp": self.timestamp.isoformat(),
        }

    def to_json(self) -> str:
        """Convert to JSON string for Redis storage."""
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DiscoveryMessage":
        """Create from dictionary."""
        entities = [ExtractedEntity.from_dict(e) for e in data.get("entities", [])]
        relationships = [ExtractedRelationship.from_dict(r) for r in data.get("relationships", [])]

        timestamp = data.get("timestamp")
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)
        elif timestamp is None:
            timestamp = datetime.now(tz=UTC)

        return cls(
            entities=entities,
            relationships=relationships,
            tenant_id=data["tenant_id"],
            connector_type=data.get("connector_type", "unknown"),
            timestamp=timestamp,
        )

    @classmethod
    def from_json(cls, json_str: str) -> "DiscoveryMessage":
        """Create from JSON string."""
        return cls.from_dict(json.loads(json_str))


class DiscoveryQueue:
    """
    Queue for topology discovery messages.

    Features:
    - Redis-backed when Redis is available (production)
    - Falls back to in-memory deque when Redis unavailable (dev/testing)
    - Non-blocking push operations
    - Batch pop for efficient processing

    Usage:
        # With Redis
        redis_client = get_redis_client(redis_url)
        queue = DiscoveryQueue(redis=redis_client)

        # Without Redis (fallback)
        queue = DiscoveryQueue()

        # Push discovery
        await queue.push(DiscoveryMessage(
            entities=[...],
            relationships=[...],
            tenant_id="tenant-1",
        ))

        # Pop batch for processing
        messages = await queue.pop_batch(max_items=100)
    """

    def __init__(
        self,
        redis_client: redis.Redis | None = None,
        queue_key: str = "topology:discovery:queue",
    ) -> None:
        """
        Initialize the discovery queue.

        Args:
            redis_client: Optional async Redis client for persistent queue
            queue_key: Redis key for the queue list
        """
        self.redis = redis_client
        self.queue_key = queue_key
        self._fallback_queue: deque[DiscoveryMessage] = deque()
        self._using_redis = redis_client is not None

        if self._using_redis:
            logger.info(f"DiscoveryQueue: Using Redis queue at {queue_key}")
        else:
            logger.info("DiscoveryQueue: Using in-memory fallback queue")

    @property
    def is_redis_backed(self) -> bool:
        """Check if queue is using Redis."""
        return self._using_redis

    async def push(self, message: DiscoveryMessage) -> bool:
        """
        Push a discovery message to the queue.

        Args:
            message: Discovery message to queue

        Returns:
            True if pushed successfully, False otherwise
        """
        try:
            if self._using_redis and self.redis:
                # Push to Redis list (right push for FIFO)
                json_data = message.to_json()
                await self.redis.rpush(self.queue_key, json_data)
                logger.debug(
                    f"Queued discovery: {len(message.entities)} entities, "
                    f"{len(message.relationships)} relationships"
                )
            else:
                # Fallback to in-memory deque
                self._fallback_queue.append(message)
                logger.debug(
                    f"Queued discovery (in-memory): {len(message.entities)} entities, "
                    f"{len(message.relationships)} relationships"
                )

            return True

        except Exception as e:
            logger.error(f"Failed to push discovery message: {e}")

            # Try fallback on Redis error
            if self._using_redis:
                logger.warning("Falling back to in-memory queue due to Redis error")
                self._fallback_queue.append(message)
                return True

            return False

    async def pop(self) -> DiscoveryMessage | None:
        """
        Pop a single message from the queue.

        Returns:
            DiscoveryMessage if available, None if queue is empty
        """
        try:
            if self._using_redis and self.redis:
                # Pop from Redis list (left pop for FIFO)
                json_data = await self.redis.lpop(self.queue_key)
                if json_data:
                    return DiscoveryMessage.from_json(json_data)
                return None
            else:
                # Pop from in-memory deque
                if self._fallback_queue:
                    return self._fallback_queue.popleft()
                return None

        except Exception as e:
            logger.error(f"Failed to pop discovery message: {e}")
            return None

    async def pop_batch(
        self, max_items: int = 100
    ) -> list[DiscoveryMessage]:  # NOSONAR (cognitive complexity)
        """
        Pop multiple messages from the queue.

        Args:
            max_items: Maximum number of messages to pop

        Returns:
            List of DiscoveryMessage objects (may be empty)
        """
        messages: list[DiscoveryMessage] = []

        try:
            if self._using_redis and self.redis:
                # Use LPOP with count (Redis 6.2+) or pipeline
                # For compatibility, we'll use a pipeline approach
                async with self.redis.pipeline() as pipe:
                    for _ in range(max_items):
                        pipe.lpop(self.queue_key)
                    results = await pipe.execute()

                for json_data in results:
                    if json_data:
                        try:
                            messages.append(DiscoveryMessage.from_json(json_data))
                        except Exception as e:
                            logger.warning(f"Failed to parse queued message: {e}")
                            continue
            else:
                # Pop from in-memory deque
                for _ in range(min(max_items, len(self._fallback_queue))):
                    if self._fallback_queue:
                        messages.append(self._fallback_queue.popleft())

            if messages:
                logger.debug(f"Popped {len(messages)} discovery messages from queue")

        except Exception as e:
            logger.error(f"Failed to pop batch from queue: {e}")

        return messages

    async def size(self) -> int:
        """
        Get the current queue size.

        Returns:
            Number of messages in the queue
        """
        try:
            if self._using_redis and self.redis:
                return await self.redis.llen(self.queue_key)
            else:
                return len(self._fallback_queue)

        except Exception as e:
            logger.error(f"Failed to get queue size: {e}")
            return 0

    async def clear(self) -> int:
        """
        Clear all messages from the queue.

        Returns:
            Number of messages cleared
        """
        try:
            if self._using_redis and self.redis:
                count = await self.redis.llen(self.queue_key)
                if count > 0:
                    await self.redis.delete(self.queue_key)
                logger.info(f"Cleared {count} messages from Redis queue")
                return count
            else:
                count = len(self._fallback_queue)
                self._fallback_queue.clear()
                logger.info(f"Cleared {count} messages from in-memory queue")
                return count

        except Exception as e:
            logger.error(f"Failed to clear queue: {e}")
            return 0

    async def peek(self, count: int = 1) -> list[DiscoveryMessage]:
        """
        Peek at messages without removing them.

        Args:
            count: Number of messages to peek

        Returns:
            List of DiscoveryMessage objects (may be empty)
        """
        messages: list[DiscoveryMessage] = []

        try:
            if self._using_redis and self.redis:
                # Use LRANGE to peek without removing
                results = await self.redis.lrange(self.queue_key, 0, count - 1)
                for json_data in results:
                    if json_data:
                        try:
                            messages.append(DiscoveryMessage.from_json(json_data))
                        except Exception as e:
                            logger.warning(f"Failed to parse peeked message: {e}")
            else:
                # Peek from in-memory deque (convert to list for indexing)
                queue_list = list(self._fallback_queue)
                messages = queue_list[:count]

        except Exception as e:
            logger.error(f"Failed to peek queue: {e}")

        return messages

    async def health_check(self) -> bool:
        """
        Check if the queue is operational.

        Returns:
            True if healthy, False otherwise
        """
        try:
            if self._using_redis and self.redis:
                return await self.redis.ping()
            else:
                # In-memory queue is always healthy
                return True

        except Exception as e:
            logger.error(f"Queue health check failed: {e}")
            return False


# =============================================================================
# Singleton / Factory
# =============================================================================

_queue_instance: DiscoveryQueue | None = None


def get_discovery_queue(
    redis_client: redis.Redis | None = None,
    queue_key: str = "topology:discovery:queue",
) -> DiscoveryQueue:
    """
    Get or create the discovery queue singleton.

    Args:
        redis_client: Optional Redis client
        queue_key: Redis key for the queue

    Returns:
        DiscoveryQueue instance
    """
    global _queue_instance

    if _queue_instance is None:
        _queue_instance = DiscoveryQueue(
            redis_client=redis_client,
            queue_key=queue_key,
        )
    elif redis_client and not _queue_instance.is_redis_backed:
        # Upgrade to Redis if client provided
        _queue_instance = DiscoveryQueue(
            redis_client=redis_client,
            queue_key=queue_key,
        )
        logger.info("DiscoveryQueue upgraded to Redis backend")

    return _queue_instance


def reset_discovery_queue() -> None:
    """Reset the queue singleton (for testing)."""
    global _queue_instance
    _queue_instance = None
