# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``result_status`` filter reads a retry row's JSON state on real PostgreSQL.

A retry row (``approval.reuse``) reads by ``payload.request_state``. The filter
reads that JSON field in SQL, and a fake database cannot prove the PostgreSQL
form. The module skips when Docker is not available; CI runs it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from meho_backplane.audit_query import AuditQueryFilters
from meho_backplane.audit_query.query import query_audit
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog

pytestmark = pytest.mark.usefixtures("pg_engine")

_BASE = datetime(2026, 11, 1, tzinfo=UTC)

#: (path, method, status code, payload, result_status)
_ROWS = [
    ("approval.reuse", "APPROVAL", 202, {"request_state": "waiting"}, "pending"),
    ("approval.reuse", "APPROVAL", 200, {"request_state": "approved_not_started"}, "pending"),
    ("approval.reuse", "APPROVAL", 200, {"request_state": "approved_started"}, "pending"),
    ("approval.reuse", "APPROVAL", 200, {"request_state": "approved_executed"}, "ok"),
    ("approval.reuse", "APPROVAL", 200, {"request_state": "approved_failed"}, "error"),
    ("approval.reuse", "APPROVAL", 200, {"request_state": "approved_uncertain"}, "error"),
    ("approval.reuse", "APPROVAL", 202, {}, "pending"),
    ("approval.withdraw", "APPROVAL", 200, {}, "withdrawn"),
    ("approval.request", "APPROVAL", 202, {"request_state": "approved_failed"}, "pending"),
    ("/mcp", "POST", 200, {"op_id": "demo.thing.read"}, "ok"),
    ("/mcp", "POST", 500, {"op_id": "demo.thing.read"}, "error"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("result_status", ["ok", "pending", "error", "withdrawn"])
async def test_the_filter_returns_exactly_the_rows_of_the_table(result_status: str) -> None:
    tenant_id = uuid.uuid4()
    expected: set[uuid.UUID] = set()
    async with get_sessionmaker()() as s:
        for i, (path, method, code, payload, status) in enumerate(_ROWS):
            row_id = uuid.uuid4()
            s.add(
                AuditLog(
                    id=row_id,
                    occurred_at=_BASE + timedelta(seconds=i),
                    operator_sub="op-pg-reuse",
                    tenant_id=tenant_id,
                    method=method,
                    path=path,
                    status_code=code,
                    duration_ms=Decimal("1.0"),
                    payload=payload,
                    event_class=AuditEventClass.POLICY_DECISION.value,
                )
            )
            if status == result_status:
                expected.add(row_id)
        await s.commit()

        result = await query_audit(
            AuditQueryFilters(result_status=result_status), tenant_id=tenant_id, session=s
        )

    assert {r.id for r in result.rows} == expected
    assert {r.result_status for r in result.rows} == {result_status}
