# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Scheduled age-off of ``audit_log.raw_payload`` (security review S19, #307).

The connector-boundary redaction middleware (G11.4-T2 #1071) captures
the raw connector response *before* the redactor runs and persists it
verbatim into :attr:`~meho_backplane.db.models.AuditLog.raw_payload` so
an auditor can reconstruct the pre-redaction view (the documented design
intent at ``db/models.py``: the trust boundary is the API surface, not
the audit log). For ``credential_read``-classified ops only the *caller*
copy is scrubbed, so a raw secret can survive in ``raw_payload``.

Unlike every sibling durable store -- topology history
(:mod:`~meho_backplane.topology.history_retention`), the flight-recorder
trace tables (:mod:`~meho_backplane.flight_recorder.reaper`), sensor
evidence, memory, announcements -- ``audit_log`` had **no** retention
sweeper. The two trace/history reapers explicitly leave it alone. So
pre-redaction bodies that echo credential material accumulated
indefinitely and were present in every DB backup: a rotated secret
outlived its rotation forever in ``raw_payload`` and old backups.

This module ships the bounded age-off: an ``asyncio`` task the FastAPI
lifespan owns that ticks on a fixed cadence (default 7 days / weekly)
and **NULLs** ``raw_payload`` on rows whose ``occurred_at`` is older than
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
statement is a bounded ``UPDATE ... SET raw_payload = NULL`` rather than a
DELETE. It rides the ``audit_log_occurred_at_idx`` btree declared in
:class:`~meho_backplane.db.models.AuditLog` ``__table_args__``, and the
``raw_payload IS NOT NULL`` predicate skips rows already aged off (and this
sweeper's own audit rows, which carry no raw payload), so the working set
converges to zero once the backlog is cleared. The value is written as a
real SQL NULL via :func:`sqlalchemy.null` -- not Python ``None``, which the
JSON column's ``none_as_null=False`` default would persist as the JSON
literal ``'null'`` and which would keep matching ``IS NOT NULL`` forever
(see :func:`_null_raw_payload_older_than`).

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

Audit-row shape (one row per **non-no-op** tick)
------------------------------------------------

A tick that NULLs N rows writes exactly one
:class:`~meho_backplane.db.models.AuditLog` row via
:func:`~meho_backplane.memory.audit.write_internal_audit_row`:
``operator_sub='system:audit-raw-payload-retention'``,
``method='INTERNAL'``, ``path='audit.raw_payload.prune'``,
``status_code=200``, ``payload={"nulled_raw_payload_rows": N,
"retention_days": D, "cutoff": <iso-ts>}``, ``tenant_id`` the system
sentinel (:data:`AUDIT_RAW_PAYLOAD_SYSTEM_TENANT_ID`) -- the same
soft-FK-exploiting per-deploy sentinel the topology / flight-recorder
prunes use so operators querying by their own tenant do not see prune
rows in their timeline.

A **no-op** tick -- the ``0`` sentinel, *or* a run that nulled zero rows
(nothing past the cutoff) -- writes **no** audit row. This mirrors the
flight-recorder reaper's "empty sweep writes no row" choice rather than
topology's "always write a zero-count row": the age-off event is the
security signal worth recording; weekly ``nulled=0`` rows are noise, and
distinguishing them from a real "swept clean" outcome adds nothing.

Fail-closed contract: the audit writer raises on commit failure; the
per-tick ``try`` / ``except`` in :func:`_prune_loop` logs and continues to
the next cadence -- one bad audit write must not kill the loop. The UPDATE
has already committed before the audit row is attempted, so a lost audit
row never leaves a secret un-aged.

Per-pod leader election is deferred (Initiative-level, same as the sibling
sweepers): under N replicas the worst case is N identical bounded UPDATEs
in the same second re-nulling rows the previous winner already nulled --
idempotent (``raw_payload IS NOT NULL`` matches nothing) and below the
noise floor of normal DB load.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import null, update

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
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


def _resolve_system_tenant_id() -> uuid.UUID:
    """Return the sentinel tenant id for the age-off audit row.

    Centralised behind a function so a future migration to per-tenant
    retention can route the call site through a lookup without rewriting
    the task; v0.2 returns the fixed sentinel.
    """
    return AUDIT_RAW_PAYLOAD_SYSTEM_TENANT_ID


async def _null_raw_payload_older_than(cutoff: datetime) -> int:
    """NULL ``raw_payload`` on rows older than *cutoff*; return the row count.

    Bounded ``UPDATE ... SET raw_payload = NULL WHERE occurred_at < cutoff
    AND raw_payload IS NOT NULL``. The ``occurred_at`` predicate rides the
    ``audit_log_occurred_at_idx`` btree.

    The value is set with :func:`sqlalchemy.null` (a real **SQL NULL**), not
    Python ``None``: ``_PORTABLE_JSON`` is ``JSON().with_variant(JSONB())``
    with the default ``none_as_null=False``, so ``.values(raw_payload=None)``
    would persist the JSON literal ``'null'`` -- which is *not* SQL NULL and
    would keep matching the ``IS NOT NULL`` predicate on every future tick
    (never converging, inflating the count forever). Writing SQL NULL makes an
    aged row invisible to the next sweep, so the working set converges to zero
    as the backlog clears. (Rows that were pre-existing JSON ``'null'`` -- the
    dispatcher's error-arm no-body rows -- are normalised to SQL NULL the one
    time they cross the cutoff; harmless.)

    ``rowcount`` is typed only on the concrete ``CursorResult`` the runtime
    returns, not the abstract ``Result`` mypy infers (same
    ``type: ignore[attr-defined]`` shape the topology sweeper documents). It
    is ``None`` only for ``executemany`` batches this single-statement UPDATE
    never triggers, so ``or 0`` collapses ``int | None`` to the ``int`` the
    audit payload needs.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            update(AuditLog)
            .where(
                AuditLog.occurred_at < cutoff,
                AuditLog.raw_payload.is_not(None),
            )
            .values(raw_payload=null())
        )
        await session.commit()
        nulled: int = result.rowcount or 0  # type: ignore[attr-defined]
    return nulled


async def _write_prune_audit_row(
    *,
    nulled_rows: int,
    retention_days: int,
    cutoff: datetime,
    duration_ms: float,
) -> None:
    """Write the one summary audit row, swallowing audit-side failures.

    The UPDATE has already committed before this helper runs; an audit-write
    failure must not stall the loop. Surface it loud-but-non-fatal so a
    flapping audit substrate is visible without leaving secrets un-aged.
    """
    try:
        await write_internal_audit_row(
            operator_sub=SYSTEM_OPERATOR_SUB,
            tenant_id=_resolve_system_tenant_id(),
            method=INTERNAL_METHOD,
            path=AUDIT_RAW_PAYLOAD_PRUNE_PATH,
            status_code=200,
            duration_ms=duration_ms,
            payload={
                "nulled_raw_payload_rows": nulled_rows,
                "retention_days": retention_days,
                "cutoff": cutoff.isoformat(),
            },
        )
    except Exception:
        _log.exception(
            "audit_raw_payload_retention_audit_write_failed",
            nulled_raw_payload_rows=nulled_rows,
            retention_days=retention_days,
        )


async def _run_one_prune_tick() -> None:
    """One age-off sweep: NULL past-cutoff ``raw_payload``, audit if non-empty.

    Reads ``raw_payload_retention_days`` and computes a cutoff of
    ``now(UTC) - retention_days``. ``0`` is the keep-forever sentinel: log a
    heartbeat and return without any UPDATE or audit row. A non-sentinel tick
    that nulls zero rows also writes no audit row (empty sweep is a no-op);
    only a tick that actually aged off ≥1 row records a security-relevant
    audit row.

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
    nulled_rows = await _null_raw_payload_older_than(cutoff)
    duration_ms = (time.perf_counter() - tick_started) * 1000.0
    _log.info(
        "audit_raw_payload_retention_tick_done",
        nulled_raw_payload_rows=nulled_rows,
        retention_days=retention_days,
        cutoff=cutoff.isoformat(),
        duration_ms=duration_ms,
    )
    if nulled_rows == 0:
        # Empty sweep: nothing aged off, so no security event to record.
        # Weekly ``nulled=0`` audit rows would be pure noise (mirrors the
        # flight-recorder reaper's "empty sweep writes no row" choice).
        return
    await _write_prune_audit_row(
        nulled_rows=nulled_rows,
        retention_days=retention_days,
        cutoff=cutoff,
        duration_ms=duration_ms,
    )


async def _prune_loop() -> None:
    """The forever loop: sleep one cadence, age off, repeat.

    Order is sleep-then-prune so the first tick after process start does not
    race the rest of startup (engine init, registrars, other schedulers) --
    mirroring the sibling sweepers for the same reason. Per-tick ``try`` /
    ``except`` guards mean a transient DB blip is logged and the loop
    continues; ``asyncio.CancelledError`` propagates so lifespan shutdown can
    stop the task cleanly.
    """
    interval = get_settings().raw_payload_prune_interval_seconds
    _log.info(
        "audit_raw_payload_retention_started",
        interval_seconds=interval,
        retention_days=get_settings().raw_payload_retention_days,
    )
    while True:
        await asyncio.sleep(get_settings().raw_payload_prune_interval_seconds)
        try:
            await _run_one_prune_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "audit_raw_payload_retention_tick_failed",
                exc_info=True,
            )
        note_loop_tick(
            "audit_raw_payload_retention",
            get_settings().raw_payload_prune_interval_seconds,
        )


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
