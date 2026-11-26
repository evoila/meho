# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Limits for the open runner enrollment route.

Two limits:

* **Requests per source address.** :func:`enforce_enroll_request_limit` counts
  each request to ``POST /api/v1/runner-principals/enroll`` by the address the
  server sees, on the shared Valkey, so every replica and worker sees the same
  numbers. Behind a trusted proxy (``FORWARDED_ALLOW_IPS``) that is the
  client address; behind a proxy that is not trusted it is the proxy address,
  which all callers share. A request with no address counts under ``unknown``.
  Over the limit the route answers 429. The cap is the setting
  ``RUNNER_ENROLL_RATE_PER_MINUTE``; ``0`` turns the limit off and no Valkey
  call is made. When Valkey cannot be reached, the error goes up and the route
  answers 503, as the event intake route does.
* **Refusal rows per runner and reason.** :func:`claim_refusal_row` lets one
  audit row per runner and reason through each minute. Its state is the
  database row :class:`~meho_backplane.db.models.RunnerEnrollRefusal`: when
  the newest audit row was written, and how many refusals got no row since
  then. The caller writes the audit row in the same transaction, so the
  count and the audit row are saved together or not at all. On PostgreSQL an
  advisory lock per runner and reason makes the next refusal wait for this
  commit.

The request counters use ``INCR`` and ``EXPIRE`` in one transaction, as the
event intake counters do (``events/ingest/rate_limit.py``).
"""

from __future__ import annotations

import hashlib
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.broadcast.client import get_broadcast_client
from meho_backplane.db.models import RunnerEnrollRefusal
from meho_backplane.settings import get_settings

__all__ = [
    "ENROLL_LIMIT_UNAVAILABLE_DETAIL",
    "ENROLL_RATE_LIMIT_DETAIL",
    "ENROLL_WINDOW_SECONDS",
    "EnrollRateLimitError",
    "claim_refusal_row",
    "enforce_enroll_request_limit",
]

#: Fixed window width: one minute, as in the ``*_per_minute`` settings.
ENROLL_WINDOW_SECONDS: Final[int] = 60

#: The fixed text of the 429 answer.
ENROLL_RATE_LIMIT_DETAIL: Final[str] = "enrollment_rate_limited"

#: The fixed text of the 503 answer when the counter cannot be reached.
ENROLL_LIMIT_UNAVAILABLE_DETAIL: Final[str] = "enrollment_limit_unavailable"

_WINDOW: Final[timedelta] = timedelta(seconds=ENROLL_WINDOW_SECONDS)


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
    """Count one request from *source*; raise when it is over the limit.

    A Valkey failure goes up to the caller (the route answers 503).
    """
    limit = get_settings().runner_enroll_rate_per_minute
    if limit <= 0:
        return
    now = int(time.time())
    window = now // ENROLL_WINDOW_SECONDS
    count = await _count(f"meho:ratelimit:runner-enroll:{source}:{window}")
    if count > limit:
        raise EnrollRateLimitError(ENROLL_WINDOW_SECONDS - (now % ENROLL_WINDOW_SECONDS))


def _refusal_lock_key(runner_id: uuid.UUID, reason: str) -> int:
    """Map a runner and a reason to a stable, non-negative 63-bit lock key."""
    digest = hashlib.blake2b(
        b"meho:runner-refusal\0" + runner_id.bytes + reason.encode(), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big") & 0x7FFF_FFFF_FFFF_FFFF


def _aware(value: datetime) -> datetime:
    # SQLite gives the stored time back without its zone; it was stored as UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def claim_refusal_row(session: AsyncSession, runner_id: uuid.UUID, reason: str) -> int | None:
    """Decide whether this refusal gets its own audit row.

    Returns ``None`` when the newest row of this runner and reason is less
    than a minute old: the refusal only adds one to the count. Otherwise
    returns the count of refusals left out since that row (``0`` for the
    first row) and sets it back to zero: the caller adds the audit row.

    Nothing is saved here. The caller commits the change together with its
    audit row, so a failed or repeated commit counts each refusal once: in
    the count or in its own row. A row written less than a minute "later" by a
    clock that is behind also counts as the same minute.
    """
    conn = await session.connection()
    if conn.dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:k)"), {"k": _refusal_lock_key(runner_id, reason)}
        )
    now = datetime.fromtimestamp(time.time(), UTC)
    state = await session.get(RunnerEnrollRefusal, (runner_id, reason))
    if state is None:
        session.add(
            RunnerEnrollRefusal(runner_id=runner_id, reason=reason, last_row_at=now, left_out=0)
        )
        return 0
    if abs(now - _aware(state.last_row_at)) < _WINDOW:
        state.left_out += 1
        return None
    left_out = state.left_out
    state.left_out = 0
    state.last_row_at = now
    return left_out
