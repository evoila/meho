# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Rate limiting utilities for MEHO API endpoints.

Uses slowapi with Redis backend for distributed rate limiting.
Per-user rate limits are applied based on user_id from auth context.

Part of TASK-186: Deep Observability & Introspection System.
"""

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from meho_app.core.config import get_config
from meho_app.core.otel import get_logger

logger = get_logger(__name__)


def get_rate_limit_key(request: Request) -> str:
    """
    Extract rate limiting key from request.

    Uses user_id from request state if authenticated,
    otherwise falls back to IP address.

    Args:
        request: FastAPI request object.

    Returns:
        Rate limit key string (user_id or IP).
    """
    # Check for authenticated user in request state
    # User is set by get_current_user dependency
    user = getattr(request.state, "user", None)
    if user is not None:
        user_id = getattr(user, "user_id", None)
        if user_id:
            return f"user:{user_id}"

    # Fall back to IP address
    return f"ip:{get_remote_address(request)}"


def create_limiter() -> Limiter:
    """
    Create and configure the rate limiter.

    Uses Redis backend for distributed rate limiting across
    multiple workers/instances.

    Returns:
        Configured Limiter instance.
    """
    config = get_config()

    # Use Redis for distributed rate limiting
    storage_uri = config.redis_url

    return Limiter(
        key_func=get_rate_limit_key,
        storage_uri=storage_uri,
        strategy="fixed-window",
        # Swallow errors to prevent Redis failures from crashing the app
        # This enables graceful degradation - rate limiting is disabled if Redis fails
        swallow_errors=True,
    )


# Create singleton limiter instance
_limiter: Limiter | None = None


def get_limiter() -> Limiter:
    """
    Get the global limiter instance.

    Creates the limiter on first access (lazy initialization).

    Returns:
        Global Limiter instance.
    """
    global _limiter
    if _limiter is None:
        _limiter = create_limiter()
    return _limiter


def reset_limiter() -> None:
    """
    Reset the global limiter instance (for testing).
    """
    global _limiter
    _limiter = None
