# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Retiring the routine REST and MCP wrapper ``audit_log`` rows (#3757).

With ``AUDIT_WRAPPER_ROWS_RETIRED=true`` the action record keeps only
actions and decisions. This file proves it across the fronts:

* **Gate** -- the setting refuses to start without request usage
  collection; every writer declares its class; the report readers read
  usage events; the routine route lists match the app.
* **REST** (the API, and the CLI, which calls the same routes) and
  **MCP** -- one invariant table: safe reads, searches, previews, resource
  reads and validation failures write a usage event and no audit row; a
  refused call writes one ``policy_decision`` row; a dispatched operation
  (success, connector error, or an audit that could not commit) has only
  the dispatcher's record; an approval decision has only its own record;
  a MEHO change with no record of its own writes one
  ``operation_execution`` row.
"""

from __future__ import annotations

import ast
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

import meho_backplane
from meho_backplane import audit_cutover
from meho_backplane.api.v1 import search_docs as search_docs_api
from meho_backplane.api.v1.approvals import router as approvals_router
from meho_backplane.api.v1.memory import router as memory_router
from meho_backplane.api.v1.operations import router as operations_router
from meho_backplane.api.v1.retrieve_usage import router as retrieve_usage_router
from meho_backplane.audit import AuditMiddleware, _write_audit_row
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    ApprovalRequestStatus,
    AuditEventClass,
    AuditLog,
)
from meho_backplane.docs_search.target_scope import TargetDeniedError
from meho_backplane.main import app as production_app
from meho_backplane.mcp.audit import write_mcp_audit_row
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.registry import ToolDefinition, ToolSurface, register_mcp_tool
from meho_backplane.mcp.tools import docs as docs_tools
from meho_backplane.memory.rbac import PermissionDeniedError
from meho_backplane.memory.schemas import MemoryScope
from meho_backplane.memory.service import MemoryService
from meho_backplane.middleware import RequestContextMiddleware, verify_jwt_and_bind
from meho_backplane.operations import _audit as dispatch_audit
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.ingest.boot_stamp import BOOT_STAMP_OPERATOR_SUB
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    build_operator,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

from ._request_usage_helpers import read_usage_events

_SRC_ROOT = Path(meho_backplane.__file__).resolve().parent
_TENANT = OPERATOR_TENANT_ID
_CONNECTOR = "stub-1.x"
_OK_OP = "stub.thing.get"
_FAILING_OP = "stub.thing.fail"
_WRITE_OP = "stub.thing.create"


# ---------------------------------------------------------------------------
# Settings fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cutover_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Turn request usage on and retire the wrapper rows."""
    monkeypatch.setenv("REQUEST_USAGE_RETENTION_DAYS", "30")
    monkeypatch.setenv("AUDIT_WRAPPER_ROWS_RETIRED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _audit_rows() -> list[AuditLog]:
    """Every audit row except the lifespan boot stamp, oldest first."""
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(AuditLog)
            .where(AuditLog.operator_sub != BOOT_STAMP_OPERATOR_SUB)
            .order_by(AuditLog.occurred_at),
        )
        return list(result.scalars().all())


def _shape(rows: list[AuditLog]) -> list[tuple[str, str, str]]:
    """``(event_class, method, path)`` per row, for readable assertions."""
    return [(row.event_class, row.method, row.path) for row in rows]


# ===========================================================================
# Gate
# ===========================================================================


def test_retired_rows_need_request_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retiring the rows without usage collection fails at startup (fail closed)."""
    monkeypatch.setenv("AUDIT_WRAPPER_ROWS_RETIRED", "true")
    monkeypatch.delenv("REQUEST_USAGE_RETENTION_DAYS", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="needs REQUEST_USAGE_RETENTION_DAYS"):
        get_settings()
    get_settings.cache_clear()


@pytest.mark.usefixtures("cutover_on")
def test_retired_rows_start_with_request_usage() -> None:
    assert get_settings().audit_wrapper_rows_retired is True
    assert audit_cutover.wrapper_rows_retired() is True


def test_default_keeps_every_wrapper_row() -> None:
    """Off by default: every request still gets a ``legacy_request`` row."""
    assert get_settings().audit_wrapper_rows_retired is False
    for status in (200, 403, 422, 500):
        for routine in (True, False):
            assert (
                audit_cutover.wrapper_row_class(status_code=status, routine=routine)
                == AuditEventClass.LEGACY_REQUEST
            )


@pytest.mark.parametrize(
    ("status", "routine", "seen", "expected"),
    [
        (200, True, False, None),
        (500, True, False, None),
        (400, False, False, None),
        (404, False, False, None),
        (422, False, False, None),
        (403, True, False, AuditEventClass.POLICY_DECISION),
        (401, False, False, AuditEventClass.POLICY_DECISION),
        (200, False, False, AuditEventClass.OPERATION_EXECUTION),
        (201, False, False, AuditEventClass.OPERATION_EXECUTION),
        (500, False, False, AuditEventClass.OPERATION_EXECUTION),
        (200, False, True, None),
        (403, False, True, None),
    ],
)
@pytest.mark.usefixtures("cutover_on")
def test_decision_table(
    status: int, routine: bool, seen: bool, expected: AuditEventClass | None
) -> None:
    token = audit_cutover.open_classified_record_scope()
    try:
        if seen:
            AuditLog(event_class=AuditEventClass.APPROVAL_DECISION)
        assert audit_cutover.wrapper_row_class(status_code=status, routine=routine) == expected
    finally:
        audit_cutover.close_classified_record_scope(token)


def test_legacy_rows_do_not_count_as_classified() -> None:
    token = audit_cutover.open_classified_record_scope()
    try:
        AuditLog(event_class=AuditEventClass.LEGACY_REQUEST)
        AuditLog()
        assert audit_cutover.classified_record_seen() is False
        AuditLog(event_class="operation_execution")
        assert audit_cutover.classified_record_seen() is True
    finally:
        audit_cutover.close_classified_record_scope(token)
    assert audit_cutover.classified_record_seen() is False


@pytest.mark.parametrize("writer", [_write_audit_row, write_mcp_audit_row])
def test_wrapper_writers_require_an_event_class(writer: Callable[..., Any]) -> None:
    """The two wrapper writers no longer hard-code ``legacy_request``."""
    import inspect

    param = inspect.signature(writer).parameters["event_class"]
    assert param.default is inspect.Parameter.empty
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def _legacy_constant_writers() -> list[str]:
    """Modules that build ``AuditLog(event_class=AuditEventClass.LEGACY_REQUEST)``."""
    found: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                value = keyword.value
                if (
                    keyword.arg == "event_class"
                    and isinstance(value, ast.Attribute)
                    and value.attr == "LEGACY_REQUEST"
                ):
                    found.append(str(path.relative_to(_SRC_ROOT)))
    return found


def test_only_the_approval_result_read_still_writes_legacy_rows() -> None:
    """Writer inventory: no wrapper writer hard-codes ``legacy_request`` any more.

    The one remaining constant is the ``approval.result`` read row, which is
    a domain record of a result read, not a request wrapper.
    """
    assert _legacy_constant_writers() == ["operations/approval_queue.py"]


_USAGE_READERS = ("compute_usage", "compute_retire_checklist", "compute_reflex_report")


def test_report_routes_read_request_usage_events() -> None:
    """Reader migration (#3747): every production report call reads usage events."""
    calls: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name not in _USAGE_READERS:
                continue
            source = next((k.value for k in node.keywords if k.arg == "source"), None)
            assert isinstance(source, ast.Constant) and source.value == "request_usage", (
                f"{path.relative_to(_SRC_ROOT)}:{node.lineno} calls {name} without "
                "source='request_usage'"
            )
            calls.append(name)
    assert sorted(set(calls)) == sorted(_USAGE_READERS)


def test_routine_route_lists_match_the_app() -> None:
    """Every listed routine or dispatch route is a real POST route."""
    post_routes = {
        context.path_format
        for context in iter_route_contexts(production_app.routes)
        if "POST" in (getattr(context, "methods", None) or set())
    }
    listed = audit_cutover.ROUTINE_POST_ROUTES | audit_cutover.DISPATCH_ROUTES
    assert listed - post_routes == set()


# ===========================================================================
# Shared: a typed stub operation the dispatcher can run
# ===========================================================================


class _StubConnector(Connector):
    product = "stub"
    version = "1.x"
    impl_id = "stub"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:
        raise NotImplementedError


async def _ok_handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True}


async def _failing_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    raise RuntimeError("vendor said no")


@pytest.fixture
async def stub_ops() -> AsyncIterator[None]:
    """Register the stub connector and three typed ops."""
    clear_registry()
    reset_dispatcher_caches()
    service = AsyncMock()
    service.encode_one.return_value = [0.0] * EMBEDDING_DIMENSION
    service.encode.return_value = [[0.0] * EMBEDDING_DIMENSION]
    service.dimension = EMBEDDING_DIMENSION
    register_connector_v2(product="stub", version="", impl_id="", cls=_StubConnector)
    for op_id, handler in (
        (_OK_OP, _ok_handler),
        (_FAILING_OP, _failing_handler),
        (_WRITE_OP, _ok_handler),
    ):
        await register_typed_operation(
            product="stub",
            version="1.x",
            impl_id="stub",
            op_id=op_id,
            handler=handler,
            summary=f"Stub op {op_id}.",
            description="Used by the wrapper cutover tests.",
            parameter_schema={"type": "object"},
            when_to_use=None,
            embedding_service=service,
        )
    yield
    clear_registry()
    reset_dispatcher_caches()


def _break_dispatch_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the dispatcher's audit write fail (an audit-uncertain result)."""

    async def _raise(**_kwargs: Any) -> None:
        raise RuntimeError("audit store down")

    monkeypatch.setattr(dispatch_audit, "write_audit_row", _raise)


# ===========================================================================
# REST (API and CLI)
# ===========================================================================

_probe = APIRouter(prefix="/api/v1/cutover-probe")


@_probe.post("/things", status_code=201)
async def _create_thing(operator: Operator = Depends(verify_jwt_and_bind)) -> dict[str, str]:
    """A MEHO change with no audit record of its own."""
    return {"created": "thing"}


@_probe.post("/broken")
async def _broken(operator: Operator = Depends(verify_jwt_and_bind)) -> dict[str, str]:
    raise RuntimeError("handler failed")


@_probe.post("/refused")
async def _refused(operator: Operator = Depends(verify_jwt_and_bind)) -> dict[str, str]:
    raise HTTPException(status_code=403, detail="insufficient_role")


class _Caller:
    """The REST caller; tests switch its role."""

    def __init__(self) -> None:
        self.role = TenantRole.OPERATOR
        self.sub = "op-rest"

    async def __call__(self) -> Operator:
        structlog.contextvars.bind_contextvars(operator_sub=self.sub, tenant_id=str(_TENANT))
        return Operator(
            sub=self.sub,
            name="Rest Caller",
            email=None,
            raw_jwt="<test-jwt>",
            tenant_id=_TENANT,
            tenant_role=self.role,
        )


def _rest_client() -> tuple[TestClient, _Caller]:
    """REST app with the real audit middleware and the CLI-facing routers."""
    rest_app = FastAPI()
    rest_app.add_middleware(AuditMiddleware)
    rest_app.add_middleware(RequestContextMiddleware)
    rest_app.include_router(operations_router)
    rest_app.include_router(approvals_router)
    rest_app.include_router(search_docs_api.router)
    rest_app.include_router(memory_router)
    rest_app.include_router(retrieve_usage_router)
    rest_app.include_router(_probe)
    caller = _Caller()
    rest_app.dependency_overrides[verify_jwt_and_bind] = caller
    return TestClient(rest_app, raise_server_exceptions=False), caller


@pytest.fixture
def rest(cutover_on: None) -> Iterator[tuple[TestClient, _Caller]]:
    yield _rest_client()


def _call_body(op_id: str) -> dict[str, Any]:
    return {"connector_id": _CONNECTOR, "op_id": op_id, "params": {}}


@pytest.mark.usefixtures("stub_ops")
async def test_rest_safe_reads_and_validation_failures_write_no_audit_row(
    rest: tuple[TestClient, _Caller],
) -> None:
    client, _caller = rest
    # A safe read (``meho operations groups``), a read-only POST (preview),
    # and two validation failures (missing body field, missing query field).
    assert client.get(f"/api/v1/operations/groups?connector_id={_CONNECTOR}").status_code == 200
    assert client.post("/api/v1/operations/preview", json=_call_body(_OK_OP)).status_code == 200
    assert client.post("/api/v1/operations/call", json={"op_id": _OK_OP}).status_code == 422
    assert client.get("/api/v1/operations/groups").status_code == 422

    assert await _audit_rows() == []
    events = await read_usage_events()
    assert len(events) == 4
    assert {event.transport for event in events} == {"rest"}


@pytest.mark.usefixtures("stub_ops")
async def test_rest_dispatch_keeps_only_the_execution_record(
    rest: tuple[TestClient, _Caller],
) -> None:
    client, _caller = rest
    ok = client.post("/api/v1/operations/call", json=_call_body(_OK_OP))
    failed = client.post("/api/v1/operations/call", json=_call_body(_FAILING_OP))
    assert ok.status_code == 200 and ok.json()["status"] == "ok"
    assert failed.status_code == 200 and failed.json()["status"] == "error"

    rows = await _audit_rows()
    assert _shape(rows) == [
        ("operation_execution", "DISPATCH", _OK_OP),
        ("operation_execution", "DISPATCH", _FAILING_OP),
    ]
    assert rows[0].status_code == 200
    assert rows[1].status_code >= 400
    assert len(await read_usage_events()) == 2


@pytest.mark.usefixtures("stub_ops")
async def test_rest_audit_uncertain_result_is_explicit_and_adds_no_wrapper_row(
    rest: tuple[TestClient, _Caller],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _caller = rest
    _break_dispatch_audit(monkeypatch)
    response = client.post("/api/v1/operations/call", json=_call_body(_WRITE_OP))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert body["error"] == "connector_error: AuditCommitError"
    # No success row, and no wrapper row standing in for the missing one.
    assert await _audit_rows() == []


async def test_rest_refusals_are_policy_decisions(rest: tuple[TestClient, _Caller]) -> None:
    client, caller = rest
    caller.role = TenantRole.READ_ONLY
    # A refused write, a refused read (the role gate), a refused probe.
    assert client.post("/api/v1/operations/call", json=_call_body(_OK_OP)).status_code == 403
    assert client.get(f"/api/v1/operations/groups?connector_id={_CONNECTOR}").status_code == 403
    caller.role = TenantRole.OPERATOR
    assert client.post("/api/v1/cutover-probe/refused").status_code == 403

    rows = await _audit_rows()
    assert _shape(rows) == [
        ("policy_decision", "POST", "/api/v1/operations/call"),
        ("policy_decision", "GET", "/api/v1/operations/groups"),
        ("policy_decision", "POST", "/api/v1/cutover-probe/refused"),
    ]
    assert {row.status_code for row in rows} == {403}


async def test_rest_meho_change_without_own_record_is_an_execution(
    rest: tuple[TestClient, _Caller],
) -> None:
    client, _caller = rest
    assert client.post("/api/v1/cutover-probe/things").status_code == 201
    assert client.post("/api/v1/cutover-probe/broken").status_code == 500

    rows = await _audit_rows()
    assert _shape(rows) == [
        ("operation_execution", "POST", "/api/v1/cutover-probe/things"),
        ("operation_execution", "POST", "/api/v1/cutover-probe/broken"),
    ]
    assert [row.status_code for row in rows] == [201, 500]


async def _seed_pending_request() -> uuid.UUID:
    request_id = uuid.uuid4()
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            ApprovalRequest(
                id=request_id,
                tenant_id=_TENANT,
                run_id=None,
                principal_sub="svc-parker",
                op_id="vault.kv.put",
                connector_id="vault-1.x",
                target_id=None,
                params_hash=compute_params_hash({}),
                params={},
                proposed_effect={"op_id": "vault.kv.put", "connector_id": "vault-1.x"},
                status=ApprovalRequestStatus.PENDING.value,
            ),
        )
    return request_id


async def test_rest_approval_decision_keeps_only_its_own_record(
    rest: tuple[TestClient, _Caller],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _caller = rest

    async def _no_broadcast(**_kwargs: Any) -> None:
        return None

    from meho_backplane.operations import approval_queue

    monkeypatch.setattr(approval_queue, "publish_approval_event", _no_broadcast)
    request_id = await _seed_pending_request()

    response = client.post(f"/api/v1/approvals/{request_id}/reject", json={"reason": "no"})
    assert response.status_code == 200, response.text

    rows = await _audit_rows()
    assert _shape(rows) == [("approval_decision", "APPROVAL", "approval.decision")]


# ===========================================================================
# MCP
# ===========================================================================


@pytest.fixture
def mcp(cutover_on: None) -> Iterator[tuple[TestClient, dict[str, Operator]]]:
    """Production app; the MCP operator is swappable through ``holder['op']``."""
    holder = {"op": build_operator(TenantRole.OPERATOR)}

    async def _fake_verify() -> Operator:
        op = holder["op"]
        structlog.contextvars.bind_contextvars(operator_sub=op.sub, tenant_id=str(op.tenant_id))
        return op

    production_app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(production_app) as client:
            yield client, holder
    finally:
        production_app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


def _tool_call(client: TestClient, name: str, arguments: Any, call_id: int = 1) -> Any:
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200
    return response.json()


async def _write_tool_handler(_operator: Operator, _arguments: dict[str, Any]) -> dict[str, Any]:
    return {"done": True}


async def _self_recording_tool_handler(
    operator: Operator, _arguments: dict[str, Any]
) -> dict[str, Any]:
    from datetime import UTC, datetime
    from decimal import Decimal

    async with get_sessionmaker()() as session:
        session.add(
            AuditLog(
                id=uuid.uuid4(),
                event_class=AuditEventClass.OPERATION_EXECUTION,
                occurred_at=datetime.now(UTC),
                operator_sub=operator.sub,
                tenant_id=operator.tenant_id,
                method="INTERNAL",
                path="test.self_recording",
                status_code=200,
                duration_ms=Decimal("1.0"),
                payload={},
            ),
        )
        await session.commit()
    return {"done": True}


def _register_tool(name: str, handler: Any, *, op_class: str = "write") -> None:
    register_mcp_tool(
        ToolDefinition(
            feature=None,
            surface=ToolSurface.WORKING,
            name=name,
            description="Test tool for the wrapper cutover.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
            required_role=TenantRole.OPERATOR,
            op_class=op_class,
        ),
        handler,
    )


async def test_mcp_reads_and_validation_failures_write_no_audit_row(
    mcp: tuple[TestClient, dict[str, Operator]],
) -> None:
    client, _holder = mcp
    _register_tool("test.cutover_read", _write_tool_handler, op_class="read")
    assert "result" in _tool_call(client, "test.cutover_read", {})
    assert "error" in _tool_call(client, "no_such_tool", {}, 2)
    assert "error" in _tool_call(client, "test.cutover_read", {"unexpected": 1}, 3)
    read = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "resources/read",
            "params": {"uri": f"meho://tenant/{_TENANT}/info"},
        },
    )
    assert read.status_code == 200

    assert await _audit_rows() == []
    mcp_events = [event for event in await read_usage_events() if event.transport == "mcp"]
    assert len(mcp_events) >= 4


async def test_mcp_refusal_is_a_policy_decision(
    mcp: tuple[TestClient, dict[str, Operator]],
) -> None:
    client, holder = mcp
    _register_tool("test.cutover_write", _write_tool_handler)
    holder["op"] = build_operator(TenantRole.READ_ONLY)
    assert "error" in _tool_call(client, "test.cutover_write", {})

    rows = await _audit_rows()
    assert _shape(rows) == [("policy_decision", "MCP", "/mcp/tools/call/test.cutover_write")]
    assert rows[0].status_code == 403


async def test_mcp_dispatch_keeps_only_the_execution_record(
    mcp: tuple[TestClient, dict[str, Operator]],
    stub_ops: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _holder = mcp
    _tool_call(client, "call_operation", {"connector_id": _CONNECTOR, "op_id": _OK_OP}, 1)
    _tool_call(client, "call_operation", {"connector_id": _CONNECTOR, "op_id": _FAILING_OP}, 2)

    rows = await _audit_rows()
    assert _shape(rows) == [
        ("operation_execution", "DISPATCH", _OK_OP),
        ("operation_execution", "DISPATCH", _FAILING_OP),
    ]

    _break_dispatch_audit(monkeypatch)
    uncertain = _tool_call(
        client, "call_operation", {"connector_id": _CONNECTOR, "op_id": _WRITE_OP}, 3
    )
    assert "AuditCommitError" in str(uncertain)
    assert len(await _audit_rows()) == 2


async def test_mcp_write_tool_rows(
    mcp: tuple[TestClient, dict[str, Operator]],
) -> None:
    client, _holder = mcp
    _register_tool("test.cutover_write", _write_tool_handler)
    _register_tool("test.cutover_self_recording", _self_recording_tool_handler)
    _tool_call(client, "test.cutover_write", {}, 1)
    _tool_call(client, "test.cutover_self_recording", {}, 2)

    assert _shape(await _audit_rows()) == [
        ("operation_execution", "MCP", "/mcp/tools/call/test.cutover_write"),
        ("operation_execution", "INTERNAL", "test.self_recording"),
    ]


async def test_mcp_without_cutover_keeps_legacy_wrapper_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off by default: the same read still writes its ``legacy_request`` row."""
    op = build_operator(TenantRole.OPERATOR)

    async def _fake_verify() -> Operator:
        return op

    production_app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(production_app) as client:
            _register_tool("test.cutover_read", _write_tool_handler, op_class="read")
            _tool_call(client, "test.cutover_read", {})
    finally:
        production_app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)
    assert _shape(await _audit_rows()) == [
        ("legacy_request", "MCP", "/mcp/tools/call/test.cutover_read"),
    ]


# ===========================================================================
# Refusals raised inside a handler (REST and MCP)
# ===========================================================================
#
# A handler can refuse a call after the route or tool gates passed: the
# caller's permission rows deny the docs target, the caller may not promote
# a memory, or a resource URI names another tenant. REST answers 403; MCP
# answers ``-32602`` through ``McpPermissionDeniedError``. Both fronts write
# one ``policy_decision`` row with the switch on, and one ``legacy_request``
# row with it off.

_FOREIGN_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000f0f0")
_DOCS_CAPABILITIES = frozenset({"meho-docs", "meho-docs:vmware"})
_TENANT_RESOURCES = ("info", "feed", "conventions/team-rule")


@pytest.fixture(params=[True, False], ids=["retired", "legacy"])
def retired(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[bool]:
    """Run the test with the switch on and with it off."""
    monkeypatch.setenv("REQUEST_USAGE_RETENTION_DAYS", "30")
    monkeypatch.setenv("AUDIT_WRAPPER_ROWS_RETIRED", "true" if request.param else "false")
    get_settings.cache_clear()
    yield request.param
    get_settings.cache_clear()


@pytest.fixture
def handler_refusals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the docs target check and the memory promote check refuse."""

    async def _deny_target(*_args: Any, **_kwargs: Any) -> Any:
        raise TargetDeniedError("vc1", "verdict=deny")

    async def _deny_promote(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionDeniedError(MemoryScope.TENANT, "insufficient_promotion_authority")

    monkeypatch.setattr(search_docs_api, "apply_target_default", _deny_target)
    monkeypatch.setattr(docs_tools, "apply_target_default", _deny_target)
    monkeypatch.setattr(MemoryService, "promote", _deny_promote)


def _expected_rows(retired: bool, paths: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    row_class = "policy_decision" if retired else "legacy_request"
    return [(row_class, method, path) for method, path in paths]


@pytest.mark.usefixtures("handler_refusals")
async def test_rest_handler_refusals_write_one_decision_row(retired: bool) -> None:
    client, caller = _rest_client()
    caller.role = TenantRole.TENANT_ADMIN
    docs = client.post(
        "/api/v1/search_docs",
        json={"query": "upgrade", "collection": "vmware", "target": "vc1"},
    )
    assert docs.status_code == 403
    promote = client.post("/api/v1/memory/user-tenant/team-rule/promote", json={"to": "tenant"})
    assert promote.status_code == 403
    caller.role = TenantRole.OPERATOR
    usage = client.get(f"/api/v1/retrieve/usage?tenant_filter={_FOREIGN_TENANT}")
    assert usage.status_code == 403

    rows = await _audit_rows()
    assert _shape(rows) == _expected_rows(
        retired,
        [
            ("POST", "/api/v1/search_docs"),
            ("POST", "/api/v1/memory/user-tenant/team-rule/promote"),
            ("GET", "/api/v1/retrieve/usage"),
        ],
    )
    assert {row.status_code for row in rows} == {403}


@pytest.mark.usefixtures("handler_refusals")
async def test_mcp_handler_refusals_write_one_decision_row(retired: bool) -> None:
    op = build_operator(TenantRole.TENANT_ADMIN).model_copy(
        update={"capabilities": _DOCS_CAPABILITIES},
    )

    async def _fake_verify() -> Operator:
        structlog.contextvars.bind_contextvars(operator_sub=op.sub, tenant_id=str(op.tenant_id))
        return op

    production_app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(production_app) as client:
            docs = _tool_call(
                client,
                "search_docs",
                {"query": "upgrade", "collection": "vmware", "target": "vc1"},
                1,
            )
            assert docs["error"]["message"].startswith("search_docs: denied")
            promote = _tool_call(
                client,
                "meho_memory_promote",
                {"source_scope": "user-tenant", "slug": "team-rule", "to": "tenant"},
                2,
            )
            assert promote["error"]["message"].endswith("insufficient_promotion_authority")
            for call_id, resource in enumerate(_TENANT_RESOURCES, start=3):
                read = post_mcp(
                    client,
                    {
                        "jsonrpc": "2.0",
                        "id": call_id,
                        "method": "resources/read",
                        "params": {"uri": f"meho://tenant/{_FOREIGN_TENANT}/{resource}"},
                    },
                )
                assert "cross-tenant" in read.json()["error"]["message"]
    finally:
        production_app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)

    rows = await _audit_rows()
    assert _shape(rows) == _expected_rows(
        retired,
        [
            ("MCP", "/mcp/tools/call/search_docs"),
            ("MCP", "/mcp/tools/call/meho_memory_promote"),
            *[
                ("MCP", f"/mcp/resources/read/meho://tenant/{_FOREIGN_TENANT}/{resource}")
                for resource in _TENANT_RESOURCES
            ],
        ],
    )
    assert {row.status_code for row in rows} == {403}
