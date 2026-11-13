# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The audit row of a call is stored whatever text the target answers with.

A fake target answers with a NUL character (in a value, in a key and in an
error text), a lone surrogate and a NaN number. For each one the call returns
its normal answer and one audit row is stored, with a visible marker where
the text was changed. When the database refuses the full row, a short row
with the ``full_row_lost`` mark is stored. When the database cannot be
reached, the call answers ``audit_commit_failed``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import sqlalchemy as sa
import sqlalchemy.exc as sa_exc
import structlog
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_engine, get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._audit import write_audit_row
from meho_backplane.operations.audit_row_safe import (
    FULL_ROW_LOST,
    INVALID_CHARACTER_MARKER,
    NUL_MARKER,
    is_database_unreachable,
    make_storable,
)
from meho_backplane.settings import get_settings

TENANT_ID = UUID("00000000-0000-0000-0000-0000000064b1")
CONNECTOR_ID = "demo-1.x"
OP_ID = "demo.thing.write"
EMBEDDING = [0.1] * 384

#: What the fake target answers with, set by each test.
answer: dict[str, Any] = {}


async def target_handler(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    """Typed handler standing in for a vendor call."""
    if "raise" in answer:
        raise RuntimeError(answer["raise"])
    return answer["value"]


class _DemoConnector(Connector):
    product = "demo"
    version = "1.x"
    impl_id = "demo"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:  # type: ignore[override]
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    answer.clear()
    yield
    reset_dispatcher_caches()
    clear_registry()
    answer.clear()
    get_settings.cache_clear()


@pytest.fixture
async def demo_op() -> None:
    service = AsyncMock()
    service.encode_one.return_value = EMBEDDING
    service.encode.return_value = [EMBEDDING]
    service.dimension = 384
    register_connector_v2(product="demo", version="", impl_id="", cls=_DemoConnector)
    await register_typed_operation(
        product="demo",
        version="1.x",
        impl_id="demo",
        op_id=OP_ID,
        handler=target_handler,
        summary="Demo op.",
        description="Demo op for the audit row text tests.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=service,
        safety_level="safe",
        requires_approval=False,
    )


def _operator() -> Operator:
    return Operator(
        sub="op-safe",
        name="Safe Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _call() -> Any:
    return await dispatch(
        operator=_operator(),
        connector_id=CONNECTOR_ID,
        op_id=OP_ID,
        target=None,
        params={},
    )


async def _rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        query = select(AuditLog).where(AuditLog.method == "DISPATCH", AuditLog.path == OP_ID)
        return list((await session.execute(query)).scalars().all())


def _refuse_audit_inserts(error: Exception, *, times: int | None) -> Any:
    """Make the database raise *error* on an insert into ``audit_log``.

    Returns the listener; the caller removes it. *times* ``None`` means every
    insert, a number means that many inserts.
    """
    seen = {"count": 0}

    def _listener(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        if statement.lstrip().upper().startswith("INSERT INTO AUDIT_LOG"):
            seen["count"] += 1
            if times is None or seen["count"] <= times:
                raise error

    sa.event.listen(get_engine().sync_engine, "before_cursor_execute", _listener)
    return _listener


def _stop_refusing(listener: Any) -> None:
    sa.event.remove(get_engine().sync_engine, "before_cursor_execute", listener)


def _data_error() -> Exception:
    return sa_exc.DataError("INSERT INTO audit_log", {}, Exception("refused"))


def _no_text_that_the_table_refuses(value: Any) -> bool:
    text = repr(value)
    return "\x00" not in text and not any("\ud800" <= ch <= "\udfff" for ch in text)


# ---------------------------------------------------------------------------
# make_storable
# ---------------------------------------------------------------------------


def test_nul_and_lone_surrogate_get_a_visible_marker() -> None:
    assert make_storable("a\x00b") == f"a{NUL_MARKER}b"
    assert make_storable("a\ud800b") == f"a{INVALID_CHARACTER_MARKER}b"


def test_a_number_json_cannot_hold_becomes_text() -> None:
    assert make_storable(float("nan")) == "nan"
    assert make_storable(float("inf")) == "inf"
    assert make_storable(float("-inf")) == "-inf"
    assert make_storable(1.5) == 1.5


def test_keys_and_nested_values_are_covered() -> None:
    value = {"k\x00": [{"x": "v\x00", "n": float("nan")}], "ok": (1, "two")}
    assert make_storable(value) == {
        f"k{NUL_MARKER}": [{"x": f"v{NUL_MARKER}", "n": "nan"}],
        "ok": [1, "two"],
    }


def test_two_keys_that_become_equal_both_stay() -> None:
    result = make_storable({"a\x00": 1, f"a{NUL_MARKER}": 2})
    assert sorted(result.values()) == [1, 2]
    assert len(result) == 2


def test_empty_and_null_values_pass_through() -> None:
    assert make_storable(None) is None
    assert make_storable([]) == []
    assert make_storable({}) == {}
    assert make_storable("") == ""


# ---------------------------------------------------------------------------
# is_database_unreachable
# ---------------------------------------------------------------------------


def test_a_connection_fault_is_unreachable() -> None:
    assert is_database_unreachable(ConnectionRefusedError())
    assert is_database_unreachable(TimeoutError())
    assert is_database_unreachable(sa_exc.OperationalError("s", {}, Exception("x")))
    assert is_database_unreachable(sa_exc.InterfaceError("s", {}, Exception("x")))
    assert is_database_unreachable(sa_exc.TimeoutError("pool"))


def test_a_sqlstate_of_a_connection_fault_is_unreachable() -> None:
    orig = Exception("x")
    orig.sqlstate = "08006"  # type: ignore[attr-defined]
    assert is_database_unreachable(sa_exc.DBAPIError("s", {}, orig))


def test_a_data_error_is_not_unreachable() -> None:
    orig = Exception("x")
    orig.sqlstate = "22021"  # type: ignore[attr-defined]
    assert not is_database_unreachable(sa_exc.DBAPIError("s", {}, orig))
    assert not is_database_unreachable(_data_error())
    assert not is_database_unreachable(ValueError("nan"))


# ---------------------------------------------------------------------------
# A target answers with text the table refuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "marker"),
    [
        ({"name": "a\x00b"}, NUL_MARKER),
        ({"k\x00ey": "v"}, NUL_MARKER),
        ({"name": "a\ud800b"}, INVALID_CHARACTER_MARKER),
    ],
)
async def test_a_target_answer_with_bad_text_is_stored_with_a_marker(
    demo_op: None, value: dict[str, Any], marker: str
) -> None:
    answer["value"] = value
    result = await _call()
    assert result.status == "ok", result.error
    rows = await _rows()
    assert len(rows) == 1
    stored = [rows[0].payload, rows[0].raw_payload, rows[0].redaction_manifest]
    assert _no_text_that_the_table_refuses(stored)
    assert FULL_ROW_LOST not in rows[0].payload
    assert marker in repr(stored)


async def test_an_error_text_with_a_nul_character_is_stored(demo_op: None) -> None:
    answer["raise"] = "vendor said: bad\x00text"
    result = await _call()
    assert result.status == "error"
    rows = await _rows()
    assert len(rows) == 1
    assert _no_text_that_the_table_refuses(rows[0].payload)
    assert NUL_MARKER in repr(rows[0].payload)


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
async def test_a_number_json_cannot_hold_is_stored_as_text(demo_op: None, number: float) -> None:
    """The writer covers ``raw_payload`` and ``redaction_manifest`` for every caller."""
    audit_id = uuid.uuid4()
    descriptor = SimpleNamespace(
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
    await write_audit_row(
        audit_id=audit_id,
        event_class=AuditEventClass.OPERATION_EXECUTION,
        operator=_operator(),
        descriptor=descriptor,  # type: ignore[arg-type]
        target=None,
        params_hash="ab",
        result_status="ok",
        duration_ms=1.0,
        raw_payload={"load": number, "k\x00": "v\ud800"},
        redaction_manifest=[{"rule": "r", "count": number}],
    )
    rows = await _rows()
    assert len(rows) == 1
    assert FULL_ROW_LOST not in rows[0].payload
    assert rows[0].raw_payload == {
        "load": str(number),
        f"k{NUL_MARKER}": f"v{INVALID_CHARACTER_MARKER}",
    }
    assert rows[0].redaction_manifest == [{"rule": "r", "count": str(number)}]


async def test_a_value_that_cannot_be_turned_into_json_gives_a_short_row(demo_op: None) -> None:
    audit_id = uuid.uuid4()
    await write_audit_row(
        audit_id=audit_id,
        event_class=AuditEventClass.OPERATION_EXECUTION,
        operator=_operator(),
        descriptor=SimpleNamespace(  # type: ignore[arg-type]
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
        ),
        target=None,
        params_hash="ab",
        result_status="ok",
        duration_ms=1.0,
        raw_payload={"value": object()},
    )
    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].payload[FULL_ROW_LOST]["error_class"]
    assert rows[0].raw_payload is None


# ---------------------------------------------------------------------------
# The database refuses the full row
# ---------------------------------------------------------------------------


async def test_a_short_row_is_stored_when_the_full_row_is_refused_once(demo_op: None) -> None:
    answer["value"] = {"ok": True}
    listener = _refuse_audit_inserts(_data_error(), times=1)
    try:
        with structlog.testing.capture_logs() as logs:
            result = await _call()
    finally:
        _stop_refusing(listener)
    assert result.status == "ok", result.error
    assert result.extras.get("error_code") != "audit_commit_failed"
    rows = await _rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.payload == {FULL_ROW_LOST: {"error_class": "DataError"}}
    assert row.raw_payload is None
    assert row.operator_sub == "op-safe"
    assert row.tenant_id == TENANT_ID
    assert row.path == OP_ID
    assert row.status_code == 200
    lost = [entry for entry in logs if entry["event"] == "audit_full_row_lost"]
    assert len(lost) == 1
    assert lost[0]["error_class"] == "DataError"


async def test_the_error_path_stores_a_short_row_too(demo_op: None) -> None:
    answer["raise"] = "vendor failed"
    listener = _refuse_audit_inserts(_data_error(), times=1)
    try:
        result = await _call()
    finally:
        _stop_refusing(listener)
    assert result.status == "error"
    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].payload == {FULL_ROW_LOST: {"error_class": "DataError"}}
    assert rows[0].status_code != 200


async def test_an_unreachable_database_answers_audit_commit_failed(demo_op: None) -> None:
    answer["value"] = {"ok": True}
    listener = _refuse_audit_inserts(
        sa_exc.OperationalError("s", {}, Exception("down")), times=None
    )
    try:
        with structlog.testing.capture_logs() as logs:
            result = await _call()
    finally:
        _stop_refusing(listener)
    assert result.status == "error"
    assert result.extras["error_code"] == "audit_commit_failed"
    assert await _rows() == []
    assert not [entry for entry in logs if entry["event"] == "audit_full_row_lost"]


async def test_a_short_row_that_is_refused_too_answers_audit_commit_failed(
    demo_op: None,
) -> None:
    answer["value"] = {"ok": True}
    listener = _refuse_audit_inserts(_data_error(), times=None)
    try:
        result = await _call()
    finally:
        _stop_refusing(listener)
    assert result.extras["error_code"] == "audit_commit_failed"
    assert await _rows() == []


# ---------------------------------------------------------------------------
# A failed audit write names the call
# ---------------------------------------------------------------------------


async def test_the_log_line_of_a_failed_audit_write_names_the_call(demo_op: None) -> None:
    answer["value"] = {"ok": True}
    request_id = uuid.uuid4()
    structlog.contextvars.bind_contextvars(request_id=str(request_id))
    listener = _refuse_audit_inserts(_data_error(), times=None)
    try:
        with structlog.testing.capture_logs() as logs:
            await _call()
    finally:
        _stop_refusing(listener)
        structlog.contextvars.unbind_contextvars("request_id")
    failed = [entry for entry in logs if entry["event"] == "dispatch_audit_failed"]
    assert len(failed) == 1
    entry = failed[0]
    assert entry["request_id"] == request_id
    assert entry["op_id"] == OP_ID
    assert entry["operator_sub"] == "op-safe"
    assert "target" in entry
    assert "target_id" in entry
