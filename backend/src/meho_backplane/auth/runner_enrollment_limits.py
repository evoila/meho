# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Limits for the open runner enrollment route.

Counters on the shared Valkey, so every replica and worker sees the same
numbers:

* **Requests per source address.** :func:`enforce_enroll_request_limit` counts
  each request to ``POST /api/v1/runner-principals/enroll`` by the address the
  server sees. Behind a trusted proxy (``FORWARDED_ALLOW_IPS``) that is the
  client address; behind a proxy that is not trusted it is the proxy address,
  which all callers share. A request with no address counts under ``unknown``.
  Over the limit the route answers 429. The cap is the setting
  ``RUNNER_ENROLL_RATE_PER_MINUTE``; ``0`` turns the limit off and no Valkey
  call is made. When Valkey cannot be reached, the error goes up and the route
  answers 503, as the event intake route does.
* **Refusal rows per runner and reason.** :func:`take_refusal_slot` lets one
  audit row per runner and reason through each minute. The row's slot is one
  key per minute, set with ``SET NX``: the first refusal of the minute gets it,
  every other refusal is left out and adds one to a counter of that runner and
  reason. That counter only goes up and has no end time. Each row stores the
  counter value it read (:func:`read_left_out_total`) and says how many
  refusals were left out since the row before. The database row is the only
  record of what was counted, so a row that is not written takes nothing away.
  When the row cannot be written, :func:`give_back_refusal_slot` deletes the
  exact slot key that was taken. When Valkey cannot be reached here, the row is
  written anyway.

The request counters use ``INCR`` and ``EXPIRE`` in one transaction, as the
event intake counters do (``events/ingest/rate_limit.py``).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Final

import structlog

from meho_backplane.broadcast.client import get_broadcast_client
from meho_backplane.settings import get_settings

__all__ = [
    "ENROLL_LIMIT_UNAVAILABLE_DETAIL",
    "ENROLL_RATE_LIMIT_DETAIL",
    "ENROLL_WINDOW_SECONDS",
    "EnrollRateLimitError",
    "RefusalSlot",
    "enforce_enroll_request_limit",
    "give_back_refusal_slot",
    "read_left_out_total",
    "take_refusal_slot",
]

#: Fixed window width: one minute, as in the ``*_per_minute`` settings.
ENROLL_WINDOW_SECONDS: Final[int] = 60

#: The fixed text of the 429 answer.
ENROLL_RATE_LIMIT_DETAIL: Final[str] = "enrollment_rate_limited"

#: The fixed text of the 503 answer when the counter cannot be reached.
ENROLL_LIMIT_UNAVAILABLE_DETAIL: Final[str] = "enrollment_limit_unavailable"

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


@dataclass(frozen=True)
class RefusalSlot:
    """The refusal row slot one request took.

    ``key`` is the slot key of the minute the slot was taken in, or ``None``
    when Valkey could not be reached.
    """

    key: str | None


def _refusal_base(runner_id: uuid.UUID, reason: str) -> str:
    return f"meho:ratelimit:runner-enroll-refusal:{runner_id.hex}:{reason}"


async def take_refusal_slot(runner_id: uuid.UUID, reason: str) -> RefusalSlot | None:
    """Ask for the one refusal row of this runner and reason in this minute.

    Returns ``None`` when the minute already has its row: write nothing, the
    refusal adds one to the count. Otherwise returns the slot. When Valkey
    cannot be reached the row is written.
    """
    base = _refusal_base(runner_id, reason)
    slot_key = f"{base}:{int(time.time()) // ENROLL_WINDOW_SECONDS}"
    try:
        client = get_broadcast_client()
        taken = await client.set(slot_key, "1", nx=True, ex=2 * ENROLL_WINDOW_SECONDS)
    except Exception:
        _log.warning("runner_enroll_limit_unavailable", exc_info=True)
        return RefusalSlot(key=None)
    if taken:
        return RefusalSlot(key=slot_key)
    try:
        await client.incr(f"{base}:left-out")
    except Exception:
        _log.warning("runner_enroll_limit_unavailable", exc_info=True)
        return RefusalSlot(key=None)
    return None


async def read_left_out_total(runner_id: uuid.UUID, reason: str) -> int | None:
    """All refusals of this runner and reason left out so far.

    ``None`` when Valkey cannot be reached. The value is read, never reset.
    """
    try:
        value = await get_broadcast_client().get(f"{_refusal_base(runner_id, reason)}:left-out")
    except Exception:
        _log.warning("runner_enroll_limit_unavailable", exc_info=True)
        return None
    return int(value or 0)


async def give_back_refusal_slot(slot: RefusalSlot) -> None:
    """Undo :func:`take_refusal_slot` after the row could not be written.

    Deletes the slot key that was taken (not the key of the current minute),
    so the next refusal writes the row. The count stays as it is.
    """
    if slot.key is None:
        return
    try:
        await get_broadcast_client().delete(slot.key)
    except Exception:
        _log.warning("runner_enroll_limit_unavailable", exc_info=True)
