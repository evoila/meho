# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Make a runner's results count like the results of a central check.

With ``RUNNER_CHECKS_ENABLED`` on, a result that a runner posts for one of its
own Sensors sets the state of that Sensor. The runner sends only the answer of
its operation. MEHO reads the Sensor's rule on that answer, so a runner never
chooses a state.

Steps for one result batch (see :func:`screen_batch` and
:func:`ingest_and_count`):

1. Refuse a result whose runner time (``produced_at``) is later than MEHO's
   clock. MEHO does not store it and names it in the answer, so the runner
   can log it.
2. Cap the size of each answer and of the whole batch. An answer over the cap
   is stored without its payload and the Sensor reads ``unknown`` with the
   reason ``answer too large``.
3. Store and count the results one at a time, in batch order, each in its own
   transaction. A result that was stored before is a duplicate and is never
   counted again. The answer and the error text are cleaned of secret values
   before the result is stored (:func:`~meho_backplane.runner.result_clean.clean_result`).
4. Count each new result whose ``check_ref`` is an active Sensor of this
   runner in this tenant. Any other ``check_ref`` stays stored, is not
   counted and is logged. A result without a runner time is stored, not
   counted. A result whose runner time is not later than the last counted
   one of the Sensor is dropped, also after the clean-up job removed the
   stored copy of that one.
5. In the same transaction, the Dashboard edge of the new state is claimed
   (:func:`~meho_backplane.checks.investigate.claim_transitions`). After the
   commit, its mails go out and the investigator can start. A critical
   reading followed by a recovery, in one batch or in two, sends both mails.

The time of a counted result is the time MEHO counts it, taken after the
runner's lock is held. So a batch that waited for the lock never counts a
reading at a time before a reading another batch counted. The runner time
is used only to refuse a time in the future and to drop an old result.

Each transaction first locks the runner's assignment row
(:func:`~meho_backplane.gateway.repository.lock_assignment_row`). Two batches
of one runner, and the dead-man sweeper, therefore never write the runner's
checks at the same time: the check of the runner time and its update are one
step.

A refused login on the runner (``error_code`` ``connector_auth_failed``)
counts toward the same pause rule as on a central check
(:mod:`meho_backplane.checks.auth_park`).

A runner that stops reporting is marked stale by the dead-man sweeper
(:mod:`meho_backplane.gateway.deadman`). :func:`mark_runner_sensors_unknown`
then sets the state of its Sensors to ``unknown``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.checks.assertions import AssertionOutcome, AssertionSpec
from meho_backplane.checks.auth_park import (
    AUTH_FAILED_ERROR_CODE,
    AUTH_PARK_REASON,
    count_auth_outcome,
)
from meho_backplane.checks.evaluate import evaluate_assertion
from meho_backplane.checks.events import publish_sensor_changed, publish_sensor_parked
from meho_backplane.checks.investigate import (
    TransitionClaims,
    claim_transitions,
    is_worsening,
    send_claimed_transitions,
)
from meho_backplane.checks.park_notify import ParkNotice, schedule_sensor_parked_notification
from meho_backplane.checks.repository import accelerate_sensor_next_fire, record_sensor_result
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Sensor, SensorCadenceKind, SensorStatus
from meho_backplane.gateway.repository import ingest_new_results, lock_assignment_row
from meho_backplane.metrics import RUNNER_CHECK_RESULTS_TOTAL
from meho_backplane.operations._errors import _sanitize_free_text
from meho_backplane.runner.result_clean import clean_result
from meho_backplane.runner.wire import RefusedResult, RunnerResult
from meho_backplane.scheduler.cron import next_fire_after
from meho_backplane.settings import get_settings

__all__ = [
    "BATCH_MAX_BYTES",
    "RESULT_MAX_BYTES",
    "CountedSensor",
    "ScreenedBatch",
    "after_commit",
    "ingest_and_count",
    "mark_runner_sensors_unknown",
    "screen_batch",
]

_log = structlog.get_logger(__name__)

#: Largest answer (as compact JSON) one result may carry: 256 KB.
RESULT_MAX_BYTES = 256 * 1024

#: Largest total of the answers of one batch: 4 MB.
BATCH_MAX_BYTES = 4 * 1024 * 1024

_ANSWER_TOO_LARGE = "answer too large"

_FUTURE_TIME = "future_time"


@dataclass(frozen=True, slots=True)
class CountedSensor:
    """A Sensor whose state a result just set; see :func:`after_commit`.

    ``claims`` holds the Dashboard edges claimed in the transaction that set
    the state. ``None`` until :func:`_claim` sets it.
    """

    sensor_id: uuid.UUID
    acknowledgement_expired: bool
    park_notice: ParkNotice | None = None
    claims: TransitionClaims | None = None


@dataclass(slots=True)
class ScreenedBatch:
    """The results to store, the ids of those over the size cap, and the refused ones."""

    results: list[RunnerResult]
    too_large: set[str] = field(default_factory=set)
    refused: list[RefusedResult] = field(default_factory=list)


def _count(outcome: str) -> None:
    RUNNER_CHECK_RESULTS_TOTAL.labels(outcome=outcome).inc()


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _answer_bytes(result: RunnerResult) -> int:
    if result.result is None:
        return 0
    return len(json.dumps(result.result, separators=(",", ":"), default=str).encode("utf-8"))


def screen_batch(
    results: list[RunnerResult],
    received_at: datetime,
    *,
    runner_name: str,
) -> ScreenedBatch:
    """Refuse results with a runner time in the future and cap the answer sizes.

    A result whose ``produced_at`` is later than *received_at* goes to
    :attr:`ScreenedBatch.refused` and is not stored. An answer over
    :data:`RESULT_MAX_BYTES`, or one that takes the batch over
    :data:`BATCH_MAX_BYTES`, is stored with no payload and the error text
    ``answer too large``, and its id goes to :attr:`ScreenedBatch.too_large`.
    """
    screened = ScreenedBatch(results=[])
    total = 0
    for item in results:
        if item.produced_at is not None and _as_utc(item.produced_at) > received_at:
            _count(_FUTURE_TIME)
            _log.warning(
                "runner_result_refused_future_time",
                runner=runner_name,
                check_ref=item.check_ref,
                result_uid=item.result_uid,
            )
            screened.refused.append(RefusedResult(result_uid=item.result_uid, reason=_FUTURE_TIME))
            continue
        size = _answer_bytes(item)
        if size > RESULT_MAX_BYTES or total + size > BATCH_MAX_BYTES:
            _log.warning(
                "runner_result_too_large",
                runner=runner_name,
                check_ref=item.check_ref,
                result_uid=item.result_uid,
                size_bytes=size,
            )
            screened.too_large.add(item.result_uid)
            screened.results.append(
                item.model_copy(update={"result": None, "error": _ANSWER_TOO_LARGE})
            )
            continue
        total += size
        screened.results.append(item)
    return screened


def _unknown(reason: str, **extra: object) -> AssertionOutcome:
    return AssertionOutcome(state="unknown", value=None, evidence={"reason": reason, **extra})


def _outcome_for(
    sensor: Sensor,
    item: RunnerResult,
    *,
    too_large: bool,
    received_at: datetime,
) -> AssertionOutcome:
    """Read the Sensor's rule on the runner's answer. Never raises.

    *item* was cleaned of secret values before it was stored
    (:func:`_cleaned`), so the rule reads the same view as the rule of a
    central check.
    """
    if too_large:
        return _unknown(_ANSWER_TOO_LARGE, max_bytes=RESULT_MAX_BYTES)
    if item.status != "ok" or item.result is None:
        extra: dict[str, object] = {"runner_error": item.error or ""}
        if item.error_code == AUTH_FAILED_ERROR_CODE:
            extra["error_code"] = AUTH_FAILED_ERROR_CODE
        return _unknown(f"runner_{item.status}", **extra)
    try:
        spec = AssertionSpec.model_validate(sensor.assertion)
        return evaluate_assertion(spec, item.result, now=received_at)
    except Exception as exc:
        return _unknown("assertion_evaluation_error", error=_sanitize_free_text(str(exc)))


def _cleaned(item: RunnerResult, sensor: Sensor | None, tenant_id: uuid.UUID) -> RunnerResult:
    """*item* with secret values taken out of its answer and error text."""
    return clean_result(
        item,
        connector_id=sensor.connector_id if sensor is not None else None,
        tenant=str(tenant_id),
    )


async def _advance_next_fire(session: AsyncSession, sensor: Sensor, evaluated_at: datetime) -> None:
    """Move the next due time after *evaluated_at*; the runner reads it from its list.

    While a state change waits for its confirming readings, the next due
    time is at most ``retry_backoff_seconds`` away, like in the central
    check loop.
    """
    if sensor.cadence_kind == SensorCadenceKind.CRON.value and sensor.cron_expr:
        sensor.next_fire_at = next_fire_after(sensor.cron_expr, evaluated_at, sensor.timezone)
    elif sensor.interval_seconds is not None:
        sensor.next_fire_at = evaluated_at + timedelta(seconds=sensor.interval_seconds)
    if sensor.pending_state is not None:
        await session.flush()
        await accelerate_sensor_next_fire(
            session,
            sensor.id,
            not_later_than=evaluated_at + timedelta(seconds=sensor.retry_backoff_seconds),
        )
        await session.refresh(sensor, attribute_names=["next_fire_at"])


async def _runner_sensor(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    runner_name: str,
    item: RunnerResult,
) -> Sensor | None:
    """The active Sensor of this runner in this tenant that *item* names, else ``None``.

    Read fresh from the database (not from the session's cache), after the
    caller locked the runner's assignment row.
    """
    try:
        sensor_id = uuid.UUID(item.check_ref)
    except ValueError:
        return None
    sensor = await session.scalar(
        select(Sensor)
        .where(
            Sensor.id == sensor_id,
            Sensor.tenant_id == tenant_id,
            Sensor.runner_name == runner_name,
            Sensor.status == SensorStatus.ACTIVE.value,
        )
        .execution_options(populate_existing=True)
    )
    if sensor is None or sensor.op_id != item.op_id:
        return None
    return sensor


async def _record(
    session: AsyncSession,
    sensor: Sensor,
    outcome: AssertionOutcome,
    evaluated_at: datetime,
) -> CountedSensor:
    """Record one outcome on *sensor*, like the central check loop does."""
    settings = get_settings()
    previous_state = sensor.last_state
    recorded = await record_sensor_result(
        session,
        sensor_id=sensor.id,
        state=outcome.state,
        value=outcome.value,
        evidence=outcome.evidence,
        evaluated_at=evaluated_at,
        record_history=settings.checks_evidence_retention_days > 0,
    )
    await _advance_next_fire(session, sensor, evaluated_at)
    if settings.checks_agent_events and recorded.changed:
        await publish_sensor_changed(
            session,
            tenant_id=sensor.tenant_id,
            sensor_id=sensor.id,
            sensor_name=sensor.name,
            previous_state=previous_state or "unknown",
            state=sensor.last_state,
            severity=sensor.severity,
            worsening=is_worsening(previous_state or "unknown", sensor.last_state),
            target=sensor.target,
            connector_id=sensor.connector_id,
            op_id=sensor.op_id,
            evaluated_at=evaluated_at,
        )
    return CountedSensor(
        sensor_id=sensor.id,
        acknowledgement_expired=recorded.acknowledgement_ended == "expired",
    )


async def _count_auth(
    session: AsyncSession,
    sensor: Sensor,
    item: RunnerResult,
    evaluated_at: datetime,
) -> ParkNotice | None:
    """Count a refused login on the runner. Pause the Sensor at the limit.

    The rule of a central check (:func:`~meho_backplane.checks.auth_park.count_auth_outcome`):
    a refused login adds one, a result whose call succeeded sets the count
    back to 0. Returns the warning mail to send after the commit, or ``None``.
    """
    settings = get_settings()
    parked = await count_auth_outcome(
        session,
        sensor.id,
        auth_failed=item.error_code == AUTH_FAILED_ERROR_CODE,
        dispatch_ok=item.status == "ok",
        threshold=settings.sensor_auth_failure_park_threshold,
    )
    if not parked:
        return None
    _log.warning("sensor_paused", sensor_id=str(sensor.id), reason=AUTH_PARK_REASON)
    if settings.checks_agent_events:
        await publish_sensor_parked(
            session,
            tenant_id=sensor.tenant_id,
            sensor_id=sensor.id,
            sensor_name=sensor.name,
            target=sensor.target,
            connector_id=sensor.connector_id,
            op_id=sensor.op_id,
            parked_at=evaluated_at,
        )
    return ParkNotice(
        tenant_id=sensor.tenant_id,
        sensor_id=sensor.id,
        sensor_name=sensor.name,
        target=sensor.target,
        connector_id=sensor.connector_id,
        op_id=sensor.op_id,
        parked_at=evaluated_at,
    )


async def _claim(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    counted: CountedSensor,
    now: datetime,
) -> CountedSensor:
    """Claim the Dashboard edges of *counted* in the open transaction.

    A failed claim raises, so the whole transaction rolls back: the state,
    the stored result and the claims of the other Dashboards. The runner
    then sends the result again, and the next sweep marks a stale runner
    again.
    """
    claims = await claim_transitions(
        session,
        sensor_id=counted.sensor_id,
        tenant_id=tenant_id,
        now=now,
        acknowledgement_expired=counted.acknowledgement_expired,
    )
    return replace(counted, claims=claims)


def _evaluation_time(sensor: Sensor, now: datetime) -> datetime:
    """*now*, or just after the Sensor's last counted time when *now* is not later."""
    last = sensor.last_evaluated_at
    if last is not None and now <= _as_utc(last):
        return _as_utc(last) + timedelta(microseconds=1)
    return now


async def _count_one(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    runner_name: str,
    sensor: Sensor | None,
    item: RunnerResult,
    too_large: bool,
    received_at: datetime,
    evaluated_at: datetime,
) -> CountedSensor | None:
    """Count one newly stored result, or return ``None`` when it does not count.

    *sensor* is the Sensor of this runner that *item* names
    (:func:`_runner_sensor`), read after the runner's lock was taken.
    *evaluated_at* is the time taken under that lock; it is moved just
    after the Sensor's last counted time when it is not later.
    """
    if sensor is None:
        _count("not_in_list")
        _log.warning(
            "runner_result_not_in_list",
            runner=runner_name,
            check_ref=item.check_ref,
            result_uid=item.result_uid,
        )
        return None
    if item.produced_at is None:
        # Without a runner time MEHO cannot tell a new result from a replay.
        _count("no_time")
        _log.warning(
            "runner_result_without_time",
            runner=runner_name,
            check_ref=item.check_ref,
            result_uid=item.result_uid,
        )
        return None
    produced_at = _as_utc(item.produced_at)
    if sensor.runner_produced_at is not None and produced_at <= _as_utc(sensor.runner_produced_at):
        _count("old")
        return None
    outcome = _outcome_for(sensor, item, too_large=too_large, received_at=received_at)
    _count("too_large" if too_large else "counted")
    sensor.runner_produced_at = produced_at
    evaluated_at = _evaluation_time(sensor, evaluated_at)
    counted = await _record(session, sensor, outcome, evaluated_at)
    notice = await _count_auth(session, sensor, item, evaluated_at)
    if notice is not None:
        counted = replace(counted, park_notice=notice)
    return await _claim(session, tenant_id=tenant_id, counted=counted, now=evaluated_at)


async def ingest_and_count(
    *,
    tenant_id: uuid.UUID,
    runner_name: str,
    screened: ScreenedBatch,
    received_at: datetime,
) -> tuple[int, int]:
    """Store and count *screened* one result at a time; return ``(accepted, duplicates)``.

    Each result gets its own transaction: lock the runner's assignment row,
    take the time of the count, clean the result, store it, count it, claim
    its Dashboard edge, commit. Then :func:`after_commit` runs for it. When a
    result fails, the ones before it stay stored and counted, and a re-post
    of the batch skips them as duplicates.
    """
    sessionmaker = get_sessionmaker()
    accepted = 0
    for item in screened.results:
        counted: CountedSensor | None = None
        async with sessionmaker() as session:
            await lock_assignment_row(session, tenant_id=tenant_id, runner_name=runner_name)
            # Taken under the lock: a batch that waited for it never counts a
            # reading at a time before one another batch already counted.
            evaluated_at = datetime.now(UTC)
            sensor = await _runner_sensor(
                session, tenant_id=tenant_id, runner_name=runner_name, item=item
            )
            cleaned = _cleaned(item, sensor, tenant_id)
            stored = await ingest_new_results(
                session, tenant_id=tenant_id, runner_name=runner_name, results=[cleaned]
            )
            if stored:
                accepted += 1
                counted = await _count_one(
                    session,
                    tenant_id=tenant_id,
                    runner_name=runner_name,
                    sensor=sensor,
                    item=cleaned,
                    too_large=item.result_uid in screened.too_large,
                    received_at=received_at,
                    evaluated_at=evaluated_at,
                )
            await session.commit()
        if counted is not None:
            await after_commit(tenant_id, [counted])
    return accepted, len(screened.results) - accepted


async def mark_runner_sensors_unknown(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    runner_name: str,
    now: datetime,
) -> list[CountedSensor]:
    """Set the state of every active Sensor of a stale runner to ``unknown``.

    A Sensor that confirms a change only after a few readings gets as many
    ``unknown`` readings, so a stale runner never leaves a check green. The
    Dashboard edge of each Sensor is claimed in the same transaction. Does
    not commit. The caller holds the lock of the runner's assignment row.
    """
    sensors = (
        (
            await session.execute(
                select(Sensor).where(
                    Sensor.tenant_id == tenant_id,
                    Sensor.runner_name == runner_name,
                    Sensor.status == SensorStatus.ACTIVE.value,
                )
            )
        )
        .scalars()
        .all()
    )
    marked: list[CountedSensor] = []
    outcome = _unknown("runner_stale", runner=runner_name)
    for sensor in sensors:
        last: CountedSensor | None = None
        evaluated_at = now
        for step in range(sensor.retry_times + 1):
            evaluated_at = _evaluation_time(sensor, now + timedelta(microseconds=step))
            last = await _record(session, sensor, outcome, evaluated_at)
        if last is not None:
            marked.append(
                await _claim(session, tenant_id=tenant_id, counted=last, now=evaluated_at)
            )
    return marked


async def after_commit(tenant_id: uuid.UUID, counted: list[CountedSensor]) -> None:
    """Send the pause mails and the claimed Dashboard mails; start the investigator.

    Call after the transaction that set the states has committed. Never raises.
    """
    for entry in counted:
        if entry.park_notice is not None:
            schedule_sensor_parked_notification(entry.park_notice)
        if entry.claims is None:
            continue
        try:
            await send_claimed_transitions(tenant_id, entry.claims)
        except Exception:
            _log.warning(
                "runner_check_transition_send_failed",
                sensor_id=str(entry.sensor_id),
                exc_info=True,
            )
