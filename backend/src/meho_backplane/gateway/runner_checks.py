# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Make a runner's results count like the results of a central check.

With ``RUNNER_CHECKS_ENABLED`` on, a result that a runner posts for one of its
own Sensors sets the state of that Sensor. The runner sends only the answer of
its operation. MEHO reads the Sensor's rule on that answer, so a runner never
chooses a state.

Steps for one result batch (see :func:`screen_batch` and
:func:`count_results`):

1. Refuse a result whose runner time is in the future.
2. Cap the size of each answer and of the whole batch. An answer over the cap
   is stored without its payload and the Sensor reads ``unknown`` with the
   reason ``answer too large``.
3. Store the results (:func:`~meho_backplane.gateway.repository.ingest_new_results`).
   A result that was stored before is a duplicate and is never counted again.
4. Count each new result whose ``check_ref`` is an active Sensor of this
   runner in this tenant. Any other ``check_ref`` stays stored, is not
   counted and is logged.

The time of a counted result is the time MEHO received it. The runner time is
used only to refuse a time in the future and to drop a result made before the
last counted one.

A runner that stops reporting is marked stale by the dead-man sweeper
(:mod:`meho_backplane.gateway.deadman`). :func:`mark_runner_sensors_unknown`
then sets the state of its Sensors to ``unknown``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.checks.assertions import AssertionOutcome, AssertionSpec
from meho_backplane.checks.evaluate import evaluate_assertion
from meho_backplane.checks.events import publish_sensor_changed
from meho_backplane.checks.investigate import investigate_on_transition, is_worsening
from meho_backplane.checks.repository import record_sensor_result
from meho_backplane.db.models import Sensor, SensorCadenceKind, SensorStatus
from meho_backplane.metrics import RUNNER_CHECK_RESULTS_TOTAL
from meho_backplane.redaction.middleware import apply_connector_boundary_redaction
from meho_backplane.runner.wire import RunnerResult
from meho_backplane.scheduler.cron import next_fire_after
from meho_backplane.settings import get_settings

__all__ = [
    "BATCH_MAX_BYTES",
    "FUTURE_SKEW_SECONDS",
    "RESULT_MAX_BYTES",
    "CountedSensor",
    "ScreenedBatch",
    "count_results",
    "investigate_counted",
    "mark_runner_sensors_unknown",
    "screen_batch",
]

_log = structlog.get_logger(__name__)

#: Largest answer (as compact JSON) one result may carry: 256 KB.
RESULT_MAX_BYTES = 256 * 1024

#: Largest total of the answers of one batch: 4 MB.
BATCH_MAX_BYTES = 4 * 1024 * 1024

#: How far a runner time may be ahead of the central clock.
FUTURE_SKEW_SECONDS = 60

#: Longest runner error text that goes into the evidence of a Sensor.
_ERROR_TEXT_MAX = 500

_ANSWER_TOO_LARGE = "answer too large"


@dataclass(frozen=True, slots=True)
class CountedSensor:
    """A Sensor whose state a result just set; the caller starts the investigator for it."""

    sensor_id: uuid.UUID
    acknowledgement_expired: bool


@dataclass(slots=True)
class ScreenedBatch:
    """The results to store, and the ids of those whose answer was over the cap."""

    results: list[RunnerResult]
    too_large: set[str] = field(default_factory=set)


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
    """Drop results with a runner time in the future and cap the answer sizes.

    An answer over :data:`RESULT_MAX_BYTES`, or one that takes the batch over
    :data:`BATCH_MAX_BYTES`, is stored with no payload and the error text
    ``answer too large``, and its id goes to :attr:`ScreenedBatch.too_large`.
    """
    latest = received_at + timedelta(seconds=FUTURE_SKEW_SECONDS)
    kept: list[RunnerResult] = []
    too_large: set[str] = set()
    total = 0
    for item in results:
        if item.produced_at is not None and _as_utc(item.produced_at) > latest:
            _count("future_time")
            _log.warning(
                "runner_result_refused_future_time",
                runner=runner_name,
                check_ref=item.check_ref,
                result_uid=item.result_uid,
            )
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
            too_large.add(item.result_uid)
            kept.append(item.model_copy(update={"result": None, "error": _ANSWER_TOO_LARGE}))
            continue
        total += size
        kept.append(item)
    return ScreenedBatch(results=kept, too_large=too_large)


def _unknown(reason: str, **extra: object) -> AssertionOutcome:
    return AssertionOutcome(state="unknown", value=None, evidence={"reason": reason, **extra})


def _outcome_for(
    sensor: Sensor,
    item: RunnerResult,
    *,
    too_large: bool,
    received_at: datetime,
) -> AssertionOutcome:
    """Read the Sensor's rule on the runner's answer. Never raises."""
    if too_large:
        return _unknown(_ANSWER_TOO_LARGE, max_bytes=RESULT_MAX_BYTES)
    if item.status != "ok" or item.result is None:
        return _unknown(
            f"runner_{item.status}",
            runner_error=(item.error or "")[:_ERROR_TEXT_MAX],
        )
    try:
        redacted = apply_connector_boundary_redaction(
            item.result,
            connector_id=sensor.connector_id,
            tenant=str(sensor.tenant_id),
            op=sensor.op_id,
        ).redacted
        spec = AssertionSpec.model_validate(sensor.assertion)
        return evaluate_assertion(spec, redacted, now=received_at)
    except Exception as exc:
        return _unknown("assertion_evaluation_error", error=str(exc))


def _advance_next_fire(sensor: Sensor, evaluated_at: datetime) -> None:
    """Move the next due time after *evaluated_at*, so the dashboard sees a live check."""
    if sensor.cadence_kind == SensorCadenceKind.CRON.value and sensor.cron_expr:
        sensor.next_fire_at = next_fire_after(sensor.cron_expr, evaluated_at, sensor.timezone)
    elif sensor.interval_seconds is not None:
        sensor.next_fire_at = evaluated_at + timedelta(seconds=sensor.interval_seconds)


async def _runner_sensor(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    runner_name: str,
    item: RunnerResult,
) -> Sensor | None:
    """The active Sensor of this runner in this tenant that *item* names, else ``None``."""
    try:
        sensor_id = uuid.UUID(item.check_ref)
    except ValueError:
        return None
    sensor = await session.scalar(
        select(Sensor).where(
            Sensor.id == sensor_id,
            Sensor.tenant_id == tenant_id,
            Sensor.runner_name == runner_name,
            Sensor.status == SensorStatus.ACTIVE.value,
        )
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
    _advance_next_fire(sensor, evaluated_at)
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


async def count_results(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    runner_name: str,
    stored: list[RunnerResult],
    too_large: set[str],
    received_at: datetime,
) -> list[CountedSensor]:
    """Count the newly stored results that name an active Sensor of this runner.

    *stored* holds only results that were new in this batch, so a re-posted
    result is never counted twice. Does not commit. The caller commits, then
    calls :func:`investigate_counted`.
    """
    counted: list[CountedSensor] = []
    for index, item in enumerate(stored):
        sensor = await _runner_sensor(
            session, tenant_id=tenant_id, runner_name=runner_name, item=item
        )
        if sensor is None:
            _count("not_in_list")
            _log.warning(
                "runner_result_not_in_list",
                runner=runner_name,
                check_ref=item.check_ref,
                result_uid=item.result_uid,
            )
            continue
        # Compare runner time only with runner time: the produced_at of the
        # last result counted for this check. Results of one check in a batch
        # keep their order, so the newest one wins.
        if (
            item.produced_at is not None
            and sensor.runner_produced_at is not None
            and _as_utc(item.produced_at) <= _as_utc(sensor.runner_produced_at)
        ):
            _count("old")
            continue
        outcome = _outcome_for(
            sensor,
            item,
            too_large=item.result_uid in too_large,
            received_at=received_at,
        )
        if item.result_uid in too_large:
            _count("too_large")
        else:
            _count("counted")
        if item.produced_at is not None:
            sensor.runner_produced_at = _as_utc(item.produced_at)
        # Several results of one Sensor in a batch get times a microsecond
        # apart, so the guard against a repeat keeps each of them.
        counted.append(
            await _record(session, sensor, outcome, received_at + timedelta(microseconds=index))
        )
    return counted


async def mark_runner_sensors_unknown(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    runner_name: str,
    now: datetime,
) -> list[CountedSensor]:
    """Set the state of every active Sensor of a stale runner to ``unknown``.

    A Sensor that confirms a change only after a few readings gets as many
    ``unknown`` readings, so a stale runner never leaves a check green. Does
    not commit.
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
        for step in range(sensor.retry_times + 1):
            last = await _record(session, sensor, outcome, now + timedelta(microseconds=step))
        if last is not None:
            marked.append(last)
    return marked


async def investigate_counted(tenant_id: uuid.UUID, counted: list[CountedSensor]) -> None:
    """Hand each counted Sensor to the transition detector (mails, investigator).

    Call after the transaction that set the states has committed. The
    detector never raises.
    """
    for entry in counted:
        await investigate_on_transition(
            sensor_id=entry.sensor_id,
            tenant_id=tenant_id,
            acknowledgement_expired=entry.acknowledgement_expired,
        )
