# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MEHO checks the audit row of a call before it sends the call.

A value the audit table cannot store is refused with ``audit_row_invalid``,
which names the field.

The tests cover each way into the dispatcher (a direct call, a step of a
multi-step operation, a call resumed after approval, and ``call_operation``).
In each one the fake target records zero calls. The runbook, scheduler and
hosted agent paths have their own tests in ``test_audit_row_precheck_paths.py``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from starlette.responses import JSONResponse

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_engine, get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, EndpointDescriptor
from meho_backplane.operations import (
    DispatchChild,
    audit_row_precheck,
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations._audit import step_id_var, work_ref_var
from meho_backplane.operations._errors import result_audit_row_invalid
from meho_backplane.operations.audit_row_precheck import (
    AFTER_CALL_JSON_COLUMNS,
    CHECKED_JSON_COLUMNS,
    CHECKED_TEXT_COLUMNS,
    FIXED_TEXT_COLUMNS,
    INDEX_TEXT_MAX_BYTES,
    INDEXED_TEXT_COLUMNS,
    audit_row_problem,
)
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings

TENANT_ID = UUID("00000000-0000-0000-0000-0000000064a7")
CONNECTOR_ID = "demo-1.x"
WRITE_OP_ID = "demo.thing.write"
APPROVAL_OP_ID = "demo.thing.needs_approval"
STEP_OP_ID = "demo.thing.step"
COMPOSITE_OP_ID = "demo.composite.bad_step"
EMBEDDING = [0.1] * 384

#: What the fake target saw: one entry per call that reached it.
target_calls: list[dict[str, Any]] = []


async def recording_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Typed handler standing in for a vendor change."""
    target_calls.append(params)
    return {"created": True}


async def bad_step_composite(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """A multi-step operation whose step runs with a ticket id the table refuses.

    The operation itself passes the check; the value turns bad before the step.
    """
    work_ref_var.set("ticket\x00bad")
    return await dispatch_child(connector_id=CONNECTOR_ID, op_id=STEP_OP_ID, params={})


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
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    target_calls.clear()
    yield
    reset_dispatcher_caches()
    clear_registry()
    target_calls.clear()


@pytest.fixture(autouse=True)
def _clean_contextvars() -> Iterator[None]:
    work_token = work_ref_var.set(None)
    step_token = step_id_var.set(None)
    yield
    step_id_var.reset(step_token)
    work_ref_var.reset(work_token)


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = EMBEDDING
    service.encode.return_value = [EMBEDDING]
    service.dimension = 384
    return service


def _operator(*, sub: str = "op-precheck") -> Operator:
    return Operator(
        sub=sub,
        name="Precheck Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _register_ops(embedding: AsyncMock) -> None:
    register_connector_v2(product="demo", version="", impl_id="", cls=_DemoConnector)
    for op_id, requires_approval in (
        (WRITE_OP_ID, False),
        (APPROVAL_OP_ID, True),
        (STEP_OP_ID, False),
    ):
        await register_typed_operation(
            product="demo",
            version="1.x",
            impl_id="demo",
            op_id=op_id,
            handler=recording_handler,
            summary="Demo op.",
            description="Demo op for the audit row check.",
            parameter_schema={"type": "object"},
            when_to_use=None,
            embedding_service=embedding,
            safety_level="safe",
            requires_approval=requires_approval,
        )
    async with get_sessionmaker()() as session:
        session.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,
                product="demo",
                version="1.x",
                impl_id="demo",
                op_id=COMPOSITE_OP_ID,
                source_kind="composite",
                method=None,
                path=None,
                handler_ref="tests.test_audit_row_precheck.bad_step_composite",
                summary="Composite.",
                description="Composite whose step has a bad ticket id.",
                tags=[],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions=None,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=EMBEDDING,
                custom_description=None,
                custom_notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def _count(model: Any) -> int:
    async with get_sessionmaker()() as session:
        return len((await session.execute(select(model))).scalars().all())


def _assert_refused(result: Any, field: str) -> None:
    status = result.status if isinstance(result, OperationResult) else result["status"]
    extras = result.extras if isinstance(result, OperationResult) else result["extras"]
    assert status == "error"
    assert extras["error_code"] == "audit_row_invalid"
    assert extras["field"] == field
    assert extras["execution_outcome"] == "not_started"
    assert target_calls == []


# ---------------------------------------------------------------------------
# The check itself
# ---------------------------------------------------------------------------


def _descriptor() -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        op_id="demo.thing.write",
        source_kind="typed",
        product="demo",
        version="1.x",
        impl_id="demo",
        safety_level="caution",
        requires_approval=False,
        method=None,
        path=None,
        tags=[],
    )


def test_a_normal_row_passes() -> None:
    assert (
        audit_row_problem(operator=_operator(), descriptor=_descriptor(), params_hash="ab") is None
    )


@pytest.mark.parametrize(
    ("sub", "work_ref", "step_id", "field"),
    [
        ("op\x00x", None, None, "operator_sub"),
        ("op-\ud800", None, None, "operator_sub"),
        ("o" * (INDEX_TEXT_MAX_BYTES + 1), None, None, "operator_sub"),
        ("op-ok", "ticket\x00", None, "work_ref"),
        ("op-ok", "t" * (INDEX_TEXT_MAX_BYTES + 1), None, "work_ref"),
        ("op-ok", "é" * (INDEX_TEXT_MAX_BYTES // 2 + 1), None, "work_ref"),
        ("op-ok", None, "step\x00", "step_id"),
    ],
    ids=[
        "nul-sub",
        "lone-surrogate",
        "long-sub",
        "nul-ticket",
        "long-ticket",
        "long-bytes",
        "step",
    ],
)
def test_a_bad_value_is_named_by_its_field(
    sub: str, work_ref: str | None, step_id: str | None, field: str
) -> None:
    work_token = work_ref_var.set(work_ref)
    step_token = step_id_var.set(step_id)
    try:
        problem = audit_row_problem(
            operator=_operator(sub=sub), descriptor=_descriptor(), params_hash="ab"
        )
    finally:
        step_id_var.reset(step_token)
        work_ref_var.reset(work_token)
    assert problem is not None
    assert problem.field == field
    assert "\x00" not in problem.reason
    assert sub not in problem.reason


def test_a_bad_value_in_the_payload_is_named_with_its_path() -> None:
    operator = _operator().model_copy(update={"name": "Bad\x00Name"})
    problem = audit_row_problem(operator=operator, descriptor=_descriptor(), params_hash="ab")
    assert problem is not None
    assert problem.field == "payload.principal_name"


@pytest.mark.parametrize(
    ("bad_key", "field"),
    [
        ("bad\ud800key", "payload.<key 1>"),
        ("bad\x00key", "payload.<key 1>"),
    ],
    ids=["lone-surrogate-key", "nul-key"],
)
def test_a_bad_key_is_named_by_its_position(
    monkeypatch: pytest.MonkeyPatch, bad_key: str, field: str
) -> None:
    """The refusal never copies a bad key, so it can still be sent as JSON."""
    monkeypatch.setattr(
        audit_row_precheck,
        "_build_audit_payload",
        lambda *_args: {"params_hash": "ab", bad_key: {"inner": "ok"}},
    )
    problem = audit_row_problem(operator=_operator(), descriptor=_descriptor(), params_hash="ab")
    assert problem is not None
    assert problem.field == field
    result = result_audit_row_invalid(WRITE_OP_ID, problem.field, problem.reason, 1.0)
    body = JSONResponse(result.model_dump(mode="json")).body
    assert b"bad" not in body
    assert json.loads(body)["extras"]["field"] == field


def test_a_bad_value_under_a_bad_key_keeps_the_safe_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        audit_row_precheck,
        "_build_audit_payload",
        lambda *_args: {"good": [{"x\ud800": "v"}]},
    )
    problem = audit_row_problem(operator=_operator(), descriptor=_descriptor(), params_hash="ab")
    assert problem is not None
    assert problem.field == "payload.good[0].<key 0>"


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_a_number_json_cannot_hold_is_named_with_its_path(
    monkeypatch: pytest.MonkeyPatch, number: float
) -> None:
    monkeypatch.setattr(
        audit_row_precheck,
        "_build_audit_payload",
        lambda *_args: {"params_hash": "ab", "cost": {"usd": number}},
    )
    problem = audit_row_problem(operator=_operator(), descriptor=_descriptor(), params_hash="ab")
    assert problem is not None
    assert problem.field == "payload.cost.usd"


def test_a_normal_number_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        audit_row_precheck,
        "_build_audit_payload",
        lambda *_args: {"params_hash": "ab", "cost": {"usd": 0.25}},
    )
    assert (
        audit_row_problem(operator=_operator(), descriptor=_descriptor(), params_hash="ab") is None
    )


def test_a_payload_too_deep_to_check_is_refused_as_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deep: dict[str, Any] = {"leaf": "ok"}
    for _ in range(2000):
        deep = {"inner": deep}
    monkeypatch.setattr(audit_row_precheck, "_build_audit_payload", lambda *_args: deep)
    problem = audit_row_problem(operator=_operator(), descriptor=_descriptor(), params_hash="ab")
    assert problem is not None
    assert problem.field == "payload"


def test_a_check_that_raises_refuses_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: Any) -> dict[str, Any]:
        raise ValueError("secret-value")

    monkeypatch.setattr(audit_row_precheck, "_build_audit_payload", _boom)
    problem = audit_row_problem(operator=_operator(), descriptor=_descriptor(), params_hash="ab")
    assert problem is not None
    assert problem.field == "payload"
    assert "secret-value" not in problem.reason


def test_the_client_id_counts_only_for_a_machine_login() -> None:
    bad_client = "client\x00id"
    person = _operator().model_copy(update={"name": None, "client_id": bad_client})
    assert audit_row_problem(operator=person, descriptor=_descriptor(), params_hash="ab") is None
    service = person.model_copy(update={"principal_kind": PrincipalKind.SERVICE})
    problem = audit_row_problem(operator=service, descriptor=_descriptor(), params_hash="ab")
    assert problem is not None
    assert problem.field == "payload.principal_name"


def test_a_long_text_is_allowed_where_no_index_holds_it() -> None:
    long_name = "n" * (INDEX_TEXT_MAX_BYTES + 100)
    operator = _operator().model_copy(update={"name": long_name})
    assert audit_row_problem(operator=operator, descriptor=_descriptor(), params_hash="ab") is None


async def test_the_check_sends_no_statement_to_the_database() -> None:
    statements: list[str] = []
    engine = get_engine().sync_engine

    def _record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", _record)
    try:
        audit_row_problem(operator=_operator(), descriptor=_descriptor(), params_hash="ab")
    finally:
        sa.event.remove(engine, "before_cursor_execute", _record)
    assert statements == []


async def test_a_normal_call_sends_no_extra_statement(embedding: AsyncMock) -> None:
    """The check adds nothing: a call costs the same statements with it as without."""
    await _register_ops(embedding)
    engine = get_engine().sync_engine
    seen: list[str] = []

    def _record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        seen.append(statement)

    async def _statements_of_a_call() -> int:
        seen.clear()
        sa.event.listen(engine, "before_cursor_execute", _record)
        try:
            result = await dispatch(
                operator=_operator(),
                connector_id=CONNECTOR_ID,
                op_id=WRITE_OP_ID,
                target=None,
                params={},
            )
        finally:
            sa.event.remove(engine, "before_cursor_execute", _record)
        assert result.status == "ok", result.error
        return len(seen)

    import meho_backplane.operations.dispatcher as dispatcher_module

    await _statements_of_a_call()  # warm the caches, so both counts start equal
    with_check = await _statements_of_a_call()
    original = dispatcher_module.audit_row_problem
    dispatcher_module.audit_row_problem = lambda **_kwargs: None  # type: ignore[assignment]
    try:
        without_check = await _statements_of_a_call()
    finally:
        dispatcher_module.audit_row_problem = original
    assert with_check == without_check


def test_every_column_of_the_audit_table_is_covered() -> None:
    """A new text or JSON column must be added to a list in ``audit_row_precheck``."""
    text_columns: set[str] = set()
    json_columns: set[str] = set()
    for column in AuditLog.__table__.columns:
        if isinstance(column.type, sa.JSON):
            json_columns.add(column.name)
        elif isinstance(column.type, sa.String | sa.Text):
            text_columns.add(column.name)
    assert text_columns == CHECKED_TEXT_COLUMNS | FIXED_TEXT_COLUMNS
    assert json_columns == CHECKED_JSON_COLUMNS | AFTER_CALL_JSON_COLUMNS
    assert INDEXED_TEXT_COLUMNS <= CHECKED_TEXT_COLUMNS
    indexed = {
        column.name for index in AuditLog.__table__.indexes for column in index.columns
    } & text_columns
    assert indexed == INDEXED_TEXT_COLUMNS


# ---------------------------------------------------------------------------
# Every way into the dispatcher: nothing reaches the target
# ---------------------------------------------------------------------------


async def test_direct_dispatch_is_refused_before_the_call(embedding: AsyncMock) -> None:
    await _register_ops(embedding)
    token = work_ref_var.set("ticket\x00bad")
    try:
        result = await dispatch(
            operator=_operator(),
            connector_id=CONNECTOR_ID,
            op_id=WRITE_OP_ID,
            target=None,
            params={},
        )
    finally:
        work_ref_var.reset(token)
    _assert_refused(result, "work_ref")
    assert "ticket" not in (result.error or "")
    assert await _count(AuditLog) == 0


async def test_refused_call_makes_no_approval_request(embedding: AsyncMock) -> None:
    await _register_ops(embedding)
    token = work_ref_var.set("t" * (INDEX_TEXT_MAX_BYTES + 1))
    try:
        result = await dispatch(
            operator=_operator(),
            connector_id=CONNECTOR_ID,
            op_id=APPROVAL_OP_ID,
            target=None,
            params={},
        )
    finally:
        work_ref_var.reset(token)
    _assert_refused(result, "work_ref")
    assert await _count(ApprovalRequest) == 0


async def test_a_step_of_a_multi_step_operation_is_refused(embedding: AsyncMock) -> None:
    await _register_ops(embedding)
    result = await dispatch(
        operator=_operator(),
        connector_id=CONNECTOR_ID,
        op_id=COMPOSITE_OP_ID,
        target=None,
        params={},
    )
    # The step comes back as the result of the operation.
    _assert_refused(result, "work_ref")


async def test_a_call_resumed_after_approval_is_refused(embedding: AsyncMock) -> None:
    await _register_ops(embedding)
    result = await dispatch(
        operator=_operator(sub="op\x00approver"),
        connector_id=CONNECTOR_ID,
        op_id=APPROVAL_OP_ID,
        target=None,
        params={},
        _approved=True,
    )
    _assert_refused(result, "operator_sub")


async def test_call_operation_is_refused_before_the_call(embedding: AsyncMock) -> None:
    """A bad ticket id given to ``call_operation`` stops the call too."""
    await _register_ops(embedding)
    result = await call_operation(
        _operator(),
        {
            "connector_id": CONNECTOR_ID,
            "op_id": WRITE_OP_ID,
            "target": None,
            "params": {},
            "work_ref": "ticket\x00bad",
        },
    )
    _assert_refused(result, "work_ref")
