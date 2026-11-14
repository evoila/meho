# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The audit writer stores any text of a target's answer on real PostgreSQL.

PostgreSQL refuses a NUL character and a lone surrogate inside a JSON value,
and a fake database cannot prove that. These tests run the real writer
against a migrated PostgreSQL (the ``pg_engine`` fixture of
:mod:`tests.integration.conftest`):

* a control insert, without the writer, shows that PostgreSQL refuses each
  kind of text;
* the writer stores a row for each kind of text, with a visible marker, and
  without the ``full_row_lost`` mark;
* a row the database refuses for another reason is stored as a short row;
* the audit search finds the short rows and the changed rows by their marks.

The module skips when Docker is not available; CI runs it.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy.exc as sa_exc
from sqlalchemy import select

from meho_backplane.audit_query import AuditQueryFilters
from meho_backplane.audit_query.query import query_audit
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog
from meho_backplane.operations._audit import write_audit_row
from meho_backplane.operations.audit_row_safe import (
    FULL_ROW_LOST,
    INVALID_CHARACTER_MARKER,
    NUL_MARKER,
    TEXT_CHANGED,
)

pytestmark = pytest.mark.usefixtures("pg_engine")

OP_ID = "demo.thing.read"

#: One answer for each kind of text the table refuses, with the marker that
#: the writer must leave in the stored row.
CASES = [
    pytest.param({"name": "a\x00b"}, NUL_MARKER, id="nul-in-value"),
    pytest.param({"na\x00me": "v"}, NUL_MARKER, id="nul-in-key"),
    pytest.param({"name": "a\ud800b"}, INVALID_CHARACTER_MARKER, id="lone-surrogate"),
    pytest.param({"load": float("nan")}, "nan", id="nan"),
]


def _operator(tenant_id: uuid.UUID | None = None) -> Operator:
    return Operator(
        sub="op-pg-text",
        name="Text Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id or uuid.uuid4(),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _descriptor() -> Any:
    return SimpleNamespace(
        op_id=OP_ID,
        source_kind="typed",
        product="demo",
        version="1.x",
        impl_id="demo",
        safety_level="safe",
        requires_approval=False,
        method=None,
        path=None,
        tags=[],
    )


async def _rows(audit_id: uuid.UUID) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        query = select(AuditLog).where(AuditLog.id == audit_id)
        return list((await session.execute(query)).scalars().all())


@pytest.mark.parametrize(("answer", "marker"), CASES)
async def test_postgres_refuses_the_text_without_the_writer(
    answer: dict[str, Any], marker: str
) -> None:
    """Control: a plain insert of the same text fails, so the next test proves something."""
    operator = _operator()
    row = AuditLog(
        id=uuid.uuid4(),
        operator_sub=operator.sub,
        tenant_id=operator.tenant_id,
        method="DISPATCH",
        path=OP_ID,
        status_code=200,
        payload={"op_id": OP_ID},
        raw_payload=answer,
        event_class=AuditEventClass.OPERATION_EXECUTION,
    )
    async with get_sessionmaker()() as session:
        session.add(row)
        with pytest.raises((sa_exc.DBAPIError, sa_exc.StatementError)):
            await session.commit()


@pytest.mark.parametrize(("answer", "marker"), CASES)
async def test_the_writer_stores_the_text_with_a_marker(
    answer: dict[str, Any], marker: str
) -> None:
    audit_id = uuid.uuid4()
    await write_audit_row(
        audit_id=audit_id,
        event_class=AuditEventClass.OPERATION_EXECUTION,
        operator=_operator(),
        descriptor=_descriptor(),
        target=None,
        params_hash="ab",
        result_status="ok",
        duration_ms=1.0,
        raw_payload=answer,
        redaction_manifest=[{"rule": "r", "sample": answer}],
    )
    rows = await _rows(audit_id)
    assert len(rows) == 1
    assert FULL_ROW_LOST not in rows[0].payload
    assert rows[0].payload[TEXT_CHANGED] is True
    assert marker in repr(rows[0].raw_payload)
    assert marker in repr(rows[0].redaction_manifest)


async def test_a_row_the_database_refuses_for_another_reason_is_stored_short() -> None:
    """A value that cannot be turned into JSON fails the full row; a short row is stored."""
    audit_id = uuid.uuid4()
    await write_audit_row(
        audit_id=audit_id,
        event_class=AuditEventClass.OPERATION_EXECUTION,
        operator=_operator(),
        descriptor=_descriptor(),
        target=None,
        params_hash="ab",
        result_status="ok",
        duration_ms=1.0,
        raw_payload={"value": object()},
    )
    rows = await _rows(audit_id)
    assert len(rows) == 1
    assert rows[0].payload[FULL_ROW_LOST]["error_class"]
    assert rows[0].raw_payload is None
    assert rows[0].path == OP_ID


async def test_the_audit_search_finds_rows_by_their_marks() -> None:
    tenant_id = uuid.uuid4()
    ids = {"changed": uuid.uuid4(), "lost": uuid.uuid4(), "plain": uuid.uuid4()}
    raw = {"changed": {"k": "a\x00"}, "lost": {"value": object()}, "plain": {"k": "a"}}
    for name, audit_id in ids.items():
        await write_audit_row(
            audit_id=audit_id,
            event_class=AuditEventClass.OPERATION_EXECUTION,
            operator=_operator(tenant_id),
            descriptor=_descriptor(),
            target=None,
            params_hash="ab",
            result_status="ok",
            duration_ms=1.0,
            raw_payload=raw[name],
        )

    async def _found(**filters: Any) -> set[uuid.UUID]:
        async with get_sessionmaker()() as session:
            result = await query_audit(
                AuditQueryFilters(**filters), tenant_id=tenant_id, session=session
            )
        return {row.id for row in result.rows}

    assert await _found(full_row_lost=True) == {ids["lost"]}
    assert await _found(full_row_lost=False) == {ids["changed"], ids["plain"]}
    assert await _found(text_changed=True) == {ids["changed"]}
    assert await _found(text_changed=False) == {ids["lost"], ids["plain"]}
