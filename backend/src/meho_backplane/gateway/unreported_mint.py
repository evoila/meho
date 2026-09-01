# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Central un-reported-mint security alarm sweeper (#2901, #3193).

Mechanism 4's centre-side half (design ``docs/research/2901-satellite-write-path.md``
§3, decision ``docs/decisions/satellite-write-path.md``): a minted
``remote-write`` capability whose effect is never reported is the un-audited
mutation window (threat T4). Because the **mint** is audited synchronously at
authorization (``gateway.command.mint``), the centre always knows a write
capability was granted — so it can detect, within the expiry window, that its
effect never came back. This sweeper raises a **security** event on exactly that:
a ``remote-write`` command past ``expires_at`` still ``consumed_at IS NULL``.

Distinct from the liveness dead-man
-----------------------------------

This is **not** the #2501 dead-man switch (:mod:`meho_backplane.gateway.deadman`),
which flags a runner's *liveness* (``last_seen_at`` behind the cutoff). This
sweeper flags a *security* condition (a specific minted write went unreported)
on a distinct audit ``path`` (:data:`GATEWAY_UNREPORTED_MINT_PATH` vs the
dead-man's ``gateway.runner.stale``). A runner can be perfectly live and still
execute-but-not-report a single write — the exact gap the liveness monitor
cannot see.

Design moulds (copied from :mod:`meho_backplane.gateway.deadman`)
----------------------------------------------------------------

* Interval-tick loop + start/stop pair the FastAPI lifespan owns (in-process
  interval-tick sweeper, **not** the DB-bound scheduler trigger loop).
* Fixed non-blocking advisory lock elects one replica per tick (no-op on
  SQLite) on a dedicated pinned connection (the tick commits mid-lock, #3010).
* Central-clock only: the expiry cutoff is ``datetime.now(UTC)`` against the
  central-stamped ``expires_at``; no runner-reported timestamp participates.
* Idempotent conditional flip: ``UPDATE ... WHERE unreported_alarm_at IS NULL``
  whose ``rowcount`` gates the audit write, so "exactly one security audit row
  per unreported mint" holds across ticks / replicas.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update

from meho_backplane.db.advisory import advisory_lock
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GatewayCommand
from meho_backplane.memory.audit import (
    INTERNAL_METHOD,
    write_internal_audit_row,
)
from meho_backplane.metrics import note_loop_tick
from meho_backplane.runner.satellite_tier import REMOTE_WRITE_SAFETY_LEVELS
from meho_backplane.settings import get_settings

__all__ = [
    "GATEWAY_UNREPORTED_MINT_PATH",
    "start_gateway_unreported_mint_sweeper",
    "stop_gateway_unreported_mint_sweeper",
]

_log = structlog.get_logger(__name__)

#: Canonical internal-audit ``path`` for an un-reported-mint security alarm.
#: Distinct from the dead-man switch's ``gateway.runner.stale`` (liveness) so
#: audit queries partition security alarms from liveness flips by ``path``.
GATEWAY_UNREPORTED_MINT_PATH: str = "gateway.command.unreported_mint"

#: The event-class marker on the alarm payload — a **security** event, not a
#: liveness one. G8 audit-query consumers filter on it.
_SECURITY_EVENT_CLASS: str = "security"

#: The synthetic identity attributed to the alarm's audit rows.
_SECURITY_OPERATOR_SUB: str = "system:unreported-mint-alarm"

#: The fixed advisory-lock key held during a tick. Same hashing shape the
#: dead-man sweeper / reaper use; a single scalar because this is a singleton
#: sweep, not a per-row claimer.
_UNREPORTED_MINT_ADVISORY_LOCK_KEY: int = (
    int.from_bytes(
        hashlib.blake2b(b"gateway_unreported_mint:v1", digest_size=8).digest(),
        "big",
    )
    & 0x7FFF_FFFF_FFFF_FFFF
)


def _as_utc(value: datetime) -> datetime:
    """Coerce a possibly-naive ``timestamptz`` read to UTC-aware (aiosqlite)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _run_one_tick() -> None:
    """One sweep: alarm on minted remote-writes past expiry, unreported.

    Two-phase shape (dead-man mould):

    1. Acquire the single advisory lock (skip the tick on PG if another replica
       won).
    2. Select ``remote-write`` command rows past ``expires_at`` still
       ``consumed_at IS NULL`` and not yet alarmed, flip each with a conditional
       ``UPDATE ... WHERE unreported_alarm_at IS NULL``, and collect the ones
       this tick won (``rowcount == 1``).

    Commits the flips once, then writes one internal **security** audit row per
    won flip (mutate + commit, then audit). An audit-write failure is logged
    loud but never rolls back a flip.
    """
    tick_started = time.perf_counter()
    now = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    # (tenant_id, command_id, runner_id, op_id, lapse_seconds) per won flip.
    flipped: list[tuple[uuid.UUID, uuid.UUID, str, str, float]] = []
    async with advisory_lock(
        _UNREPORTED_MINT_ADVISORY_LOCK_KEY, subsystem="gateway_unreported_mint"
    ) as locked:
        if not locked:
            _log.debug("gateway_unreported_mint_tick_skipped_lock_held")
            return
        async with sessionmaker() as session:
            candidate_stmt = (
                select(
                    GatewayCommand.id,
                    GatewayCommand.tenant_id,
                    GatewayCommand.runner_id,
                    GatewayCommand.op_id,
                    GatewayCommand.expires_at,
                )
                .where(
                    GatewayCommand.safety_level.in_(sorted(REMOTE_WRITE_SAFETY_LEVELS)),
                    GatewayCommand.consumed_at.is_(None),
                    GatewayCommand.expires_at < now,
                    GatewayCommand.unreported_alarm_at.is_(None),
                )
                .order_by(GatewayCommand.expires_at.asc())
            )
            candidates = (await session.execute(candidate_stmt)).all()
            for command_id, tenant_id, runner_id, op_id, expires_at in candidates:
                result = await session.execute(
                    update(GatewayCommand)
                    .where(
                        GatewayCommand.id == command_id,
                        GatewayCommand.unreported_alarm_at.is_(None),
                    )
                    .values(unreported_alarm_at=now)
                )
                # ``rowcount`` is only typed on the concrete ``CursorResult`` an
                # UPDATE produces at runtime; the ignore mirrors ``deadman.py``.
                won_flip: int = result.rowcount  # type: ignore[attr-defined]
                if won_flip == 1:
                    lapse_seconds = (now - _as_utc(expires_at)).total_seconds()
                    flipped.append((tenant_id, command_id, runner_id, op_id, lapse_seconds))
            await session.commit()

    if not flipped:
        _log.debug("gateway_unreported_mint_tick_clean")
        return

    duration_ms = (time.perf_counter() - tick_started) * 1000.0
    await _audit_alarms(flipped, duration_ms=duration_ms)
    _log.warning(
        "gateway_unreported_mint_tick_done",
        alarmed=len(flipped),
        duration_ms=duration_ms,
    )


async def _audit_alarms(
    flipped: list[tuple[uuid.UUID, uuid.UUID, str, str, float]],
    *,
    duration_ms: float,
) -> None:
    """Write one internal **security** audit row per won alarm, isolating failures."""
    for tenant_id, command_id, runner_id, op_id, lapse_seconds in flipped:
        try:
            await write_internal_audit_row(
                operator_sub=_SECURITY_OPERATOR_SUB,
                tenant_id=tenant_id,
                method=INTERNAL_METHOD,
                path=GATEWAY_UNREPORTED_MINT_PATH,
                status_code=200,
                duration_ms=duration_ms,
                payload={
                    "event_class": _SECURITY_EVENT_CLASS,
                    "command_id": str(command_id),
                    "runner": runner_id,
                    "op_id": op_id,
                    "lapse_seconds": lapse_seconds,
                },
            )
        except Exception:
            # An audit-write failure must not stall the tick (the flip is already
            # committed; we cannot roll it back). Surface it loud for operators.
            _log.exception(
                "gateway_unreported_mint_audit_write_failed",
                tenant_id=str(tenant_id),
                command_id=str(command_id),
                runner=runner_id,
            )


async def _sweeper_loop() -> None:
    """The forever loop: sleep one cadence, sweep, repeat (dead-man mould)."""
    interval = get_settings().gateway_unreported_mint_tick_interval_seconds
    _log.info("gateway_unreported_mint_sweeper_started", interval_seconds=interval)
    while True:
        await asyncio.sleep(get_settings().gateway_unreported_mint_tick_interval_seconds)
        try:
            await _run_one_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("gateway_unreported_mint_tick_failed", exc_info=True)
        note_loop_tick(
            "gateway_unreported_mint",
            get_settings().gateway_unreported_mint_tick_interval_seconds,
        )


def start_gateway_unreported_mint_sweeper() -> asyncio.Task[None]:
    """Start the background alarm sweeper loop and return its task handle.

    Registered in :func:`meho_backplane.main.lifespan` behind
    ``GATEWAY_UNREPORTED_MINT_ENABLED`` (default on). Returning the task keeps a
    strong reference alive so it is not garbage-collected mid-flight.
    """
    return asyncio.create_task(_sweeper_loop(), name="gateway-unreported-mint-sweeper")


async def stop_gateway_unreported_mint_sweeper(task: asyncio.Task[None]) -> None:
    """Cancel the sweeper task and await its unwind (dead-man mould)."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
