# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Durable-row operations for the ``sensor`` table (#2503).

The narrow data-access layer the Sensor admin service and #2505's runner
call. Two concerns:

1. **Insert** a new sensor with a materialised ``next_fire_at`` (the admin
   service calls this; exposed here so tests can construct sensors without
   going through HTTP).
2. **Record a result** onto the latest-state projection -- the one named
   write path (Decision D). #2505's runner calls it locally after an
   evaluation, #2507 hooks the same persist path, and #2415-T3's gateway
   batch-post calls it for remote results.

All functions take an :class:`AsyncSession` they did not open -- the
caller's transaction owns the commit / rollback. They flush so the row
state is visible within the transaction but do not commit (the
flush-not-commit discipline
:mod:`meho_backplane.scheduler.repository` follows).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.checks.assertions import CheckState
from meho_backplane.db.models import Sensor, SensorCadenceKind, SensorResult, SensorStatus
from meho_backplane.scheduler.cron import next_fire_after

__all__ = [
    "accelerate_sensor_next_fire",
    "advance_sensor_next_fire",
    "claim_due_sensors",
    "create_sensor",
    "list_sensor_results",
    "park_sensor",
    "record_sensor_result",
]


async def create_sensor(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    connector_id: str,
    op_id: str,
    target: dict[str, object] | None,
    params: dict[str, object],
    assertion: dict[str, object],
    cadence_kind: SensorCadenceKind,
    interval_seconds: int | None,
    cron_expr: str | None,
    timezone: str,
    severity: str,
    for_seconds: int,
    retry_times: int,
    retry_backoff_seconds: int,
    identity_sub: str,
    created_by_sub: str,
    base: datetime | None = None,
) -> Sensor:
    """Insert a sensor with a materialised first ``next_fire_at``.

    *base* is the instant the first fire is computed from; the default
    ``datetime.now(UTC)`` is what production wants. Tests pass an explicit
    base to make the first-fire instant deterministic. For an interval
    cadence ``next_fire_at`` is ``base + interval_seconds``; for a cron
    cadence it is :func:`~meho_backplane.scheduler.cron.next_fire_after`
    (the same materialisation the scheduler uses), so #2505's claim query
    (``status='active' AND next_fire_at <= now``) is uniform across kinds.

    *cadence_kind* determines which of *interval_seconds* / *cron_expr* is
    non-``None`` -- the wire schema's model validator already proved the
    cadence union, so this function trusts the shape and asserts it for
    the type-checker.

    Raises:
        InvalidCronExpressionError: *cron_expr* is not a valid 5-field
            cron expression (cron cadence only).
    """
    if base is None:
        base = datetime.now(UTC)
    if cadence_kind == SensorCadenceKind.CRON:
        assert cron_expr is not None
        next_fire = next_fire_after(cron_expr, base, timezone)
    else:
        assert interval_seconds is not None
        next_fire = base + timedelta(seconds=interval_seconds)
    row = Sensor(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=name,
        connector_id=connector_id,
        op_id=op_id,
        target=target,
        params=params,
        assertion=assertion,
        status=SensorStatus.ACTIVE.value,
        cadence_kind=cadence_kind.value,
        interval_seconds=interval_seconds,
        cron_expr=cron_expr,
        timezone=timezone,
        next_fire_at=next_fire,
        severity=severity,
        for_seconds=for_seconds,
        retry_times=retry_times,
        retry_backoff_seconds=retry_backoff_seconds,
        identity_sub=identity_sub,
        created_by_sub=created_by_sub,
    )
    session.add(row)
    await session.flush()
    return row


def _as_utc(dt: datetime) -> datetime:
    """Normalise a possibly-naive datetime to UTC-aware for comparison.

    ``DateTime(timezone=True)`` round-trips *naive* on aiosqlite (the
    unit-test path) and *aware* on PG; the runner always evaluates in UTC,
    so a naive stored value denotes a UTC instant. Attaching UTC to naive
    values lets the monotonicity comparison work on either dialect without
    raising ``TypeError`` on a naive-vs-aware compare.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _append_evidence_row(
    session: AsyncSession,
    *,
    sensor_id: uuid.UUID,
    state: CheckState,
    value: object,
    evidence: dict[str, object],
    evaluated_at: datetime,
) -> None:
    """Append one per-tick :class:`SensorResult` history row (#2756).

    Called by :func:`record_sensor_result` for every non-stale evaluation when
    retention is enabled, in the caller's transaction (flush-not-commit). The
    row records the *observed* outcome -- committed, soft/pending (#2799), or
    re-confirmed -- so a flapping reading that never commits still lands in
    history. ``evidence['reason']`` (the evaluator's machine reason on an
    ``unknown`` outcome, absent otherwise) is surfaced as a first-class
    ``reason`` column so "why did it flap" reads without unpacking the blob.
    """
    raw_reason = evidence.get("reason") if isinstance(evidence, dict) else None
    session.add(
        SensorResult(
            sensor_id=sensor_id,
            evaluated_at=evaluated_at,
            state=state,
            value=value,
            evidence=evidence,
            reason=str(raw_reason) if raw_reason is not None else None,
        )
    )


async def _commit_through_confirmation_gate(
    session: AsyncSession,
    row: Sensor,
    state: CheckState,
    evaluated_at: datetime,
) -> bool:
    """Commit *state* onto *row* through the soft/hard confirmation gate (#2799).

    With ``retry_times == 0`` (the default) every differing reading commits
    immediately -- the pre-#2799 behaviour, bit for bit. With ``retry_times >
    0`` a reading that differs from the committed ``last_state`` is held as a
    soft state instead:

    * differs from ``pending_state`` too -> a new confirmation window opens
      (``pending_state = state``, ``pending_count = 1``); an escalation
      mid-window restarts the count on the new candidate.
    * equals ``pending_state`` -> ``pending_count += 1``; once
      ``pending_count > retry_times`` the state commits (``last_state`` flips,
      ``state_since`` bumps to this result's ``evaluated_at``) and the window
      clears.
    * equals the committed ``last_state`` (the candidate reverted) -> the
      window clears without committing anything.

    Symmetric in both directions -- recovery to ``ok`` is confirmed exactly
    like a degradation (deliberately unlike Nagios' immediate hard recovery:
    the observed flap's second nuisance mail *was* a flapping all-clear, the
    failure mode Prometheus grew ``keep_firing_for`` for) -- and ``unknown``
    participates like any state, so a transient dispatch timeout cannot flip
    the sensor. The gate lives in this repository seam, not the runner task, so
    the satellite-gateway batch-post path is confirmed identically. Flushes and
    returns ``True`` iff the committed state changed.
    """
    if state == row.last_state:
        # The committed state re-confirmed itself; any half-open confirmation
        # window was a transient -- close it.
        row.pending_state = None
        row.pending_count = 0
        await session.flush()
        return False
    if row.retry_times > 0:
        if state != row.pending_state:
            # First differing reading (or an escalation mid-window): open a
            # fresh window on this candidate.
            row.pending_state = state
            row.pending_count = 1
        else:
            row.pending_count += 1
        if row.pending_count <= row.retry_times:
            # Soft state -- observed but unconfirmed. No commit, no transition
            # for downstream consumers.
            await session.flush()
            return False
    row.last_state = state
    row.state_since = evaluated_at
    row.pending_state = None
    row.pending_count = 0
    await session.flush()
    return True


async def record_sensor_result(
    session: AsyncSession,
    *,
    sensor_id: uuid.UUID,
    state: CheckState,
    value: object,
    evidence: dict[str, object],
    evaluated_at: datetime,
    record_history: bool = False,
) -> bool:
    """Record one evaluation onto *sensor_id*'s projection; return state-committed.

    Updates ``last_value`` / ``last_evidence`` / ``last_evaluated_at`` on every
    accepted result, then commits ``last_state`` / ``state_since`` through
    :func:`_commit_through_confirmation_gate` (#2799's soft/hard-state gate) --
    returning ``True`` iff the *committed* state changed, so #2506's ``for:``
    hold-time hysteresis measures confirmed time. When *record_history* is true
    it also appends a per-tick :class:`~meho_backplane.db.models.SensorResult`
    row via :func:`_append_evidence_row` (#2756) **in the same transaction** as
    the projection update, so history and projection cannot diverge; the
    runner's ``_persist_outcome`` sets it from
    ``CHECKS_EVIDENCE_RETENTION_DAYS > 0`` (``0`` disables it -- the pre-#2756
    latest-only behaviour). The full confirmation-gate + evidence-history
    mechanics are in ``docs/codebase/sensor.md``.

    **Monotonicity guard.** A result whose ``evaluated_at`` is not strictly
    newer than the row's recorded ``last_evaluated_at`` is ignored (returns
    ``False`` without mutating the projection **or appending history**).
    #2505's runner is the single serialised evaluator, but a retried or
    reordered persist -- the gateway batch-post (#2415-T3) can deliver remote
    results out of order -- must not overwrite a newer projection with a stale
    one, move ``state_since`` backwards, or duplicate a history row. An equal
    timestamp is an already-recorded idempotent retry; gating the append on the
    same guard keeps ``evaluated_at`` strictly monotonic per sensor, which the
    trend query's keyset pagination relies on.

    A ``sensor_id`` that names no row (deleted between an evaluation and the
    persist) returns ``False`` without raising -- "nothing to record".
    """
    row = await session.get(Sensor, sensor_id)
    if row is None:
        return False
    if row.last_evaluated_at is not None and _as_utc(evaluated_at) <= _as_utc(
        row.last_evaluated_at
    ):
        # Stale or duplicate result -- keep the newer projection intact and
        # append no history row.
        return False
    row.last_value = value
    row.last_evidence = evidence
    row.last_evaluated_at = evaluated_at
    if record_history:
        # Append after the monotonicity guard (no dup on a stale persist) and
        # before the confirmation gate (a pending/unconfirmed reading still
        # lands in history), so every non-stale evaluation records exactly one
        # observed-outcome row.
        _append_evidence_row(
            session,
            sensor_id=sensor_id,
            state=state,
            value=value,
            evidence=evidence,
            evaluated_at=evaluated_at,
        )
    return await _commit_through_confirmation_gate(session, row, state, evaluated_at)


async def list_sensor_results(
    session: AsyncSession,
    *,
    sensor_id: uuid.UUID,
    from_ts: datetime | None,
    to_ts: datetime | None,
    state: CheckState | None,
    limit: int,
    after: datetime | None,
) -> tuple[Sequence[SensorResult], datetime | None]:
    """Return up to *limit* evidence rows for *sensor_id*, ``evaluated_at ASC``.

    The forensic trend query (#2756). Binary filters only -- an inclusive
    ``[from_ts, to_ts]`` window, an optional exact *state*, and a keyset
    *after* cursor (``evaluated_at > after``); no smoothing, downsampling, or
    scoring. Raw rows in deterministic ``evaluated_at ASC`` order; the caller
    aggregates.

    Over-fetches ``limit + 1`` so the next-page cursor is computed without a
    second COUNT: when more than *limit* rows match, the surplus row is dropped
    and the last kept row's ``evaluated_at`` is returned as the next cursor
    (``None`` when this is the final page). ``evaluated_at`` is strictly
    monotonic within one sensor's history (:func:`record_sensor_result`'s
    guard), so it totally orders the rows and the keyset needs no tiebreaker.

    Tenant scoping is the caller's job: this function trusts *sensor_id* has
    already been resolved within the caller's tenant (the service verifies
    ownership before calling), so it filters on ``sensor_id`` alone.
    """
    stmt = (
        select(SensorResult)
        .where(SensorResult.sensor_id == sensor_id)
        .order_by(SensorResult.evaluated_at.asc())
        .limit(limit + 1)
    )
    if from_ts is not None:
        stmt = stmt.where(SensorResult.evaluated_at >= from_ts)
    if to_ts is not None:
        stmt = stmt.where(SensorResult.evaluated_at <= to_ts)
    if state is not None:
        stmt = stmt.where(SensorResult.state == state)
    if after is not None:
        stmt = stmt.where(SensorResult.evaluated_at > after)
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    if len(rows) > limit:
        kept = rows[:limit]
        return kept, kept[-1].evaluated_at
    return rows, None


async def claim_due_sensors(
    session: AsyncSession,
    *,
    now: datetime,
    limit: int,
) -> Sequence[Sensor]:
    """Return up to *limit* active sensors whose ``next_fire_at`` <= *now*.

    Copies :func:`meho_backplane.scheduler.repository.claim_due_triggers`'s
    belt-and-braces replica-safety discipline onto the ``sensor`` table:

    * On PostgreSQL: ``SELECT ... WHERE ... FOR UPDATE SKIP LOCKED`` so a
      second claimer in another transaction (another replica, another
      process) never receives a row this transaction has locked. The row
      locks release on the caller's commit / rollback, so the runner holds
      them across the conditional advance in
      :func:`advance_sensor_next_fire` (the claim-advance sequence).
    * On SQLite (the unit-test path): the locking clause no-ops (SQLite has
      a single writer at a time). Single-fire across two in-process ticks is
      enforced by the conditional ``UPDATE ... WHERE next_fire_at=:previous``
      in :func:`advance_sensor_next_fire`.

    ``next_fire_at <= now`` excludes NULL rows (a NULL comparison is NULL
    under SQL), so only rows with a materialised next-fire are claimed. The
    ``ORDER BY next_fire_at ASC`` fires the most-overdue sensors first (a
    deployment resuming after a pause walks its backlog forward), riding
    #2503's partial ``sensor_due_idx (status, next_fire_at) WHERE
    status='active'``.
    """
    conn = await session.connection()
    stmt = (
        select(Sensor)
        .where(
            Sensor.status == SensorStatus.ACTIVE.value,
            Sensor.next_fire_at <= now,
        )
        .order_by(Sensor.next_fire_at.asc())
        .limit(limit)
    )
    if conn.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def advance_sensor_next_fire(
    session: AsyncSession,
    row: Sensor,
    *,
    fire_instant: datetime,
) -> Sensor | None:
    """Advance *row*'s ``next_fire_at`` past *fire_instant*; return the row.

    Handles both cadence kinds uniformly:

    * ``interval`` -- ``next_fire_at = fire_instant + interval_seconds``.
    * ``cron`` -- ``next_fire_at = next_fire_after(cron_expr, fire_instant,
      timezone)`` (:func:`meho_backplane.scheduler.cron.next_fire_after`, the
      same materialisation :func:`create_sensor` used at insert).

    The advance is a conditional UPDATE (``WHERE status='active' AND
    next_fire_at=:previous``) that mirrors
    :func:`meho_backplane.scheduler.repository.advance_cron_trigger`: a
    concurrent claimer that already advanced this row (the SKIP-LOCKED-less
    dialect race, or the belt-and-braces guard on PG) matches zero rows, so
    this call returns ``None`` and the runner skips the dispatch -- the other
    claimer owns this tick. The advance commits (via the caller) *before* the
    dispatch, so a slow or crashed evaluation cannot delay or double-fire the
    next scheduled instant (the at-most-once contract #804 proved).

    Returns the row on a successful advance, ``None`` when the conditional
    UPDATE matched zero rows (another claimer won the race).

    Raises:
        ~meho_backplane.scheduler.cron.InvalidCronExpressionError: the
            persisted ``cron_expr`` no longer parses (the runner catches this
            and parks the row via :func:`park_sensor`).
        ~meho_backplane.scheduler.cron.InvalidTimezoneError: the persisted
            ``timezone`` is not a resolvable IANA name (same park path).
    """
    previous_next = row.next_fire_at
    if row.cadence_kind == SensorCadenceKind.CRON.value:
        # ``ck_sensor_cadence_fields`` guarantees a cron row carries a
        # non-NULL cron_expr; assert it for the type-checker. A corrupt
        # *value* (unparseable expression / timezone) raises out of
        # next_fire_after, which the runner catches to park the row.
        assert row.cron_expr is not None
        new_next = next_fire_after(row.cron_expr, fire_instant, row.timezone)
    else:
        # ``ck_sensor_cadence_fields`` guarantees an interval row carries a
        # non-NULL interval_seconds.
        assert row.interval_seconds is not None
        new_next = fire_instant + timedelta(seconds=row.interval_seconds)
    stmt = (
        update(Sensor)
        .where(
            Sensor.id == row.id,
            Sensor.status == SensorStatus.ACTIVE.value,
            Sensor.next_fire_at == previous_next,
        )
        .values(next_fire_at=new_next)
    )
    result = await session.execute(stmt)
    await session.flush()
    rowcount: int = result.rowcount  # type: ignore[attr-defined]
    if rowcount == 0:
        return None
    # Refresh the in-memory object so the caller reads the advanced value
    # without re-querying.
    row.next_fire_at = new_next
    return row


async def accelerate_sensor_next_fire(
    session: AsyncSession,
    sensor_id: uuid.UUID,
    *,
    not_later_than: datetime,
) -> bool:
    """Pull ``next_fire_at`` forward to *not_later_than* if it is later.

    The accelerated re-check half of #2799's confirmation retries: after a
    pending (unconfirmed) result, the runner pulls the sensor's next fire
    to ``evaluated_at + retry_backoff_seconds`` so the confirming
    re-evaluation does not wait a full cadence. Implemented as one
    conditional UPDATE (``WHERE next_fire_at > :accel``), i.e.
    ``min(next_fire_at, accel)`` semantics done atomically:

    * never *delays* a fire -- a row already scheduled sooner (or pulled
      sooner by a concurrent writer) matches zero rows;
    * only rides the existing claim/advance machinery -- the re-check is
      an ordinary due row on the next tick, so the #804 at-most-once
      discipline, the overlap guard, and replica-safety are untouched
      (no in-process sleep loop that would die on ``stop_sensor_runner``
      and pin ``_IN_FLIGHT``);
    * ``status='active'`` guard so a row parked between the evaluation
      and this persist is not resurrected onto the due index.

    Returns ``True`` when the row was pulled forward, ``False`` when the
    UPDATE matched nothing (already sooner, parked, or deleted).

    ``synchronize_session=False``: the caller's session already holds the
    row (``record_sensor_result`` loaded it), and the default ``'auto'``
    strategy would evaluate the ``>`` predicate *Python-side* against the
    in-memory attribute -- naive on the aiosqlite round-trip vs the aware
    *not_later_than*, a ``TypeError``. The DB-side comparison is
    dialect-consistent, and nothing reads the in-memory ``next_fire_at``
    after this call (the session commits and closes).
    """
    stmt = (
        update(Sensor)
        .where(
            Sensor.id == sensor_id,
            Sensor.status == SensorStatus.ACTIVE.value,
            Sensor.next_fire_at > not_later_than,
        )
        .values(next_fire_at=not_later_than)
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    await session.flush()
    rowcount: int = result.rowcount  # type: ignore[attr-defined]
    return rowcount > 0


async def park_sensor(
    session: AsyncSession,
    sensor_id: uuid.UUID,
    *,
    reason: str,
) -> None:
    """Transition a corrupt sensor to ``status='paused'`` with a reason.

    Called by #2505's runner for a row whose persisted cadence no longer
    computes a next fire (an unparseable ``cron_expr`` / ``timezone`` that
    bypassed the create-time validator). Parking stops the runner from
    re-tripping on the same bad row every tick; the *reason* is stamped onto
    ``status_reason`` so the parked state explains itself on #2506's read
    surfaces. Mirrors
    :func:`meho_backplane.scheduler.loop._park_trigger`. Flush-not-commit --
    the caller owns the transaction (and commits so a sibling-row rollback
    later in the tick cannot revert the park)."""
    await session.execute(
        update(Sensor)
        .where(Sensor.id == sensor_id)
        .values(
            status=SensorStatus.PAUSED.value,
            status_reason=reason,
        )
    )
    await session.flush()
