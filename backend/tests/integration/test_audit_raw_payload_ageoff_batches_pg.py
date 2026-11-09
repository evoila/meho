# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``raw_payload`` age-off empties a big backlog in batches on PostgreSQL.

On PostgreSQL the ``audit_log_append_only`` trigger checks every changed
row, and it lets an ``UPDATE`` through only when it sets ``raw_payload`` from
a value to SQL ``NULL`` and changes nothing else. These tests run the real
age-off against a migrated PostgreSQL (the ``pg_engine`` fixture of
:mod:`tests.integration.conftest`) and prove:

* a backlog bigger than one batch (with the real ``_BATCH_SIZE``) is fully
  emptied in one run, one transaction per batch, and every batch passes the
  trigger;
* rows newer than the cutoff keep their ``raw_payload``;
* every other column of every row keeps its exact stored text;
* two runs at the same time over the same rows both succeed: each batch
  skips the rows the other run has locked, so the trigger never sees an
  update of a row that is already empty;
* the batch statement runs as a role with only the runtime role's rights
  (``SELECT``, ``INSERT`` and ``UPDATE (raw_payload)`` on ``audit_log``):
  its ``FOR UPDATE`` lock needs no other right.

The module skips when Docker is not available; CI runs it.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from sqlalchemy import insert, text

from meho_backplane import audit_retention
from meho_backplane.audit_retention import AUDIT_RAW_PAYLOAD_PRUNE_PATH
from meho_backplane.db.engine import get_engine
from meho_backplane.db.models import AuditLog
from meho_backplane.db.runtime_role import apply_runtime_grants

pytestmark = pytest.mark.usefixtures("pg_engine")

_RAW_BODY: dict[str, object] = {"pre_redaction": "raw-body-marker"}


def _row(occurred_at: datetime) -> dict[str, object]:
    """One fully filled audit row, so a change of any column would show."""
    return {
        "id": uuid.uuid4(),
        "occurred_at": occurred_at,
        "operator_sub": "operator-batches",
        "method": "DISPATCH",
        "path": "test.op.read",
        "status_code": 200,
        "request_id": uuid.uuid4(),
        "duration_ms": Decimal("12.345"),
        "payload": {"op_id": "test.op.read", "redaction_policy_id": "test-policy-v1"},
        "tenant_id": uuid.uuid4(),
        "target_id": uuid.uuid4(),
        "parent_audit_id": uuid.uuid4(),
        "agent_session_id": uuid.uuid4(),
        "actor_sub": "actor-batches",
        "raw_payload": dict(_RAW_BODY),
        "redaction_manifest": [{"rule": "r-test", "action": "redact", "count": 1}],
        "run_id": uuid.uuid4(),
        "step_id": "step-1",
        "work_ref": "work-batches",
        "policy_decision": "auto-execute",
    }


async def _seed(old_count: int, recent_count: int) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """Insert *old_count* rows past the 90-day cutoff and *recent_count* newer rows."""
    now = datetime.now(UTC)
    old = [_row(now - timedelta(days=200, seconds=i)) for i in range(old_count)]
    recent = [_row(now - timedelta(days=1, seconds=i)) for i in range(recent_count)]
    async with get_engine().begin() as conn:
        await conn.execute(insert(AuditLog), old + recent)
    return {cast(uuid.UUID, r["id"]) for r in old}, {cast(uuid.UUID, r["id"]) for r in recent}


async def _rows_as_text() -> dict[uuid.UUID, dict[str, str | None]]:
    """Every column's exact stored text, per row id (SQL NULL -> ``None``)."""
    async with get_engine().connect() as conn:
        names = [
            str(r[0])
            for r in await conn.execute(
                text(
                    "SELECT attname FROM pg_attribute WHERE attrelid = 'audit_log'::regclass "
                    "AND attnum > 0 AND NOT attisdropped ORDER BY attnum"
                )
            )
        ]
        select_list = ", ".join(f'a."{name}"::text' for name in names)
        rows = await conn.execute(
            text(f"SELECT a.id, {select_list} FROM audit_log a WHERE a.path <> :p"),
            {"p": AUDIT_RAW_PAYLOAD_PRUNE_PATH},
        )
        return {row[0]: dict(zip(names, row[1:], strict=True)) for row in rows}


def _without_raw(row: dict[str, str | None]) -> dict[str, str | None]:
    return {name: value for name, value in row.items() if name != "raw_payload"}


async def _run_payloads() -> list[dict[str, object]]:
    async with get_engine().connect() as conn:
        rows = await conn.execute(
            text("SELECT payload FROM audit_log WHERE path = :p"),
            {"p": AUDIT_RAW_PAYLOAD_PRUNE_PATH},
        )
        return [json.loads(row[0]) if isinstance(row[0], str) else dict(row[0]) for row in rows]


@pytest.mark.asyncio
async def test_backlog_bigger_than_one_batch_is_fully_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batches: list[int] = []
    real = audit_retention._null_one_batch

    async def _spy(cutoff: datetime, *, not_before: datetime | None) -> list[datetime]:
        occurred = await real(cutoff, not_before=not_before)
        batches.append(len(occurred))
        return occurred

    monkeypatch.setattr(audit_retention, "_null_one_batch", _spy)
    batch_size = audit_retention._BATCH_SIZE
    old_ids, recent_ids = await _seed(batch_size + 7, 3)
    before = await _rows_as_text()

    await audit_retention._run_one_prune_tick()

    after = await _rows_as_text()
    assert batches == [batch_size, 7], "two batches, each in its own transaction"
    assert all(after[row_id]["raw_payload"] is None for row_id in old_ids)
    for row_id in recent_ids:
        assert after[row_id] == before[row_id], "a row newer than the cutoff is untouched"
    for row_id in old_ids:
        assert _without_raw(after[row_id]) == _without_raw(before[row_id])
    payloads = await _run_payloads()
    assert len(payloads) == 1
    assert payloads[0]["nulled_raw_payload_rows"] == batch_size + 7
    assert payloads[0]["stopped_early"] is False


@pytest.mark.asyncio
async def test_two_runs_at_the_same_time_both_succeed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit_retention, "_BATCH_SIZE", 10)
    old_ids, recent_ids = await _seed(45, 2)
    before = await _rows_as_text()
    cutoff = datetime.now(UTC) - timedelta(days=90)

    first, second = await asyncio.gather(
        audit_retention._null_raw_payload_older_than(cutoff),
        audit_retention._null_raw_payload_older_than(cutoff),
    )
    # A run that waited for the other may end early; a third run empties the rest.
    rest = await audit_retention._null_raw_payload_older_than(cutoff)

    after = await _rows_as_text()
    assert first.nulled_rows + second.nulled_rows + rest.nulled_rows == 45
    assert all(after[row_id]["raw_payload"] is None for row_id in old_ids)
    for row_id in recent_ids:
        assert after[row_id] == before[row_id]
    for row_id in old_ids:
        assert _without_raw(after[row_id]) == _without_raw(before[row_id])


@pytest.mark.asyncio
async def test_batch_runs_with_only_the_runtime_role_rights() -> None:
    old_ids, _ = await _seed(3, 0)
    role = f"ageoff_runtime_{uuid.uuid4().hex[:12]}"
    async with get_engine().connect() as conn:
        await conn.execute(text(f"CREATE ROLE {role} NOLOGIN"))
        await conn.commit()
        await apply_runtime_grants(conn, role, say=lambda _line: None)
        await conn.execute(text(f"SET LOCAL ROLE {role}"))
        statement = audit_retention._batch_statement(
            datetime.now(UTC) - timedelta(days=90), not_before=None
        )
        emptied = (await conn.execute(statement)).scalars().all()
        await conn.commit()

    assert len(emptied) == 3
    after = await _rows_as_text()
    assert all(after[row_id]["raw_payload"] is None for row_id in old_ids)
