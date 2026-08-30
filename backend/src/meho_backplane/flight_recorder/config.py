# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Flight-recorder capture-enablement + retention resolver (#3212, F1/F4).

Answers two questions for the capture seam (#3214) and the persistence API,
both **fail-open** (an unreadable policy or a DB error never fails a dispatch
and never captures more than it can prove it should):

* :func:`should_capture` -- should this dispatch's vendor traffic be recorded?
  Resolution precedence, highest first (F1):

  1. **Global kill switch** -- ``settings.flight_recorder_enabled``. ``False``
     = capture nothing anywhere, overriding everything below.
  2. **Per-target override** -- ``targets.flight_recorder_capture`` tri-state:
     ``True`` force on, ``False`` force off, ``NULL`` inherit.
  3. **Per-tenant default** -- ``tenant.flight_recorder_enabled`` (OFF except
     for lab-class tenants an operator flipped ON).

  On any error the resolver returns ``False`` (capture nothing) -- the
  fail-open direction for a *capture* decision is *less* exposure, matching
  the F5 "doubt reduces exposure" posture.

* :func:`resolve_retention_days` -- how long should this tenant's trace live
  (F4)? ``tenant.flight_recorder_retention_days`` when set (14 for lab-class),
  else the global default (``settings.flight_recorder_retention_days_default``,
  7 days). On error, the global default. The persistence API stamps
  :func:`compute_expires_at` onto the header at write time so the reaper is a
  plain ``expires_at < now()`` sweep and this window math is unit-testable.

Cache discipline mirrors :mod:`meho_backplane.broadcast.announce_gate`: a 60s
per-key TTL cache keeps the policy off the DB on the per-dispatch path; only a
miss awaits a one-row SELECT. One combined tenant cache serves both questions
(both read the same ``tenant`` row).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

import structlog
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target, Tenant
from meho_backplane.settings import get_settings

__all__ = [
    "compute_expires_at",
    "reset_flight_recorder_config_cache_for_testing",
    "resolve_retention_days",
    "should_capture",
]

_log = structlog.get_logger(__name__)

#: Per-key TTL for both caches, mirroring the announce-gate resolver.
_CACHE_TTL_SECONDS: Final[float] = 60.0

#: Per-tenant policy cache. Value = ((enabled, retention_days_override),
#: monotonic_expires_at). A hit is a pure dict lookup; a miss awaits the
#: one-row SELECT that reads both policy fields at once.
_TENANT_CACHE: dict[UUID, tuple[tuple[bool, int | None], float]] = {}

#: Per-target override cache. Value = (override_tri_state, monotonic_expires_at)
#: where the tri-state is ``True`` / ``False`` / ``None`` (inherit).
_TARGET_CACHE: dict[UUID, tuple[bool | None, float]] = {}


def reset_flight_recorder_config_cache_for_testing() -> None:
    """Clear both per-key caches (test isolation only)."""
    _TENANT_CACHE.clear()
    _TARGET_CACHE.clear()


def compute_expires_at(created_at: datetime, retention_days: int) -> datetime:
    """Return the retention deadline for a trace written at *created_at* (F4).

    Pure window math -- ``created_at + retention_days``. Stamped onto
    :attr:`~meho_backplane.db.models.DispatchTrace.expires_at` at write time so
    the retention reaper is a portable ``WHERE expires_at < now()`` sweep.
    """
    return created_at + timedelta(days=retention_days)


async def _resolve_tenant_policy(tenant_id: UUID) -> tuple[bool, int | None]:
    """Return ``(capture_enabled, retention_days_override)`` for *tenant_id*.

    Cache-aware; a miss reads both policy columns in one SELECT. An unknown
    tenant resolves to ``(False, None)`` (capture OFF, default retention) and
    is cached so a nonexistent id does not re-query every dispatch. DB errors
    propagate to the fail-open handlers in the public callers.
    """
    now = time.monotonic()
    cached = _TENANT_CACHE.get(tenant_id)
    if cached is not None and cached[1] > now:
        return cached[0]
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(
                    Tenant.flight_recorder_enabled,
                    Tenant.flight_recorder_retention_days,
                ).where(Tenant.id == tenant_id)
            )
        ).one_or_none()
    policy: tuple[bool, int | None] = (False, None) if row is None else (bool(row[0]), row[1])
    _TENANT_CACHE[tenant_id] = (policy, now + _CACHE_TTL_SECONDS)
    return policy


async def _resolve_target_override(target_id: UUID) -> bool | None:
    """Return the per-target capture override tri-state for *target_id*.

    ``True`` force on / ``False`` force off / ``None`` inherit the tenant
    default. ``scalar_one_or_none`` returns ``None`` both for "no such target"
    and "target with a NULL override" -- both mean "no per-target override", so
    the tri-state collapses correctly. Cache-aware; DB errors propagate.
    """
    now = time.monotonic()
    cached = _TARGET_CACHE.get(target_id)
    if cached is not None and cached[1] > now:
        return cached[0]
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        override: bool | None = (
            await session.execute(
                select(Target.flight_recorder_capture).where(Target.id == target_id)
            )
        ).scalar_one_or_none()
    _TARGET_CACHE[target_id] = (override, now + _CACHE_TTL_SECONDS)
    return override


async def should_capture(*, tenant_id: UUID, target_id: UUID | None = None) -> bool:
    """Whether this dispatch's flight-recorder trace should be captured (F1).

    Precedence: global kill switch > per-target override > per-tenant default.
    Fail-open to ``False`` (capture nothing) on any error -- a resolution
    failure must never fail a dispatch, and the safe default on doubt is less
    exposure, not more.
    """
    if not get_settings().flight_recorder_enabled:
        # Global kill switch -- overrides every per-tenant / per-target setting.
        return False
    try:
        if target_id is not None:
            override = await _resolve_target_override(target_id)
            if override is not None:
                return override
        enabled, _ = await _resolve_tenant_policy(tenant_id)
        return enabled
    except Exception:
        _log.warning(
            "flight_recorder_capture_resolution_failed",
            tenant_id=str(tenant_id),
            target_id=None if target_id is None else str(target_id),
        )
        return False


async def resolve_retention_days(tenant_id: UUID) -> int:
    """Resolve the trace-retention window in days for *tenant_id* (F4).

    Per-tenant override when set, else the global default. Fail-open to the
    global default on any error.
    """
    default = get_settings().flight_recorder_retention_days_default
    try:
        _, override = await _resolve_tenant_policy(tenant_id)
    except Exception:
        _log.warning("flight_recorder_retention_resolution_failed", tenant_id=str(tenant_id))
        return default
    return override if override is not None else default
