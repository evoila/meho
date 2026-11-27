# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A retry row (``approval.reuse``) reads by the state of the request it found.

The audit query (the shown ``result_status`` and the ``result_status`` filter)
and the console drawer follow one table: ``REUSE_STATES``. A plain 200 row
still reads ``ok``, and a withdraw row still reads ``withdrawn``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.audit_query import AuditQueryFilters
from meho_backplane.audit_query.query import REUSE_STATES, _derive_result_status, query_audit
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog
from meho_backplane.settings import get_settings
from meho_backplane.ui.references import _REUSE_TONES

_BASE = datetime(2026, 11, 1, tzinfo=UTC)

#: state -> (status code, result_status) as the rule table says.
_TABLE = {
    "waiting": (202, "pending"),
    "approved_not_started": (200, "pending"),
    "approved_started": (200, "pending"),
    "approved_executed": (200, "ok"),
    "approved_failed": (200, "error"),
    "approved_uncertain": (200, "error"),
}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


def _row(
    *,
    path: str = "approval.reuse",
    method: str = "APPROVAL",
    status_code: int = 200,
    payload: dict[str, object] | None = None,
    tenant_id: uuid.UUID | None = None,
    second: int = 0,
) -> AuditLog:
    return AuditLog(
        id=uuid.uuid4(),
        occurred_at=_BASE + timedelta(seconds=second),
        operator_sub="op-1",
        tenant_id=tenant_id or uuid.uuid4(),
        method=method,
        path=path,
        status_code=status_code,
        duration_ms=Decimal("1.0"),
        payload=payload if payload is not None else {},
        event_class=AuditEventClass.POLICY_DECISION.value,
    )


def test_the_table_has_the_six_states_of_the_writer() -> None:
    assert set(REUSE_STATES) == set(_TABLE)
    assert {s.result_status for s in REUSE_STATES.values()} <= set(_REUSE_TONES)
    for state, (_, result_status) in _TABLE.items():
        assert REUSE_STATES[state].result_status == result_status


@pytest.mark.parametrize("state", list(_TABLE))
def test_a_reuse_row_reads_by_its_request_state(state: str) -> None:
    code, expected = _TABLE[state]
    assert (
        _derive_result_status(_row(status_code=code, payload={"request_state": state})) == expected
    )


def test_a_reuse_row_with_no_state_reads_by_its_status_code() -> None:
    assert _derive_result_status(_row(status_code=202)) == "pending"


def test_other_rows_still_read_by_status_code() -> None:
    assert _derive_result_status(_row(method="POST", path="/mcp", status_code=200)) == "ok"
    assert _derive_result_status(_row(path="approval.withdraw", status_code=200)) == "withdrawn"
    # The same payload on another path does not change the reading.
    other = _row(
        path="approval.request", status_code=202, payload={"request_state": "approved_failed"}
    )
    assert _derive_result_status(other) == "pending"


@pytest.mark.asyncio
async def test_each_filter_returns_exactly_the_rows_of_the_table(session: AsyncSession) -> None:
    tenant_id = uuid.uuid4()
    expected: dict[str, set[uuid.UUID]] = {"ok": set(), "pending": set(), "error": set()}
    for i, (state, (code, result_status)) in enumerate(_TABLE.items()):
        row = _row(
            status_code=code, payload={"request_state": state}, tenant_id=tenant_id, second=i
        )
        session.add(row)
        expected[result_status].add(row.id)
    # A reuse row with no state (same-call reuse): 202, pending.
    same_call = _row(status_code=202, tenant_id=tenant_id, second=10)
    expected["pending"].add(same_call.id)
    plain = _row(method="POST", path="/mcp", status_code=200, tenant_id=tenant_id, second=11)
    expected["ok"].add(plain.id)
    plain_error = _row(method="POST", path="/mcp", status_code=500, tenant_id=tenant_id, second=12)
    expected["error"].add(plain_error.id)
    withdraw = _row(path="approval.withdraw", tenant_id=tenant_id, second=13)
    session.add_all([same_call, plain, plain_error, withdraw])
    await session.commit()

    for result_status, ids in expected.items():
        result = await query_audit(
            AuditQueryFilters(result_status=result_status), tenant_id=tenant_id, session=session
        )
        assert {r.id for r in result.rows} == ids, result_status
        assert {r.result_status for r in result.rows} == {result_status}
    withdrawn = await query_audit(
        AuditQueryFilters(result_status="withdrawn"), tenant_id=tenant_id, session=session
    )
    assert {r.id for r in withdrawn.rows} == {withdraw.id}
