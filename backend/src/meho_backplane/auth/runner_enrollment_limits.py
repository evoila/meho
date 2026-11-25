# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Limits for the open runner enrollment route.

Two fixed one-minute windows, counted on the shared Valkey so every replica
and worker sees the same numbers:

* **Requests per source address.** :func:`enforce_enroll_request_limit` counts
  each request to ``POST /api/v1/runner-principals/enroll`` by the address the
  server sees (behind a trusted proxy that is the client address). Over the
  limit the route answers 429. The cap is the setting
  ``RUNNER_ENROLL_RATE_PER_MINUTE``; ``0`` turns the limit off.
* **Refusal rows per runner and reason.** :func:`take_refusal_slot` lets one
  audit row per runner and reason through each minute. The row after a busy
  minute carries the number of refusals the minute before left out.

The counters are the same kind as the event intake counters
(``events/ingest/rate_limit.py``): ``INCR`` and ``EXPIRE`` in one
transaction. When Valkey cannot be reached, the request limit lets the request
through, so a runner can still enroll, and the refusal limit lets the row
through, so the audit stays complete.
"""

from __future__ import annotations

import time
import uuid
from typing import Final

import structlog

from meho_backplane.broadcast.client import get_broadcast_client
from meho_backplane.settings import get_settings

__all__ = [
    "ENROLL_RATE_LIMIT_DETAIL",
    "ENROLL_WINDOW_SECONDS",
    "EnrollRateLimitError",
    "enforce_enroll_request_limit",
    "take_refusal_slot",
]

#: Fixed window width: one minute, as in the ``*_per_minute`` settings.
ENROLL_WINDOW_SECONDS: Final[int] = 60

#: The fixed text of the 429 answer.
ENROLL_RATE_LIMIT_DETAIL: Final[str] = "enrollment_rate_limited"

_log = structlog.get_logger(__name__)


class EnrollRateLimitError(Exception):
    """The source address sent more requests than the limit in this minute."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(ENROLL_RATE_LIMIT_DETAIL)
        self.retry_after_seconds = retry_after_seconds


async def _count(key: str) -> int:
    """``INCR`` and ``EXPIRE`` the counter *key* in one transaction."""
    client = get_broadcast_client()
    async with client.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, 2 * ENROLL_WINDOW_SECONDS)
        count, _ = await pipe.execute()
    return int(count)


async def enforce_enroll_request_limit(source: str) -> None:
    """Count one request from *source*; raise when it is over the limit."""
    limit = get_settings().runner_enroll_rate_per_minute
    if limit <= 0:
        return
    now = int(time.time())
    window = now // ENROLL_WINDOW_SECONDS
    try:
        count = await _count(f"meho:ratelimit:runner-enroll:{source}:{window}")
    except Exception:
        _log.warning("runner_enroll_limit_unavailable", exc_info=True)
        return
    if count > limit:
        raise EnrollRateLimitError(ENROLL_WINDOW_SECONDS - (now % ENROLL_WINDOW_SECONDS))


async def take_refusal_slot(runner_id: uuid.UUID, reason: str) -> int | None:
    """Ask for the one refusal row of this runner and reason in this minute.

    Returns ``None`` when the minute already has its row: write nothing.
    Otherwise returns how many refusals of the minute before were left out
    (``0`` when none), for the row to carry.
    """
    window = int(time.time()) // ENROLL_WINDOW_SECONDS
    key = f"meho:ratelimit:runner-enroll-refusal:{runner_id.hex}:{reason}"
    try:
        if await _count(f"{key}:{window}") > 1:
            return None
        previous = await get_broadcast_client().get(f"{key}:{window - 1}")
    except Exception:
        _log.warning("runner_enroll_limit_unavailable", exc_info=True)
        return 0
    return max(int(previous or 0) - 1, 0)
