# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every durable ``audit_log`` writer declares its event class (#3733).

* **Writer inventory.** Scans every module under ``meho_backplane`` and
  fails on any ``AuditLog(...)`` call without an explicit ``event_class=``
  keyword, and on any bulk ``insert(AuditLog)``. The shared writer helpers
  must take ``event_class`` as a required argument, so their callers
  declare it too.
* **Database.** An unknown class is rejected; a row written without a class
  (an old row) reads as ``legacy_request``.
* **Audit query.** The ``event_class`` filter narrows by class. Without it
  the query returns every class, as before.
"""

from __future__ import annotations

import ast
import inspect
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane
from meho_backplane.audit_query import AuditQueryFilters, query_audit
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AUDIT_EVENT_CLASSES, AuditEventClass, AuditLog
from meho_backplane.memory.audit import write_internal_audit_row
from meho_backplane.operations import approval_queue
from meho_backplane.operations._audit import audit_and_broadcast_safe, write_audit_row
from meho_backplane.settings import get_settings

_SRC_ROOT = Path(meho_backplane.__file__).resolve().parent


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the Keycloak + Vault env vars :class:`Settings` requires."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


# ---------------------------------------------------------------------------
# Writer inventory
# ---------------------------------------------------------------------------


def _called_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_audit_log(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "AuditLog") or (
        isinstance(node, ast.Attribute) and node.attr == "AuditLog"
    )


def _scan_source(source: str, where: str) -> tuple[list[str], list[str]]:
    """Return ``(classified, problems)`` for one module, as ``where:line`` strings."""
    classified: list[str] = []
    problems: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node)
        if name == "AuditLog":
            keyword = next((k for k in node.keywords if k.arg == "event_class"), None)
            if keyword is None:
                problems.append(f"{where}:{node.lineno}: AuditLog(...) without event_class=")
            elif isinstance(keyword.value, ast.Constant) and keyword.value.value is None:
                problems.append(f"{where}:{node.lineno}: AuditLog(event_class=None)")
            else:
                classified.append(f"{where}:{node.lineno}")
        elif name == "insert" and node.args and _is_audit_log(node.args[0]):
            problems.append(
                f"{where}:{node.lineno}: bulk insert(AuditLog) bypasses the "
                "writer inventory; build AuditLog(..., event_class=...) rows instead"
            )
    return classified, problems


def test_every_audit_log_construction_declares_an_event_class() -> None:
    """No writer in the codebase builds an ``AuditLog`` row without a class."""
    classified: list[str] = []
    problems: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        found, bad = _scan_source(
            path.read_text(encoding="utf-8"), str(path.relative_to(_SRC_ROOT))
        )
        classified += found
        problems += bad
    assert problems == [], "unclassified audit writers:\n" + "\n".join(problems)
    # Guard against a scan that silently finds nothing (wrong root, renamed
    # model): the codebase has well over a dozen direct writers today.
    assert len(classified) >= 15, classified


def test_inventory_flags_each_unclassified_shape() -> None:
    """The scan catches a missing class, a ``None`` class, ``**kwargs``, and bulk insert."""
    source = (
        "a = AuditLog(id=1, method='GET')\n"
        "b = models.AuditLog(**kwargs)\n"
        "c = AuditLog(id=2, event_class=None)\n"
        "d = sa.insert(AuditLog)\n"
        "e = AuditLog(id=3, event_class=AuditEventClass.SECURITY_EVENT)\n"
    )
    classified, problems = _scan_source(source, "example.py")
    assert classified == ["example.py:5"]
    assert [p.split(":")[1] for p in problems] == ["1", "2", "3", "4"]


@pytest.mark.parametrize(
    "writer",
    [
        write_audit_row,
        audit_and_broadcast_safe,
        write_internal_audit_row,
        approval_queue._write_audit_row,
    ],
    ids=lambda f: f.__qualname__,
)
def test_shared_writers_require_an_event_class(writer: object) -> None:
    """Shared writer helpers take ``event_class`` with no default value."""
    param = inspect.signature(writer).parameters["event_class"]  # type: ignore[arg-type]
    assert param.default is inspect.Parameter.empty
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_vocabulary_is_the_closed_five() -> None:
    assert set(AUDIT_EVENT_CLASSES) == {
        "operation_execution",
        "policy_decision",
        "approval_decision",
        "security_event",
        "legacy_request",
    }


# ---------------------------------------------------------------------------
# Database contract
# ---------------------------------------------------------------------------


def _row(*, event_class: str | None, tenant_id: uuid.UUID, occurred_at: datetime) -> AuditLog:
    """Build one row; ``event_class=None`` leaves the column to the DB default."""
    kwargs: dict[str, object] = {
        "id": uuid.uuid4(),
        "occurred_at": occurred_at,
        "operator_sub": "op-1",
        "tenant_id": tenant_id,
        "method": "DISPATCH",
        "path": "vault.kv.read",
        "status_code": 200,
        "duration_ms": Decimal("1.0"),
        "payload": {},
    }
    if event_class is not None:
        kwargs["event_class"] = event_class
    return AuditLog(**kwargs)


@pytest.mark.asyncio
async def test_database_rejects_unknown_class(session: AsyncSession) -> None:
    session.add(
        _row(event_class="bogus", tenant_id=uuid.uuid4(), occurred_at=datetime.now(UTC)),
    )
    with pytest.raises(IntegrityError, match="ck_audit_log_event_class"):
        await session.commit()


@pytest.mark.asyncio
async def test_row_without_class_reads_as_legacy_request(session: AsyncSession) -> None:
    """The server default gives an unclassified (old) row ``legacy_request``."""
    row = _row(event_class=None, tenant_id=uuid.uuid4(), occurred_at=datetime.now(UTC))
    session.add(row)
    await session.commit()
    await session.refresh(row)
    assert row.event_class == AuditEventClass.LEGACY_REQUEST


# ---------------------------------------------------------------------------
# Audit query filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_class_filter_narrows_and_no_filter_keeps_every_row(
    session: AsyncSession,
) -> None:
    tenant_id = uuid.uuid4()
    start = datetime(2026, 10, 1, tzinfo=UTC)
    rows = {
        cls: _row(event_class=cls, tenant_id=tenant_id, occurred_at=start + timedelta(seconds=i))
        for i, cls in enumerate(
            [None, "operation_execution", "policy_decision", "approval_decision"]
        )
    }
    session.add_all(rows.values())
    await session.commit()

    everything = await query_audit(AuditQueryFilters(), tenant_id=tenant_id, session=session)
    assert {e.id for e in everything.rows} == {r.id for r in rows.values()}
    assert {e.event_class for e in everything.rows} == {
        "legacy_request",
        "operation_execution",
        "policy_decision",
        "approval_decision",
    }

    executed = await query_audit(
        AuditQueryFilters(event_class=AuditEventClass.OPERATION_EXECUTION),
        tenant_id=tenant_id,
        session=session,
    )
    assert [e.id for e in executed.rows] == [rows["operation_execution"].id]

    legacy = await query_audit(
        AuditQueryFilters(event_class=AuditEventClass.LEGACY_REQUEST),
        tenant_id=tenant_id,
        session=session,
    )
    assert [e.id for e in legacy.rows] == [rows[None].id]
