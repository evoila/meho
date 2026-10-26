# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the audit raw-payload age-off sweeper (S19, #307).

Coverage matrix (Task #307 acceptance criteria):

* **Past-cutoff ``raw_payload`` is NULLed; the redacted record of account
  survives** -- one audit row past the cutoff and one inside the window.
  After one tick the past-cutoff row's ``raw_payload`` is ``None`` while
  its ``payload`` (carrying ``redaction_policy_id``) and
  ``redaction_manifest`` are untouched; the in-window row keeps its
  ``raw_payload``. This is the load-bearing AC.
* **RAW_PAYLOAD_RETENTION_DAYS=0 is a no-op (keep-forever) and writes no
  audit row** -- a past-cutoff row keeps its ``raw_payload`` and no prune
  audit row is written.
* **Every run writes exactly one audit row** scoped to the
  system-tenant sentinel (``method='INTERNAL'``,
  ``path='audit.raw_payload.prune'``,
  ``operator_sub='system:audit-raw-payload-retention'``,
  ``payload={"nulled_raw_payload_rows": N, "retention_days": D,
  "cutoff": <iso-ts>}``) -- also a run that nulled zero rows, because the
  row is the record of the run that the schedule reads.
* **The schedule counts from the last recorded run, not from process
  start** -- a restart before the interval has passed waits only for the
  rest of it (no early run); a restart after it has passed, or with no
  recorded run, runs one minute after start; a last-run time in the
  future counts as due; the wait is never longer than one interval; a
  failed read runs at once; only rows this job writes count as a run.
* **Settings bounds** -- Pydantic validators reject out-of-range values
  for the three new ``RAW_PAYLOAD_*`` knobs at construction time.
* **Loop survives a bad tick / start-stop lifecycle / configured cadence**
  -- the same lifespan-owned-loop discipline the topology sweeper pins.

Tests run against the autouse ``_default_database_url`` SQLite-backed
engine (``tests.conftest`` migrates a fresh per-test DB to head); rows are
seeded directly through the sessionmaker. The bounded UPDATE rides the
``audit_log_occurred_at_idx`` btree on PG; the unit suite exercises the
same SQLAlchemy 2.x ``update().where(...)`` statement on SQLite. SQLite has
no ``audit_log`` append-only trigger, so the PostgreSQL proof that the
trigger lets this statement through lives in
``tests/migrations/test_migration_0103_audit_log_raw_payload_ageoff.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import structlog
from pydantic import ValidationError
from sqlalchemy import select
from structlog.testing import capture_logs

from meho_backplane import audit_retention
from meho_backplane.audit_retention import (
    _FIRST_RUN_DELAY_SECONDS,
    AUDIT_RAW_PAYLOAD_PRUNE_PATH,
    AUDIT_RAW_PAYLOAD_SYSTEM_TENANT_ID,
    SYSTEM_OPERATOR_SUB,
    _run_one_prune_tick,
    start_audit_raw_payload_reaper,
    stop_audit_raw_payload_reaper,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.memory.audit import INTERNAL_METHOD
from meho_backplane.settings import Settings, get_settings

# Deliberately non-secret placeholder bodies: the test only needs
# ``raw_payload`` to be a non-null JSON value that becomes null. Using
# obviously-fake markers keeps secret scanners quiet.
_RAW_BODY: dict[str, object] = {"pre_redaction": "raw-body-marker"}
_REDACTED_PAYLOAD: dict[str, object] = {
    "op_id": "vsphere.vm.list",
    "redaction_policy_id": "test-policy-v1",
    "connector_impl_id": "vsphere-rest",
}
_MANIFEST: list[dict[str, object]] = [
    {"rule": "r-bearer", "pattern": "bearer_token", "action": "redact", "count": 1}
]


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_audit_row(
    *,
    occurred_at: datetime,
    raw_payload: object,
    payload: dict[str, object] | None = None,
    redaction_manifest: object = None,
) -> uuid.UUID:
    """Insert one :class:`AuditLog` row and return its id.

    Mirrors the dispatcher's redaction write contract: ``payload`` is the
    redacted record of account (carrying ``redaction_policy_id``),
    ``raw_payload`` is the pre-redaction body, ``redaction_manifest`` the
    per-rule firing record.
    """
    audit_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            AuditLog(
                id=audit_id,
                occurred_at=occurred_at,
                operator_sub="operator-1",
                tenant_id=uuid.uuid4(),
                method="DISPATCH",
                path="vsphere.vm.list",
                status_code=200,
                duration_ms=Decimal("1.0"),
                payload=payload if payload is not None else dict(_REDACTED_PAYLOAD),
                raw_payload=raw_payload,
                redaction_manifest=redaction_manifest,
            )
        )
        await session.commit()
    return audit_id


async def _get_audit_row(audit_id: uuid.UUID) -> AuditLog:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.id == audit_id))
        return result.scalar_one()


async def _list_prune_audit_rows() -> list[AuditLog]:
    """Return only the sweeper's own age-off audit rows (by ``path``)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.path == AUDIT_RAW_PAYLOAD_PRUNE_PATH)
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Settings validators (out-of-range values rejected at construction)
# ---------------------------------------------------------------------------


def _settings_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "keycloak_issuer_url": "https://keycloak.test/realms/meho",
        "keycloak_audience": "meho-backplane",
        "vault_addr": "https://vault.test",
        "database_url": "sqlite+aiosqlite:///./test.db",
    }
    base.update(overrides)
    return base


def test_raw_payload_retention_days_default_is_ninety() -> None:
    """Default 90 mirrors ``topology_history_retention_days``."""
    s = Settings(**_settings_kwargs())
    assert s.raw_payload_retention_days == 90


def test_raw_payload_retention_days_accepts_zero_sentinel() -> None:
    """``0`` is the keep-forever opt-out sentinel."""
    s = Settings(**_settings_kwargs(raw_payload_retention_days=0))
    assert s.raw_payload_retention_days == 0


def test_raw_payload_retention_days_rejects_negative() -> None:
    """Range floor is 0 (the sentinel); -1 is not a meaningful retention."""
    with pytest.raises(ValidationError):
        Settings(**_settings_kwargs(raw_payload_retention_days=-1))


def test_raw_payload_retention_days_rejects_above_ten_years() -> None:
    """Range ceiling is 3650 days (10y); above is functionally permanent."""
    with pytest.raises(ValidationError):
        Settings(**_settings_kwargs(raw_payload_retention_days=3651))


def test_raw_payload_prune_interval_default_is_one_week() -> None:
    """Default 604800 (7d / weekly) matches the topology-history prune."""
    s = Settings(**_settings_kwargs())
    assert s.raw_payload_prune_interval_seconds == 604800


def test_raw_payload_prune_interval_rejects_below_one_minute() -> None:
    """Range floor is 60s; below one minute competes with write load."""
    with pytest.raises(ValidationError):
        Settings(**_settings_kwargs(raw_payload_prune_interval_seconds=59))


def test_raw_payload_prune_interval_rejects_above_one_week() -> None:
    """Range ceiling is 604800s (1w)."""
    with pytest.raises(ValidationError):
        Settings(**_settings_kwargs(raw_payload_prune_interval_seconds=604801))


def test_raw_payload_prune_enabled_default_is_true() -> None:
    """Default True: the in-process age-off is the shipped mechanism."""
    s = Settings(**_settings_kwargs())
    assert s.raw_payload_prune_enabled is True


def test_settings_prune_enabled_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """``RAW_PAYLOAD_PRUNE_ENABLED=false`` resolves to ``False``."""
    monkeypatch.setenv("RAW_PAYLOAD_PRUNE_ENABLED", "false")
    get_settings.cache_clear()
    s = get_settings()
    assert s.raw_payload_prune_enabled is False


# ---------------------------------------------------------------------------
# Happy path -- past-cutoff raw_payload NULLed, redacted record preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_nulls_past_cutoff_raw_payload_and_preserves_redacted_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past-cutoff ``raw_payload`` is NULLed; ``payload`` + manifest survive.

    Acceptance criterion: "a row older than the cutoff has ``raw_payload``
    NULLed while ``payload``, ``redaction_policy_id`` are unchanged".
    ``redaction_policy_id`` is a key inside the redacted ``payload`` dict,
    so asserting ``payload`` is unchanged proves it survives.
    """
    monkeypatch.setenv("RAW_PAYLOAD_RETENTION_DAYS", "90")
    get_settings.cache_clear()

    old_row = await _seed_audit_row(
        occurred_at=datetime.now(UTC) - timedelta(days=100),
        raw_payload=dict(_RAW_BODY),
        payload=dict(_REDACTED_PAYLOAD),
        redaction_manifest=[dict(_MANIFEST[0])],
    )
    new_row = await _seed_audit_row(
        occurred_at=datetime.now(UTC) - timedelta(days=1),
        raw_payload=dict(_RAW_BODY),
        payload=dict(_REDACTED_PAYLOAD),
        redaction_manifest=[dict(_MANIFEST[0])],
    )

    await _run_one_prune_tick()

    aged = await _get_audit_row(old_row)
    assert aged.raw_payload is None, "past-cutoff raw_payload was not NULLed"
    # The redacted record of account is untouched.
    assert aged.payload == _REDACTED_PAYLOAD
    assert aged.payload["redaction_policy_id"] == "test-policy-v1"
    assert aged.redaction_manifest == [_MANIFEST[0]]

    kept = await _get_audit_row(new_row)
    assert kept.raw_payload == _RAW_BODY, "in-window raw_payload was wrongly NULLed"


# ---------------------------------------------------------------------------
# Opt-out sentinel -- RAW_PAYLOAD_RETENTION_DAYS=0 is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_with_retention_zero_is_no_op_and_writes_no_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``RETENTION_DAYS=0`` keeps ``raw_payload`` forever and writes no audit row.

    Acceptance criterion: "a no-op tick writes no audit row". The sentinel
    tick is a logged heartbeat only -- no UPDATE, no audit row.
    """
    monkeypatch.setenv("RAW_PAYLOAD_RETENTION_DAYS", "0")
    get_settings.cache_clear()

    very_old = await _seed_audit_row(
        occurred_at=datetime.now(UTC) - timedelta(days=365 * 5),
        raw_payload=dict(_RAW_BODY),
    )

    await _run_one_prune_tick()

    row = await _get_audit_row(very_old)
    assert row.raw_payload == _RAW_BODY, "RETENTION_DAYS=0 must keep raw_payload"
    assert await _list_prune_audit_rows() == [], "no-op tick must not write an audit row"


# ---------------------------------------------------------------------------
# Audit-row shape -- exactly one row per non-no-op tick
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_that_nulls_rows_writes_one_audit_row_scoped_to_system_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tick that NULLs N rows writes exactly one system-tenant audit row.

    Acceptance criterion: "each tick writes exactly one retention audit row
    scoped to the system-tenant sentinel (``method='INTERNAL'``)".
    """
    monkeypatch.setenv("RAW_PAYLOAD_RETENTION_DAYS", "30")
    get_settings.cache_clear()

    for _ in range(3):
        await _seed_audit_row(
            occurred_at=datetime.now(UTC) - timedelta(days=60),
            raw_payload=dict(_RAW_BODY),
        )

    await _run_one_prune_tick()

    prune_rows = await _list_prune_audit_rows()
    assert len(prune_rows) == 1, f"expected exactly one audit row; got {len(prune_rows)}"
    row = prune_rows[0]
    assert row.method == INTERNAL_METHOD
    assert row.path == AUDIT_RAW_PAYLOAD_PRUNE_PATH
    assert row.operator_sub == SYSTEM_OPERATOR_SUB
    assert row.tenant_id == AUDIT_RAW_PAYLOAD_SYSTEM_TENANT_ID
    assert row.status_code == 200
    assert row.payload["nulled_raw_payload_rows"] == 3
    assert row.payload["retention_days"] == 30
    assert isinstance(row.payload["cutoff"], str)
    assert "+00:00" in row.payload["cutoff"] or row.payload["cutoff"].endswith("Z")
    # The sweeper's own audit row carries no pre-redaction body.
    assert row.raw_payload is None


@pytest.mark.asyncio
async def test_zero_row_tick_writes_its_audit_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tick with nothing past the cutoff still writes its run row (count 0).

    The row is the record of the run: the next process start reads it to
    know when the age-off last ran. Without it, a deployment with nothing
    to age off would look like it never ran.
    """
    monkeypatch.setenv("RAW_PAYLOAD_RETENTION_DAYS", "30")
    get_settings.cache_clear()

    kept = await _seed_audit_row(
        occurred_at=datetime.now(UTC) - timedelta(days=1),
        raw_payload=dict(_RAW_BODY),
    )

    await _run_one_prune_tick()

    prune_rows = await _list_prune_audit_rows()
    assert len(prune_rows) == 1, "a zero-row run must still write one audit row"
    row = prune_rows[0]
    assert row.payload["nulled_raw_payload_rows"] == 0
    assert row.payload["retention_days"] == 30
    assert row.tenant_id == AUDIT_RAW_PAYLOAD_SYSTEM_TENANT_ID
    assert row.operator_sub == SYSTEM_OPERATOR_SUB
    assert row.method == INTERNAL_METHOD
    assert row.raw_payload is None
    # Nothing was past the cutoff, so the in-window body is untouched.
    assert (await _get_audit_row(kept)).raw_payload == _RAW_BODY
    # The schedule reads this row as the last run.
    since = datetime.now(UTC) - timedelta(days=7)
    assert await audit_retention._last_recorded_run(since=since) is not None


@pytest.mark.asyncio
async def test_second_tick_over_aged_rows_nulls_nothing_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows aged off on tick 1 are SQL NULL, so tick 2 nulls zero rows.

    Convergence: the UPDATE writes a real SQL NULL (via ``sqlalchemy.null``,
    not Python ``None`` which the JSON column would persist as the literal
    ``'null'``), so aged rows fall out of the ``raw_payload IS NOT NULL``
    predicate and a second sweep over the same backlog nulls zero rows. It
    still records its run (a second row with count ``0``).
    """
    monkeypatch.setenv("RAW_PAYLOAD_RETENTION_DAYS", "30")
    get_settings.cache_clear()

    for _ in range(2):
        await _seed_audit_row(
            occurred_at=datetime.now(UTC) - timedelta(days=60),
            raw_payload=dict(_RAW_BODY),
        )

    await _run_one_prune_tick()
    first_rows = await _list_prune_audit_rows()
    assert len(first_rows) == 1
    assert first_rows[0].payload["nulled_raw_payload_rows"] == 2

    # Second tick: everything past the cutoff is already SQL NULL.
    await _run_one_prune_tick()
    second_rows = await _list_prune_audit_rows()
    assert len(second_rows) == 2, "the second run must record itself too"
    counts = sorted(r.payload["nulled_raw_payload_rows"] for r in second_rows)
    assert counts == [0, 2], "the second run must null nothing more"


# ---------------------------------------------------------------------------
# Loop survives bad ticks / lifecycle / cadence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_survives_tick_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing tick logs and the loop reaches the next sleep."""
    monkeypatch.setenv("RAW_PAYLOAD_PRUNE_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("RAW_PAYLOAD_RETENTION_DAYS", "90")
    get_settings.cache_clear()

    tick_calls = 0
    sleep_calls = 0

    async def _flaky_tick() -> None:
        nonlocal tick_calls
        tick_calls += 1
        if tick_calls == 1:
            raise RuntimeError("transient DB blip")

    async def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    # A fresh logger proxy: the module-level ``_log`` may already have cached
    # its bound methods (``cache_logger_on_first_use=True``) in an earlier
    # test on this worker, and a cached logger bypasses ``capture_logs``.
    monkeypatch.setattr(audit_retention, "_log", structlog.get_logger(audit_retention.__name__))

    with (
        patch(
            "meho_backplane.audit_retention._run_one_prune_tick",
            new=_flaky_tick,
        ),
        patch("asyncio.sleep", new=_fake_sleep),
        capture_logs() as captured,
        pytest.raises(asyncio.CancelledError),
    ):
        await audit_retention._prune_loop()

    assert tick_calls == 1
    assert sleep_calls == 2
    # A failed tick leaves un-redacted bodies in place, so it must log at
    # error level and name the error type (easy to find and alert on).
    failed = [e for e in captured if e["event"] == "audit_raw_payload_retention_tick_failed"]
    assert len(failed) == 1
    assert failed[0]["log_level"] == "error"
    assert failed[0]["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_start_and_stop_reaper_lifecycle() -> None:
    """start + stop the task with no destroyed-task warnings."""
    with (
        patch(
            "meho_backplane.audit_retention._run_one_prune_tick",
            new=AsyncMock(),
        ),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        task = start_audit_raw_payload_reaper()
        assert not task.done()
        await stop_audit_raw_payload_reaper(task)
        assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_loop_sleeps_configured_interval_between_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a run, the loop sleeps ``RAW_PAYLOAD_PRUNE_INTERVAL_SECONDS``.

    No run is recorded, so the first run comes right after the one-minute
    start delay; the sleep after that run is the configured interval.
    """
    monkeypatch.setenv("RAW_PAYLOAD_PRUNE_INTERVAL_SECONDS", "1234")
    get_settings.cache_clear()

    sleeps, tick_calls = await _drive_loop(cancel_at_sleep=2)

    assert sleeps == [_FIRST_RUN_DELAY_SECONDS, 1234]
    assert tick_calls == 1


# ---------------------------------------------------------------------------
# Schedule -- the interval counts from the last recorded run, not from start
# ---------------------------------------------------------------------------

_WEEK_SECONDS = 604800
_DAY_SECONDS = 86400


async def _seed_prune_row(
    *,
    occurred_at: datetime,
    tenant_id: uuid.UUID = AUDIT_RAW_PAYLOAD_SYSTEM_TENANT_ID,
    path: str = AUDIT_RAW_PAYLOAD_PRUNE_PATH,
    method: str = INTERNAL_METHOD,
    operator_sub: str = SYSTEM_OPERATOR_SUB,
) -> None:
    """Insert one age-off run row, as an earlier process would have written it.

    The keyword arguments let a test write a row that looks like a run but
    was written by something else (another tenant, path, method or sub).
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            AuditLog(
                id=uuid.uuid4(),
                occurred_at=occurred_at,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                method=method,
                path=path,
                status_code=200,
                duration_ms=Decimal("1.0"),
                payload={"nulled_raw_payload_rows": 0, "retention_days": 90},
            )
        )
        await session.commit()


async def _drive_loop(
    *,
    cancel_at_sleep: int,
    real_tick: bool = False,
) -> tuple[list[float], int]:
    """Run :func:`_prune_loop` with a fake ``asyncio.sleep``; stop at sleep N.

    Returns every sleep the loop asked for (the N-th one included) and how
    many runs happened before the loop was stopped. With ``real_tick`` the
    real tick runs (and writes its audit row); otherwise a counting stub.
    """
    sleeps: list[float] = []
    tick_calls = 0
    real = audit_retention._run_one_prune_tick

    async def _counting_tick() -> None:
        nonlocal tick_calls
        tick_calls += 1
        if real_tick:
            await real()

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= cancel_at_sleep:
            raise asyncio.CancelledError

    with (
        patch("meho_backplane.audit_retention._run_one_prune_tick", new=_counting_tick),
        patch("asyncio.sleep", new=_fake_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await audit_retention._prune_loop()
    return sleeps, tick_calls


@pytest.mark.asyncio
async def test_restart_before_interval_waits_only_the_rest_of_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Last run 2 days ago, weekly interval: no early run, wait about 5 days.

    This is the bug the schedule fixes: before, every start slept a full
    week, so a pod that restarted more often than weekly never ran.
    """
    monkeypatch.setenv("RAW_PAYLOAD_PRUNE_INTERVAL_SECONDS", str(_WEEK_SECONDS))
    get_settings.cache_clear()
    await _seed_prune_row(occurred_at=datetime.now(UTC) - timedelta(days=2))
    monkeypatch.setattr(audit_retention, "_log", structlog.get_logger(audit_retention.__name__))

    with capture_logs() as captured:
        sleeps, tick_calls = await _drive_loop(cancel_at_sleep=2)

    assert tick_calls == 0, "a restart before the interval has passed must not run early"
    assert sleeps[0] == _FIRST_RUN_DELAY_SECONDS
    rest = _WEEK_SECONDS - 2 * _DAY_SECONDS
    assert rest - 5 <= sleeps[1] <= rest, f"expected the rest of the interval; got {sleeps[1]}"
    scheduled = [e for e in captured if e["event"] == "audit_raw_payload_retention_scheduled"]
    assert len(scheduled) == 1
    assert scheduled[0]["last_run_at"] is not None
    assert scheduled[0]["next_run_in_seconds"] == sleeps[1]


@pytest.mark.asyncio
async def test_restart_after_interval_runs_soon_after_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Last run 8 days ago, weekly interval: run one minute after start."""
    monkeypatch.setenv("RAW_PAYLOAD_PRUNE_INTERVAL_SECONDS", str(_WEEK_SECONDS))
    get_settings.cache_clear()
    await _seed_prune_row(occurred_at=datetime.now(UTC) - timedelta(days=8))

    sleeps, tick_calls = await _drive_loop(cancel_at_sleep=2)

    assert sleeps == [_FIRST_RUN_DELAY_SECONDS, _WEEK_SECONDS]
    assert tick_calls == 1, "a due run must happen right after the start delay"


@pytest.mark.asyncio
async def test_no_recorded_run_runs_soon_after_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No run recorded (fresh install): run one minute after start."""
    monkeypatch.setenv("RAW_PAYLOAD_PRUNE_INTERVAL_SECONDS", str(_WEEK_SECONDS))
    get_settings.cache_clear()
    since = datetime.now(UTC) - timedelta(seconds=_WEEK_SECONDS)
    assert await audit_retention._last_recorded_run(since=since) is None

    sleeps, tick_calls = await _drive_loop(cancel_at_sleep=2)

    assert sleeps == [_FIRST_RUN_DELAY_SECONDS, _WEEK_SECONDS]
    assert tick_calls == 1


@pytest.mark.asyncio
async def test_last_run_in_the_future_counts_as_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run row stamped 2 days ahead (a pod with a fast clock): run soon.

    Waiting a full interval here would push the schedule back by the clock
    gap. Running early is safe: a run is safe to run twice.
    """
    monkeypatch.setenv("RAW_PAYLOAD_PRUNE_INTERVAL_SECONDS", str(_WEEK_SECONDS))
    get_settings.cache_clear()
    await _seed_prune_row(occurred_at=datetime.now(UTC) + timedelta(days=2))

    sleeps, tick_calls = await _drive_loop(cancel_at_sleep=2)

    assert sleeps == [_FIRST_RUN_DELAY_SECONDS, _WEEK_SECONDS]
    assert tick_calls == 1, "a future last-run time must count as due"


@pytest.mark.asyncio
async def test_a_real_run_is_found_by_the_next_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: start, run, restart -- the second start does not run again.

    The first start has no recorded run, so it runs (the real tick, with
    nothing to age off) and writes its row. A second start right after
    reads that row and waits about one full interval instead.
    """
    monkeypatch.setenv("RAW_PAYLOAD_PRUNE_INTERVAL_SECONDS", str(_WEEK_SECONDS))
    monkeypatch.setenv("RAW_PAYLOAD_RETENTION_DAYS", "90")
    get_settings.cache_clear()

    _, first_ticks = await _drive_loop(cancel_at_sleep=2, real_tick=True)
    assert first_ticks == 1
    assert len(await _list_prune_audit_rows()) == 1

    sleeps, second_ticks = await _drive_loop(cancel_at_sleep=2, real_tick=True)
    assert second_ticks == 0, "a restart right after a run must not run again"
    assert _WEEK_SECONDS - 60 <= sleeps[1] <= _WEEK_SECONDS
    assert len(await _list_prune_audit_rows()) == 1


@pytest.mark.asyncio
async def test_last_run_read_failure_runs_soon(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the last run cannot be read, log it and run after the start delay."""
    monkeypatch.setenv("RAW_PAYLOAD_PRUNE_INTERVAL_SECONDS", str(_WEEK_SECONDS))
    get_settings.cache_clear()
    monkeypatch.setattr(audit_retention, "_log", structlog.get_logger(audit_retention.__name__))
    monkeypatch.setattr(
        audit_retention,
        "_last_recorded_run",
        AsyncMock(side_effect=RuntimeError("database not reachable")),
    )

    with capture_logs() as captured:
        sleeps, tick_calls = await _drive_loop(cancel_at_sleep=2)

    assert sleeps == [_FIRST_RUN_DELAY_SECONDS, _WEEK_SECONDS]
    assert tick_calls == 1
    failed = [
        e for e in captured if e["event"] == "audit_raw_payload_retention_last_run_read_failed"
    ]
    assert len(failed) == 1
    assert failed[0]["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_last_recorded_run_reads_only_age_off_rows() -> None:
    """Only rows this job writes count, and only within the given window.

    A row counts only with the system tenant id, ``method='INTERNAL'``,
    this job's ``operator_sub`` and the age-off path. A dispatch audit row
    with the same tenant id and path (an operation whose id happens to be
    ``audit.raw_payload.prune``) must never count as a run. The newest
    matching row wins, and the value comes back in UTC with a time zone
    (SQLite returns it without one).
    """
    now = datetime.now(UTC)
    since = now - timedelta(seconds=_WEEK_SECONDS)
    # Same path, another tenant: not a run of this job.
    await _seed_prune_row(occurred_at=now - timedelta(hours=1), tenant_id=uuid.uuid4())
    # System tenant, another path: not a run of this job.
    await _seed_prune_row(occurred_at=now - timedelta(hours=2), path="topology.history.prune")
    # A dispatch audit row with the same tenant id and path: not a run.
    await _seed_prune_row(
        occurred_at=now - timedelta(hours=3),
        method="DISPATCH",
        operator_sub="operator-1",
    )
    # INTERNAL, same tenant id and path, but another job's sub: not a run.
    await _seed_prune_row(
        occurred_at=now - timedelta(hours=4),
        operator_sub="system:another-job",
    )
    assert await audit_retention._last_recorded_run(since=since) is None

    # A real run older than the window is not read (it means "due now").
    await _seed_prune_row(occurred_at=now - timedelta(days=9))
    assert await audit_retention._last_recorded_run(since=since) is None

    older = now - timedelta(days=5)
    newer = now - timedelta(days=3)
    await _seed_prune_row(occurred_at=older)
    await _seed_prune_row(occurred_at=newer)

    last_run = await audit_retention._last_recorded_run(since=since)
    assert last_run is not None
    assert last_run.tzinfo is not None
    assert last_run == newer


@pytest.mark.parametrize(
    ("last_run_offset_seconds", "expected"),
    [
        (None, 0.0),  # no recorded run: due now
        (-_WEEK_SECONDS - 1, 0.0),  # more than one interval ago: due now
        (-_WEEK_SECONDS, 0.0),  # exactly one interval ago: due now
        (-2 * _DAY_SECONDS, float(_WEEK_SECONDS - 2 * _DAY_SECONDS)),  # the rest
        (0, float(_WEEK_SECONDS)),  # just ran: one full interval
        (1, 0.0),  # one second in the future (fast clock): due now
        (30 * _DAY_SECONDS, 0.0),  # far in the future: due now
    ],
)
def test_seconds_until_due(last_run_offset_seconds: int | None, expected: float) -> None:
    """The wait is the rest of the interval; a future last run is due now."""
    now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    last_run = (
        None
        if last_run_offset_seconds is None
        else now + timedelta(seconds=last_run_offset_seconds)
    )
    assert (
        audit_retention._seconds_until_due(last_run, now=now, interval_seconds=_WEEK_SECONDS)
        == expected
    )
