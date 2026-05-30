# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Async Valkey client + lifespan management.

The backplane reaches Valkey (Redis-protocol-compatible) through
redis-py's asyncio API (:mod:`redis.asyncio`). Two clients per process,
each with its own connection pool, partitioned by read-timeout
expectations:

* :func:`get_broadcast_client` — the "fast" client. ``socket_timeout``
  pinned at :data:`_BROADCAST_FAST_TIMEOUT_SECONDS` (5 s). Used for the
  readiness probe (``PING``) and the publish hot path (``XADD``) where
  a hung Valkey must surface within the ``/ready`` poll window. A
  hung Valkey on this client raises :class:`redis.exceptions.TimeoutError`
  at 5 s.
* :func:`get_broadcast_blocking_client` — the "blocking" client.
  ``socket_timeout`` pinned at :data:`BROADCAST_BLOCKING_SOCKET_TIMEOUT_SECONDS`
  (35 s — the SSE ``XREAD BLOCK`` window plus a 5 s buffer). Used for
  long-poll readers (SSE feed, UI SSE bridge, ``meho.broadcast.watch``
  MCP tool, agent approval wait). A quiet stream returns ``None`` from
  ``XREAD`` after BLOCK expires (the natural keepalive path); only a
  *genuine* transport failure — socket dead longer than the BLOCK +
  buffer window — raises :class:`redis.exceptions.TimeoutError`.

Why two clients, not one parameterised. redis-py 7.4's ``read_response``
resolves the per-read timeout from the connection's ``socket_timeout``
when the caller passes ``timeout=None`` (which ``xread`` does — there
is no public per-call read-timeout hook on ``xread``). A single client
with a long ``socket_timeout`` would push the readiness probe's
fail-fast contract past its 5 s SLO; a single client with the short
timeout produces the spurious ``feed_error`` frame at ~5 s on every
fresh SSE connection (RDC #789 N1, Initiative #1353). The two-client
split is the simplest shape that preserves both contracts; the
publisher and readiness probe stay on the fast client, blocking
readers move to the long-timeout client.

Mirrors the SQLAlchemy ``db/engine`` lifecycle (see
:mod:`meho_backplane.db.engine`):

* Each getter returns the cached client, building it on first call from
  ``BROADCAST_REDIS_URL`` in :class:`Settings`. The clients themselves
  are **lazy** about TCP — :func:`redis.asyncio.from_url` parses the URL
  and constructs the connection pool, but the first socket isn't opened
  until the first command runs. Re-instantiation in the same process is
  therefore cheap; the singletons avoid creating parallel pools per
  request.
* Each dispose helper is awaited from the FastAPI lifespan shutdown
  phase. ``aclose`` is the redis-py asyncio idiom — the synchronous
  ``close`` cannot reach connections that were spawned on a different
  event loop, mirroring the same warning the SQLAlchemy 2.x async
  docs make for ``AsyncEngine.dispose``.
* The ``_for_testing`` resetters clear the caches without calling
  ``aclose``. Tests use them after monkey-patching
  ``BROADCAST_REDIS_URL`` to force the next getter call to read the
  new value.

References
----------
* https://redis.readthedocs.io/en/stable/examples/asyncio_examples.html
* Valkey wire-protocol compatibility: https://valkey.io/topics/streams-intro/
* redis-py 7.4 read-timeout resolution:
  ``redis/asyncio/connection.py`` ``AbstractConnection.read_response``
  — falls back to ``self.socket_timeout`` when the caller's ``timeout``
  is ``None``.
"""

from __future__ import annotations

from typing import Final

import redis.asyncio as redis
import structlog

from meho_backplane.settings import get_settings

__all__ = [
    "BROADCAST_BLOCKING_SOCKET_TIMEOUT_SECONDS",
    "dispose_broadcast_blocking_client",
    "dispose_broadcast_client",
    "get_broadcast_blocking_client",
    "get_broadcast_client",
    "reset_broadcast_blocking_client_for_testing",
    "reset_broadcast_client_for_testing",
]


_log = structlog.get_logger(__name__)

#: Fast-client read timeout. Pinned to 5 s so the readiness ``PING``
#: surfaces a hung Valkey inside the ``/ready`` poll window and the
#: fail-open publisher (``XADD``) caps its retry wait at the same value.
_BROADCAST_FAST_TIMEOUT_SECONDS: Final[float] = 5.0

#: Blocking-client read timeout. Must exceed the longest ``XREAD BLOCK``
#: window any caller of :func:`get_broadcast_blocking_client` uses, so a
#: quiet stream returns ``None`` (natural keepalive) instead of raising
#: ``redis.TimeoutError`` from inside the BLOCK call. The SSE feed
#: (:mod:`meho_backplane.api.v1.feed`) pins ``_XREAD_BLOCK_MS = 30_000``
#: and the ``meho.broadcast.watch`` MCP tool
#: (:mod:`meho_backplane.mcp.tools.broadcast`) caps ``timeout_ms`` at
#: ``_WATCH_MAX_TIMEOUT_MS = 30_000``; 30 s + 5 s buffer = 35 s. The
#: buffer absorbs event-loop scheduling jitter, redis-py's BLOCK
#: response-time slop, and a short TCP retransmit so a healthy-but-slow
#: response still arrives before the socket times out.
#:
#: Increase this value if any caller raises its BLOCK window past 30 s;
#: keep at least a 5 s buffer so genuinely-broken sockets still surface
#: as transport failures (consumers of the SSE generator distinguish
#: "BLOCK expired naturally → ``None``" from "transport failed →
#: ``redis.TimeoutError`` → ``feed_error`` frame").
BROADCAST_BLOCKING_SOCKET_TIMEOUT_SECONDS: Final[float] = 35.0

#: Connect-timeout shared by both clients — a hung TCP handshake is the
#: same operator-visible condition (broadcast pod unreachable) regardless
#: of the per-call read timeout the client uses afterwards.
_BROADCAST_CONNECT_TIMEOUT_SECONDS: Final[float] = 3.0


_CLIENT: redis.Redis | None = None
_BLOCKING_CLIENT: redis.Redis | None = None


def get_broadcast_client() -> redis.Redis:
    """Return the process-wide async Valkey "fast" client.

    Subsequent callers in the same process share the connection pool.
    ``socket_timeout`` and ``socket_connect_timeout`` are pinned so a
    hung Valkey fails-fast — the readiness probe and the fail-open
    publish hot path (``XADD``) both depend on bounded latency. Long-
    poll readers (``XREAD BLOCK``) must use
    :func:`get_broadcast_blocking_client` instead: at the fast client's
    5 s ``socket_timeout``, every BLOCK call past 5 s raises
    ``redis.TimeoutError`` from the socket layer regardless of the
    ``BLOCK`` argument — which is the underlying cause of the spurious
    ``feed_error`` frame catalogued in RDC #789 N1 (Initiative #1353).

    ``decode_responses=True`` keeps the client surface ``str``-typed
    rather than ``bytes``-typed — :class:`BroadcastEvent` JSON
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
            socket_timeout=_BROADCAST_FAST_TIMEOUT_SECONDS,
            socket_connect_timeout=_BROADCAST_CONNECT_TIMEOUT_SECONDS,
        )
    return _CLIENT


def get_broadcast_blocking_client() -> redis.Redis:
    """Return the process-wide async Valkey client for blocking-XREAD callers.

    Separate cache and separate connection pool from
    :func:`get_broadcast_client` — see this module's docstring for the
    two-client rationale. ``socket_timeout`` is pinned at
    :data:`BROADCAST_BLOCKING_SOCKET_TIMEOUT_SECONDS` so a 30 s
    ``XREAD BLOCK`` against a quiet stream returns ``None`` (natural
    keepalive path) instead of raising ``redis.TimeoutError`` from the
    socket layer at 5 s — which would otherwise produce the spurious
    ``feed_error`` frame catalogued in RDC #789 N1 (Initiative #1353).

    Callers: the SSE feed (:mod:`meho_backplane.api.v1.feed`), the UI
    SSE bridge (:mod:`meho_backplane.ui.routes.broadcast.stream`), the
    ``meho.broadcast.watch`` MCP tool
    (:mod:`meho_backplane.mcp.tools.broadcast`), and the agent approval
    wait loop (:mod:`meho_backplane.agent.approval_wait`). Non-blocking
    operations (``PING``, ``XADD``, ``XRANGE``, ``XREVRANGE``) stay on
    the fast client so a hung Valkey keeps surfacing inside the
    ``/ready`` poll window.

    Same ``decode_responses=True`` posture as the fast client so callers
    that share helpers across both clients (e.g. ``_emit_backlog_prelude``
    on the fast client, ``XREAD BLOCK`` on the blocking client) read
    identically-shaped responses.
    """
    global _BLOCKING_CLIENT
    if _BLOCKING_CLIENT is None:
        settings = get_settings()
        _BLOCKING_CLIENT = redis.from_url(
            settings.broadcast_redis_url,
            decode_responses=True,
            socket_timeout=BROADCAST_BLOCKING_SOCKET_TIMEOUT_SECONDS,
            socket_connect_timeout=_BROADCAST_CONNECT_TIMEOUT_SECONDS,
        )
    return _BLOCKING_CLIENT


async def _aclose_client_or_log(
    client: redis.Redis | None,
    *,
    log_event: str,
) -> None:
    """Close *client* idempotently; log + swallow failures.

    Extracted out of the two dispose helpers so the shutdown path stays
    DRY — both helpers clear their cache before awaiting ``aclose``,
    both swallow errors with the same structured log event, and both
    are otherwise the same six lines of code. The shared helper ensures
    the two dispose paths drift together: any future change to the
    lifespan shutdown contract (timeout, retry, drain) lands in one
    place.

    The cache is cleared by the caller **before** ``aclose`` is awaited
    so a failure in the close path doesn't leave the module pointing
    at a half-closed client; the next ``get_*`` call therefore starts
    from a clean slate rather than re-entering ``aclose`` on the same
    potentially-broken object.
    """
    if client is None:
        return
    try:
        await client.aclose()
    except Exception:
        _log.warning(log_event, exc_info=True)


async def dispose_broadcast_client() -> None:
    """Tear down the cached fast client and release its connection pool.

    Called from the FastAPI ``lifespan`` shutdown phase so the
    connection pool releases its sockets cleanly when the worker
    exits. The redis-py asyncio docs are explicit that ``aclose`` must
    be ``await``\\ ed in async contexts — calling the synchronous
    ``close`` leaves connections reachable only from a different event
    loop, which the GC cannot reliably close. Idempotent: calling
    twice (or before any :func:`get_broadcast_client` call) is a
    silent no-op even when the first ``aclose`` raises.

    Shutdown-path ``aclose`` failures are logged via structlog (the
    docstring's "silent no-op" idempotency contract trumps the raise —
    propagating here would tear down the FastAPI lifespan shutdown and
    prevent neighbouring disposers from running).
    """
    global _CLIENT
    client = _CLIENT
    _CLIENT = None
    await _aclose_client_or_log(client, log_event="broadcast_dispose_failed")


async def dispose_broadcast_blocking_client() -> None:
    """Tear down the cached blocking client and release its connection pool.

    Mirror of :func:`dispose_broadcast_client` for the long-poll client.
    The lifespan shutdown phase awaits both disposers (independent
    ``try`` / ``except`` arms in :mod:`meho_backplane.main` so a failure
    in one doesn't leak the other's pool); the per-client cache reset
    follows the same "clear before close" ordering documented in
    :func:`_aclose_client_or_log`.
    """
    global _BLOCKING_CLIENT
    client = _BLOCKING_CLIENT
    _BLOCKING_CLIENT = None
    await _aclose_client_or_log(
        client,
        log_event="broadcast_blocking_dispose_failed",
    )


def reset_broadcast_client_for_testing() -> None:
    """Clear the cached fast client. Test-only.

    Production code never calls this — :func:`dispose_broadcast_client`
    is the correct shutdown path. The cache reset is needed by tests
    that swap ``BROADCAST_REDIS_URL`` between cases; without it the
    second case would silently reuse the first case's pool against the
    first case's URL.
    """
    global _CLIENT
    _CLIENT = None


def reset_broadcast_blocking_client_for_testing() -> None:
    """Clear the cached blocking client. Test-only.

    Mirror of :func:`reset_broadcast_client_for_testing` for the
    blocking client; tests that swap ``BROADCAST_REDIS_URL`` need to
    reset both caches.
    """
    global _BLOCKING_CLIENT
    _BLOCKING_CLIENT = None
