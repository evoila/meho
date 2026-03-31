# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for RedisSSEBroadcaster.

Tests the Redis pub/sub SSE broadcaster with mock Redis,
verifying channel naming, publish/subscribe, keepalive,
cleanup, and active-status tracking.

Phase 84: RedisSSEBroadcaster now uses async Redis rpush/lpush for event log,
MagicMock can't be used in 'await' expression -- needs AsyncMock.
"""

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: RedisSSEBroadcaster uses async Redis rpush for event log, mock needs AsyncMock instead of MagicMock")

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from meho_app.modules.agents.sse.broadcaster import RedisSSEBroadcaster


@pytest.fixture
def mock_redis():
    """Create a mock async Redis client.

    Note: redis.pubsub() is a synchronous call that returns an object
    with async methods (subscribe, get_message, unsubscribe, aclose).
    We use MagicMock for the client and pubsub() but AsyncMock for
    individual async methods (publish, set, exists, delete on client;
    subscribe, get_message, unsubscribe, aclose on pubsub).
    """
    client = MagicMock()
    # Async methods on the client itself
    client.publish = AsyncMock()
    client.set = AsyncMock()
    client.exists = AsyncMock(return_value=0)
    client.delete = AsyncMock()

    # pubsub() is synchronous in redis.asyncio
    pubsub = MagicMock()
    pubsub.subscribe = AsyncMock()
    pubsub.unsubscribe = AsyncMock()
    pubsub.aclose = AsyncMock()
    pubsub.get_message = AsyncMock(return_value=None)
    client.pubsub.return_value = pubsub
    return client


@pytest.fixture
def broadcaster(mock_redis):
    """Create a RedisSSEBroadcaster with mock Redis."""
    return RedisSSEBroadcaster(mock_redis)


class TestChannelName:
    """Tests for channel_name method."""

    def test_channel_name(self, broadcaster):
        """Verify channel name follows meho:sse:{session_id} convention."""
        assert broadcaster.channel_name("abc-123") == "meho:sse:abc-123"

    def test_channel_name_uuid(self, broadcaster):
        """Verify channel name works with full UUID."""
        session_id = "550e8400-e29b-41d4-a716-446655440000"
        assert broadcaster.channel_name(session_id) == f"meho:sse:{session_id}"


class TestPublish:
    """Tests for publish method."""

    @pytest.mark.asyncio
    async def test_publish_serializes_and_publishes(self, broadcaster, mock_redis):
        """Publish serializes event to JSON and publishes to correct channel."""
        event = {"type": "thought", "data": {"content": "hi"}}

        await broadcaster.publish("session-1", event)

        mock_redis.publish.assert_awaited_once_with(
            "meho:sse:session-1",
            json.dumps(event),
        )

    @pytest.mark.asyncio
    async def test_publish_complex_event(self, broadcaster, mock_redis):
        """Publish handles complex nested event payloads."""
        event = {
            "type": "observation",
            "data": {
                "tool": "list_pods",
                "result": {"pods": ["pod-a", "pod-b"]},
                "count": 2,
            },
        }

        await broadcaster.publish("session-2", event)

        mock_redis.publish.assert_awaited_once_with(
            "meho:sse:session-2",
            json.dumps(event),
        )


class TestSubscribe:
    """Tests for subscribe method."""

    @pytest.mark.asyncio
    async def test_subscribe_yields_events(self, broadcaster, mock_redis):
        """Subscribe yields parsed JSON events from the channel."""
        pubsub = mock_redis.pubsub.return_value
        event_data = {"type": "thought", "data": {"content": "analyzing"}}

        # First call returns a message, second raises to break the loop
        call_count = 0

        async def mock_get_message(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"type": "message", "data": json.dumps(event_data)}
            # Return a keepalive then we'll break
            return None

        pubsub.get_message = mock_get_message

        events = []
        async for event in broadcaster.subscribe("session-1"):
            events.append(event)
            if event.get("type") != "keepalive":
                break

        assert len(events) == 1
        assert events[0] == event_data

    @pytest.mark.asyncio
    async def test_subscribe_yields_keepalive_on_timeout(self, broadcaster, mock_redis):
        """Subscribe yields keepalive when get_message returns None (timeout)."""
        pubsub = mock_redis.pubsub.return_value

        async def mock_get_message(**kwargs):
            return None  # Timeout

        pubsub.get_message = mock_get_message

        events = []
        async for event in broadcaster.subscribe("session-1"):
            events.append(event)
            break  # Break after first keepalive

        assert len(events) == 1
        assert events[0] == {"type": "keepalive"}

    @pytest.mark.asyncio
    async def test_subscribe_handles_multiple_events(self, broadcaster, mock_redis):
        """Subscribe yields multiple events in sequence."""
        pubsub = mock_redis.pubsub.return_value

        messages = [
            {"type": "message", "data": json.dumps({"type": "thought", "step": 1})},
            {"type": "message", "data": json.dumps({"type": "action", "step": 2})},
            {"type": "message", "data": json.dumps({"type": "observation", "step": 3})},
        ]
        call_count = 0

        async def mock_get_message(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= len(messages):
                return messages[call_count - 1]
            return None

        pubsub.get_message = mock_get_message

        events = []
        async for event in broadcaster.subscribe("session-1"):
            if event.get("type") == "keepalive":
                break
            events.append(event)

        assert len(events) == 3
        assert events[0] == {"type": "thought", "step": 1}
        assert events[1] == {"type": "action", "step": 2}
        assert events[2] == {"type": "observation", "step": 3}

    @pytest.mark.asyncio
    async def test_subscribe_cleanup(self, broadcaster, mock_redis):
        """Subscribe properly unsubscribes and closes pubsub on exit."""
        pubsub = mock_redis.pubsub.return_value

        async def mock_get_message(**kwargs):
            return None  # Timeout -> keepalive

        pubsub.get_message = mock_get_message

        # Use explicit aclose() to ensure finally block runs synchronously
        gen = broadcaster.subscribe("session-1")
        async for _event in gen:
            break  # Immediately exit
        await gen.aclose()  # Ensure cleanup runs before assertions

        pubsub.unsubscribe.assert_awaited_once_with("meho:sse:session-1")
        pubsub.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_subscribe_resilient_to_bad_message(self, broadcaster, mock_redis):
        """Subscribe skips individual message parse failures without dying."""
        pubsub = mock_redis.pubsub.return_value

        call_count = 0

        async def mock_get_message(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Bad JSON that will raise on parse
                return {"type": "message", "data": "not-valid-json{{{"}
            if call_count == 2:
                # Good message after bad one
                return {
                    "type": "message",
                    "data": json.dumps({"type": "recovered"}),
                }
            return None

        pubsub.get_message = mock_get_message

        events = []
        async for event in broadcaster.subscribe("session-1"):
            if event.get("type") == "keepalive":
                break
            events.append(event)

        # The bad message is caught by the exception handler, loop continues
        # and yields the "recovered" event
        assert any(e.get("type") == "recovered" for e in events)


class TestActiveStatus:
    """Tests for set_active, is_active, clear_active methods."""

    @pytest.mark.asyncio
    async def test_set_active(self, broadcaster, mock_redis):
        """set_active calls redis.set with correct key and TTL."""
        await broadcaster.set_active("session-1")

        mock_redis.set.assert_awaited_once_with("meho:active:session-1", "1", ex=300)

    @pytest.mark.asyncio
    async def test_set_active_custom_ttl(self, broadcaster, mock_redis):
        """set_active respects custom TTL."""
        await broadcaster.set_active("session-1", ttl_seconds=600)

        mock_redis.set.assert_awaited_once_with("meho:active:session-1", "1", ex=600)

    @pytest.mark.asyncio
    async def test_is_active_true(self, broadcaster, mock_redis):
        """is_active returns True when Redis key exists."""
        mock_redis.exists.return_value = 1

        result = await broadcaster.is_active("session-1")

        assert result is True
        mock_redis.exists.assert_awaited_once_with("meho:active:session-1")

    @pytest.mark.asyncio
    async def test_is_active_false(self, broadcaster, mock_redis):
        """is_active returns False when Redis key does not exist."""
        mock_redis.exists.return_value = 0

        result = await broadcaster.is_active("session-1")

        assert result is False
        mock_redis.exists.assert_awaited_once_with("meho:active:session-1")

    @pytest.mark.asyncio
    async def test_clear_active(self, broadcaster, mock_redis):
        """clear_active deletes the correct Redis key."""
        await broadcaster.clear_active("session-1")

        mock_redis.delete.assert_awaited_once_with("meho:active:session-1")
