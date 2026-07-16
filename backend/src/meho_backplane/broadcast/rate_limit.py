# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-principal fixed-window rate limit for agent announcements (G6.5-T6).

``meho.broadcast.announce`` writes to the count-trimmed per-tenant
Valkey stream (``XADD ... MAXLEN ~ 10000``, see
:mod:`~meho_backplane.broadcast.publisher`). Nothing bounded the write
rate: a single looping principal could emit >10 000 announcements in a
burst and evict the entire tenant's coordination window -- the exact
crossfire-avoidance property the broadcast discipline exists to
provide. This module adds a per-``(tenant, principal)`` cap enforced in
the announce handler *before* the publish.

Algorithm -- fixed-window counter
=================================

The canonical Redis ``INCR``-based rate limiter (the "Pattern: Rate
limiter" section of https://redis.io/docs/latest/commands/incr/): one
counter key per ``(tenant, principal, window)`` triple, incremented on
every announce, rejected once the count exceeds the limit. The current
time is quantised to a fixed window (``bucket = floor(now / window)``)
and embedded in the key, so a fresh window starts with a fresh counter
and stale windows expire on their own. ``INCR`` + ``EXPIRE`` run in one
``MULTI``/``EXEC`` pipeline so the counter can never outlive its window
even if the process dies between the two commands.

Fail-loud, consistent with the publisher
=========================================

The limiter runs on the same fast broadcast client the fail-loud
publisher uses (:func:`~meho_backplane.broadcast.client.get_broadcast_client`).
A Valkey teardown during the check propagates to the MCP dispatcher's
``-32603`` Internal Error path -- the same fate the announce would meet
one step later at the publish. Making the check fail-open would let a
principal bypass the cap during a Valkey wobble, defeating the
protection; making it fail-loud costs nothing over the publish's own
failure mode.

The limiter raises a domain error (:class:`AnnounceRateLimitError`),
not an MCP-vocabulary error: the announce handler translates it into the
wire ``-32000`` rate-limited error (mirrors the ``InvalidSinceError``
seam, which keeps :mod:`~meho_backplane.broadcast.history` free of the
MCP error vocabulary).
"""

from __future__ import annotations

import time
from typing import Final
from uuid import UUID

from prometheus_client import Counter

from meho_backplane.broadcast.client import get_broadcast_client
from meho_backplane.settings import get_settings

__all__ = [
    "ANNOUNCE_RATE_LIMIT_WINDOW_SECONDS",
    "BROADCAST_ANNOUNCE_RATE_LIMITED_TOTAL",
    "AnnounceRateLimitError",
    "enforce_announce_rate_limit",
]


#: The fixed window, in seconds, the per-minute limit counts against.
#: Named "per minute" in the settings knob, so the window is 60 s. Kept
#: as a module constant (not a setting) because the knob's unit is the
#: contract -- an operator tunes the *count*, not the window length.
ANNOUNCE_RATE_LIMIT_WINDOW_SECONDS: Final[int] = 60


#: Counter of announces rejected by the per-principal rate limit.
#: Unlabelled (same coarse-cardinality posture as
#: :data:`~meho_backplane.broadcast.publisher.BROADCAST_PUBLISH_ERRORS_TOTAL`):
#: a sustained nonzero rate is the operational signal that a principal is
#: looping or the limit is set too low; per-tenant / per-principal
#: attribution lives in the structured ``mcp_announce_rate_limited`` log
#: the handler emits, not in a high-cardinality metric label.
BROADCAST_ANNOUNCE_RATE_LIMITED_TOTAL: Counter = Counter(
    "broadcast_announce_rate_limited_total",
    "Agent announcements rejected by the per-principal rate limit (G6.5-T6).",
)


class AnnounceRateLimitError(Exception):
    """Raised when a principal exceeds the announce rate limit in a window.

    Carries the numbers the announce handler surfaces to the calling
    agent so it can back off intelligently: the ``limit`` it tripped,
    the ``window_seconds`` the limit counts against, and
    ``retry_after_seconds`` (whole seconds until the current window
    rolls over and the counter resets).
    """

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: int,
        retry_after_seconds: int,
    ) -> None:
        super().__init__(
            f"announce rate limit exceeded: {limit} per {window_seconds}s "
            f"window for this principal; retry after {retry_after_seconds}s",
        )
        self.limit = limit
        self.window_seconds = window_seconds
        self.retry_after_seconds = retry_after_seconds


def _window_key(tenant_id: UUID, principal_sub: str, bucket: int) -> str:
    """Build the per-``(tenant, principal, window)`` counter key.

    The bucket (``floor(now / window)``) is embedded so each window gets
    its own key: a new window starts from zero and the ``EXPIRE`` on the
    old key reclaims it. Distinct from the ``meho:feed:{tenant}`` stream
    key namespace so the limiter never collides with the broadcast
    stream itself.
    """
    return f"meho:ratelimit:announce:{tenant_id}:{principal_sub}:{bucket}"


async def enforce_announce_rate_limit(
    tenant_id: UUID,
    principal_sub: str,
) -> None:
    """Reject the announce if this principal is over its per-window cap.

    Reads the limit from
    :attr:`~meho_backplane.settings.Settings.broadcast_announce_rate_per_minute`.
    A limit of ``0`` disables enforcement entirely -- no Valkey
    round-trip is made, so the announce hot path stays a single ``XADD``
    when an operator opts out. Otherwise increments the current window's
    per-principal counter and raises :class:`AnnounceRateLimitError`
    once the count exceeds the limit.

    The counter is per ``(tenant_id, principal_sub, window)``, so one
    principal hitting its cap never affects another principal in the same
    tenant, and cross-tenant isolation is structural (the key is derived
    from the JWT-bound ``tenant_id``).

    Raises
    ------
    AnnounceRateLimitError
        When the principal has already made ``limit`` announces in the
        current window.
    Exception
        Any redis-py / Valkey failure propagates verbatim (fail-loud,
        consistent with the publisher this gate protects).
    """
    limit = get_settings().broadcast_announce_rate_per_minute
    if limit <= 0:
        return

    window = ANNOUNCE_RATE_LIMIT_WINDOW_SECONDS
    now = int(time.time())
    bucket = now // window
    key = _window_key(tenant_id, principal_sub, bucket)

    client = get_broadcast_client()
    # INCR + EXPIRE atomically so a counter can never outlive its window:
    # the EXPIRE re-arms on every call within the window, which only ever
    # extends the key's life to at most one window past its last write --
    # harmless, since the next window uses a different key.
    async with client.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, window)
        count, _ = await pipe.execute()

    if count > limit:
        BROADCAST_ANNOUNCE_RATE_LIMITED_TOTAL.inc()
        retry_after = window - (now % window)
        raise AnnounceRateLimitError(
            limit=limit,
            window_seconds=window,
            retry_after_seconds=retry_after,
        )
