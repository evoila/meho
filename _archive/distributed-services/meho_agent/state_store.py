"""
Redis-backed Agent State Persistence

Provides persistent storage for AgentSessionState across requests,
enabling true conversation continuity with automatic TTL cleanup.
"""
import json
import logging
from typing import Optional
from datetime import timedelta

import redis.asyncio as redis

from meho_agent.session_state import AgentSessionState

logger = logging.getLogger(__name__)


class RedisStateStore:
    """
    Redis-backed state storage with automatic TTL cleanup.
    
    Features:
    - Fast read/write (< 5ms)
    - Automatic expiration after TTL
    - Graceful degradation on failures
    - Session-scoped storage
    
    Usage:
        store = RedisStateStore(redis_client)
        
        # Save state
        await store.save_state(session_id, state)
        
        # Load state
        state = await store.load_state(session_id)
    """
    
    def __init__(
        self,
        redis_client: redis.Redis,
        ttl: timedelta = timedelta(hours=24),
        key_prefix: str = "meho:state"
    ):
        """
        Initialize Redis state store.
        
        Args:
            redis_client: Async Redis client
            ttl: How long to keep state (default 24 hours)
            key_prefix: Redis key prefix for namespacing
        """
        self.redis = redis_client
        self.ttl = ttl
        self.key_prefix = key_prefix
    
    def _make_key(self, session_id: str) -> str:
        """Generate Redis key for session"""
        return f"{self.key_prefix}:{session_id}"
    
    async def save_state(
        self,
        session_id: str,
        state: AgentSessionState,
        ttl: Optional[timedelta] = None
    ) -> bool:
        """
        Save agent state to Redis with TTL.
        
        Args:
            session_id: Unique session identifier
            state: AgentSessionState to persist
            ttl: Optional TTL override (uses default if None)
        
        Returns:
            True if saved successfully, False otherwise
        """
        try:
            key = self._make_key(session_id)
            data = state.to_dict()
            json_data = json.dumps(data)
            
            ttl_seconds = int((ttl or self.ttl).total_seconds())
            
            await self.redis.setex(key, ttl_seconds, json_data)
            
            logger.info(
                f"💾 Saved state for session {session_id[:8]}... "
                f"(size: {len(json_data)} bytes, TTL: {ttl_seconds}s)"
            )
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to save state for {session_id}: {e}")
            return False
    
    async def load_state(self, session_id: str) -> Optional[AgentSessionState]:
        """
        Load agent state from Redis.
        
        Args:
            session_id: Unique session identifier
        
        Returns:
            AgentSessionState if found and valid, None otherwise
        """
        try:
            key = self._make_key(session_id)
            data = await self.redis.get(key)
            
            if not data:
                logger.debug(f"📭 No state found for session {session_id[:8]}...")
                return None
            
            # Parse JSON and deserialize
            json_data = json.loads(data)
            state = AgentSessionState.from_dict(json_data)
            
            logger.info(
                f"📬 Loaded state for session {session_id[:8]}... "
                f"(size: {len(data)} bytes)"
            )
            return state
            
        except json.JSONDecodeError as e:
            logger.error(f"❌ Invalid JSON in state for {session_id}: {e}")
            # Delete corrupted state
            await self.delete_state(session_id)
            return None
            
        except Exception as e:
            logger.error(f"❌ Failed to load state for {session_id}: {e}")
            return None
    
    async def delete_state(self, session_id: str) -> bool:
        """
        Explicitly delete session state.
        
        Args:
            session_id: Unique session identifier
        
        Returns:
            True if deleted, False otherwise
        """
        try:
            key = self._make_key(session_id)
            deleted = await self.redis.delete(key)
            
            if deleted:
                logger.info(f"🗑️ Deleted state for session {session_id[:8]}...")
            
            return bool(deleted)
            
        except Exception as e:
            logger.error(f"❌ Failed to delete state for {session_id}: {e}")
            return False
    
    async def exists(self, session_id: str) -> bool:
        """
        Check if state exists for session.
        
        Args:
            session_id: Unique session identifier
        
        Returns:
            True if state exists, False otherwise
        """
        try:
            key = self._make_key(session_id)
            return bool(await self.redis.exists(key))
        except Exception as e:
            logger.error(f"❌ Failed to check state existence for {session_id}: {e}")
            return False
    
    async def get_ttl(self, session_id: str) -> Optional[int]:
        """
        Get remaining TTL for session state in seconds.
        
        Args:
            session_id: Unique session identifier
        
        Returns:
            TTL in seconds, None if key doesn't exist or error
        """
        try:
            key = self._make_key(session_id)
            ttl = await self.redis.ttl(key)
            
            if ttl < 0:
                # -1 means no expiration, -2 means key doesn't exist
                return None
            
            return ttl
            
        except Exception as e:
            logger.error(f"❌ Failed to get TTL for {session_id}: {e}")
            return None
    
    async def extend_ttl(
        self,
        session_id: str,
        additional_seconds: int = 3600
    ) -> bool:
        """
        Extend TTL for existing state.
        
        Useful for long-running conversations.
        
        Args:
            session_id: Unique session identifier
            additional_seconds: How many seconds to add (default 1 hour)
        
        Returns:
            True if extended, False otherwise
        """
        try:
            key = self._make_key(session_id)
            current_ttl = await self.redis.ttl(key)
            
            if current_ttl < 0:
                logger.warning(f"⚠️ Cannot extend TTL for {session_id} (doesn't exist)")
                return False
            
            new_ttl = current_ttl + additional_seconds
            await self.redis.expire(key, new_ttl)
            
            logger.info(f"⏰ Extended TTL for {session_id[:8]}... to {new_ttl}s")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to extend TTL for {session_id}: {e}")
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
            logger.error(f"❌ Redis ping failed: {e}")
            return False


async def get_redis_client(redis_url: str) -> redis.Redis:
    """
    Create and return async Redis client.
    
    Args:
        redis_url: Redis connection URL (e.g., redis://localhost:6379/0)
    
    Returns:
        Configured async Redis client
    """
    return redis.from_url(
        redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )

