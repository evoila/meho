# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Evaluation-loop watchdog for the sensor check-runner (#2763).

The one outage the checks plane structurally cannot alert on is its own:
when :func:`~meho_backplane.checks.runner._runner_loop` goes quiet, no
evaluations run, no transitions fire, and the notifier stays silent — a
37-minute fleet-wide stall (v0.26.0, recurring on v0.27.0) surfaced only
because an operator happened to read a dashboard. This module gives the
loop a liveness signal of its own: the runner stamps every completed
tick, an independent watchdog task detects "no tick completed for
``SENSOR_RUNNER_STALL_AFTER_TICKS`` x the tick interval" and emits a
structured log plus a ``checks.scheduler_stalled`` broadcast event, and
the health surface exposes the staleness for an external prober — the
Prometheus ``Watchdog``/dead-man's-switch division: MEHO surfaces the
signal, the external prober consumes it.

Why an in-process stamp, not a persisted fleet-level one
========================================================

Every replica runs :func:`~meho_backplane.checks.runner.run_one_sensor_tick`;
the per-tick advisory lock decides who claims the due batch. A tick that
finds the lock held still *completes* — so on every healthy replica the
stamp refreshes each cadence, and it goes stale exactly when the observed
failure mode (the loop coroutine going quiet in one process) occurs. A
persisted fleet-level stamp would cost a DB write per tick and would
*mask* a single wedged replica behind its healthy siblings; the
per-process stamp is the signal that matches the failure. The health
facet is served by the same process whose loop it reports on, so each
replica answers for itself.

Why a separate task
===================

The runner loop cannot watch itself — it is the thing that dies. The
watchdog is a sibling asyncio task (same lifespan gate,
``SENSOR_RUNNER_ENABLED``) that only reads the stamp and compares
clocks. It shares the runner's fate on whole-event-loop starvation, but
that failure mode is what the *external* prober against the health facet
catches (its symptom: the facet goes stale or unreachable).

Re-emission policy: once per continuous stall
=============================================

A stall emits exactly one ``checks_scheduler_stalled`` log + one
``checks.scheduler_stalled`` event per affected tenant, however long the
stall lasts. The watchdog keeps returning ``True`` while stalled but
does not re-emit; the next completed tick emits the paired
``checks_scheduler_recovered`` carrying the stall duration and re-arms
the detector. One stall, one stalled/recovered pair — an external
consumer never has to dedupe.

Recovery is emitted from the tick path, not the watchdog
========================================================

:func:`note_tick_completed` (called by ``run_one_sensor_tick`` on every
completed tick) emits the recovery the instant the loop proves itself
alive — zero detection latency and no dependency on the watchdog task
still running. The stall duration is measured from the last completed
tick before the stall (the reference the detector tripped on), matching
the "gap between batches" number an operator reconstructs from the
audit ledger.

Tenant fan-out
==============

Broadcast feeds are per-tenant (``meho:feed:{tenant_id}``); a stall is
platform-wide. The stalled event fans out to every tenant with at least
one active Sensor — each of them is blind while the loop is quiet — and
the recovered event goes to exactly the tenants the stalled event went
to (the set is captured at detection), so the pair always matches even
when sensors are created or deleted mid-stall. The structured log lines
are process-level and emitted regardless of tenant fan-out.

Failure posture
===============

Fail-open, the ``checks_transition_broadcast_failed`` posture (#2720):
state updates commit first, then emission; a failing tenant query or
publish warn-logs ``checks_watchdog_emit_failed`` and never reaches the
runner's tick path or kills the watchdog loop. The health facet derives
staleness live from the stamp on every read — it keeps working even if
the watchdog task itself has died.

Deliberately **no** ``/ready`` probe: flipping readiness on a stalled
evaluation loop would pull the whole API pod out of the load balancer
over a monitoring-plane outage. The health facet is the queryable
surface; reacting to it belongs to the external prober.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import structlog
from sqlalchemy import select

from meho_backplane.checks.broadcast import (
    SCHEDULER_RECOVERED_OP_ID,
    SCHEDULER_STALLED_OP_ID,
    publish_scheduler_liveness_event,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Sensor, SensorStatus
from meho_backplane.settings import get_settings

__all__ = [
    "SensorRunnerLiveness",
    "evaluate_stall_watchdog",
    "note_tick_completed",
    "reset_watchdog_state",
    "sensor_runner_liveness",
    "start_checks_watchdog",
    "stop_checks_watchdog",
]

#: When the last completed runner tick finished, per process. Stamped by
#: :func:`note_tick_completed` on every completed tick (including
#: lock-not-acquired no-op ticks — see the module docstring).
_LAST_TICK_COMPLETED_AT: datetime | None = None

#: When the last tick that actually **held the advisory lock** finished,
#: per process (#3010). A lock-miss tick proves the loop coroutine is
#: alive but not that this process is evaluating anything — the observed
#: failure mode was a lock stranded on an idle pooled connection: every
#: tick completed, ``seconds_since_last_tick`` stayed green, and zero
#: sensors were claimed for hours. This stamp feeds the
#: ``seconds_since_last_claim`` facet so that class is visible. On a
#: multi-replica deploy only the lock-winning replica's claim stamp
#: advances — which is why ``stalled`` deliberately stays keyed on the
#: tick stamp, not this one.
_LAST_CLAIM_AT: datetime | None = None

#: Watchdog start instant — the staleness reference until the first tick
#: completes, so a runner that never manages a single tick still trips
#: the detector instead of hiding behind a ``None`` stamp.
_BASELINE: datetime | None = None

#: The staleness reference the detector tripped on, while a stall is
#: flagged; ``None`` when the loop is healthy. Doubles as the
#: once-per-stall latch and the recovery event's duration origin.
_STALLED_SINCE: datetime | None = None

#: Tenants the stalled event was addressed to, captured at detection so
#: the recovered event goes to exactly the same feeds.
_STALL_TENANTS: tuple[uuid.UUID, ...] = ()


def _log() -> structlog.typing.FilteringBoundLogger:
    """Resolve the structlog logger per call (not a module-level proxy).

    Same discipline as :mod:`meho_backplane.checks.runner`: a module-level
    proxy caches its bound methods on first use under the production
    ``cache_logger_on_first_use=True`` config, orphaning them from a later
    :func:`structlog.testing.capture_logs`.
    """
    return cast("structlog.typing.FilteringBoundLogger", structlog.get_logger(__name__))


@dataclass(frozen=True, slots=True)
class SensorRunnerLiveness:
    """Point-in-time liveness view of this process's sensor-runner loop.

    ``seconds_since_last_tick`` is ``None`` only when neither a completed
    tick nor a watchdog start has happened in this process (the runner is
    enabled but not started — early startup, or a test path driving ticks
    directly). ``stalled`` is derived live from the stamp against the
    threshold on every read, deliberately **not** from the watchdog's
    emission latch, so a dead watchdog task cannot blind the health facet.

    ``seconds_since_last_claim`` (#3010) measures from the last tick that
    actually **held** the advisory lock (falling back to the watchdog
    start, like the tick reference). It diverges from
    ``seconds_since_last_tick`` exactly when the loop is alive but never
    winning the lock — the stranded-lock starvation the tick facet is
    structurally blind to. It is per-process and informational: on a
    multi-replica deploy the non-leader replicas' claim staleness grows
    legitimately, so ``stalled`` stays keyed on the tick stamp and an
    external prober alerts on claim staleness fleet-wide (min across
    replicas), not per pod.
    """

    seconds_since_last_tick: float | None
    seconds_since_last_claim: float | None
    stalled: bool
    stall_threshold_seconds: float


def reset_watchdog_state() -> None:
    """Drop all per-process watchdog state (test seam).

    Called from :func:`meho_backplane.checks.runner.reset_sensor_runner_state`
    so the runner test fixtures reset both modules in one place.
    """
    global _LAST_TICK_COMPLETED_AT, _LAST_CLAIM_AT, _BASELINE, _STALLED_SINCE, _STALL_TENANTS
    _LAST_TICK_COMPLETED_AT = None
    _LAST_CLAIM_AT = None
    _BASELINE = None
    _STALLED_SINCE = None
    _STALL_TENANTS = ()


def _stall_threshold_seconds() -> float:
    """Quiet time that counts as a stall: ``stall_after_ticks x tick interval``.

    Both factors are deployment settings (``SENSOR_RUNNER_STALL_AFTER_TICKS``,
    default 6, and ``SENSOR_RUNNER_TICK_INTERVAL_SECONDS``, default 10 —
    60 s by default), re-read per call so a settings-cache reset takes
    effect without a restart. The threshold scales with the tick grid,
    matching the reporter's "N x cadence" framing.
    """
    settings = get_settings()
    return float(
        settings.sensor_runner_stall_after_ticks * settings.sensor_runner_tick_interval_seconds
    )


def _staleness_reference() -> datetime | None:
    """The instant staleness is measured from: last completed tick, else start."""
    if _LAST_TICK_COMPLETED_AT is not None:
        return _LAST_TICK_COMPLETED_AT
    return _BASELINE


def sensor_runner_liveness(now: datetime | None = None) -> SensorRunnerLiveness:
    """Compute the loop's current liveness for the health surface.

    Pure read — no emission, no state change, no I/O — so the health
    route can call it on every request. *now* is injectable for
    deterministic tests and defaults to the real clock.
    """
    check_at = now if now is not None else datetime.now(UTC)
    threshold = _stall_threshold_seconds()
    reference = _staleness_reference()
    claim_reference = _LAST_CLAIM_AT if _LAST_CLAIM_AT is not None else _BASELINE
    claim_seconds = (
        (check_at - claim_reference).total_seconds() if claim_reference is not None else None
    )
    if reference is None:
        return SensorRunnerLiveness(
            seconds_since_last_tick=None,
            seconds_since_last_claim=claim_seconds,
            stalled=False,
            stall_threshold_seconds=threshold,
        )
    quiet_seconds = (check_at - reference).total_seconds()
    return SensorRunnerLiveness(
        seconds_since_last_tick=quiet_seconds,
        seconds_since_last_claim=claim_seconds,
        stalled=quiet_seconds > threshold,
        stall_threshold_seconds=threshold,
    )


async def _active_sensor_tenant_ids() -> list[uuid.UUID]:
    """Distinct tenants holding at least one active Sensor (own session)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = await session.scalars(
            select(Sensor.tenant_id).where(Sensor.status == SensorStatus.ACTIVE.value).distinct()
        )
        return list(rows)


async def note_tick_completed(now: datetime | None = None, *, lock_acquired: bool = True) -> None:
    """Stamp a completed runner tick; emit recovery if a stall was flagged.

    Called by :func:`~meho_backplane.checks.runner.run_one_sensor_tick` on
    every completed tick. *lock_acquired* (#3010) says whether the tick
    actually held the advisory lock: only those ticks stamp the claim
    facet, so a loop that completes every tick but never wins the lock (a
    stranded lock on an idle pooled connection, or genuine multi-replica
    contention) shows a growing ``seconds_since_last_claim`` instead of
    reading fully healthy. The tick stamp itself is unconditional — a
    lock-miss tick still proves the loop coroutine alive, which is the
    stall class the tick facet exists for. **Never raises**: the stamp and
    the stall-latch clear commit before any emission, and the emission
    block is guarded, so a broadcast or logging failure cannot fail the
    tick that proved the loop alive. *now* is injectable for
    deterministic tests.
    """
    global _LAST_TICK_COMPLETED_AT, _LAST_CLAIM_AT, _STALLED_SINCE, _STALL_TENANTS
    stamp = now if now is not None else datetime.now(UTC)
    stalled_since = _STALLED_SINCE
    notified = _STALL_TENANTS
    _LAST_TICK_COMPLETED_AT = stamp
    if lock_acquired:
        _LAST_CLAIM_AT = stamp
    if stalled_since is None:
        return
    _STALLED_SINCE = None
    _STALL_TENANTS = ()
    stalled_for = (stamp - stalled_since).total_seconds()
    try:
        _log().warning(
            "checks_scheduler_recovered",
            stalled_for_seconds=stalled_for,
        )
        for tenant_id in notified:
            await publish_scheduler_liveness_event(
                tenant_id=tenant_id,
                op_id=SCHEDULER_RECOVERED_OP_ID,
                detail={"stalled_for_seconds": stalled_for},
            )
    except Exception:
        _log().warning(
            "checks_watchdog_emit_failed",
            op_id=SCHEDULER_RECOVERED_OP_ID,
            exc_info=True,
        )


async def evaluate_stall_watchdog(now: datetime | None = None) -> bool:
    """One watchdog check. Returns ``True`` while the loop reads as stalled.

    Detection edge (quiet time first exceeds the threshold): set the
    once-per-stall latch, emit the ``checks_scheduler_stalled`` log, and
    fan the ``checks.scheduler_stalled`` event out to every tenant with
    an active Sensor. Subsequent checks during the same stall return
    ``True`` without re-emitting. The latch is set *before* the emission
    block so a failing tenant query or publish (warn-logged, fail-open)
    still arms the recovery pair. *now* is injectable for deterministic
    tests.
    """
    global _STALLED_SINCE, _STALL_TENANTS
    check_at = now if now is not None else datetime.now(UTC)
    reference = _staleness_reference()
    if reference is None:
        return False
    threshold = _stall_threshold_seconds()
    quiet_seconds = (check_at - reference).total_seconds()
    if quiet_seconds <= threshold:
        return False
    if _STALLED_SINCE is not None:
        return True
    _STALLED_SINCE = reference
    tick_interval = get_settings().sensor_runner_tick_interval_seconds
    _log().error(
        "checks_scheduler_stalled",
        seconds_since_last_tick=quiet_seconds,
        stall_threshold_seconds=threshold,
        tick_interval_seconds=tick_interval,
    )
    try:
        tenants = tuple(await _active_sensor_tenant_ids())
        _STALL_TENANTS = tenants
        for tenant_id in tenants:
            await publish_scheduler_liveness_event(
                tenant_id=tenant_id,
                op_id=SCHEDULER_STALLED_OP_ID,
                detail={
                    "seconds_since_last_tick": quiet_seconds,
                    "stall_threshold_seconds": threshold,
                    "tick_interval_seconds": tick_interval,
                },
            )
    except Exception:
        _log().warning(
            "checks_watchdog_emit_failed",
            op_id=SCHEDULER_STALLED_OP_ID,
            exc_info=True,
        )
    return True


async def _watchdog_loop() -> None:
    """The forever loop: sleep one runner cadence, check the stamp, repeat.

    Rides the runner's own tick interval (re-read per iteration, like the
    runner loop) rather than a cadence of its own — the check is one
    in-memory clock comparison, and a finer grid would not tighten the
    detection bound below ``threshold + one interval``. Per-check ``try``
    / ``except`` so a transient failure is logged and the loop continues;
    ``CancelledError`` propagates so lifespan shutdown stops the task
    cleanly (memory-expiry / gateway-deadman mould).
    """
    _log().info(
        "checks_watchdog_started",
        interval_seconds=get_settings().sensor_runner_tick_interval_seconds,
        stall_threshold_seconds=_stall_threshold_seconds(),
    )
    while True:
        await asyncio.sleep(get_settings().sensor_runner_tick_interval_seconds)
        try:
            await evaluate_stall_watchdog()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log().warning("checks_watchdog_check_failed", exc_info=True)


def start_checks_watchdog() -> asyncio.Task[None]:
    """Start the watchdog loop and return its task handle.

    Registered in :func:`meho_backplane.main.lifespan` behind the same
    ``SENSOR_RUNNER_ENABLED`` gate as the runner it watches — a watchdog
    with its own kill switch is how the stall class stays invisible.
    Sets the staleness baseline to *now* so the pre-first-tick window is
    graced but a loop that never completes a tick still trips. Returning
    the task keeps a strong reference alive (the "Task was destroyed but
    it is pending!" class the lifecycle tests bar).
    """
    global _BASELINE
    _BASELINE = datetime.now(UTC)
    return asyncio.create_task(_watchdog_loop(), name="checks-watchdog")


async def stop_checks_watchdog(task: asyncio.Task[None]) -> None:
    """Cancel the watchdog loop and await its unwind.

    Swallows the expected :class:`asyncio.CancelledError`; anything else
    raised during unwind propagates so a broken shutdown is visible.
    Mould: :func:`meho_backplane.gateway.deadman.stop_gateway_deadman_sweeper`.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
