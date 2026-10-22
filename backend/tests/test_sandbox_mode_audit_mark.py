# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The audit mark ``audit_log.sandbox_mode`` (#4622).

One model default sets the mark for every writer: the ORM insert hook on
the model reads the process flag, so no writer names it. This file proves:

* no ``AuditLog(...)`` construction in ``src/`` passes ``sandbox_mode``
  (the inventory of ``test_audit_event_class.py``), so the one default is
  the only source;
* a row built through the model reads ``true`` with the mode on and
  ``false`` with it off, through the shared writers too;
* a row inserted without the column, as an older build writes it, reads
  ``false``.
"""

from __future__ import annotations

import ast
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog, EndpointDescriptor
from meho_backplane.operations._audit import audit_safe
from meho_backplane.settings import get_settings
from tests._sandbox_mode_helpers import turn_sandbox_mode_off, turn_sandbox_mode_on

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "meho_backplane"
_TENANT = uuid.UUID("00000000-0000-0000-0000-000000004622")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _audit_log_calls_passing_sandbox_mode() -> list[str]:
    hits: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "AuditLog":
                continue
            if any(keyword.arg == "sandbox_mode" for keyword in node.keywords):
                hits.append(f"{path.relative_to(_SRC_ROOT)}:{node.lineno}")
    return hits


def test_no_writer_names_the_mark() -> None:
    """Every writer in the inventory relies on the one model default."""
    assert _audit_log_calls_passing_sandbox_mode() == []


def test_model_default_is_the_process_flag() -> None:
    """The column has no Core default; the ORM insert hook is the one default."""
    from sqlalchemy import event

    from meho_backplane.db.models import _stamp_audit_sandbox_mode

    column = AuditLog.__table__.c.sandbox_mode
    assert column.nullable is False
    assert column.default is None
    assert column.server_default is not None
    assert event.contains(AuditLog, "before_insert", _stamp_audit_sandbox_mode)


async def _marks() -> dict[str, bool]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    return {row.operator_sub: bool(row.sandbox_mode) for row in rows}


def _row(sub: str) -> AuditLog:
    return AuditLog(
        id=uuid.uuid4(),
        event_class=AuditEventClass.OPERATION_EXECUTION,
        occurred_at=datetime.now(UTC),
        operator_sub=sub,
        tenant_id=_TENANT,
        method="POST",
        path="/x",
        status_code=200,
        duration_ms=Decimal("1.00"),
        payload={},
    )


@pytest.mark.asyncio
async def test_rows_carry_the_mode_they_were_written_in(monkeypatch: pytest.MonkeyPatch) -> None:
    turn_sandbox_mode_off(monkeypatch)
    async with get_sessionmaker()() as session:
        session.add(_row("off"))
        await session.commit()
    turn_sandbox_mode_on(monkeypatch)
    async with get_sessionmaker()() as session:
        session.add(_row("on"))
        await session.commit()
    assert await _marks() == {"off": False, "on": True}


@pytest.mark.asyncio
async def test_shared_writer_carries_the_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    """``audit_safe`` (the dispatcher's writer) builds the row through the model."""
    descriptor = EndpointDescriptor(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        op_id="GET:/vcenter/vm",
        source_kind="ingested",
        safety_level="safe",
        requires_approval=False,
        parameter_schema={},
    )
    for mode, sub in ((False, "writer-off"), (True, "writer-on")):
        if mode:
            turn_sandbox_mode_on(monkeypatch)
        else:
            turn_sandbox_mode_off(monkeypatch)
        operator = Operator(
            sub=sub,
            raw_jwt="<jwt>",
            tenant_id=_TENANT,
            tenant_role=TenantRole.OPERATOR,
            principal_kind=PrincipalKind.USER,
        )
        await audit_safe(
            audit_id=uuid.uuid4(),
            event_class=AuditEventClass.OPERATION_EXECUTION,
            operator=operator,
            descriptor=descriptor,
            target=None,
            params_hash="x",
            result_status="ok",
            duration_ms=1.0,
        )
    assert await _marks() == {"writer-off": False, "writer-on": True}


@pytest.mark.asyncio
async def test_row_written_without_the_column_reads_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """An older build does not know the column; the database default covers it."""
    turn_sandbox_mode_on(monkeypatch)
    async with get_sessionmaker()() as session:
        await session.execute(
            text(
                "INSERT INTO audit_log (id, occurred_at, operator_sub, method, path, "
                "status_code, payload) VALUES (:id, :now, 'older-build', 'GET', '/y', 200, '{}')"
            ),
            {"id": uuid.uuid4().hex, "now": datetime.now(UTC)},
        )
        await session.commit()
    assert await _marks() == {"older-build": False}
