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
* **A non-no-op tick writes exactly one audit row** scoped to the
  system-tenant sentinel (``method='INTERNAL'``,
  ``path='audit.raw_payload.prune'``,
  ``operator_sub='system:audit-raw-payload-retention'``,
  ``payload={"nulled_raw_payload_rows": N, "retention_days": D,
  "cutoff": <iso-ts>}``).
* **An empty tick (retention > 0 but nothing past the cutoff) writes no
  audit row** -- the "no-op" case is nulled-zero, not only the sentinel.
* **Settings bounds** -- Pydantic validators reject out-of-range values
  for the three new ``RAW_PAYLOAD_*`` knobs at construction time.
* **Loop survives a bad tick / start-stop lifecycle / configured cadence**
  -- the same lifespan-owned-loop discipline the topology sweeper pins.

Tests run against the autouse ``_default_database_url`` SQLite-backed
engine (``tests.conftest`` migrates a fresh per-test DB to head); rows are
seeded directly through the sessionmaker. The bounded UPDATE rides the
``audit_log_occurred_at_idx`` btree on PG; the unit suite exercises the
same SQLAlchemy 2.x ``update().where(...)`` statement on SQLite.
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
from pydantic import ValidationError
from sqlalchemy import select

from meho_backplane import audit_retention
from meho_backplane.audit_retention import (
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
async def test_non_no_op_tick_writes_one_audit_row_scoped_to_system_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tick that NULLs N rows writes exactly one system-tenant audit row.

    Acceptance criterion: "each non-no-op tick writes exactly one retention
    audit row scoped to the system-tenant sentinel (``method='INTERNAL'``)".
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
async def test_empty_tick_writes_no_audit_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tick with nothing past the cutoff nulls zero rows and writes no row.

    The "no-op" case is nulled-zero, not only the ``0`` sentinel: an empty
    sweep is a no-op that emits no audit row (mirrors the flight-recorder
    reaper's "empty sweep writes no row" choice; weekly ``nulled=0`` rows
    would be pure noise).
    """
    monkeypatch.setenv("RAW_PAYLOAD_RETENTION_DAYS", "30")
    get_settings.cache_clear()

    kept = await _seed_audit_row(
        occurred_at=datetime.now(UTC) - timedelta(days=1),
        raw_payload=dict(_RAW_BODY),
    )

    await _run_one_prune_tick()

    assert await _list_prune_audit_rows() == [], "empty tick must not write an audit row"
    row = await _get_audit_row(kept)
    assert row.raw_payload == _RAW_BODY


@pytest.mark.asyncio
async def test_second_tick_over_aged_rows_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows aged off on tick 1 are SQL NULL, so tick 2 is a no-op.

    Convergence: the UPDATE writes a real SQL NULL (via ``sqlalchemy.null``,
    not Python ``None`` which the JSON column would persist as the literal
    ``'null'``), so aged rows fall out of the ``raw_payload IS NOT NULL``
    predicate and a second sweep over the same backlog nulls zero rows and
    writes no further audit row.
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
    assert len(second_rows) == 1, "converged sweep must not write a new audit row"


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

    with (
        patch(
            "meho_backplane.audit_retention._run_one_prune_tick",
            new=_flaky_tick,
        ),
        patch("asyncio.sleep", new=_fake_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await audit_retention._prune_loop()

    assert tick_calls == 1
    assert sleep_calls == 2


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
async def test_loop_sleeps_configured_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """The age-off loop honours ``RAW_PAYLOAD_PRUNE_INTERVAL_SECONDS``."""
    monkeypatch.setenv("RAW_PAYLOAD_PRUNE_INTERVAL_SECONDS", "1234")
    get_settings.cache_clear()

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise asyncio.CancelledError

    with (
        patch(
            "meho_backplane.audit_retention._run_one_prune_tick",
            new=AsyncMock(),
        ),
        patch("asyncio.sleep", new=_fake_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await audit_retention._prune_loop()

    assert sleeps == [1234]
