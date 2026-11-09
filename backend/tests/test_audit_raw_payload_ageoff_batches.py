# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The audit ``raw_payload`` age-off empties old rows in batches.

These tests run on the per-test SQLite database (``tests.conftest``). They
make the batch small (``_BATCH_SIZE``) and check that one run:

* empties a backlog bigger than one batch, oldest first, one transaction
  per batch, and leaves rows newer than the cutoff alone;
* goes on after a batch where many rows share one ``occurred_at``;
* stops at the time limit (``_MAX_RUN_SECONDS``), says so in its audit row,
  and the next run goes on from the oldest row that is left.

The PostgreSQL proof (with the append-only trigger) is in
``tests/integration/test_audit_raw_payload_ageoff_batches_pg.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from meho_backplane import audit_retention
from meho_backplane.audit_retention import AUDIT_RAW_PAYLOAD_PRUNE_PATH, _run_one_prune_tick
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.settings import get_settings

_RAW_BODY: dict[str, object] = {"pre_redaction": "raw-body-marker"}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("RAW_PAYLOAD_RETENTION_DAYS", "30")
    monkeypatch.setattr(audit_retention, "_BATCH_SIZE", 2)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def batch_calls(monkeypatch: pytest.MonkeyPatch) -> list[datetime | None]:
    """Record the ``not_before`` of every batch the run makes."""
    calls: list[datetime | None] = []
    real = audit_retention._null_one_batch

    async def _spy(cutoff: datetime, *, not_before: datetime | None) -> list[datetime]:
        calls.append(not_before)
        return await real(cutoff, not_before=not_before)

    monkeypatch.setattr(audit_retention, "_null_one_batch", _spy)
    return calls


async def _seed(occurred_at: datetime) -> uuid.UUID:
    row_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            AuditLog(
                id=row_id,
                occurred_at=occurred_at,
                operator_sub="operator-1",
                tenant_id=uuid.uuid4(),
                method="DISPATCH",
                path="test.op.read",
                status_code=200,
                duration_ms=Decimal("1.0"),
                payload={"op_id": "test.op.read"},
                raw_payload=dict(_RAW_BODY),
            )
        )
        await session.commit()
    return row_id


async def _raw_payloads(ids: list[uuid.UUID]) -> list[object]:
    async with get_sessionmaker()() as session:
        rows = {
            row.id: row.raw_payload
            for row in (await session.execute(select(AuditLog).where(AuditLog.id.in_(ids))))
            .scalars()
            .all()
        }
    return [rows[row_id] for row_id in ids]


async def _run_payloads() -> list[dict[str, object]]:
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.path == AUDIT_RAW_PAYLOAD_PRUNE_PATH)
                .order_by(AuditLog.occurred_at)
            )
        ).scalars()
        return [dict(row.payload) for row in rows]


@pytest.mark.asyncio
async def test_backlog_bigger_than_one_batch_is_cleared_in_one_run(
    batch_calls: list[datetime | None],
) -> None:
    now = datetime.now(UTC)
    old = [await _seed(now - timedelta(days=60 - i)) for i in range(5)]
    recent = await _seed(now - timedelta(days=1))

    await _run_one_prune_tick()

    assert await _raw_payloads(old) == [None] * 5
    assert await _raw_payloads([recent]) == [_RAW_BODY]
    # Batches of 2, 2 and 1 rows; each batch is its own call (and commit).
    assert len(batch_calls) == 3
    assert batch_calls[0] is None
    assert batch_calls[1] is not None and batch_calls[2] is not None
    assert batch_calls[1] < batch_calls[2], "each batch starts where the last one ended"
    payloads = await _run_payloads()
    assert len(payloads) == 1, "one audit row per run, not per batch"
    assert payloads[0]["nulled_raw_payload_rows"] == 5
    assert payloads[0]["stopped_early"] is False


@pytest.mark.asyncio
async def test_backlog_of_exactly_full_batches_ends_with_an_empty_batch(
    batch_calls: list[datetime | None],
) -> None:
    now = datetime.now(UTC)
    old = [await _seed(now - timedelta(days=60 - i)) for i in range(4)]

    await _run_one_prune_tick()

    assert await _raw_payloads(old) == [None] * 4
    assert len(batch_calls) == 3
    payloads = await _run_payloads()
    assert payloads[0]["nulled_raw_payload_rows"] == 4
    assert payloads[0]["stopped_early"] is False


@pytest.mark.asyncio
async def test_rows_with_the_same_time_are_all_cleared() -> None:
    same_time = datetime.now(UTC) - timedelta(days=60)
    old = [await _seed(same_time) for _ in range(5)]

    await _run_one_prune_tick()

    assert await _raw_payloads(old) == [None] * 5
    assert (await _run_payloads())[0]["nulled_raw_payload_rows"] == 5


@pytest.mark.asyncio
async def test_empty_table_writes_a_zero_row_not_stopped_early(
    batch_calls: list[datetime | None],
) -> None:
    await _run_one_prune_tick()

    assert batch_calls == [None]
    payloads = await _run_payloads()
    assert payloads == [
        {
            "nulled_raw_payload_rows": 0,
            "retention_days": 30,
            "cutoff": payloads[0]["cutoff"],
            "stopped_early": False,
        }
    ]


@pytest.mark.asyncio
async def test_time_limit_stops_the_run_and_the_next_run_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit_retention, "_MAX_RUN_SECONDS", 0.0)
    now = datetime.now(UTC)
    old = [await _seed(now - timedelta(days=60 - i)) for i in range(3)]

    await _run_one_prune_tick()

    # Only the first batch ran: the two oldest rows.
    assert await _raw_payloads(old) == [None, None, _RAW_BODY]
    first = (await _run_payloads())[0]
    assert first["nulled_raw_payload_rows"] == 2
    assert first["stopped_early"] is True

    await _run_one_prune_tick()

    assert await _raw_payloads(old) == [None, None, None]
    second = (await _run_payloads())[1]
    assert second["nulled_raw_payload_rows"] == 1
    assert second["stopped_early"] is False
