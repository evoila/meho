# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Make a runner's results count like the results of a central check.

With ``RUNNER_CHECKS_ENABLED`` on, a result that a runner posts for one of its
own Sensors sets the state of that Sensor. The runner sends only the answer of
its operation. MEHO reads the Sensor's rule on that answer, so a runner never
chooses a state.

Steps for one result batch (see :func:`screen_batch` and
:func:`ingest_and_count`):

1. Refuse a result whose runner time (``produced_at``) is more than
   :data:`CLOCK_ALLOWANCE` later than MEHO's clock. MEHO does not store it
   and names it in the answer. When every result of a batch is refused, the
   answer is a 422, so the runner knows the batch was refused. The counter
   ``runner_check_results_total`` rises for a refused result once the rest
   of the batch has been stored. A refused result is counted once in each
   MEHO process, however often its batch is sent.
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
import re
import uuid
from collections import OrderedDict
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
from meho_backplane.db.models import (
    RunnerAssignmentRow,
    Sensor,
    SensorCadenceKind,
    SensorStatus,
)
from meho_backplane.gateway.repository import ingest_new_results, lock_assignment_row
from meho_backplane.metrics import RUNNER_CHECK_RESULTS_TOTAL
from meho_backplane.operations._errors import _sanitize_free_text
from meho_backplane.runner.result_clean import clean_result
from meho_backplane.runner.wire import (
    SET_ASIDE_NOTICES_MAX,
    RefusedResult,
    RunnerResult,
    SetAsideNotice,
)
from meho_backplane.scheduler.cron import next_fire_after
from meho_backplane.settings import get_settings

__all__ = [
    "BATCH_MAX_BYTES",
    "CLOCK_ALLOWANCE",
    "RESULT_MAX_BYTES",
    "CountedSensor",
    "ScreenedBatch",
    "SetAsideNotSavedError",
    "after_commit",
    "ingest_and_count",
    "mark_left_off_sensors",
    "mark_runner_sensors_unknown",
    "mark_sensor_unknown",
    "record_set_aside",
    "screen_batch",
]

_log = structlog.get_logger(__name__)

#: Largest answer (as compact JSON) one result may carry: 256 KB.
RESULT_MAX_BYTES = 256 * 1024

#: Largest total of the answers of one batch: 4 MB.
BATCH_MAX_BYTES = 4 * 1024 * 1024

_ANSWER_TOO_LARGE = "answer too large"

_FUTURE_TIME = "future_time"

#: How many refused results this process remembers, so that one refused
#: result is counted once in this MEHO process however often its batch is sent.
_REFUSED_SEEN_MAX = 10_000
_refused_seen: OrderedDict[tuple[uuid.UUID, str, str], None] = OrderedDict()


def _count_refused_once(tenant_id: uuid.UUID, runner_name: str, result_uid: str) -> None:
    """Raise the counter for a refused result the first time it is seen."""
    key = (tenant_id, runner_name, result_uid)
    if key in _refused_seen:
        return
    _refused_seen[key] = None
    while len(_refused_seen) > _REFUSED_SEEN_MAX:
        _refused_seen.popitem(last=False)
    _count(_FUTURE_TIME)


#: How far a result's runner time may be ahead of MEHO's clock: 30 seconds.
#: Two clocks that are set by network time still differ by a few seconds, and
#: a runner whose clock is a little ahead must not lose its results. A larger
#: difference means the runner clock is wrong, and MEHO refuses the result.
CLOCK_ALLOWANCE = timedelta(seconds=30)


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

    A result whose ``produced_at`` is later than *received_at* plus
    :data:`CLOCK_ALLOWANCE` goes to
    :attr:`ScreenedBatch.refused` and is not stored. An answer over
    :data:`RESULT_MAX_BYTES`, or one that takes the batch over
    :data:`BATCH_MAX_BYTES`, is stored with no payload and the error text
    ``answer too large``, and its id goes to :attr:`ScreenedBatch.too_large`.
    """
    screened = ScreenedBatch(results=[])
    total = 0
    for item in results:
        if (
            item.produced_at is not None
            and _as_utc(item.produced_at) > received_at + CLOCK_ALLOWANCE
        ):
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
    *,
    advance_due_time: bool = True,
) -> CountedSensor:
    """Record one outcome on *sensor*, like the central check loop does.

    Without *advance_due_time* the next due time stays: the runner did not
    run the check, so it keeps its scheduled run.
    """
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
    if advance_due_time:
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
) -> tuple[str, CountedSensor | None]:
    """Count one newly stored result; return its outcome for the counter and the Sensor.

    The Sensor is ``None`` when the result does not count. The caller adds the
    outcome to ``runner_check_results_total`` once the transaction commits, so
    a result whose transaction fails and is sent again is counted once.

    *sensor* is the Sensor of this runner that *item* names
    (:func:`_runner_sensor`), read after the runner's lock was taken.
    *evaluated_at* is the time taken under that lock; it is moved just
    after the Sensor's last counted time when it is not later.
    """
    if sensor is None:
        _log.warning(
            "runner_result_not_in_list",
            runner=runner_name,
            check_ref=item.check_ref,
            result_uid=item.result_uid,
        )
        return "not_in_list", None
    if item.produced_at is None:
        # Without a runner time MEHO cannot tell a new result from a replay.
        _log.warning(
            "runner_result_without_time",
            runner=runner_name,
            check_ref=item.check_ref,
            result_uid=item.result_uid,
        )
        return "no_time", None
    produced_at = _as_utc(item.produced_at)
    if sensor.runner_produced_at is not None and produced_at <= _as_utc(sensor.runner_produced_at):
        return "old", None
    outcome = _outcome_for(sensor, item, too_large=too_large, received_at=received_at)
    sensor.runner_produced_at = produced_at
    evaluated_at = _evaluation_time(sensor, evaluated_at)
    counted = await _record(session, sensor, outcome, evaluated_at)
    notice = await _count_auth(session, sensor, item, evaluated_at)
    if notice is not None:
        counted = replace(counted, park_notice=notice)
    claimed = await _claim(session, tenant_id=tenant_id, counted=counted, now=evaluated_at)
    return ("too_large" if too_large else "counted"), claimed


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
    its Dashboard edge, commit. Then the counter rises and :func:`after_commit`
    runs for it. When a result fails, the ones before it stay stored and
    counted, and a re-post of the batch skips them as duplicates. The refused
    results of *screened* are counted after the last result is stored.
    """
    sessionmaker = get_sessionmaker()
    accepted = 0
    for item in screened.results:
        counted: CountedSensor | None = None
        label: str | None = None
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
                label, counted = await _count_one(
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
        if label is not None:
            _count(label)
        if counted is not None:
            await after_commit(tenant_id, [counted])
    for refused in screened.refused:
        _count_refused_once(tenant_id, runner_name, refused.result_uid)
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
    next due time of each Sensor stays, so a runner that comes back runs
    the check at its scheduled time. The Dashboard edge of each Sensor is
    claimed in the same transaction. Does not commit. The caller holds the
    lock of the runner's assignment row.
    """
    sensors = (
        (
            await session.execute(
                select(Sensor)
                .where(
                    Sensor.tenant_id == tenant_id,
                    Sensor.runner_name == runner_name,
                    Sensor.status == SensorStatus.ACTIVE.value,
                )
                .order_by(Sensor.id)
            )
        )
        .scalars()
        .all()
    )
    marked: list[CountedSensor] = []
    for sensor in sensors:
        marked.append(
            await mark_sensor_unknown(
                session, sensor, now=now, reason="runner_stale", runner=runner_name
            )
        )
    return marked


async def mark_sensor_unknown(
    session: AsyncSession,
    sensor: Sensor,
    *,
    now: datetime,
    reason: str,
    **extra: object,
) -> CountedSensor:
    """Record ``unknown`` with *reason* on *sensor* and claim its Dashboard edge.

    A Sensor that confirms a change only after a few readings gets as many
    ``unknown`` readings (``retry_times`` + 1). The next due time stays.
    Does not commit. The caller holds the lock of the runner's assignment row
    and sends the mails with :func:`after_commit` after the commit.
    """
    outcome = _unknown(reason, **extra)
    last: CountedSensor | None = None
    evaluated_at = now
    for step in range(sensor.retry_times + 1):
        evaluated_at = _evaluation_time(sensor, now + timedelta(microseconds=step))
        last = await _record(session, sensor, outcome, evaluated_at, advance_due_time=False)
    assert last is not None
    return await _claim(session, tenant_id=sensor.tenant_id, counted=last, now=evaluated_at)


_NOT_ON_LIST = "not_on_runner_list"


def _already_marked(sensor: Sensor, why: str) -> bool:
    """True when the last reading of *sensor* already says it is off the runner's list for *why*."""
    evidence = sensor.last_evidence or {}
    return evidence.get("reason") == _NOT_ON_LIST and evidence.get("why") == why


async def mark_left_off_sensors(
    *,
    tenant_id: uuid.UUID,
    runner_name: str,
    left_off: dict[uuid.UUID, str],
) -> None:
    """Record ``unknown`` on the bound Sensors that MEHO left off the runner's list.

    *left_off* maps a Sensor id to the reason. A Sensor whose last reading
    already names the same reason is not written again, so the record and
    the notice happen once for each change of reason. The Dashboard edge of
    each written Sensor is claimed in the same transaction, so the Dashboard
    mail goes out also when every check of the Dashboard runs on this runner.
    A failure is logged and never reaches the runner's poll.
    """
    if not left_off:
        return
    try:
        counted: list[CountedSensor] = []
        async with get_sessionmaker()() as session:
            await lock_assignment_row(session, tenant_id=tenant_id, runner_name=runner_name)
            now = datetime.now(UTC)
            sensors = (
                (
                    await session.execute(
                        select(Sensor)
                        .where(
                            Sensor.id.in_(list(left_off)),
                            Sensor.tenant_id == tenant_id,
                            Sensor.runner_name == runner_name,
                            Sensor.status == SensorStatus.ACTIVE.value,
                        )
                        .order_by(Sensor.id)
                        .execution_options(populate_existing=True)
                    )
                )
                .scalars()
                .all()
            )
            for sensor in sensors:
                why = left_off[sensor.id]
                if _already_marked(sensor, why):
                    continue
                counted.append(
                    await mark_sensor_unknown(
                        session,
                        sensor,
                        now=now,
                        reason=_NOT_ON_LIST,
                        why=why,
                        runner=runner_name,
                    )
                )
            await session.commit()
        await after_commit(tenant_id, counted)
    except Exception:
        _log.warning("runner_left_off_mark_failed", runner=runner_name, exc_info=True)


_RESULT_REFUSED = "result_refused"

#: The form of a result id and a code in a set-aside notice: 1 to 64 lowercase
#: letters, digits and underscores. The runner keeps a code in the same form.
_NOTICE_ID = re.compile(r"[a-z0-9_]{1,64}")


def _kept_notice(notice: SetAsideNotice) -> SetAsideNotice | None:
    """*notice* with only the values MEHO keeps, or ``None`` to skip it.

    A notice whose ``result_uid`` is not in the short form is skipped. A code
    in another form is kept as no code, and a status outside 400 to 499 as 0.
    """
    if not _NOTICE_ID.fullmatch(notice.result_uid):
        return None
    code = notice.code if notice.code and _NOTICE_ID.fullmatch(notice.code) else None
    status = notice.status if 400 <= notice.status <= 499 else 0
    return notice.model_copy(update={"code": code, "status": status})


#: The largest set-aside count the integer column holds.
_SET_ASIDE_COUNT_MAX = 2_147_483_647


class SetAsideNotSavedError(Exception):
    """The set-aside notices of a post were not saved; nothing of them is kept."""


def _first_notices(runner_name: str, notices: list[SetAsideNotice]) -> list[SetAsideNotice]:
    """The first :data:`SET_ASIDE_NOTICES_MAX` notices of one post; the rest is skipped."""
    skipped = len(notices) - SET_ASIDE_NOTICES_MAX
    if skipped > 0:
        _log.warning("runner_set_aside_notices_skipped", runner=runner_name, skipped=skipped)
    return notices[:SET_ASIDE_NOTICES_MAX]


def _kept_time(value: datetime | None) -> datetime | None:
    """*value* in UTC, or ``None`` without a time zone or when UTC cannot hold it."""
    if value is None or value.utcoffset() is None:
        return None
    try:
        return value.astimezone(UTC)
    except OverflowError:
        return None


def _keep_summary(row: RunnerAssignmentRow, total: int | None, last_at: datetime | None) -> None:
    """Store the runner's count and time on *row*; a value outside the limits changes nothing."""
    if total is not None and 0 <= total <= _SET_ASIDE_COUNT_MAX:
        row.set_aside_count = total
    kept_at = _kept_time(last_at)
    if kept_at is not None:
        row.set_aside_last_at = kept_at


async def record_set_aside(
    *,
    tenant_id: uuid.UUID,
    runner_name: str,
    notices: list[SetAsideNotice],
    total: int | None,
    last_at: datetime | None,
) -> None:
    """Show the checks of set-aside results as ``unknown``, and keep the runner's count.

    Each notice names a result of a batch that MEHO refused on every try. The
    check of that result reads ``unknown`` with the reason ``result_refused``,
    the HTTP status and the code (no free text). Only short, fixed forms of
    these values are kept (:func:`_kept_notice`); another value never makes
    MEHO refuse the post. A check that already has a newer counted result of
    the runner, or already shows this refusal, is not written again. A notice
    without a runner time never replaces a counted result. Only the first
    :data:`SET_ASIDE_NOTICES_MAX` notices of one post are read. The count and
    the time of the newest set-aside batch are stored on the runner's assignment
    row, the count only from 0 to 2147483647 and the time only with a time zone.

    Everything is saved in one transaction. When the save fails, nothing is
    kept, no mail goes out, the failure is logged and
    :class:`SetAsideNotSavedError` is raised. The endpoint then answers 503
    with the code ``set_aside_not_saved``, and the runner keeps its notices and
    sends them again.
    """
    notices = _first_notices(runner_name, notices)
    try:
        counted: list[CountedSensor] = []
        async with get_sessionmaker()() as session:
            await lock_assignment_row(session, tenant_id=tenant_id, runner_name=runner_name)
            now = datetime.now(UTC)
            row = await session.scalar(
                select(RunnerAssignmentRow)
                .where(
                    RunnerAssignmentRow.tenant_id == tenant_id,
                    RunnerAssignmentRow.runner_name == runner_name,
                )
                .execution_options(populate_existing=True)
            )
            if row is not None:
                _keep_summary(row, total, last_at)
            kept = [n for n in map(_kept_notice, notices) if n is not None]
            for notice in sorted(kept, key=lambda n: n.check_ref):
                sensor = await _runner_sensor_by_ref(session, tenant_id, runner_name, notice)
                if sensor is None or _refusal_shown(sensor, notice):
                    continue
                counted.append(
                    await mark_sensor_unknown(
                        session,
                        sensor,
                        now=now,
                        reason=_RESULT_REFUSED,
                        result_uid=notice.result_uid,
                        http_status=notice.status,
                        code=notice.code,
                    )
                )
            await session.commit()
    except Exception as exc:
        _log.warning("runner_set_aside_record_failed", runner=runner_name, exc_info=True)
        raise SetAsideNotSavedError from exc
    await after_commit(tenant_id, counted)


async def _runner_sensor_by_ref(
    session: AsyncSession, tenant_id: uuid.UUID, runner_name: str, notice: SetAsideNotice
) -> Sensor | None:
    """The active Sensor of this runner that *notice* names, else ``None``."""
    try:
        sensor_id = uuid.UUID(notice.check_ref)
    except ValueError:
        return None
    sensor: Sensor | None = await session.scalar(
        select(Sensor)
        .where(
            Sensor.id == sensor_id,
            Sensor.tenant_id == tenant_id,
            Sensor.runner_name == runner_name,
            Sensor.status == SensorStatus.ACTIVE.value,
        )
        .execution_options(populate_existing=True)
    )
    return sensor


def _refusal_shown(sensor: Sensor, notice: SetAsideNotice) -> bool:
    """True when *sensor* already shows this refusal, or has a newer result of the runner.

    A notice without a runner time cannot be newer, so it never replaces a
    counted result.
    """
    evidence = sensor.last_evidence or {}
    if (
        evidence.get("reason") == _RESULT_REFUSED
        and evidence.get("result_uid") == notice.result_uid
    ):
        return True
    if sensor.runner_produced_at is None:
        return False
    if notice.produced_at is None:
        return True
    return _as_utc(sensor.runner_produced_at) >= _as_utc(notice.produced_at)


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
