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

* :func:`should_expose_to_agent` -- may an **agent** read this tenant's
  traces (F5, #3216)? The operator override on F5 makes traces agent-readable
  through the narrow-waist result-handle idiom, but only through this
  per-tenant gate, and **independent of the operator plane's full access**.
  Resolution, fail-open to ``False`` (doubt reduces agent exposure):

  1. **Global kill switch** -- ``settings.flight_recorder_enabled``. ``False``
     = no capture and no agent exposure anywhere.
  2. **Per-tenant explicit override** --
     ``tenant.flight_recorder_agent_readable`` when set: ``True`` force on,
     ``False`` force off (operator plane unaffected).
  3. **Inherit the capture default** -- when the override is ``NULL``, follow
     ``tenant.flight_recorder_enabled`` (the F1 lab-on posture): a lab-class
     tenant with capture ON exposes traces to agents by default.

  This governs only the **agent** surface; the operator read plane keeps full
  access regardless. The redaction-uncertainty degrade (a withheld trace) is
  enforced separately at mint time
  (:func:`meho_backplane.flight_recorder.agent_read.materialize_agent_trace_handle`),
  not here -- this resolver is the per-tenant policy, that is the per-trace
  degrade.

Cache discipline mirrors :mod:`meho_backplane.broadcast.announce_gate`: a 60s
per-key TTL cache keeps the policy off the DB on the per-dispatch path; only a
miss awaits a one-row SELECT. One combined tenant cache serves all three
questions (all read the same ``tenant`` row).
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
    "should_expose_to_agent",
]

_log = structlog.get_logger(__name__)

#: Per-key TTL for both caches, mirroring the announce-gate resolver.
_CACHE_TTL_SECONDS: Final[float] = 60.0

#: Per-tenant policy cache. Value = ((enabled, retention_days_override,
#: agent_readable_override), monotonic_expires_at). A hit is a pure dict
#: lookup; a miss awaits the one-row SELECT that reads all three policy fields
#: at once (capture default F1, retention override F4, agent-read override F5).
_TENANT_CACHE: dict[UUID, tuple[tuple[bool, int | None, bool | None], float]] = {}

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


async def _resolve_tenant_policy(tenant_id: UUID) -> tuple[bool, int | None, bool | None]:
    """Return ``(capture_enabled, retention_days_override, agent_readable_override)``.

    Cache-aware; a miss reads all three policy columns in one SELECT. An
    unknown tenant resolves to ``(False, None, None)`` (capture OFF, default
    retention, agent-read inherits = OFF) and is cached so a nonexistent id
    does not re-query every dispatch. DB errors propagate to the fail-open
    handlers in the public callers.
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
                    Tenant.flight_recorder_agent_readable,
                ).where(Tenant.id == tenant_id)
            )
        ).one_or_none()
    policy: tuple[bool, int | None, bool | None] = (
        (False, None, None) if row is None else (bool(row[0]), row[1], row[2])
    )
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
        enabled, _, _ = await _resolve_tenant_policy(tenant_id)
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
        _, override, _ = await _resolve_tenant_policy(tenant_id)
    except Exception:
        _log.warning("flight_recorder_retention_resolution_failed", tenant_id=str(tenant_id))
        return default
    return override if override is not None else default


async def should_expose_to_agent(*, tenant_id: UUID) -> bool:
    """Whether an **agent** may read this tenant's flight-recorder traces (F5).

    The operator override on F5 makes traces agent-readable through the
    narrow-waist result-handle idiom, gated per tenant and **independent of
    the operator plane's full access**. Precedence: global kill switch >
    per-tenant explicit override > inherit the capture default (the F1 lab-on
    posture). Fail-open to ``False`` (no agent exposure) on any error -- a
    resolution failure must never fail a read, and the safe default on doubt
    is *less* agent exposure, not more.

    This is the per-tenant *policy* gate only. The per-trace
    redaction-uncertainty degrade (a doubtful trace withheld from the agent
    handle entirely) is enforced separately at mint time in
    :func:`meho_backplane.flight_recorder.agent_read.materialize_agent_trace_handle`.
    """
    if not get_settings().flight_recorder_enabled:
        # Global kill switch -- no capture and no agent exposure anywhere.
        return False
    try:
        enabled, _, agent_readable = await _resolve_tenant_policy(tenant_id)
    except Exception:
        _log.warning("flight_recorder_agent_read_resolution_failed", tenant_id=str(tenant_id))
        return False
    if agent_readable is not None:
        # Explicit per-tenant override, both directions -- and independent of
        # the operator plane, which keeps full access regardless of this flag.
        return agent_readable
    # Inherit the capture default: "follows the F1 default (lab-on)".
    return enabled
