# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-source fixed-window rate limits for the inbound ingest endpoint.

Two counters per source and window:

* the **delivery** counter (:func:`enforce_ingest_rate_limit`) counts only
  deliveries that passed the sender check;
* the **failed-check** counter (:func:`count_failed_check`,
  :func:`failed_check_limit_reached`) counts deliveries that failed it.

So a flood of wrongly signed deliveries fills its own counter and leaves the
real sender's delivery limit alone. Both use the same per-minute cap.

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
    "count_failed_check",
    "enforce_ingest_rate_limit",
    "failed_check_limit_error",
    "failed_check_limit_reached",
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


def _failed_check_key(tenant_id: UUID, source_slug: str, bucket: int) -> str:
    """Build the per-``(tenant, source, window)`` key of failed sender checks."""
    return f"meho:ratelimit:ingest-failed:{tenant_id}:{source_slug}:{bucket}"


def _limit_error(limit: int, now: int) -> IngestRateLimitError:
    window = INGEST_RATE_LIMIT_WINDOW_SECONDS
    return IngestRateLimitError(
        limit=limit,
        window_seconds=window,
        retry_after_seconds=window - (now % window),
    )


async def _count_in_window(key: str) -> int:
    """``INCR`` + ``EXPIRE`` the window counter *key*; return the new count.

    Both run in one transaction, so a counter can never outlive its window:
    the ``EXPIRE`` re-arms on every call within the window, extending the
    key's life to at most one window past its last write -- harmless, since
    the next window uses a different key.
    """
    client = get_broadcast_client()
    async with client.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, INGEST_RATE_LIMIT_WINDOW_SECONDS)
        count, _ = await pipe.execute()
    return int(count)


async def failed_check_limit_reached(tenant_id: UUID, source_slug: str, limit: int) -> bool:
    """Return ``True`` when the source had *limit* failed sender checks this window.

    A read only; it counts nothing. A *limit* of ``0`` (or negative) disables
    the limit with no Valkey round-trip.
    """
    if limit <= 0:
        return False
    bucket = int(time.time()) // INGEST_RATE_LIMIT_WINDOW_SECONDS
    count = await get_broadcast_client().get(_failed_check_key(tenant_id, source_slug, bucket))
    return count is not None and int(count) >= limit


async def count_failed_check(tenant_id: UUID, source_slug: str, limit: int) -> None:
    """Count one failed sender check for the source in the current window.

    A *limit* of ``0`` (or negative) disables the limit with no Valkey
    round-trip.
    """
    if limit <= 0:
        return
    bucket = int(time.time()) // INGEST_RATE_LIMIT_WINDOW_SECONDS
    await _count_in_window(_failed_check_key(tenant_id, source_slug, bucket))


def failed_check_limit_error(limit: int) -> IngestRateLimitError:
    """The ``429`` error for a source over its failed-check limit."""
    return _limit_error(limit, int(time.time()))


async def enforce_ingest_rate_limit(
    tenant_id: UUID,
    source_slug: str,
    limit: int,
) -> None:
    """Reject the delivery if this source is over *limit* in the current window.

    Called only for a delivery that passed the sender check. A *limit* of
    ``0`` (or negative) disables enforcement with no Valkey round-trip.
    Otherwise increments the current window's per-source counter and raises
    :class:`IngestRateLimitError` once the count exceeds *limit*.

    Raises:
        IngestRateLimitError: The source has already made *limit* deliveries
            in the current window.
        Exception: Any Valkey failure propagates verbatim (fail-loud; the
            endpoint maps it to a ``503``).
    """
    if limit <= 0:
        return

    now = int(time.time())
    bucket = now // INGEST_RATE_LIMIT_WINDOW_SECONDS
    count = await _count_in_window(_window_key(tenant_id, source_slug, bucket))
    if count > limit:
        raise _limit_error(limit, now)
