# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Redis-backed state store for the new agent architecture.

Mirrors the API of meho_app/modules/agent/state_store.py but stores
OrchestratorSessionState instead of AgentSessionState.
"""

import json
from datetime import UTC, datetime, timedelta

import redis.asyncio as redis

from meho_app.core.otel import get_logger
from meho_app.modules.agents.persistence.session_state import OrchestratorSessionState

logger = get_logger(__name__)


class AgentStateStore:
    """
    Redis-backed state storage for orchestrator sessions.

    Features:
    - Fast read/write (< 10ms)
    - Automatic TTL expiration (default 24 hours)
    - Graceful degradation on Redis failures
    - Different key prefix from legacy (no collision)
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        ttl: timedelta = timedelta(hours=24),
        key_prefix: str = "meho:agents:state",
    ) -> None:
        """
        Initialize state store.

        Args:
            redis_client: Async Redis client
            ttl: How long to keep state (default 24 hours)
            key_prefix: Redis key prefix (different from legacy!)
        """
        self.redis = redis_client
        self.ttl = ttl
        self.key_prefix = key_prefix

    def _make_key(self, session_id: str) -> str:
        """Generate Redis key for session."""
        return f"{self.key_prefix}:{session_id}"

    async def load_state(self, session_id: str) -> OrchestratorSessionState | None:
        """
        Load session state from Redis.

        Args:
            session_id: Unique session identifier

        Returns:
            OrchestratorSessionState if found, None otherwise
        """
        try:
            key = self._make_key(session_id)
            data = await self.redis.get(key)

            if not data:
                logger.debug(f"No state for session {session_id[:8]}...")
                return None

            json_data = json.loads(data)
            state = OrchestratorSessionState.from_dict(json_data)

            logger.info(
                f"Loaded orchestrator state for {session_id[:8]}... "
                f"(turn {state.turn_count}, {len(state.connectors)} connectors)"
            )
            return state

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in state for {session_id}: {e}")
            await self.delete_state(session_id)
            return None
        except Exception as e:
            logger.error(f"Failed to load state for {session_id}: {e}")
            return None

    async def save_state(
        self,
        session_id: str,
        state: OrchestratorSessionState,
        ttl: timedelta | None = None,
    ) -> bool:
        """
        Save session state to Redis.

        Args:
            session_id: Unique session identifier
            state: OrchestratorSessionState to persist
            ttl: Optional TTL override

        Returns:
            True if saved successfully
        """
        try:
            key = self._make_key(session_id)

            # Update metadata
            state.turn_count += 1
            state.last_updated = datetime.now(tz=UTC)

            data = state.to_dict()
            json_data = json.dumps(data)

            ttl_seconds = int((ttl or self.ttl).total_seconds())
            await self.redis.setex(key, ttl_seconds, json_data)

            logger.info(
                f"Saved orchestrator state for {session_id[:8]}... "
                f"(turn {state.turn_count}, {len(json_data)} bytes)"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to save state for {session_id}: {e}")
            return False

    async def delete_state(self, session_id: str) -> bool:
        """Delete session state."""
        try:
            key = self._make_key(session_id)
            deleted = await self.redis.delete(key)
            if deleted:
                logger.info(f"Deleted state for {session_id[:8]}...")
            return bool(deleted)
        except Exception as e:
            logger.error(f"Failed to delete state for {session_id}: {e}")
            return False

    async def exists(self, session_id: str) -> bool:
        """Check if state exists."""
        try:
            key = self._make_key(session_id)
            return bool(await self.redis.exists(key))
        except Exception as e:
            logger.error(f"Failed to check state for {session_id}: {e}")
            return False

    async def ping(self) -> bool:
        """
        Check if Redis is accessible.

        Returns:
            True if Redis is reachable, False otherwise
        """
        try:
            return await self.redis.ping()
        except Exception as e:
            logger.error(f"Redis ping failed: {e}")
            return False
