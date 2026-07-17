# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Deterministic, no-LLM sensor check-runner (#2505).

The lifespan-owned background loop at the heart of Initiative #2416's check
layer. On each cadence (default 10 s, ``SENSOR_RUNNER_TICK_INTERVAL_SECONDS``)
it claims every due :class:`~meho_backplane.db.models.Sensor` row (#2503),
dispatches each sensor's ``safe`` read-only op through the operations
:func:`~meho_backplane.operations.dispatcher.dispatch` seam under a synthetic
per-tenant identity, feeds the payload to #2504's pure
:func:`~meho_backplane.checks.evaluate.evaluate_assertion`, and persists the
outcome via #2503's :func:`~meho_backplane.checks.repository.record_sensor_result`.
No agent, no model call, no LLM anywhere on this path.

One loop, two cadences
======================

A single interval-tick loop drives both cadence kinds. Sub-minute *interval*
sensors ride the tick grid directly; ``>=1``-minute *cron* sensors advance via
:func:`~meho_backplane.scheduler.cron.next_fire_after`. Both share the durable
claim/advance discipline the #804 scheduler proved out. **Sub-tick cadences
quantize to the tick grid**: a sensor whose ``interval_seconds`` is below
``SENSOR_RUNNER_TICK_INTERVAL_SECONDS`` fires at most once per tick, not once
per its nominal interval (the precedent #2245 documented for the scheduler's
30 s grid). Operators wanting finer granularity lower the tick interval.

Replica-safety (copied from ``scheduler/loop.py``'s belt-and-braces)
===================================================================

Each tick acquires a process-wide ``pg_try_advisory_lock`` under
:data:`_SENSOR_RUNNER_ADVISORY_LOCK_KEY` (a fixed 63-bit key distinct from the
scheduler's ``_SCHEDULER_ADVISORY_LOCK_KEY`` and the topology per-target
keyspace) so only one replica runs the tick body at a time. Due rows are
claimed with ``FOR UPDATE SKIP LOCKED`` and each claimed row's ``next_fire_at``
is advanced by a conditional ``UPDATE ... WHERE next_fire_at=:previous`` that
commits *before* dispatch. The advisory lock alone leaves the SQLite test path
uncovered; the conditional advance is what enforces single-fire there, and the
combination is the at-most-once-per-scheduled-instant property #804 already
proved. A crashed or overlapping evaluation surfaces as staleness
(-> #2506's stale-``unknown`` derivation), never a double-fire.

No lock-wedge (#1502 regression class)
======================================

Evaluations run as tracked background :class:`asyncio.Task`s, never awaited
under the advisory lock. The tick advances the claimed rows, releases the lock,
then spawns the evaluations and returns. A hung op dispatch (or a
``requires_approval`` wait, which cannot happen here -- sensors reference only
``safe`` ops) never strands the lock for the rest of the connection's life.

Overlap + concurrency bounds
============================

Evaluations are tracked in a per-process ``dict[sensor_id, Task]``. If a
sensor's previous evaluation is still running when it comes due again, the tick
skips the new dispatch and logs ``sensor_evaluation_overlap_skipped`` -- the
missed instant becomes staleness, consistent with the at-most-once contract.
Concurrent evaluations are bounded by a fixed-constant
:class:`asyncio.Semaphore` and each evaluation by an :func:`asyncio.timeout`
that maps to ``unknown``. Residual cross-replica overlap (an evaluation
outliving its cadence while the tick lock migrates replicas) is accepted, not
guarded by a DB in-flight marker: the op is ``safe``/read-only and idempotent,
the result write is last-writer-wins stamped with ``evaluated_at``, and a
marker column would strand on crash and demand a reaper -- the same
stampede-cost-below-noise-floor reasoning
:mod:`meho_backplane.memory.expiry` documents.

Dispatch identity
=================

Each dispatch runs as a synthetic per-tenant :class:`~meho_backplane.auth.operator.Operator`
with ``sub=sensor.identity_sub`` (#2503's per-row column, default
``"__sensor__"``), ``tenant_id=sensor.tenant_id``, ``raw_jwt=""``,
``TenantRole.OPERATOR`` -- the topology-refresh ``_system_operator`` mould with
the sub sourced from the row. ``principal_kind`` defaults to ``USER``, so
:func:`~meho_backplane.operations._validate.policy_gate` auto-executes the
``safe`` op (#2503's registration guard guarantees a sensor only ever
references a ``safe`` op; the agent-credential path is never touched here and
no agent run may exist on this path). A connector requiring an operator-context
Vault credential read fails closed for a synthetic operator -- such a dispatch
returns a structured error and the sensor reads ``unknown``; targets with
stored credentials (the topology-refresh model) evaluate normally.

Result vocabulary
================

The runner never synthesizes any state other than ``unknown``: a ``status ==
"ok"`` dispatch routes its payload into #2504's evaluator and persists its
emitted ``{state, value, evidence}``; any non-``ok`` dispatch status or an
evaluation timeout persists ``unknown`` with evidence carrying the failure.
Rollup, hysteresis, and ``skip`` derivation belong to #2506.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.checks.assertions import AssertionOutcome, AssertionSpec
from meho_backplane.checks.evaluate import evaluate_assertion
from meho_backplane.checks.investigate import investigate_on_transition
from meho_backplane.checks.repository import (
    advance_sensor_next_fire,
    claim_due_sensors,
    park_sensor,
    record_sensor_result,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Sensor
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.scheduler.cron import (
    InvalidCronExpressionError,
    InvalidTimezoneError,
)
from meho_backplane.settings import get_settings

__all__ = [
    "run_one_sensor_tick",
    "start_sensor_runner",
    "stop_sensor_runner",
]

#: 63-bit signed-int key for ``pg_try_advisory_lock``. A fixed literal so every
#: replica computes the same key and exactly one holds the lock at a time.
#: Deliberately distinct from the scheduler's ``_SCHEDULER_ADVISORY_LOCK_KEY``
#: (``0x4D45_484F_5343_4844`` -- "MEHOSCHD") and from the topology scheduler's
#: per-target blake2b keyspace, so the sensor runner and the agent scheduler
#: never contend on one lock. "MEHOSENS".
_SENSOR_RUNNER_ADVISORY_LOCK_KEY: int = 0x4D45_484F_5345_4E53

#: Maximum sensor rows claimed per tick. Bounds per-tick work even under a
#: catch-up burst after an outage; the next tick picks up the rest. Matches the
#: scheduler's ``_CLAIM_BATCH_LIMIT`` posture -- a dumb, fixed loop bound.
_CLAIM_BATCH_LIMIT: int = 50

#: Ceiling on concurrent in-flight evaluations. A fixed constant (not a
#: per-deployment tunable), same posture as :data:`_CLAIM_BATCH_LIMIT`: a tick
#: may claim up to the batch limit, and this bounds how many op dispatches hit
#: their targets at once so a wide sensor fleet cannot open 50 simultaneous
#: connector calls.
_MAX_CONCURRENT_EVALUATIONS: int = 16

#: Per-evaluation wall-clock ceiling (seconds). A backstop above the
#: connector's own timeouts: a dispatch that hangs past this maps the sensor to
#: ``unknown`` rather than pinning an evaluation slot forever. A fixed constant
#: for the same substrate-minimalism reason as the bounds above.
_EVAL_TIMEOUT_SECONDS: float = 30.0

#: Per-process registry of in-flight evaluation tasks, keyed by sensor id. The
#: overlap guard reads it (skip a sensor whose previous evaluation is not
#: ``done()``); :func:`stop_sensor_runner` cancels every outstanding task.
_IN_FLIGHT: dict[uuid.UUID, asyncio.Task[None]] = {}

#: Lazily-created concurrency semaphore. Built on first use in the running loop
#: rather than at import so it binds to the app's event loop (an
#: import-time-constructed ``asyncio`` primitive strands across the per-test
#: event loops pytest-asyncio creates). Reset via
#: :func:`reset_sensor_runner_state`.
_EVAL_SEMAPHORE: asyncio.Semaphore | None = None


def _log() -> structlog.typing.FilteringBoundLogger:
    """Resolve the structlog logger per call (not a module-level proxy).

    A module-level ``_log = structlog.get_logger(__name__)`` proxy caches its
    bound methods on first use under the production
    ``cache_logger_on_first_use=True`` config; a later
    :func:`structlog.testing.capture_logs` in the same worker then cannot reach
    the orphaned closure, so the overlap-skip test's log assertion flakes under
    pytest-xdist ``loadscope``. Resolving the logger on every call reads the
    live config each time.

    The :func:`~typing.cast` documents the runtime type the production
    ``make_filtering_bound_logger`` config yields; ``structlog.get_logger`` is
    typed ``Any`` in the stubs, so an un-cast return trips ``no-any-return``.
    """
    return cast("structlog.typing.FilteringBoundLogger", structlog.get_logger(__name__))


def _eval_semaphore() -> asyncio.Semaphore:
    """Get (or lazily build) the concurrency semaphore for the current loop."""
    global _EVAL_SEMAPHORE
    if _EVAL_SEMAPHORE is None:
        _EVAL_SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENT_EVALUATIONS)
    return _EVAL_SEMAPHORE


def reset_sensor_runner_state() -> None:
    """Drop all per-process runner state (test seam).

    Clears the in-flight registry and the lazily-built semaphore so a fresh
    test starts with no leftover tasks and a semaphore bound to its own event
    loop. Mirrors :func:`meho_backplane.db.engine.reset_engine_for_testing`'s
    role as an explicit reset for module-level state.
    """
    _IN_FLIGHT.clear()
    global _EVAL_SEMAPHORE
    _EVAL_SEMAPHORE = None


@dataclass(frozen=True, slots=True)
class _SensorSnapshot:
    """The immutable view an evaluation needs, detached from the tick session.

    Captured while the claimed row is still fresh in the tick's session, so the
    backgrounded evaluation (which opens its own short session) never touches an
    expired ORM object bound to a closed session.
    """

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    connector_id: str
    op_id: str
    target: dict[str, object] | None
    params: dict[str, object]
    assertion: dict[str, object]
    identity_sub: str

    @classmethod
    def from_row(cls, row: Sensor) -> _SensorSnapshot:
        return cls(
            id=row.id,
            tenant_id=row.tenant_id,
            name=row.name,
            connector_id=row.connector_id,
            op_id=row.op_id,
            target=row.target,
            params=row.params,
            assertion=row.assertion,
            identity_sub=row.identity_sub,
        )


async def _try_advisory_lock(session: AsyncSession, key: int) -> bool:
    """Acquire the process-wide PG advisory lock; ``True`` on non-PG.

    Returns ``True`` when the lock is held (or the dialect has no advisory
    locks -- the SQLite single-replica test path) and the caller should
    proceed; ``False`` when another replica holds it and this tick is skipped.
    Mirrors :func:`meho_backplane.scheduler.loop._try_advisory_lock`.
    """
    conn = await session.connection()
    if conn.dialect.name != "postgresql":
        return True
    locked = await session.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
    return bool(locked)


async def _advisory_unlock(session: AsyncSession, key: int) -> None:
    """Release the advisory lock; no-op on non-PG dialects."""
    conn = await session.connection()
    if conn.dialect.name != "postgresql":
        return
    await session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})


def _sensor_operator(snap: _SensorSnapshot) -> Operator:
    """Build the synthetic per-tenant operator a sensor dispatch runs as.

    ``raw_jwt`` is empty -- the runner forwards no bearer token; a target with
    stored credentials evaluates normally, one needing an operator-context
    Vault read fails closed to a structured error (-> ``unknown``).
    ``principal_kind`` defaults to ``USER``, so the policy gate auto-executes
    the ``safe`` op #2503's registration guard restricts sensors to.
    """
    return Operator(
        sub=snap.identity_sub,
        name=None,
        email=None,
        raw_jwt="",
        tenant_id=snap.tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


def _unknown_outcome(reason: str, **extra: object) -> AssertionOutcome:
    """An ``unknown`` outcome carrying *reason* (+ any extra evidence)."""
    evidence: dict[str, object] = {"reason": reason}
    evidence.update(extra)
    return AssertionOutcome(state="unknown", value=None, evidence=evidence)


async def _run_evaluation(snap: _SensorSnapshot) -> AssertionOutcome:
    """Dispatch the sensor's op and evaluate the assertion. Never raises.

    ``status == "ok"`` routes the payload into #2504's evaluator; any non-``ok``
    dispatch status, an evaluation timeout, or a spec/evaluator failure maps to
    ``unknown`` with the cause in the evidence.
    """
    now = datetime.now(UTC)
    operator = _sensor_operator(snap)
    try:
        async with asyncio.timeout(_EVAL_TIMEOUT_SECONDS):
            result = await dispatch(
                operator=operator,
                connector_id=snap.connector_id,
                op_id=snap.op_id,
                target=snap.target,
                params=snap.params,
            )
    except TimeoutError:
        return _unknown_outcome(
            "evaluation_timeout",
            timeout_seconds=_EVAL_TIMEOUT_SECONDS,
        )

    if result.status != "ok":
        return _unknown_outcome(
            "dispatch_not_ok",
            dispatch_status=result.status,
            dispatch_error=result.error,
        )

    try:
        spec = AssertionSpec.model_validate(snap.assertion)
        return evaluate_assertion(spec, result.result, now=now)
    except Exception as exc:
        # Never-raises contract: a corrupt persisted spec or an unexpected
        # evaluator failure is an ``unknown`` outcome, not a crashed task.
        return _unknown_outcome("assertion_evaluation_error", error=str(exc))


async def _persist_outcome(snap: _SensorSnapshot, outcome: AssertionOutcome) -> None:
    """Write *outcome* onto the sensor's latest-state projection (own session).

    After the projection commits, hand off to #2507's transition detector: it
    recomputes the rollup for the Dashboards holding this sensor, maintains the
    ``last_rollup_state`` memo, and fires a diagnose-only investigator on a
    green->non-green edge. The hook never raises (its contract), so the persist
    path is unaffected by any investigation-side failure -- and it runs on every
    result (not only state changes) because a ``for:`` hold expiring flips a
    Dashboard non-green with no sensor-state change.
    """
    evaluated_at = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await record_sensor_result(
            session,
            sensor_id=snap.id,
            state=outcome.state,
            value=outcome.value,
            evidence=outcome.evidence,
            evaluated_at=evaluated_at,
        )
        await session.commit()
    await investigate_on_transition(sensor_id=snap.id, tenant_id=snap.tenant_id)


async def _evaluate_and_record(snap: _SensorSnapshot) -> None:
    """One backgrounded evaluation: dispatch -> evaluate -> persist.

    Bounded by the concurrency semaphore (around the dispatch/evaluate step)
    and the per-evaluation timeout inside :func:`_run_evaluation`. Every failure
    mode is contained so a broken evaluation never crashes the task with an
    unretrieved exception; ``CancelledError`` propagates so shutdown can cancel
    cleanly.
    """
    try:
        async with _eval_semaphore():
            outcome = await _run_evaluation(snap)
        await _persist_outcome(snap, outcome)
        _log().info(
            "sensor_evaluated",
            sensor_id=str(snap.id),
            sensor_name=snap.name,
            state=outcome.state,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log().warning(
            "sensor_evaluation_errored",
            sensor_id=str(snap.id),
            exc_info=True,
        )


def _spawn_evaluation(snap: _SensorSnapshot) -> bool:
    """Spawn a background evaluation for *snap* unless one is still in flight.

    Returns ``True`` when a task was spawned, ``False`` on an overlap skip. The
    overlap guard is the only per-sensor serialization: a sensor whose previous
    evaluation is not ``done()`` skips this dispatch and logs
    ``sensor_evaluation_overlap_skipped``. Runs outside the advisory lock.
    """
    existing = _IN_FLIGHT.get(snap.id)
    if existing is not None and not existing.done():
        _log().info(
            "sensor_evaluation_overlap_skipped",
            sensor_id=str(snap.id),
            sensor_name=snap.name,
        )
        return False
    task = asyncio.create_task(
        _evaluate_and_record(snap),
        name=f"sensor-eval-{snap.id}",
    )
    _IN_FLIGHT[snap.id] = task
    task.add_done_callback(lambda t: _discard_task(snap.id, t))
    return True


def _discard_task(sensor_id: uuid.UUID, task: asyncio.Task[None]) -> None:
    """Drop a finished evaluation from the in-flight registry (if still current)."""
    if _IN_FLIGHT.get(sensor_id) is task:
        del _IN_FLIGHT[sensor_id]


async def run_one_sensor_tick() -> int:
    """Execute one runner tick. Returns the number of evaluations dispatched.

    Public so tests can drive a deterministic single tick without the cadence
    sleep (mould: :func:`meho_backplane.scheduler.loop.run_one_tick`). Claims
    due sensors under the advisory lock, advances each claimed row's
    ``next_fire_at`` (committing before dispatch), parks a row whose persisted
    cadence no longer parses, then -- after releasing the lock -- spawns a
    backgrounded evaluation per claimed row (subject to the overlap guard). The
    tick never awaits the evaluations, so it returns promptly and the lock is
    held for the DB work only.
    """
    now = datetime.now(UTC)
    to_dispatch: list[_SensorSnapshot] = []
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        locked = await _try_advisory_lock(session, _SENSOR_RUNNER_ADVISORY_LOCK_KEY)
        if not locked:
            return 0
        try:
            rows = await claim_due_sensors(session, now=now, limit=_CLAIM_BATCH_LIMIT)
            for row in rows:
                try:
                    try:
                        advanced = await advance_sensor_next_fire(session, row, fire_instant=now)
                    except (InvalidCronExpressionError, InvalidTimezoneError) as exc:
                        await park_sensor(
                            session,
                            row.id,
                            reason=f"invalid_cadence:{type(exc).__name__}",
                        )
                        # Commit the park so a later sibling-row rollback cannot
                        # revert it and leave the row re-tripping every tick.
                        await session.commit()
                        _log().warning(
                            "sensor_paused",
                            sensor_id=str(row.id),
                            reason=str(exc),
                        )
                        continue
                    if advanced is None:
                        # Another claimer already advanced this row -- it owns
                        # this tick.
                        continue
                    # Snapshot while the row is fresh, then commit the advance
                    # before dispatch (release the row lock).
                    snap = _SensorSnapshot.from_row(row)
                    await session.commit()
                    to_dispatch.append(snap)
                except Exception:
                    # Per-row isolation: one bad row never stalls the tick.
                    _log().exception("sensor_tick_row_failed", sensor_id=str(row.id))
                    await session.rollback()
        finally:
            await _advisory_unlock(session, _SENSOR_RUNNER_ADVISORY_LOCK_KEY)
            await session.commit()

    # Advisory lock released, advances committed. Spawn the evaluations as
    # background tasks -- never awaited here (the #1502 lock-wedge class).
    dispatched = 0
    for snap in to_dispatch:
        if _spawn_evaluation(snap):
            dispatched += 1
    return dispatched


async def _runner_loop() -> None:
    """The forever loop: sleep one cadence, tick, repeat.

    Sleep-then-tick so the first tick after process start is delayed by one
    cadence -- letting the rest of the lifespan eager-init complete before the
    loop touches the DB (mould:
    :func:`meho_backplane.memory.expiry._sweeper_loop`). Per-tick ``try`` /
    ``except`` so a transient failure (DB blip, advisory-lock query error) is
    logged and the loop continues; ``CancelledError`` propagates so lifespan
    shutdown stops the task cleanly.
    """
    interval = get_settings().sensor_runner_tick_interval_seconds
    _log().info("sensor_runner_started", interval_seconds=interval)
    while True:
        await asyncio.sleep(get_settings().sensor_runner_tick_interval_seconds)
        try:
            await run_one_sensor_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log().warning("sensor_runner_tick_failed", exc_info=True)


def start_sensor_runner() -> asyncio.Task[None]:
    """Start the background runner loop and return its task handle.

    Registered in :func:`meho_backplane.main.lifespan` behind the
    ``SENSOR_RUNNER_ENABLED`` setting. The returned task is cancelled on
    lifespan shutdown; :func:`stop_sensor_runner` awaits the cancellation.
    Returning the task keeps a strong reference alive so it is not GC'd
    mid-flight (the "Task was destroyed but it is pending!" warning the
    lifecycle test bars). Mould:
    :func:`meho_backplane.memory.expiry.start_memory_expiry_sweeper`.
    """
    return asyncio.create_task(_runner_loop(), name="sensor-runner")


async def stop_sensor_runner(task: asyncio.Task[None]) -> None:
    """Cancel the runner loop + every outstanding evaluation, await their unwind.

    Cancels the tick loop first, then every in-flight evaluation task so no
    backgrounded op dispatch outlives the shutdown (leaving a "Task was
    destroyed but it is pending!" warning or an in-flight session racing the
    engine-pool teardown). Swallows the expected :class:`asyncio.CancelledError`
    on each; any other exception during unwind propagates so a broken shutdown
    is visible. Mould:
    :func:`meho_backplane.memory.expiry.stop_memory_expiry_sweeper`.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    outstanding = [t for t in _IN_FLIGHT.values() if not t.done()]
    for evaluation in outstanding:
        evaluation.cancel()
    for evaluation in outstanding:
        with contextlib.suppress(asyncio.CancelledError):
            await evaluation
    _IN_FLIGHT.clear()
