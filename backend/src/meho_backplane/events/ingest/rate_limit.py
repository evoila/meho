# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-source fixed-window rate limit for the inbound ingest endpoint (#2881).

The broadcast-announce limiter (``broadcast/rate_limit.py``) proved out the
fixed-window ``INCR`` + ``EXPIRE`` shape on the shared Valkey; this is the
same mechanism under a distinct key namespace,
``meho:ratelimit:ingest:{tenant}:{source}``, so the two limiters never
collide and each source's counter is isolated from every other source (and
every other tenant, structurally, via the key).

The cap is passed in by the caller (resolved from
``event_source.extras["rate_per_minute"]`` or the
:attr:`~meho_backplane.settings.Settings.events_ingest_rate_per_minute`
default) rather than read here, so this function stays a pure limiter over an
explicit limit. A cap of ``0`` disables enforcement entirely -- no Valkey
round-trip, keeping the ingest hot path a single insert when a source opts
out.

Fail-loud on a Valkey outage
============================

Any redis-py / Valkey failure propagates verbatim (the endpoint maps it to a
``503``). A webhook sender is a well-behaved retrier by nature, so failing
the delivery closed on an infrastructure blip -- rather than fail-open
admitting unbounded traffic -- is the safe posture. This mirrors the
broadcast limiter's fail-loud contract.
"""

from __future__ import annotations

import time
from typing import Final
from uuid import UUID

from meho_backplane.broadcast.client import get_broadcast_client

__all__ = [
    "INGEST_RATE_LIMIT_WINDOW_SECONDS",
    "IngestRateLimitError",
    "enforce_ingest_rate_limit",
]

#: Fixed window width. One minute, matching the ``*_per_minute`` cap unit.
INGEST_RATE_LIMIT_WINDOW_SECONDS: Final[int] = 60


class IngestRateLimitError(Exception):
    """The source is over its per-window cap. Carries a ``Retry-After`` hint."""

    def __init__(self, *, limit: int, window_seconds: int, retry_after_seconds: int) -> None:
        super().__init__(
            f"ingest rate limit exceeded: {limit} per {window_seconds}s window "
            f"for this source; retry after {retry_after_seconds}s",
        )
        self.limit = limit
        self.window_seconds = window_seconds
        self.retry_after_seconds = retry_after_seconds


def _window_key(tenant_id: UUID, source_slug: str, bucket: int) -> str:
    """Build the per-``(tenant, source, window)`` counter key.

    The bucket (``floor(now / window)``) is embedded so each window gets its
    own key: a new window starts from zero and the ``EXPIRE`` on the old key
    reclaims it. The ``ingest`` segment keeps this namespace clear of the
    broadcast limiter's ``meho:ratelimit:announce:`` keys.
    """
    return f"meho:ratelimit:ingest:{tenant_id}:{source_slug}:{bucket}"


async def enforce_ingest_rate_limit(
    tenant_id: UUID,
    source_slug: str,
    limit: int,
) -> None:
    """Reject the delivery if this source is over *limit* in the current window.

    A *limit* of ``0`` (or negative) disables enforcement with no Valkey
    round-trip. Otherwise increments the current window's per-source counter
    and raises :class:`IngestRateLimitError` once the count exceeds *limit*.

    Raises:
        IngestRateLimitError: The source has already made *limit* deliveries
            in the current window.
        Exception: Any Valkey failure propagates verbatim (fail-loud; the
            endpoint maps it to a ``503``).
    """
    if limit <= 0:
        return

    window = INGEST_RATE_LIMIT_WINDOW_SECONDS
    now = int(time.time())
    bucket = now // window
    key = _window_key(tenant_id, source_slug, bucket)

    client = get_broadcast_client()
    # INCR + EXPIRE atomically so a counter can never outlive its window: the
    # EXPIRE re-arms on every call within the window, extending the key's life
    # to at most one window past its last write -- harmless, since the next
    # window uses a different key.
    async with client.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, window)
        count, _ = await pipe.execute()

    if count > limit:
        retry_after = window - (now % window)
        raise IngestRateLimitError(
            limit=limit,
            window_seconds=window,
            retry_after_seconds=retry_after,
        )
