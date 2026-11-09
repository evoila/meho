# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Scheduled age-off of ``audit_log.raw_payload`` (security review S19, #307).

The connector-boundary redaction middleware (G11.4-T2 #1071) captures
the raw connector response *before* the redactor runs and persists it
into :attr:`~meho_backplane.db.models.AuditLog.raw_payload` so an
auditor can reconstruct the pre-redaction view. Each value the redaction
hides is stored as a keyed fingerprint
(:mod:`~meho_backplane.operations._secret_values`); rows written before
that change can still hold raw values, and the rest of a raw answer is
still a vendor body that can be large.

Unlike every sibling durable store -- topology history
(:mod:`~meho_backplane.topology.history_retention`), the flight-recorder
trace tables (:mod:`~meho_backplane.flight_recorder.reaper`), sensor
evidence, memory, announcements -- ``audit_log`` had **no** retention
sweeper. The two trace/history reapers explicitly leave it alone. So
pre-redaction bodies that echo credential material accumulated
indefinitely and were present in every DB backup: a rotated secret
outlived its rotation forever in ``raw_payload`` and old backups.

This module ships the bounded age-off: an ``asyncio`` task the FastAPI
lifespan owns that runs once per interval (default 7 days / weekly,
counted from its last recorded run -- see "When it runs" below) and
**NULLs** ``raw_payload`` on rows whose ``occurred_at`` is older than
``now() - raw_payload_retention_days``. It is deliberately narrow:

* Only ``raw_payload`` is nulled. The redacted ``payload`` (which carries
  ``redaction_policy_id``), the ``redaction_manifest``, ``policy_decision``
  and every other column -- the governance **record of account** -- are
  left untouched and age off on their own (currently unbounded) schedule.
  The manifest holds redaction *metadata* (``rule`` / ``pattern`` /
  ``action`` / ``count`` / ``span`` / ``reason`` / ``path``), never the
  matched secret value, so keeping it does not re-introduce the exposure.
* The column is **not** removed and within-window reconstruction is **not**
  broken -- both are explicitly out of scope (documented design intent).
  Auditors keep the pre-redaction view for the operator-configured window,
  then it ages off.

Why NULL and not DELETE
-----------------------

The topology / flight-recorder reapers DELETE whole rows because their
tables are pure history/trace. Here the row is the audit record of
account and must survive; only the pre-redaction *body* ages off. So the
statement is an ``UPDATE ... SET raw_payload = NULL`` rather than a
DELETE. The ``raw_payload IS NOT NULL`` predicate skips rows already aged
off (and this sweeper's own audit rows, which carry no raw payload), so the
working set converges to zero once the backlog is cleared.

Batches
-------

One run empties old rows in batches of :data:`_BATCH_SIZE` rows, oldest
first, and commits after each batch. So a big backlog (the first run after a
long gap) never holds one long transaction, and on PostgreSQL the
append-only trigger checks at most one batch of rows per transaction. Each
batch picks its rows through the ``audit_log_occurred_at_idx`` btree and
starts at the ``occurred_at`` where the last batch ended, so a batch does not
read the rows that earlier batches of the run already emptied. A batch with
fewer rows than :data:`_BATCH_SIZE` is the last one. A run also stops after
:data:`_MAX_RUN_SECONDS`; the next run then goes on from the oldest row that
is still left. If a batch fails, the batches before it stay committed. The
value is written as a
real SQL NULL via :func:`sqlalchemy.null` -- not Python ``None``, which the
JSON column's ``none_as_null=False`` default would persist as the JSON
literal ``'null'`` and which would keep matching ``IS NOT NULL`` forever
(see :func:`_null_raw_payload_older_than`).

The append-only guard on PostgreSQL
-----------------------------------

On PostgreSQL the ``audit_log_append_only`` trigger (migration ``0100``)
rejects ``UPDATE`` and ``DELETE`` on ``audit_log``. Migration ``0103``
lets one kind of ``UPDATE`` through: one that sets ``raw_payload`` from a
value to SQL NULL and leaves every other column value unchanged, byte for
byte. This sweeper's statement is of that kind. The trigger does not know
which code runs the statement, so any role with ``UPDATE`` on ``audit_log``
can do the same on any row; it still cannot change anything else or delete a
row, unless it also has the ``TRIGGER`` or ``TRUNCATE`` right on the table
(treat both like ownership; see migration ``0103``). The runtime role of the
running backplane (:mod:`meho_backplane.db.runtime_role`) has ``UPDATE`` on
the ``raw_payload`` column only, which is all this sweeper needs. If this statement ever
starts to change another column, the trigger rejects it and every tick fails
with ``audit_raw_payload_retention_tick_failed``. The Postgres tests in
``tests/migrations/test_migration_0103_audit_log_raw_payload_ageoff.py``
pin this.

When it runs
------------

The interval counts from the **last recorded run**, not from process
start. Every run writes one audit row (see "Audit-row shape" below), so
the newest row this job wrote is the last real run. A row counts only if
it has the system tenant id, ``method='INTERNAL'``, this job's
``operator_sub`` and the ``audit.raw_payload.prune`` path. Every
``INTERNAL`` writer uses a fixed path that no request can choose, so a
dispatch audit row with the same tenant id and path never counts. After
the process starts, the loop:

1. waits :data:`_FIRST_RUN_DELAY_SECONDS` (one minute), so the first run
   does not race the rest of startup;
2. reads the time of the newest run within the last interval
   (:func:`_last_recorded_run`);
3. runs at once if there is no such run (none within the last interval, a
   fresh install, or a database that has no such row yet); otherwise it
   waits only for the rest of the interval (:func:`_seconds_until_due`);
4. then runs once per interval, as before.

So a pod restart (an update, a crash, a node drain) never starts the
interval again. Before this, the loop slept a full interval after every
start, so a deployment that restarted more often than once a week never
ran the age-off at all.

A last-run time in the future (a pod with a fast clock wrote it) counts
as "due now", so it can never push the schedule back. If the read fails,
the loop logs ``audit_raw_payload_retention_last_run_read_failed`` and
runs at once. In both cases this is safe: a run is safe to run twice, and
running early is better than running late. The wait in step 3 is never
longer than one interval.

The read is cheap in every case. It looks only at rows from the last
interval (``occurred_at`` newer than now minus the interval): an older
run means "due now" anyway, so the answer is the same. PostgreSQL then
either uses the ``audit_log_tenant_id_idx`` btree (only this job writes
the system tenant id, so there are few such rows) or reads the
``occurred_at`` index over the last interval only.

Why ``asyncio.create_task`` and not APScheduler 4.x
---------------------------------------------------

Identical reasoning to :mod:`~meho_backplane.topology.history_retention`
and :mod:`~meho_backplane.memory.expiry`: APScheduler 4.x has only shipped
alphas the maintainer flags "should NOT be used in production". A stdlib
``asyncio`` loop registered in the lifespan is zero-dependency and the same
shape every other lifespan-owned background loop already uses.

Opt-out via ``RAW_PAYLOAD_RETENTION_DAYS=0``
--------------------------------------------

``0`` is the "keep forever" sentinel: a tick still runs, observes the
no-op shape, logs ``audit_raw_payload_retention_disabled`` and returns
without issuing any UPDATE or audit row. This is distinct from
``RAW_PAYLOAD_PRUNE_ENABLED=false`` (which skips starting the loop
entirely in the lifespan). Because ``0`` keeps un-redacted secrets in
``raw_payload`` forever, the security tradeoff is flagged in the setting
docstring, the Helm values comment, and the audit-channel doc.

Audit-row shape (one row per run)
---------------------------------

Every run writes exactly one
:class:`~meho_backplane.db.models.AuditLog` row via
:func:`~meho_backplane.memory.audit.write_internal_audit_row`:
``operator_sub='system:audit-raw-payload-retention'``,
``method='INTERNAL'``, ``path='audit.raw_payload.prune'``,
``status_code=200``, ``payload={"nulled_raw_payload_rows": N,
"retention_days": D, "cutoff": <iso-ts>, "stopped_early": B}``. ``N`` is
the total over all batches of the run; ``stopped_early`` is ``true`` when
the run hit :data:`_MAX_RUN_SECONDS` (old rows may be left for the next
run). ``tenant_id`` is the system
sentinel (:data:`AUDIT_RAW_PAYLOAD_SYSTEM_TENANT_ID`) -- the same
soft-FK-exploiting per-deploy sentinel the topology / flight-recorder
prunes use so operators querying by their own tenant do not see prune
rows in their timeline.

A run that nulled **zero** rows (nothing past the cutoff) also writes its
row, with ``nulled_raw_payload_rows: 0``. The row is the record of the
run: the next process start reads it to know when the age-off last ran
(see "When it runs"), and an auditor can see that the age-off ran even
when nothing was due. At the default weekly interval that is about 52
rows a year. Only the ``0`` sentinel (age-off turned off) writes **no**
row: nothing ran, so there is nothing to record.

Fail-closed contract: the audit writer raises on commit failure; the
per-tick ``try`` / ``except`` in :func:`_prune_loop` logs and continues to
the next cadence -- one bad audit write must not kill the loop. Every
batch has already committed before the audit row is attempted, so a lost
audit row never leaves a secret un-aged.

Per-pod leader election is deferred (Initiative-level, same as the sibling
sweepers): under N replicas the worst case is N runs at the same time over
the same rows. Each batch skips the rows another run has locked, so the
runs share the work without waiting for each other; a run that finds fewer
rows than one batch ends, and the other runs empty the rest. Each replica
writes its own run row; the next start of any replica reads the newest one.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import structlog
from sqlalchemy import Update, null, select, update

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog
from meho_backplane.memory.audit import (
    INTERNAL_METHOD,
    write_internal_audit_row,
)
from meho_backplane.metrics import note_loop_tick
from meho_backplane.settings import get_settings

__all__ = [
    "AUDIT_RAW_PAYLOAD_PRUNE_PATH",
    "AUDIT_RAW_PAYLOAD_SYSTEM_TENANT_ID",
    "SYSTEM_OPERATOR_SUB",
    "start_audit_raw_payload_reaper",
    "stop_audit_raw_payload_reaper",
]

_log = structlog.get_logger(__name__)

#: Synthetic ``operator_sub`` on age-off audit rows. The ``"system:<job>"``
#: convention keeps background-job rows partitionable from operator rows by
#: ``operator_sub`` alone (matching the topology / flight-recorder sweepers).
SYSTEM_OPERATOR_SUB: str = "system:audit-raw-payload-retention"

#: Canonical ``INTERNAL`` ``path`` for the age-off audit row. One shared
#: symbol for the task, the audit-doc registry, and future audit-query
#: consumers. Registered in ``docs/architecture/audit.md``.
AUDIT_RAW_PAYLOAD_PRUNE_PATH: str = "audit.raw_payload.prune"

#: Sentinel ``tenant_id`` for the system-wide age-off audit row. The cutoff
#: spans every tenant's rows in one statement, so attributing the row to any
#: real tenant would mislead operators querying their own timeline. The last
#: segment embeds the task number (``307``) so the literal is greppable and
#: stable across restarts; it exploits the ``audit_log.tenant_id`` soft-FK
#: (no matching ``tenant`` row required), same as the sibling sentinels.
AUDIT_RAW_PAYLOAD_SYSTEM_TENANT_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000307")

#: How long the loop waits after the process starts before it reads the last
#: recorded run. A run that is due then happens about one minute after start:
#: soon, but not in the middle of the rest of startup (engine init,
#: registrars, other loops). Settings allow no interval below 60 seconds, so
#: this never exceeds the interval.
_FIRST_RUN_DELAY_SECONDS: float = 60.0

#: Rows one batch empties, in one transaction (see "Batches" above).
_BATCH_SIZE: int = 5_000

#: A run starts no new batch after this many seconds. The next run goes on
#: from the oldest row that is still left.
_MAX_RUN_SECONDS: float = 15 * 60.0


class _AgeOffResult(NamedTuple):
    """What one run of the age-off did."""

    nulled_rows: int
    stopped_early: bool


def _resolve_system_tenant_id() -> uuid.UUID:
    """Return the sentinel tenant id for the age-off audit row.

    Centralised behind a function so a future migration to per-tenant
    retention can route the call site through a lookup without rewriting
    the task; v0.2 returns the fixed sentinel.
    """
    return AUDIT_RAW_PAYLOAD_SYSTEM_TENANT_ID


async def _last_recorded_run(*, since: datetime) -> datetime | None:
    """Return the newest run of this job after *since*, or ``None`` if none.

    Every run writes one audit row, so the newest such row is the last real
    run. The filter matches only rows this job writes: the system tenant id,
    ``method='INTERNAL'``, :data:`SYSTEM_OPERATOR_SUB` and
    :data:`AUDIT_RAW_PAYLOAD_PRUNE_PATH`. A dispatch audit row with the same
    tenant id and path (``method='DISPATCH'``) never counts.

    The caller passes ``since = now - interval``. A run older than that means
    "due now" anyway, and the bound keeps the read cheap in every query plan
    (see "When it runs" in the module docstring).

    SQLite gives back a datetime without a time zone; the writer stores UTC,
    so such a value is read as UTC. PostgreSQL gives back an aware value.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        last_run = await session.scalar(
            select(AuditLog.occurred_at)
            .where(
                AuditLog.tenant_id == _resolve_system_tenant_id(),
                AuditLog.method == INTERNAL_METHOD,
                AuditLog.operator_sub == SYSTEM_OPERATOR_SUB,
                AuditLog.path == AUDIT_RAW_PAYLOAD_PRUNE_PATH,
                AuditLog.occurred_at > since,
            )
            .order_by(AuditLog.occurred_at.desc())
            .limit(1)
        )
    if last_run is None:
        return None
    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=UTC)
    return last_run


def _seconds_until_due(
    last_run: datetime | None,
    *,
    now: datetime,
    interval_seconds: float,
) -> float:
    """Return how many seconds are left until the next run is due.

    * No recorded run: ``0`` (run now).
    * *last_run* is in the future (a pod with a fast clock wrote it): ``0``
      (run now), so a wrong clock can never push the schedule back.
    * A full interval has passed since *last_run*: ``0`` (run now).
    * Otherwise: what is left of the interval.

    So the result is always between ``0`` and one interval.
    """
    if last_run is None or last_run > now:
        return 0.0
    remaining = interval_seconds - (now - last_run).total_seconds()
    return max(0.0, remaining)


async def _read_last_run_or_none(*, since: datetime) -> datetime | None:
    """Read the last recorded run; on a read error, log it and return ``None``.

    ``None`` makes the loop run at once. That is the safe choice: a run is
    safe to run twice, and running early is better than running late.
    """
    try:
        return await _last_recorded_run(since=since)
    except Exception as exc:
        _log.warning(
            "audit_raw_payload_retention_last_run_read_failed",
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return None


def _batch_statement(cutoff: datetime, *, not_before: datetime | None) -> Update:
    """Build the ``UPDATE`` that empties ``raw_payload`` on one batch of old rows.

    The inner ``SELECT`` picks up to :data:`_BATCH_SIZE` ids of rows with
    ``not_before <= occurred_at < cutoff`` and ``raw_payload IS NOT NULL``,
    oldest first, through the ``audit_log_occurred_at_idx`` btree. On
    PostgreSQL it locks them with ``FOR UPDATE SKIP LOCKED``: two runs at the
    same time take different rows and never wait for each other. A row that
    another run emptied first fails the ``raw_payload IS NOT NULL`` check, so
    the append-only trigger never sees a NULL-to-NULL update (which it
    rejects). ``FOR UPDATE`` needs
    the ``UPDATE`` right on one column, which the runtime role has. SQLite
    ignores the lock clause.

    The value is set with :func:`sqlalchemy.null` (a real **SQL NULL**), not
    Python ``None``: ``_PORTABLE_JSON`` is ``JSON().with_variant(JSONB())``
    with the default ``none_as_null=False``, so ``.values(raw_payload=None)``
    would persist the JSON literal ``'null'`` -- which is *not* SQL NULL and
    would keep matching the ``IS NOT NULL`` predicate on every future tick
    (never converging, inflating the count forever). (Rows that were
    pre-existing JSON ``'null'`` -- the dispatcher's error-arm no-body rows --
    are normalised to SQL NULL the one time they cross the cutoff; harmless.)

    ``RETURNING occurred_at`` gives the row count and where the next batch
    starts. It works on PostgreSQL and on SQLite 3.35 and newer.
    """
    picked = [AuditLog.raw_payload.is_not(None), AuditLog.occurred_at < cutoff]
    if not_before is not None:
        picked.append(AuditLog.occurred_at >= not_before)
    batch_ids = (
        select(AuditLog.id)
        .where(*picked)
        .order_by(AuditLog.occurred_at)
        .limit(_BATCH_SIZE)
        .with_for_update(skip_locked=True)
    )
    return (
        update(AuditLog)
        .where(AuditLog.id.in_(batch_ids))
        .values(raw_payload=null())
        .returning(AuditLog.occurred_at)
        .execution_options(synchronize_session=False)
    )


async def _null_one_batch(cutoff: datetime, *, not_before: datetime | None) -> list[datetime]:
    """Run one batch (:func:`_batch_statement`) in its own transaction.

    Return the ``occurred_at`` of each row it emptied.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(_batch_statement(cutoff, not_before=not_before))
        occurred = list(result.scalars())
        await session.commit()
    return occurred


async def _null_raw_payload_older_than(cutoff: datetime) -> _AgeOffResult:
    """Empty ``raw_payload`` on all rows older than *cutoff*, one batch at a time.

    Each batch is its own transaction (see "Batches" in the module
    docstring). The run ends after a batch with fewer than
    :data:`_BATCH_SIZE` rows, or, with ``stopped_early=True``, when
    :data:`_MAX_RUN_SECONDS` have passed.
    """
    deadline = time.monotonic() + _MAX_RUN_SECONDS
    nulled = 0
    not_before: datetime | None = None
    while True:
        occurred = await _null_one_batch(cutoff, not_before=not_before)
        nulled += len(occurred)
        if len(occurred) < _BATCH_SIZE:
            return _AgeOffResult(nulled_rows=nulled, stopped_early=False)
        if time.monotonic() >= deadline:
            return _AgeOffResult(nulled_rows=nulled, stopped_early=True)
        not_before = max(occurred)


async def _write_prune_audit_row(
    *,
    result: _AgeOffResult,
    retention_days: int,
    cutoff: datetime,
    duration_ms: float,
) -> None:
    """Write the one summary audit row, swallowing audit-side failures.

    Every batch has already committed before this helper runs; an audit-write
    failure must not stall the loop. Surface it loud-but-non-fatal so a
    flapping audit substrate is visible without leaving secrets un-aged.
    Without the row, the next process start finds no record of this run and
    runs the age-off again soon after start, which is safe.
    """
    try:
        await write_internal_audit_row(
            event_class=AuditEventClass.OPERATION_EXECUTION,
            operator_sub=SYSTEM_OPERATOR_SUB,
            tenant_id=_resolve_system_tenant_id(),
            method=INTERNAL_METHOD,
            path=AUDIT_RAW_PAYLOAD_PRUNE_PATH,
            status_code=200,
            duration_ms=duration_ms,
            payload={
                "nulled_raw_payload_rows": result.nulled_rows,
                "retention_days": retention_days,
                "cutoff": cutoff.isoformat(),
                "stopped_early": result.stopped_early,
            },
        )
    except Exception:
        _log.exception(
            "audit_raw_payload_retention_audit_write_failed",
            nulled_raw_payload_rows=result.nulled_rows,
            retention_days=retention_days,
        )


async def _run_one_prune_tick() -> None:
    """One age-off sweep: NULL past-cutoff ``raw_payload``, then record the run.

    Reads ``raw_payload_retention_days`` and computes a cutoff of
    ``now(UTC) - retention_days``. ``0`` is the keep-forever sentinel: log a
    heartbeat and return without any UPDATE or audit row. Every other run
    writes one audit row, also when it nulled zero rows: the row is the
    record of the run that :func:`_last_recorded_run` reads at the next
    process start.

    Raises nothing to the loop by contract -- the per-tick ``try`` / ``except``
    in :func:`_prune_loop` catches any exception so one bad tick cannot kill
    the loop.
    """
    tick_started = time.perf_counter()
    settings = get_settings()
    retention_days = settings.raw_payload_retention_days

    if retention_days == 0:
        _log.info(
            "audit_raw_payload_retention_disabled",
            retention_days=0,
            interval_seconds=settings.raw_payload_prune_interval_seconds,
        )
        return

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    result = await _null_raw_payload_older_than(cutoff)
    duration_ms = (time.perf_counter() - tick_started) * 1000.0
    _log.info(
        "audit_raw_payload_retention_tick_done",
        nulled_raw_payload_rows=result.nulled_rows,
        stopped_early=result.stopped_early,
        retention_days=retention_days,
        cutoff=cutoff.isoformat(),
        duration_ms=duration_ms,
    )
    await _write_prune_audit_row(
        result=result,
        retention_days=retention_days,
        cutoff=cutoff,
        duration_ms=duration_ms,
    )


async def _prune_loop() -> None:
    """The forever loop: wait until a run is due, age off, repeat.

    The first run is timed from the last recorded run, not from process
    start (see "When it runs" in the module docstring): wait one minute so
    startup can finish, read the last run, then run at once if it is due or
    wait only for the rest of the interval. After that, the loop runs once
    per interval. Per-tick ``try`` / ``except`` guards mean a transient DB
    blip is logged and the loop continues; ``asyncio.CancelledError``
    propagates so lifespan shutdown can stop the task cleanly.
    """
    _log.info(
        "audit_raw_payload_retention_started",
        interval_seconds=get_settings().raw_payload_prune_interval_seconds,
        retention_days=get_settings().raw_payload_retention_days,
        first_check_in_seconds=_FIRST_RUN_DELAY_SECONDS,
    )
    await asyncio.sleep(_FIRST_RUN_DELAY_SECONDS)

    interval = get_settings().raw_payload_prune_interval_seconds
    now = datetime.now(UTC)
    last_run = await _read_last_run_or_none(since=now - timedelta(seconds=interval))
    wait_seconds = _seconds_until_due(last_run, now=now, interval_seconds=interval)
    # One line per start that says when the age-off last ran (within the
    # last interval; ``None`` if not) and when it runs next, so an operator
    # can check the schedule after a restart.
    _log.info(
        "audit_raw_payload_retention_scheduled",
        last_run_at=last_run.isoformat() if last_run is not None else None,
        next_run_in_seconds=wait_seconds,
        interval_seconds=interval,
    )
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)

    while True:
        try:
            await _run_one_prune_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Error level, not warning: a failed tick means un-redacted
            # bodies stay in ``raw_payload``. ``error_type`` makes a failure
            # that repeats every week easy to find and alert on.
            _log.exception(
                "audit_raw_payload_retention_tick_failed",
                error_type=type(exc).__name__,
            )
        note_loop_tick(
            "audit_raw_payload_retention",
            get_settings().raw_payload_prune_interval_seconds,
        )
        await asyncio.sleep(get_settings().raw_payload_prune_interval_seconds)


def start_audit_raw_payload_reaper() -> asyncio.Task[None]:
    """Start the background age-off loop and return its task handle.

    Registered in :func:`meho_backplane.main._start_background_tasks` behind
    the ``RAW_PAYLOAD_PRUNE_ENABLED`` setting. The returned task is cancelled
    on lifespan shutdown; returning it keeps a strong reference so the task is
    not GC'd mid-flight ("Task was destroyed but it is pending!").
    """
    return asyncio.create_task(
        _prune_loop(),
        name="audit-raw-payload-retention-reaper",
    )


async def stop_audit_raw_payload_reaper(task: asyncio.Task[None]) -> None:
    """Cancel the age-off task and await its unwind (swallowing the cancel).

    Any exception other than the expected :class:`asyncio.CancelledError`
    propagates so a broken shutdown is visible. Shape mirrors the sibling
    sweepers' ``stop_*`` helpers so one disposal pattern spans the
    lifespan-owned loops.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
