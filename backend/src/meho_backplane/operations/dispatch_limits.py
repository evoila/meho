# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-principal / per-tenant dispatch rate limit + concurrent-op cap (#3500).

MEHO's only pre-existing rate limits are the event-ingest webhook
(``events/ingest/rate_limit.py``) and the agent broadcast-announce
(``broadcast/rate_limit.py``); ``identity_budget`` caps only MEHO's own
internal agent-run LLM spend. Nothing bounded a client's
``call_operation`` / ``search_operations`` request volume, so one
visitor -- or a runaway agent loop -- on a shared instance could exhaust
DB / connector pools and, via triggered internal agent runs, the
server-side LLM budget, degrading every tenant. This module is the
server-side backstop the finding (meho-internal#321) asked for.

Two controls, both enforced on the shared dispatch path so CLI, MCP and
the REST dispatch route are covered by one seam (they all funnel through
:func:`meho_backplane.operations.dispatcher.dispatch`):

* **Rate limit** -- a per-``(tenant, principal)`` fixed-window request
  cap. A burst above the per-minute cap is rejected with a
  ``retry_after`` hint. This reuses the ``INCR`` + ``EXPIRE`` shape the
  two existing limiters already proved on the shared Valkey; a
  token-bucket refinement was considered and rejected for v1 -- the
  fixed-window counter is atomic on a single ``INCR`` (no Lua / no
  read-modify-write race), matches the codebase's two other limiters,
  and satisfies the "a burst is throttled" acceptance criterion.

* **Concurrent-op cap** -- a per-``(tenant, principal)`` count of
  in-flight top-level dispatches. Acquired before an op executes,
  released when it finishes. A safety ``EXPIRE`` bounds how long a
  slot leaked by a crashed worker survives before it self-heals.

Per-tenant configuration
========================

Every limit is a global default plus a per-tenant override, expressed
purely in settings -- no migration, mirroring the
``agent_runs_disabled_tenants`` precedent. The override string is a CSV
of ``<tenant-uuid>=<int>`` pairs; a tenant absent from the override map
falls back to the global default. **A limit of ``0`` (the default for
every knob) disables that control entirely** -- no Valkey round-trip --
so existing tenants are unaffected until an operator opts in. The lab
enables tight caps for the untrusted-visitor tenant only.

Fail-loud on a Valkey outage
============================

Any redis-py / Valkey failure propagates verbatim, consistent with the
two existing limiters. Making the check fail-open would let a principal
bypass the cap during a Valkey wobble -- defeating an abuse control --
so the safe posture is to fail the dispatch closed (the dispatcher's
surrounding ``try`` converts it into a structured ``connector_error``).
"""

from __future__ import annotations

import time
from typing import Any, Final
from uuid import UUID

import structlog
from prometheus_client import Counter

from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast.client import get_broadcast_client
from meho_backplane.settings import Settings, get_settings

__all__ = [
    "DISPATCH_CONCURRENCY_LIMITED_TOTAL",
    "DISPATCH_CONCURRENCY_RETRY_AFTER_SECONDS",
    "DISPATCH_RATE_LIMITED_TOTAL",
    "DISPATCH_RATE_LIMIT_WINDOW_SECONDS",
    "acquire_dispatch_slot",
    "check_dispatch_rate_limit",
    "rate_limited_read_envelope",
    "release_dispatch_slot",
    "resolve_dispatch_concurrency_cap",
    "resolve_dispatch_rate_limit",
    "resolve_int_override",
]

_log = structlog.get_logger(__name__)

#: Fixed window width. One minute, matching the ``*_per_minute`` cap unit
#: the two existing limiters use.
DISPATCH_RATE_LIMIT_WINDOW_SECONDS: Final[int] = 60

#: Retry-after hint (seconds) returned when the concurrency cap is hit.
#: A concurrent-op rejection has no window to roll over -- the caller
#: should retry once an in-flight op of its own finishes, which is
#: typically sub-second, so a small fixed hint is the honest signal.
DISPATCH_CONCURRENCY_RETRY_AFTER_SECONDS: Final[int] = 1

#: Dispatches rejected by the per-principal rate limit. Unlabelled
#: (coarse-cardinality posture, same as the broadcast limiter's
#: counter): a sustained nonzero rate is the operational signal; the
#: per-tenant / per-principal attribution lives on the ``rate_limited``
#: audit rows the dispatcher writes, not in a high-cardinality label.
DISPATCH_RATE_LIMITED_TOTAL: Counter = Counter(
    "dispatch_rate_limited_total",
    "Dispatches rejected by the per-principal rate limit (#3500).",
)

#: Dispatches rejected by the per-principal concurrent-op cap.
DISPATCH_CONCURRENCY_LIMITED_TOTAL: Counter = Counter(
    "dispatch_concurrency_limited_total",
    "Dispatches rejected by the per-principal concurrent-op cap (#3500).",
)


def resolve_int_override(overrides_csv: str, tenant_id: UUID, default: int) -> int:
    """Resolve a per-tenant integer override, falling back to *default*.

    *overrides_csv* is a comma-separated list of ``<tenant-uuid>=<int>``
    pairs (case-insensitive UUID, whitespace ignored), mirroring the
    ``agent_runs_disabled_tenants`` CSV shape but carrying a value per
    tenant. The first entry whose UUID matches *tenant_id* wins; a
    malformed entry (bad UUID, non-integer value, missing ``=``) is
    skipped with a warning so one typo cannot silently drop every
    override. When no entry matches, the global *default* applies.
    """
    if not overrides_csv:
        return default
    for raw in overrides_csv.split(","):
        entry = raw.strip()
        if not entry:
            continue
        key, sep, value = entry.partition("=")
        if not sep:
            _log.warning("dispatch_limit_override_malformed", entry=entry, reason="no '='")
            continue
        try:
            entry_tenant = UUID(key.strip())
        except ValueError:
            _log.warning("dispatch_limit_override_malformed", entry=entry, reason="bad uuid")
            continue
        try:
            entry_value = int(value.strip())
        except ValueError:
            _log.warning("dispatch_limit_override_malformed", entry=entry, reason="bad int")
            continue
        if entry_tenant == tenant_id:
            return entry_value
    return default


def resolve_dispatch_rate_limit(settings: Settings, tenant_id: UUID) -> int:
    """Per-minute dispatch rate limit for *tenant_id* (0 = disabled)."""
    return resolve_int_override(
        settings.dispatch_rate_limit_per_minute_overrides,
        tenant_id,
        settings.dispatch_rate_limit_per_minute,
    )


def resolve_dispatch_concurrency_cap(settings: Settings, tenant_id: UUID) -> int:
    """Per-principal concurrent-op cap for *tenant_id* (0 = disabled)."""
    return resolve_int_override(
        settings.dispatch_max_concurrent_ops_overrides,
        tenant_id,
        settings.dispatch_max_concurrent_ops,
    )


def _rate_window_key(tenant_id: UUID, principal_sub: str, bucket: int) -> str:
    """Per-``(tenant, principal, window)`` rate-counter key.

    The bucket (``floor(now / window)``) is embedded so each window gets
    its own key: a new window starts from zero and the ``EXPIRE`` on the
    old key reclaims it. The ``dispatch`` segment keeps this namespace
    clear of the ``announce`` / ``ingest`` limiter keys.
    """
    return f"meho:ratelimit:dispatch:{tenant_id}:{principal_sub}:{bucket}"


def _concurrency_key(tenant_id: UUID, principal_sub: str) -> str:
    """Per-``(tenant, principal)`` in-flight-dispatch counter key."""
    return f"meho:concurrency:dispatch:{tenant_id}:{principal_sub}"


async def rate_limited_read_envelope(operator: Operator) -> dict[str, Any] | None:
    """Rate-limit a read-path meta-tool; return an envelope when over limit.

    The read-path meta-tools (``search_operations`` / ``preview_operation``
    / ``result_query``) return a plain ``dict`` envelope rather than an
    :class:`OperationResult`, and do no vendor traffic. They share the same
    per-principal dispatch rate budget as ``call_operation`` (one bucket per
    principal bounds a caller's total request volume across the working
    surface). Returns ``None`` when the caller is under its limit (or the
    limit is disabled); otherwise a structured ``rate_limited`` envelope
    carrying ``retry_after_seconds`` for the caller to back off on.
    """
    settings = get_settings()
    limit = resolve_dispatch_rate_limit(settings, operator.tenant_id)
    retry_after = await check_dispatch_rate_limit(operator.tenant_id, operator.sub, limit)
    if retry_after is None:
        return None
    return {
        "status": "rate_limited",
        "error_code": "rate_limited",
        "kind": "rate",
        "limit": limit,
        "retry_after_seconds": retry_after,
        "error": (
            f"rate_limited: {limit} dispatches per minute for this principal; "
            f"retry after {retry_after}s"
        ),
    }


async def check_dispatch_rate_limit(
    tenant_id: UUID,
    principal_sub: str,
    limit: int,
) -> int | None:
    """Increment the window counter; return a retry-after when over *limit*.

    A *limit* of ``0`` (or negative) disables the check with no Valkey
    round-trip. Otherwise increments the current window's per-principal
    counter; when the count exceeds *limit* returns the whole seconds
    until the window rolls over (the caller surfaces it as the
    ``retry_after_seconds`` on a rate-limited result), else ``None``.

    The counter is per ``(tenant_id, principal_sub, window)``, so one
    principal hitting its cap never affects another principal in the
    same tenant, and cross-tenant isolation is structural (the key is
    derived from the JWT-bound ``tenant_id``).

    Raises:
        Exception: Any redis-py / Valkey failure propagates verbatim
            (fail-loud, consistent with the two existing limiters).
    """
    if limit <= 0:
        return None

    window = DISPATCH_RATE_LIMIT_WINDOW_SECONDS
    now = int(time.time())
    bucket = now // window
    key = _rate_window_key(tenant_id, principal_sub, bucket)

    client = get_broadcast_client()
    # INCR + EXPIRE atomically so a counter can never outlive its window:
    # the EXPIRE re-arms on every call within the window, extending the
    # key's life to at most one window past its last write -- harmless,
    # since the next window uses a different key.
    async with client.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, window)
        count, _ = await pipe.execute()

    if count > limit:
        DISPATCH_RATE_LIMITED_TOTAL.inc()
        return window - (now % window)
    return None


async def acquire_dispatch_slot(
    tenant_id: UUID,
    principal_sub: str,
    cap: int,
    ttl_seconds: int,
) -> tuple[int | None, str | None]:
    """Try to take one in-flight-dispatch slot for this principal.

    A *cap* of ``0`` (or negative) disables the check with no Valkey
    round-trip and returns ``(None, None)`` -- nothing to release.

    Otherwise increments the per-principal in-flight counter (arming a
    safety *ttl_seconds* ``EXPIRE`` so a slot leaked by a crashed worker
    self-heals). When the resulting count exceeds *cap* the increment is
    rolled back immediately and ``(retry_after, None)`` is returned --
    the caller rejects the dispatch without holding a slot. On success
    returns ``(None, key)``; the caller **must** pass *key* to
    :func:`release_dispatch_slot` in a ``finally`` once the op finishes.

    Raises:
        Exception: Any redis-py / Valkey failure propagates verbatim.
    """
    if cap <= 0:
        return None, None

    key = _concurrency_key(tenant_id, principal_sub)
    client = get_broadcast_client()
    async with client.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, ttl_seconds)
        count, _ = await pipe.execute()

    if count > cap:
        # Roll back our own increment so a rejected attempt does not
        # permanently consume a slot. Use the same self-healing DECR the
        # release path uses (deletes the key at zero) so a burst of
        # rejections cannot drive the counter negative.
        await _decr_and_reap(key)
        DISPATCH_CONCURRENCY_LIMITED_TOTAL.inc()
        return DISPATCH_CONCURRENCY_RETRY_AFTER_SECONDS, None
    return None, key


async def release_dispatch_slot(key: str | None) -> None:
    """Release a slot taken by :func:`acquire_dispatch_slot`.

    ``None`` (the disabled / not-acquired case) is a no-op. Any Valkey
    failure propagates verbatim; the dispatcher runs this in its
    ``finally`` where an exception would already be in flight.
    """
    if key is None:
        return
    await _decr_and_reap(key)


async def _decr_and_reap(key: str) -> None:
    """DECR the counter, deleting it once it reaches zero.

    Deleting at zero keeps the counter non-negative and self-healing:
    a release that races an expired window's reset cannot drive the key
    below zero (which would silently widen the effective cap).
    """
    client = get_broadcast_client()
    remaining = await client.decr(key)
    if remaining <= 0:
        await client.delete(key)
