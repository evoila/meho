# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Redis Pub/Sub SSE Broadcaster for group session fan-out.

Publishes agent events to Redis channels and provides async generator
subscriptions for viewer SSE endpoints. Only used for group/tenant
sessions -- private sessions use the existing direct queue/callback path.

Channel naming: meho:sse:{session_id}
Event log: meho:event_log:{session_id} (Redis list with 10-min TTL)
Active status: meho:active:{session_id} (Redis key with 5-min TTL)

Event replay: Events are stored in a Redis list alongside pub/sub.
When a subscriber connects (e.g. user opens an event-triggered session),
stored events are replayed first, then live pub/sub events are streamed.
Deduplication uses a sequence number (_seq) assigned from the list length.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import redis.asyncio as redis

from meho_app.core.otel import get_logger

logger = get_logger(__name__)

EVENT_LOG_TTL_SECONDS = 600  # 10 minutes


class RedisSSEBroadcaster:
    """Fan-out broadcaster that publishes agent events to Redis pub/sub
    channels and provides async generator subscriptions for SSE endpoints.

    Each group/tenant session gets its own Redis channel (meho:sse:{session_id}).
    Multiple viewers subscribe to the same channel and receive events in real time.

    Events are also stored in a Redis list for replay. When a subscriber
    connects after events have already been published (e.g. event-triggered sessions),
    stored events are replayed first before switching to live pub/sub.

    Usage:
        broadcaster = RedisSSEBroadcaster(redis_client)

        # Agent side: publish events
        await broadcaster.publish(session_id, {"type": "thought", "data": {...}})

        # Viewer side: subscribe to event stream (with replay)
        async for event in broadcaster.subscribe(session_id):
            yield f"data: {json.dumps(event)}\\n\\n"
    """

    def __init__(self, redis_client: redis.Redis) -> None:
        self._redis = redis_client

    def channel_name(self, session_id: str) -> str:
        return f"meho:sse:{session_id}"

    def _event_log_key(self, session_id: str) -> str:
        return f"meho:event_log:{session_id}"

    async def publish(self, session_id: str, event: dict) -> None:
        """Publish an event to the session's Redis channel and event log.

        The event is stored in a Redis list (for replay) and published to
        pub/sub (for live subscribers). The list entry gets a sequence
        number (RPUSH return value) that is included in the pub/sub
        message for deduplication during replay-to-live transitions.

        Args:
            session_id: The session UUID.
            event: Event payload dict to publish.
        """
        channel = self.channel_name(session_id)
        event_log_key = self._event_log_key(session_id)
        data = json.dumps(event)

        # Store in list first -- RPUSH returns the new length (1-based seq)
        seq = await self._redis.rpush(event_log_key, data)
        await self._redis.expire(event_log_key, EVENT_LOG_TTL_SECONDS)

        # Publish to pub/sub with _seq for subscriber dedup
        event_with_seq = {**event, "_seq": seq}
        await self._redis.publish(channel, json.dumps(event_with_seq))
        logger.debug(f"Published event to {channel}: type={event.get('type')} seq={seq}")

    async def subscribe(
        self, session_id: str
    ) -> AsyncIterator[dict]:  # NOSONAR (cognitive complexity)
        """Subscribe to a session's event stream with replay support.

        Replay protocol (handles late-connecting subscribers):
        1. Subscribe to pub/sub first (no events lost from this point)
        2. If the session is active, LRANGE the event log and yield all
           stored events (replay). Record max_replay_seq = len(events).
        3. Switch to live pub/sub. Each pub/sub message has a ``_seq``
           field; messages with ``_seq <= max_replay_seq`` are duplicates
           (already replayed) and are skipped.

        This ensures zero event loss regardless of when the subscriber
        connects relative to the agent's execution.

        Args:
            session_id: The session UUID.

        Yields:
            Event dicts parsed from JSON, or ``{"type": "keepalive"}``
            on timeout.
        """
        channel = self.channel_name(session_id)
        event_log_key = self._event_log_key(session_id)

        # Step 1: Subscribe to pub/sub first (captures all events from now)
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(channel)
        logger.debug(f"Subscribed to channel {channel}")

        # Step 2: Replay stored events if the session is still active
        max_replay_seq = 0
        if await self.is_active(session_id):
            stored_events = await self._redis.lrange(event_log_key, 0, -1)
            max_replay_seq = len(stored_events)
            if max_replay_seq > 0:
                logger.info(f"Replaying {max_replay_seq} stored events for {session_id}")
            for raw in stored_events:
                if isinstance(raw, (str, bytes)):
                    yield json.loads(raw)

        # Step 3: Live events from pub/sub (dedup against replayed events)
        try:
            while True:
                try:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=1.0,
                    )
                    if message is not None:
                        raw_data = message.get("data")
                        if isinstance(raw_data, (str, bytes)):
                            event = json.loads(raw_data)
                            seq = event.pop("_seq", 0)
                            if seq <= max_replay_seq:
                                continue  # Already replayed
                            yield event
                        else:
                            logger.debug(
                                f"Skipping non-data message on {channel}: {type(raw_data)}"
                            )
                    else:
                        yield {"type": "keepalive"}
                except GeneratorExit:
                    break
                except Exception:
                    logger.exception(f"Error processing message on channel {channel}")
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()  # type: ignore[attr-defined]  # redis-py >=5.x exposes aclose at runtime
            logger.debug(f"Unsubscribed and closed pubsub for {channel}")

    async def set_active(self, session_id: str, ttl_seconds: int = 300) -> None:
        """Mark a session as actively running.

        Sets a Redis key with a TTL so the active status auto-expires
        if the agent crashes without clearing it.

        Args:
            session_id: The session UUID.
            ttl_seconds: Time-to-live in seconds (default 5 minutes).
        """
        key = f"meho:active:{session_id}"
        await self._redis.set(key, "1", ex=ttl_seconds)
        logger.debug(f"Set active flag for {session_id} (TTL={ttl_seconds}s)")

    async def is_active(self, session_id: str) -> bool:
        """Check if a session is currently active.

        Args:
            session_id: The session UUID.

        Returns:
            True if the session has an active flag in Redis.
        """
        key = f"meho:active:{session_id}"
        return bool(await self._redis.exists(key))

    async def clear_active(self, session_id: str) -> None:
        """Clear the active flag for a session.

        Called when the agent finishes processing to indicate
        the session is no longer actively running.

        Args:
            session_id: The session UUID.
        """
        key = f"meho:active:{session_id}"
        await self._redis.delete(key)
        logger.debug(f"Cleared active flag for {session_id}")
