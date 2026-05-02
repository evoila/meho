# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Centralized Redis client factory with connection resilience.

Provides a process-wide singleton Redis client with:
- Auto-retry on connection errors (up to 3 retries)
- Health checks every 30s (detects dead connections)
- TCP keepalive (OS detects dead sockets faster)
- Retry on timeout

This ensures all Redis operations recover transparently
when Redis restarts or connections go stale.
"""

import redis.asyncio as redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from meho_app.core.otel import get_logger

logger = get_logger(__name__)

_redis_client: redis.Redis | None = None


def get_redis_client(redis_url: str) -> redis.Redis:
    """
    Get or create the shared async Redis client.

    Returns a singleton client with built-in resilience:
    - Retries failed operations up to 3 times
    - Pings idle connections every 30s
    - Uses TCP keepalive for faster dead-socket detection

    Args:
        redis_url: Redis connection URL (e.g., redis://localhost:6379/0)

    Returns:
        Configured async Redis client (singleton)
    """
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            socket_keepalive=True,
            health_check_interval=30,
            retry_on_error=[
                redis.ConnectionError,
                redis.TimeoutError,
                ConnectionError,
                TimeoutError,
            ],
            retry_on_timeout=True,
            retry=Retry(NoBackoff(), retries=3),
        )
        logger.info(
            "Redis client created with resilience params (retry=3, health_check=30s, keepalive=True)"
        )
    return _redis_client


async def close_redis_client() -> None:
    """
    Close the shared Redis client and release all connections.

    Call this during application shutdown to cleanly close the pool.
    """
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()  # type: ignore[attr-defined]  # redis-py >=5.x exposes aclose at runtime
        _redis_client = None
        logger.info("Redis client closed")


def reset_redis_client() -> None:
    """
    Reset the singleton without closing (for testing only).

    This allows tests to inject mocked Redis clients
    by clearing the cached singleton.
    """
    global _redis_client
    _redis_client = None
