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

import asyncio
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import sqlalchemy as sa
import sqlalchemy.exc as sa_exc
import structlog
from prometheus_client import REGISTRY
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.api.v1.audit_models import AuditQueryRequest
from meho_backplane.audit_query import AuditQueryFilters
from meho_backplane.audit_query.query import query_audit
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_engine, get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog
from meho_backplane.operations import _audit as audit_module
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._audit import write_audit_row
from meho_backplane.operations.audit_row_safe import (
    FULL_ROW_LOST,
    INVALID_CHARACTER_MARKER,
    MAX_DURATION_MS,
    NUL_MARKER,
    TEXT_CHANGED,
    is_database_unreachable,
    make_storable,
    short_row_payload,
    storable_and_changed,
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
    if "audit_extras" in answer:
        structlog.contextvars.bind_contextvars(**answer["audit_extras"])
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


async def _call(op_id: str = OP_ID, params: dict[str, Any] | None = None) -> Any:
    return await dispatch(
        operator=_operator(),
        connector_id=CONNECTOR_ID,
        op_id=op_id,
        target=None,
        params=params or {},
    )


async def _rows(op_id: str = OP_ID) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        query = select(AuditLog).where(AuditLog.method == "DISPATCH", AuditLog.path == op_id)
        return list((await session.execute(query)).scalars().all())


def _refuse_audit_inserts(error: Exception, *, times: int | None) -> Any:
    """Make the database raise *error* on an insert of a DISPATCH row into ``audit_log``.

    Returns the listener; the caller removes it. *times* ``None`` means every
    insert, a number means that many inserts.
    """
    seen = {"count": 0}

    def _listener(_conn: Any, _cursor: Any, statement: str, parameters: Any, *_rest: Any) -> None:
        if statement.lstrip().upper().startswith("INSERT INTO AUDIT_LOG") and (
            "DISPATCH" in repr(parameters)
        ):
            seen["count"] += 1
            if times is None or seen["count"] <= times:
                raise error

    sa.event.listen(get_engine().sync_engine, "before_cursor_execute", _listener)
    return _listener


def _stop_refusing(listener: Any) -> None:
    sa.event.remove(get_engine().sync_engine, "before_cursor_execute", listener)


def _data_error() -> Exception:
    return sa_exc.DataError("INSERT INTO audit_log", {}, Exception("refused"))


def _sqlstate_error(sqlstate: str) -> sa_exc.DBAPIError:
    orig = Exception("x")
    orig.sqlstate = sqlstate  # type: ignore[attr-defined]
    return sa_exc.DBAPIError("INSERT INTO audit_log", {}, orig)


def _assert_short_row(payload: dict[str, Any], error_class: str, result_status: str = "ok") -> None:
    """A short row keeps every value MEHO builds itself, and no error text."""
    assert payload[FULL_ROW_LOST] == {"error_class": error_class}
    assert payload["op_id"] == OP_ID
    assert payload["op_class"] == "write"
    assert payload["result_status"] == result_status
    assert payload["source_kind"] == "typed"
    assert payload["connector_product"] == "demo"
    assert payload["connector_version"] == "1.x"
    assert payload["connector_impl_id"] == "demo"
    assert payload["principal_name"] == "Safe Test"
    assert isinstance(payload["params_hash"], str)
    assert "error" not in payload


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


@pytest.mark.parametrize("sqlstate", ["08006", "08001", "53300", "57P01", "57P03", "57P05"])
def test_a_sqlstate_of_a_connection_fault_is_unreachable(sqlstate: str) -> None:
    assert is_database_unreachable(_sqlstate_error(sqlstate))


@pytest.mark.parametrize("sqlstate", ["57014", "53100", "53200"])
def test_a_cancelled_statement_or_a_resource_error_is_not_unreachable(sqlstate: str) -> None:
    """57014 is a statement timeout: the database still takes a short row."""
    assert not is_database_unreachable(_sqlstate_error(sqlstate))


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
    _assert_short_row(row.payload, "DataError")
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
    _assert_short_row(rows[0].payload, "DataError", result_status="error")
    assert rows[0].status_code != 200


async def test_a_statement_timeout_on_the_full_row_gives_a_short_row(demo_op: None) -> None:
    answer["value"] = {"ok": True}
    listener = _refuse_audit_inserts(_sqlstate_error("57014"), times=1)
    try:
        result = await _call()
    finally:
        _stop_refusing(listener)
    assert result.status == "ok", result.error
    rows = await _rows()
    assert len(rows) == 1
    _assert_short_row(rows[0].payload, "DBAPIError")


async def test_the_operation_filter_finds_a_short_row(demo_op: None) -> None:
    answer["value"] = {"ok": True}
    listener = _refuse_audit_inserts(_data_error(), times=1)
    try:
        await _call()
    finally:
        _stop_refusing(listener)
    async with get_sessionmaker()() as session:
        found = await query_audit(
            AuditQueryFilters(op_id=OP_ID, op_class="write"), tenant_id=TENANT_ID, session=session
        )
    assert len(found.rows) == 1
    entry = found.rows[0]
    assert FULL_ROW_LOST in entry.payload
    assert entry.op_id == OP_ID
    assert entry.op_class == "write"


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


# ---------------------------------------------------------------------------
# The short row keeps what MEHO builds itself
# ---------------------------------------------------------------------------


async def test_a_short_row_keeps_the_reveal_choice(demo_op: None) -> None:
    answer["value"] = {"secret": "s"}
    listener = _refuse_audit_inserts(_data_error(), times=1)
    try:
        result = await _call(params={"reveal_secret": True})
    finally:
        _stop_refusing(listener)
    assert result.status == "ok", result.error
    rows = await _rows()
    assert len(rows) == 1
    _assert_short_row(rows[0].payload, "DataError")
    assert rows[0].payload["reveal_secret"] is True


async def test_a_short_row_keeps_the_approval_request_id(demo_op: None) -> None:
    """The row of a call that waits for approval keeps the request id from the code value."""
    request_id = uuid.uuid4()
    listener = _refuse_audit_inserts(_data_error(), times=1)
    try:
        await _write(
            result_status="pending",
            error_extras={
                "error_code": "approval_required",
                "approval_request_id": str(request_id),
                "message": f"waits for approval {uuid.uuid4()}",
            },
        )
    finally:
        _stop_refusing(listener)
    rows = await _rows()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload[FULL_ROW_LOST] == {"error_class": "DataError"}
    assert payload["approval_request_id"] == str(request_id)
    assert payload["result_status"] == "pending"
    assert "error" not in payload


def test_an_approval_request_id_that_is_not_a_uuid_is_left_out() -> None:
    short = short_row_payload(
        ValueError("x"), {"op_id": OP_ID}, error_extras={"approval_request_id": "free text"}
    )
    assert "approval_request_id" not in short
    assert short == {"op_id": OP_ID, FULL_ROW_LOST: {"error_class": "ValueError"}}


def test_a_short_row_leaves_out_the_handler_extras_and_the_error() -> None:
    core = {"op_id": OP_ID, "params_hash": "ab", "principal_name": "Safe Test"}
    short = short_row_payload(
        ValueError("x"),
        core,
        error_extras={"error_code": "e", "upstream_message": "target text"},
        payload={**core, "vendor_note": "target text", "error": {"x": "target text"}},
    )
    assert "target text" not in repr(short)
    assert short["params_hash"] == "ab"


async def test_the_audit_search_finds_a_short_row_by_principal_name(demo_op: None) -> None:
    answer["value"] = {"ok": True}
    listener = _refuse_audit_inserts(_data_error(), times=1)
    try:
        await _call()
    finally:
        _stop_refusing(listener)
    async with get_sessionmaker()() as session:
        found = await query_audit(
            AuditQueryFilters(principal="Safe Test"), tenant_id=TENANT_ID, session=session
        )
    assert len(found.rows) == 1
    assert FULL_ROW_LOST in found.rows[0].payload
    assert found.rows[0].principal_name == "Safe Test"


# ---------------------------------------------------------------------------
# The text_changed mark
# ---------------------------------------------------------------------------


def test_a_replaced_character_or_number_counts_as_a_change() -> None:
    assert storable_and_changed("a\x00") == (f"a{NUL_MARKER}", True)
    assert storable_and_changed({"n": float("nan")}) == ({"n": "nan"}, True)
    assert storable_and_changed(f"plain {NUL_MARKER}") == (f"plain {NUL_MARKER}", False)
    assert storable_and_changed({"a": [1, 2.5, None, True]}) == ({"a": [1, 2.5, None, True]}, False)


async def test_an_answer_with_the_marker_as_plain_text_gets_no_mark(demo_op: None) -> None:
    answer["value"] = {"name": f"plain {NUL_MARKER}", TEXT_CHANGED: True}
    result = await _call()
    assert result.status == "ok", result.error
    rows = await _rows()
    assert len(rows) == 1
    assert TEXT_CHANGED not in rows[0].payload


async def test_an_answer_with_a_nul_character_gets_the_mark(demo_op: None) -> None:
    answer["value"] = {"name": "a\x00b"}
    result = await _call()
    assert result.status == "ok", result.error
    rows = await _rows()
    assert rows[0].payload[TEXT_CHANGED] is True


async def test_a_nan_through_the_dispatcher_is_stored_with_the_mark(demo_op: None) -> None:
    """A NaN in the answer and in a handler's audit extra: the full row is stored.

    The connector-boundary redaction already replaces the NaN of the answer;
    the NaN of the handler's ``audit_*`` extra reaches the writer, which
    writes it as text and sets the mark.
    """
    answer["value"] = {"items": [{"ratio": float("nan")}]}
    answer["audit_extras"] = {"audit_ratio": float("nan")}
    try:
        result = await _call()
    finally:
        structlog.contextvars.unbind_contextvars("audit_ratio")
    assert result.status == "ok", result.error
    rows = await _rows()
    assert len(rows) == 1
    assert FULL_ROW_LOST not in rows[0].payload
    assert rows[0].payload["ratio"] == "nan"
    assert rows[0].payload[TEXT_CHANGED] is True
    assert _no_text_that_the_table_refuses(rows[0].raw_payload)


# ---------------------------------------------------------------------------
# Values JSON has no type for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("1.50"), "1.50"),
        (datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC), "2026-01-02T03:04:05+00:00"),
        (date(2026, 1, 2), "2026-01-02"),
        (UUID("00000000-0000-0000-0000-000000000001"), "00000000-0000-0000-0000-000000000001"),
        (b"ab", "ab"),
        (bytearray(b"ab"), "ab"),
        ({3, 1, 2}, [1, 2, 3]),
        (frozenset({"b", "a"}), ["a", "b"]),
    ],
)
def test_a_value_json_has_no_type_for_becomes_text(value: Any, expected: Any) -> None:
    assert storable_and_changed(value) == (expected, False)


def test_bytes_with_a_nul_character_get_the_marker() -> None:
    assert storable_and_changed(b"a\x00\xff") == (f"a{NUL_MARKER}\\xff", True)


def test_a_key_that_is_not_text_becomes_its_text_form() -> None:
    value = {2: "a", None: "b", True: "c", 1.5: "d", (1, 2): "e"}
    assert storable_and_changed(value) == (
        {"2": "a", "null": "b", "true": "c", "1.5": "d", "(1, 2)": "e"},
        False,
    )


def test_two_keys_with_the_same_text_form_both_stay() -> None:
    stored, changed = storable_and_changed({1: "a", "1": "b"})
    assert stored == {"1": "a", "1#2": "b"}
    assert changed


async def test_values_json_has_no_type_for_do_not_shorten_the_row(demo_op: None) -> None:
    raw = {
        "cost": Decimal("1.5"),
        "when": datetime(2026, 1, 2, tzinfo=UTC),
        "day": date(2026, 1, 2),
        "id": UUID("00000000-0000-0000-0000-000000000001"),
        "blob": b"ab",
        "tags": {"x"},
        7: "seven",
    }
    await _write(raw_payload=raw)
    rows = await _rows()
    assert len(rows) == 1
    assert FULL_ROW_LOST not in rows[0].payload
    assert TEXT_CHANGED not in rows[0].payload
    assert rows[0].raw_payload == {
        "cost": "1.5",
        "when": "2026-01-02T00:00:00+00:00",
        "day": "2026-01-02",
        "id": "00000000-0000-0000-0000-000000000001",
        "blob": "ab",
        "tags": ["x"],
        "7": "seven",
    }


# ---------------------------------------------------------------------------
# A cancel during the write, and duration_ms
# ---------------------------------------------------------------------------


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


async def _write(audit_id: uuid.UUID | None = None, **kwargs: Any) -> None:
    fields: dict[str, Any] = {
        "audit_id": audit_id or uuid.uuid4(),
        "event_class": AuditEventClass.OPERATION_EXECUTION,
        "operator": _operator(),
        "descriptor": _descriptor(),
        "target": None,
        "params_hash": "ab",
        "result_status": "ok",
        "duration_ms": 1.0,
    }
    await write_audit_row(**{**fields, **kwargs})


async def test_a_cancel_during_the_insert_still_stores_the_row(
    demo_op: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()
    commit = AsyncSession.commit

    async def _slow_commit(self: AsyncSession) -> None:
        started.set()
        await asyncio.sleep(0.05)
        await commit(self)

    monkeypatch.setattr(AsyncSession, "commit", _slow_commit)
    audit_id = uuid.uuid4()
    task = asyncio.create_task(_write(audit_id))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.undo()
    rows = await _rows()
    assert [row.id for row in rows] == [audit_id]


async def test_a_duration_too_large_for_the_column_is_cut(demo_op: None) -> None:
    await _write(duration_ms=1e12)
    listener = _refuse_audit_inserts(_data_error(), times=1)
    try:
        await _write(duration_ms=float("inf"))
    finally:
        _stop_refusing(listener)
    rows = await _rows()
    assert len(rows) == 2
    assert {row.duration_ms for row in rows} == {MAX_DURATION_MS}
    assert sum(FULL_ROW_LOST in row.payload for row in rows) == 1


# ---------------------------------------------------------------------------
# Operators see shortened and changed rows
# ---------------------------------------------------------------------------


def _counter(name: str, labels: dict[str, str] | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


async def test_the_counters_count_short_and_changed_rows(demo_op: None) -> None:
    lost_before = _counter("meho_audit_full_row_lost_total", {"error_class": "DataError"})
    changed_before = _counter("meho_audit_text_changed_total")
    await _write(raw_payload={"k": "a\x00"})
    listener = _refuse_audit_inserts(_data_error(), times=1)
    try:
        await _write()
    finally:
        _stop_refusing(listener)
    assert _counter("meho_audit_full_row_lost_total", {"error_class": "DataError"}) == (
        lost_before + 1
    )
    assert _counter("meho_audit_text_changed_total") == changed_before + 1


async def test_the_audit_search_filters_short_and_changed_rows(demo_op: None) -> None:
    changed_id, lost_id, plain_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _write(changed_id, raw_payload={"k": "a\x00"})
    await _write(plain_id, raw_payload={"k": "a"})
    listener = _refuse_audit_inserts(_data_error(), times=1)
    try:
        await _write(lost_id)
    finally:
        _stop_refusing(listener)

    async def _ids(**filters: Any) -> set[uuid.UUID]:
        async with get_sessionmaker()() as session:
            found = await query_audit(
                AuditQueryFilters(**filters), tenant_id=TENANT_ID, session=session
            )
        return {row.id for row in found.rows}

    assert await _ids(full_row_lost=True) == {lost_id}
    assert await _ids(full_row_lost=False) == {changed_id, plain_id}
    assert await _ids(text_changed=True) == {changed_id}
    assert await _ids(text_changed=False) == {lost_id, plain_id}


def test_the_rest_body_takes_the_two_filters() -> None:
    filters = AuditQueryRequest(full_row_lost=True, text_changed=False).to_filters()
    assert filters.full_row_lost is True
    assert filters.text_changed is False


# ---------------------------------------------------------------------------
# A normal row is stored as before
# ---------------------------------------------------------------------------


async def test_a_normal_row_is_stored_exactly_as_built(
    demo_op: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[Any] = []

    def _keep(value: Any) -> tuple[Any, bool]:
        built.append(value)
        return storable_and_changed(value)

    monkeypatch.setattr(audit_module, "storable_and_changed", _keep)
    answer["value"] = {"name": "vm-1", "count": 3, "ratio": 2.5, "tags": ["a"], "on": None}
    result = await _call()
    assert result.status == "ok", result.error
    rows = await _rows()
    assert len(rows) == 1
    payload, raw_payload, _manifest = built
    assert rows[0].payload == json.loads(json.dumps(payload))
    assert rows[0].raw_payload == json.loads(json.dumps(raw_payload))
    assert TEXT_CHANGED not in rows[0].payload
