# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Async Valkey client + lifespan management.

The backplane reaches Valkey (Redis-protocol-compatible) through
redis-py's asyncio API (:mod:`redis.asyncio`). One client per process;
the connection pool is hung off the client object so subsequent
:func:`get_broadcast_client` callers share connections.

Mirrors the SQLAlchemy ``db/engine`` lifecycle (see
:mod:`meho_backplane.db.engine`):

* :func:`get_broadcast_client` returns the cached client, building it
  on first call from ``BROADCAST_REDIS_URL`` in :class:`Settings`. The
  client itself is **lazy** about TCP — :func:`redis.asyncio.from_url`
  parses the URL and constructs the connection pool, but the first
  socket isn't opened until the first command runs. Re-instantiation
  in the same process is therefore cheap; the singleton avoids
  creating parallel pools per request.
* :func:`dispose_broadcast_client` is awaited from the FastAPI
  lifespan shutdown phase. ``aclose`` is the redis-py asyncio idiom —
  the synchronous ``close`` cannot reach connections that were spawned
  on a different event loop, mirroring the same warning the SQLAlchemy
  2.x async docs make for ``AsyncEngine.dispose``.
* :func:`reset_broadcast_client_for_testing` clears the cache without
  calling ``aclose``. Tests use it after monkey-patching
  ``BROADCAST_REDIS_URL`` to force the next
  :func:`get_broadcast_client` call to read the new value.

References
----------
* https://redis.readthedocs.io/en/stable/examples/asyncio_examples.html
* Valkey wire-protocol compatibility: https://valkey.io/topics/streams-intro/
"""

from __future__ import annotations

import redis.asyncio as redis

from meho_backplane.settings import get_settings

__all__ = [
    "dispose_broadcast_client",
    "get_broadcast_client",
    "reset_broadcast_client_for_testing",
]


_CLIENT: redis.Redis | None = None


def get_broadcast_client() -> redis.Redis:
    """Return the process-wide async Valkey client, creating on first call.

    Subsequent callers in the same process share the connection pool.
    ``socket_timeout`` and ``socket_connect_timeout`` are pinned so a
    hung Valkey fails-fast — the readiness probe (T1's only consumer)
    must not block the ``/ready`` poll indefinitely. The timeouts are
    intentionally tight; T3 (publish-on-write) will revisit them for
    the per-request hot path where 5 s is a request-blocking eternity.

    ``decode_responses=True`` keeps the client surface ``str``-typed
    rather than ``bytes``-typed — T2's :class:`BroadcastEvent` JSON
    serialisation expects strings, and the cost of decoding on the
    server-bound side is negligible for the small payloads broadcast
    events carry.
    """
    global _CLIENT
    if _CLIENT is None:
        settings = get_settings()
        _CLIENT = redis.from_url(
            settings.broadcast_redis_url,
            decode_responses=True,
            socket_timeout=5.0,
            socket_connect_timeout=3.0,
        )
    return _CLIENT


async def dispose_broadcast_client() -> None:
    """Tear down the cached client and release its connection pool.

    Called from the FastAPI ``lifespan`` shutdown phase so the
    connection pool releases its sockets cleanly when the worker
    exits. The redis-py asyncio docs are explicit that ``aclose`` must
    be ``await``\\ ed in async contexts — calling the synchronous
    ``close`` leaves connections reachable only from a different event
    loop, which the GC cannot reliably close. Idempotent: calling
    twice (or before any :func:`get_broadcast_client` call) is a
    silent no-op.
    """
    global _CLIENT
    if _CLIENT is not None:
        await _CLIENT.aclose()
    _CLIENT = None


def reset_broadcast_client_for_testing() -> None:
    """Clear the cached client. Test-only.

    Production code never calls this — :func:`dispose_broadcast_client`
    is the correct shutdown path. The cache reset is needed by tests
    that swap ``BROADCAST_REDIS_URL`` between cases; without it the
    second case would silently reuse the first case's pool against the
    first case's URL.
    """
    global _CLIENT
    _CLIENT = None
