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
  ``operation_execution`` row; a change whose own record names another
  identity keeps the caller's row; a write that failed in Keycloak keeps
  one ``operation_execution`` row; a connector edit's own record carries
  the agent session, the delegated actor and the request id the wrapper
  row used to carry; a change whose own record was rolled back with it
  keeps one ``operation_execution`` row for the failed attempt; a bulk
  import's dry run is routine, while its apply keeps only the records it
  wrote itself.
"""

from __future__ import annotations

import ast
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Response
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
from meho_backplane.api.v1.runner_principals import router as runner_principals_router
from meho_backplane.api.v1.topology import router as topology_router
from meho_backplane.audit import AuditMiddleware, _write_audit_row
from meho_backplane.auth.agent_principals import AgentPrincipalService
from meho_backplane.auth.delegation import ACTOR_SUB_KEY
from meho_backplane.auth.keycloak_admin import KeycloakAdminError
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.runner_principals import RunnerPrincipalService
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    ApprovalRequestStatus,
    AuditEventClass,
    AuditLog,
    GraphNode,
    OperationGroup,
    RunnerPrincipal,
    Tenant,
)
from meho_backplane.docs_search.target_scope import TargetDeniedError
from meho_backplane.main import app as production_app
from meho_backplane.mcp.audit import write_mcp_audit_row
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.registry import ToolDefinition, ToolSurface, register_mcp_tool
from meho_backplane.mcp.server import McpExecutionFailedError
from meho_backplane.mcp.tools import docs as docs_tools
from meho_backplane.memory.audit import INTERNAL_METHOD, write_internal_audit_row
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
    seed_doc_collection,
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
                audit_cutover.wrapper_row_class(
                    status_code=status, routine=routine, operator_sub="op"
                )
                == AuditEventClass.LEGACY_REQUEST
            )


@pytest.mark.parametrize(
    ("status", "routine", "recorded_by", "expected"),
    [
        (200, True, None, None),
        (500, True, None, None),
        (400, False, None, None),
        (404, False, None, None),
        (422, False, None, None),
        (403, True, None, AuditEventClass.POLICY_DECISION),
        (401, False, None, AuditEventClass.POLICY_DECISION),
        (200, False, None, AuditEventClass.OPERATION_EXECUTION),
        (201, False, None, AuditEventClass.OPERATION_EXECUTION),
        (500, False, None, AuditEventClass.OPERATION_EXECUTION),
        (200, False, "op", None),
        (403, False, "op", None),
        # A record under another identity does not say who called.
        (200, False, "system:job", AuditEventClass.OPERATION_EXECUTION),
        (403, False, "system:job", AuditEventClass.POLICY_DECISION),
    ],
)
@pytest.mark.usefixtures("cutover_on")
async def test_decision_table(
    status: int, routine: bool, recorded_by: str | None, expected: AuditEventClass | None
) -> None:
    token = audit_cutover.open_classified_record_scope()
    try:
        if recorded_by is not None:
            await _store(
                _classified_row(AuditEventClass.APPROVAL_DECISION, operator_sub=recorded_by),
                commit=True,
            )
        assert (
            audit_cutover.wrapper_row_class(status_code=status, routine=routine, operator_sub="op")
            == expected
        )
    finally:
        audit_cutover.close_classified_record_scope(token)


def _classified_row(
    event_class: AuditEventClass = AuditEventClass.OPERATION_EXECUTION,
    *,
    operator_sub: str = "op-rest",
    path: str = "probe.edit",
    **lineage: Any,
) -> AuditLog:
    """Build (not store) a row the way a domain writer does."""
    return AuditLog(
        id=uuid.uuid4(),
        event_class=event_class,
        occurred_at=datetime.now(UTC),
        operator_sub=operator_sub,
        tenant_id=_TENANT,
        method="SERVICE",
        path=path,
        status_code=200,
        duration_ms=Decimal("0"),
        payload={},
        **lineage,
    )


async def _store(row: AuditLog, *, commit: bool) -> None:
    """Add *row* in its own session, then commit it or roll it back."""
    async with get_sessionmaker()() as session:
        session.add(row)
        await session.flush()
        if commit:
            await session.commit()
        else:
            await session.rollback()


async def test_only_committed_classified_rows_count() -> None:
    """A row counts once its transaction commits; a rolled-back row never does.

    A ``legacy_request`` row and a row under another identity do not
    count even when committed.
    """
    token = audit_cutover.open_classified_record_scope()
    try:
        built = _classified_row()
        assert audit_cutover.classified_record_seen("op-rest") is False
        await _store(_classified_row(), commit=False)
        assert audit_cutover.classified_record_seen("op-rest") is False
        await _store(_classified_row(AuditEventClass.LEGACY_REQUEST), commit=True)
        await _store(_classified_row(operator_sub="system:job"), commit=True)
        assert audit_cutover.classified_record_seen("op-rest") is False
        await _store(built, commit=True)
        assert audit_cutover.classified_record_seen("op-rest") is True
    finally:
        audit_cutover.close_classified_record_scope(token)
    assert audit_cutover.classified_record_seen("op-rest") is False


@pytest.mark.usefixtures("cutover_on")
def test_a_preview_is_routine_unless_refused() -> None:
    """A dry-run branch marks the request as a preview; a refusal still counts."""
    token = audit_cutover.open_classified_record_scope()
    try:
        assert audit_cutover.request_is_preview() is False
        audit_cutover.note_preview()
        assert audit_cutover.request_is_preview() is True
        for status in (200, 500):
            assert (
                audit_cutover.wrapper_row_class(
                    status_code=status, routine=False, operator_sub="op"
                )
                is None
            )
        assert (
            audit_cutover.wrapper_row_class(status_code=403, routine=False, operator_sub="op")
            == AuditEventClass.POLICY_DECISION
        )
    finally:
        audit_cutover.close_classified_record_scope(token)
    audit_cutover.note_preview()
    assert audit_cutover.request_is_preview() is False


def _lineage(row: AuditLog) -> tuple[Any, ...]:
    return (row.agent_session_id, row.actor_sub, row.parent_audit_id, row.request_id, row.work_ref)


@pytest.mark.usefixtures("cutover_on")
def test_classified_rows_inherit_the_request_lineage() -> None:
    """A record built inside a request gets the lineage its writer left empty.

    A value the writer set is kept. A ``legacy_request`` row (a wrapper
    row) and a row built outside a scope are left alone.
    """
    session_id, parent_id, request_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    bound = structlog.contextvars.bound_contextvars(
        mcp_session_id=str(session_id),
        request_id=str(request_id),
        **{ACTOR_SUB_KEY: "agent:planner"},
    )
    parent_token = dispatch_audit.parent_audit_id_var.set(parent_id)
    work_ref_token = dispatch_audit.work_ref_var.set("ticket-1")
    try:
        with bound:
            scope = audit_cutover.open_classified_record_scope()
            try:
                filled = _classified_row()
                explicit = _classified_row(actor_sub="agent:other", request_id=None)
                legacy = _classified_row(event_class=AuditEventClass.LEGACY_REQUEST)
            finally:
                audit_cutover.close_classified_record_scope(scope)
            outside = _classified_row()
    finally:
        dispatch_audit.work_ref_var.reset(work_ref_token)
        dispatch_audit.parent_audit_id_var.reset(parent_token)

    assert _lineage(filled) == (session_id, "agent:planner", parent_id, request_id, "ticket-1")
    assert _lineage(explicit) == (session_id, "agent:other", parent_id, request_id, "ticket-1")
    assert _lineage(legacy) == (None, None, None, None, None)
    assert _lineage(outside) == (None, None, None, None, None)


def test_classified_rows_keep_their_lineage_with_the_cutover_off() -> None:
    """Off by default: a domain record is built exactly as its writer wrote it."""
    scope = audit_cutover.open_classified_record_scope()
    try:
        with structlog.contextvars.bound_contextvars(
            mcp_session_id=str(uuid.uuid4()), **{ACTOR_SUB_KEY: "agent:planner"}
        ):
            row = _classified_row()
    finally:
        audit_cutover.close_classified_record_scope(scope)
    assert _lineage(row) == (None, None, None, None, None)


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


@_probe.post("/rolled-back", status_code=201)
async def _rolled_back(operator: Operator = Depends(verify_jwt_and_bind)) -> dict[str, str]:
    """A MEHO change whose own record was written, then rolled back with the change."""
    async with get_sessionmaker()() as session, session.begin():
        session.add(_classified_row(operator_sub=operator.sub, path="probe.rolled_back"))
        await session.flush()
        raise RuntimeError("the change failed after its record was written")


@_probe.post("/system-recorded", status_code=201)
async def _system_recorded(operator: Operator = Depends(verify_jwt_and_bind)) -> dict[str, str]:
    """A MEHO change whose own record names a system identity, not the caller."""
    await write_internal_audit_row(
        event_class=AuditEventClass.OPERATION_EXECUTION,
        operator_sub="system:probe",
        tenant_id=operator.tenant_id,
        method=INTERNAL_METHOD,
        path="probe.system_recorded",
        status_code=200,
        duration_ms=1.0,
        payload={},
    )
    return {"recorded": "by-system"}


@_probe.post("/recorded", status_code=204, response_class=Response)
async def _recorded(operator: Operator = Depends(verify_jwt_and_bind)) -> Response:
    """A MEHO change that writes its own record in the caller's name, answering 204."""
    await _store(_classified_row(operator_sub=operator.sub, path="probe.recorded"), commit=True)
    return Response(status_code=204)


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
    rest_app.include_router(runner_principals_router)
    rest_app.include_router(topology_router)
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
    # The handler finished but its audit row did not commit: the result says
    # the outcome is uncertain and must not be retried, with no audit_id.
    assert body["error"].startswith("audit_commit_failed:")
    assert body["extras"]["error_code"] == "audit_commit_failed"
    assert body["extras"]["execution_outcome"] == "uncertain"
    assert body["extras"]["retry"] is False
    assert body.get("audit_id") is None
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


async def test_rest_rolled_back_record_keeps_the_failed_attempt(
    rest: tuple[TestClient, _Caller],
) -> None:
    """A record that rolled back with its change is no record; the failure row stays."""
    client, caller = rest
    assert client.post("/api/v1/cutover-probe/rolled-back").status_code == 500

    rows = await _audit_rows()
    assert _shape(rows) == [("operation_execution", "POST", "/api/v1/cutover-probe/rolled-back")]
    assert rows[0].status_code == 500
    assert rows[0].operator_sub == caller.sub


async def _seed_topology_nodes() -> None:
    """Insert the tenant and two graph nodes a bulk import can join."""
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=_TENANT, slug="op-tenant", name="Op Tenant"))
        for kind, name in (("service", "svc-a"), ("database", "db-b")):
            session.add(
                GraphNode(
                    id=uuid.uuid4(),
                    tenant_id=_TENANT,
                    kind=kind,
                    name=name,
                    target_id=None,
                    properties={},
                    discovered_by="test",
                    first_seen=datetime.now(UTC),
                )
            )
        await session.commit()


_BULK_ROUTE = "/api/v1/topology/edges/bulk"
_BULK_EDGE = {"from": {"name": "svc-a"}, "kind": "depends-on", "to": {"name": "db-b"}}
_ANNOTATE_ROW = ("operation_execution", "ANNOTATE", "topology.annotate")


async def test_rest_bulk_import_dry_run_is_routine_and_apply_keeps_its_own_record(
    rest: tuple[TestClient, _Caller],
) -> None:
    """The same write route is a preview with ``dry_run`` and a change without it."""
    client, caller = rest
    caller.role = TenantRole.TENANT_ADMIN
    await _seed_topology_nodes()

    preview = client.post(_BULK_ROUTE, json={"edges": [_BULK_EDGE], "dry_run": True})
    assert preview.status_code == 200, preview.text
    assert preview.json()["dry_run"] is True
    assert await _audit_rows() == []

    applied = client.post(_BULK_ROUTE, json={"edges": [_BULK_EDGE], "dry_run": False})
    assert applied.status_code == 200, applied.text
    rows = await _audit_rows()
    assert _shape(rows) == [_ANNOTATE_ROW]
    assert rows[0].operator_sub == caller.sub

    caller.role = TenantRole.OPERATOR
    refused = client.post(_BULK_ROUTE, json={"edges": [_BULK_EDGE], "dry_run": True})
    assert refused.status_code == 403
    assert _shape(await _audit_rows()) == [_ANNOTATE_ROW, ("policy_decision", "POST", _BULK_ROUTE)]


async def test_rest_record_under_another_identity_keeps_the_callers_row(
    rest: tuple[TestClient, _Caller],
) -> None:
    """A record in a system job's name does not say who called; the wrapper row stays."""
    client, caller = rest
    response = client.post("/api/v1/cutover-probe/system-recorded")
    assert response.status_code == 201

    rows = await _audit_rows()
    assert sorted(_shape(rows)) == [
        ("operation_execution", "INTERNAL", "probe.system_recorded"),
        ("operation_execution", "POST", "/api/v1/cutover-probe/system-recorded"),
    ]
    assert {row.operator_sub for row in rows} == {"system:probe", caller.sub}


async def test_rest_runner_revocation_keeps_its_own_record_in_the_callers_name(
    rest: tuple[TestClient, _Caller],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revoking a runner leaves one row, and that row says who revoked it."""
    client, caller = rest
    caller.role = TenantRole.TENANT_ADMIN
    async with get_sessionmaker()() as session:
        session.add(
            RunnerPrincipal(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                name="edge-runner",
                keycloak_client_id="runner:edge-runner",
                keycloak_internal_id="kc-edge-runner",
                owner_sub=caller.sub,
                created_by_sub=caller.sub,
                revoked=False,
            )
        )
        await session.commit()

    async def _keycloak_disabled(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(RunnerPrincipalService, "_disable_keycloak_client", _keycloak_disabled)

    response = client.delete("/api/v1/runner-principals/edge-runner/revoke")
    assert response.status_code == 200, response.text

    rows = await _audit_rows()
    assert _shape(rows) == [("operation_execution", "INTERNAL", "runner.principal.revoked")]
    assert rows[0].operator_sub == caller.sub
    assert rows[0].request_id is not None


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
) -> None:
    client, _caller = rest
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


def _tool_call(
    client: TestClient,
    name: str,
    arguments: Any,
    call_id: int = 1,
    *,
    headers: dict[str, str] | None = None,
) -> Any:
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()


async def _write_tool_handler(_operator: Operator, _arguments: dict[str, Any]) -> dict[str, Any]:
    return {"done": True}


async def _self_recording_tool_handler(
    operator: Operator, _arguments: dict[str, Any]
) -> dict[str, Any]:
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


async def _rolled_back_tool_handler(
    operator: Operator, _arguments: dict[str, Any]
) -> dict[str, Any]:
    """A write whose own record rolled back with it; the failure is a failed change."""
    try:
        async with get_sessionmaker()() as session, session.begin():
            session.add(_classified_row(operator_sub=operator.sub, path="test.rolled_back"))
            await session.flush()
            raise RuntimeError("the change failed after its record was written")
    except RuntimeError as exc:
        raise McpExecutionFailedError(str(exc)) from exc


def _register_tool(
    name: str,
    handler: Any,
    *,
    op_class: str = "write",
    properties: dict[str, Any] | None = None,
) -> None:
    register_mcp_tool(
        ToolDefinition(
            feature=None,
            surface=ToolSurface.WORKING,
            name=name,
            description="Test tool for the wrapper cutover.",
            inputSchema={
                "type": "object",
                "properties": properties or {},
                "additionalProperties": False,
            },
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
    # A read tool with no arguments ignores extra ones (#4137), so the
    # schema failure uses a read tool that takes one argument.
    _register_tool(
        "test.cutover_read_with_args",
        _write_tool_handler,
        op_class="read",
        properties={"limit": {"type": "integer"}},
    )
    assert "result" in _tool_call(client, "test.cutover_read", {})
    assert "error" in _tool_call(client, "no_such_tool", {}, 2)
    assert "error" in _tool_call(client, "test.cutover_read_with_args", {"limit": "x"}, 3)
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
    assert "audit_commit_failed" in str(uncertain)
    assert "uncertain" in str(uncertain)
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


async def test_mcp_rolled_back_record_keeps_the_failed_attempt(
    mcp: tuple[TestClient, dict[str, Operator]],
) -> None:
    client, holder = mcp
    _register_tool("test.cutover_rolled_back", _rolled_back_tool_handler)
    result = _tool_call(client, "test.cutover_rolled_back", {})
    assert "error" in result, result

    rows = await _audit_rows()
    assert _shape(rows) == [
        ("operation_execution", "MCP", "/mcp/tools/call/test.cutover_rolled_back")
    ]
    assert rows[0].status_code == 502
    assert rows[0].operator_sub == holder["op"].sub


async def test_mcp_bulk_import_dry_run_is_routine_and_apply_keeps_its_own_records(
    mcp: tuple[TestClient, dict[str, Operator]],
) -> None:
    """The ``write`` tool is a preview by default and a change with ``dry_run: false``."""
    client, holder = mcp
    holder["op"] = build_operator(TenantRole.TENANT_ADMIN)
    await _seed_topology_nodes()
    rows_arg = [{"from_name": "svc-a", "kind": "depends-on", "to_name": "db-b"}]
    tool_path = "/mcp/tools/call/meho_topology_bulk_import"

    preview = _tool_call(client, "meho_topology_bulk_import", {"rows": rows_arg}, 1)
    assert "error" not in preview, preview
    assert await _audit_rows() == []

    applied = _tool_call(
        client, "meho_topology_bulk_import", {"rows": rows_arg, "dry_run": False}, 2
    )
    assert "error" not in applied, applied
    rows = await _audit_rows()
    assert sorted(_shape(rows)) == [
        _ANNOTATE_ROW,
        ("operation_execution", "DISPATCH", "topology.bulk_import"),
    ]
    assert {row.operator_sub for row in rows} == {holder["op"].sub}

    holder["op"] = build_operator(TenantRole.OPERATOR)
    refused = _tool_call(client, "meho_topology_bulk_import", {"rows": rows_arg}, 3)
    assert "error" in refused, refused
    assert _shape(await _audit_rows())[-1] == ("policy_decision", "MCP", tool_path)


@pytest.mark.usefixtures("cutover_on")
async def test_mcp_connector_edit_keeps_the_session_and_actor_on_its_own_record() -> None:
    """A connector edit over MCP stays in its session's replay after the cutover.

    The edit's own record used to leave the agent session, the delegated
    actor and the request id empty; the wrapper row carried them. After
    the cutover the record is the only row, so it must carry them itself,
    and the request's usage event must point at the same request.
    """
    op = build_operator(TenantRole.TENANT_ADMIN)
    session_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            OperationGroup(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                group_key="inventory",
                name="Inventory",
                when_to_use="Use for inventory reads.",
                review_status="staged",
            )
        )
        await session.commit()

    async def _delegated_verify() -> Operator:
        structlog.contextvars.bind_contextvars(
            operator_sub=op.sub, tenant_id=str(op.tenant_id), **{ACTOR_SUB_KEY: "agent:planner"}
        )
        return op

    production_app.dependency_overrides[verify_mcp_jwt_and_bind] = _delegated_verify
    try:
        with TestClient(production_app) as client:
            result = _tool_call(
                client,
                "meho_connector_edit_group",
                {
                    "connector_id": "vmware-rest-9.0",
                    "group_key": "inventory",
                    "when_to_use": "Use for inventory reads and searches.",
                    "tenant_id": str(_TENANT),
                },
                headers={"Mcp-Session-Id": str(session_id)},
            )
            assert "error" not in result, result
    finally:
        production_app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)

    rows = await _audit_rows()
    assert _shape(rows) == [("operation_execution", "SERVICE", "meho_connector_edit_group")]
    assert rows[0].operator_sub == op.sub
    assert rows[0].actor_sub == "agent:planner"
    assert rows[0].agent_session_id == session_id
    assert rows[0].request_id is not None
    events = await read_usage_events()
    assert [(event.session_id, event.request_id) for event in events] == [
        (session_id, rows[0].request_id)
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


async def test_mcp_scope_refusals_write_one_decision_row(retired: bool) -> None:
    """A docs collection the caller may not use, and an ingest into another tenant.

    REST answers 403 for both. On MCP each one writes one
    ``policy_decision`` row with the switch on, and one ``legacy_request``
    row with it off.
    """
    await seed_doc_collection()
    # Base docs capability only: the tool is visible, the vmware
    # collection is not.
    op = build_operator(TenantRole.TENANT_ADMIN).model_copy(
        update={"capabilities": frozenset({"meho-docs"})},
    )

    async def _fake_verify() -> Operator:
        structlog.contextvars.bind_contextvars(operator_sub=op.sub, tenant_id=str(op.tenant_id))
        return op

    production_app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(production_app) as client:
            docs = _tool_call(
                client, "search_docs", {"query": "upgrade", "collection": "vmware"}, 1
            )
            assert docs["error"]["data"]["reason"] == "not_entitled"
            ingest = _tool_call(
                client,
                "meho_connector_ingest",
                {
                    "product": "scopedenied",
                    "version": "1.0",
                    "impl_id": "scopedenied-rest",
                    "specs": [{"uri": "docs:never-fetched/spec.yaml"}],
                    "tenant_id": str(_FOREIGN_TENANT),
                },
                2,
            )
            assert "cannot ingest into" in ingest["error"]["message"]
    finally:
        production_app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)

    rows = await _audit_rows()
    assert _shape(rows) == _expected_rows(
        retired,
        [
            ("MCP", "/mcp/tools/call/search_docs"),
            ("MCP", "/mcp/tools/call/meho_connector_ingest"),
        ],
    )
    assert {row.status_code for row in rows} == {403}


# ===========================================================================
# A write that failed (MCP)
# ===========================================================================


async def test_mcp_failed_write_keeps_one_execution_row(
    retired: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that fails in Keycloak is a failed attempt, not a bad argument.

    REST answers 502 and writes an ``operation_execution`` row. The MCP
    handler raises ``McpExecutionFailedError`` for the same failure, so the
    attempt keeps one ``operation_execution`` row with the switch on.
    """
    op = build_operator(TenantRole.TENANT_ADMIN)

    async def _keycloak_down(*_args: Any, **_kwargs: Any) -> Any:
        raise KeycloakAdminError("keycloak unreachable")

    monkeypatch.setattr(AgentPrincipalService, "revoke", _keycloak_down)

    async def _fake_verify() -> Operator:
        structlog.contextvars.bind_contextvars(operator_sub=op.sub, tenant_id=str(op.tenant_id))
        return op

    production_app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(production_app) as client:
            result = _tool_call(client, "meho_agent_principals_revoke", {"name": "agent-x"})
            assert result["error"]["message"] == "Keycloak admin error: KeycloakAdminError"
    finally:
        production_app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)

    rows = await _audit_rows()
    row_class = "operation_execution" if retired else "legacy_request"
    assert _shape(rows) == [(row_class, "MCP", "/mcp/tools/call/meho_agent_principals_revoke")]
    assert rows[0].status_code == 502


# ===========================================================================
# Audit receipt header (#4976)
# ===========================================================================


def _receipt(response: Any) -> uuid.UUID | None:
    raw = response.headers.get("meho-audit-id")
    return None if raw is None else uuid.UUID(raw)


async def test_rest_response_names_its_audit_record(retired: bool) -> None:
    """``Meho-Audit-Id`` names the row ``meho audit query --audit-id`` returns.

    A change with its own record names that record, with or without the
    cutover (the legacy wrapper row is not the receipt). A change with no
    record of its own names the wrapper row. A routine read names its
    legacy row, or nothing after the cutover.
    """
    client, caller = _rest_client()
    recorded = client.post("/api/v1/cutover-probe/recorded")
    created = client.post("/api/v1/cutover-probe/things")
    read = client.get(f"/api/v1/operations/groups?connector_id={_CONNECTOR}")
    assert (recorded.status_code, created.status_code) == (204, 201)

    rows = {row.id: row for row in await _audit_rows()}
    recorded_row = rows[_receipt(recorded)]
    assert recorded_row.path == "probe.recorded"
    assert recorded_row.event_class == "operation_execution"
    created_row = rows[_receipt(created)]
    assert (created_row.method, created_row.path) == ("POST", "/api/v1/cutover-probe/things")
    assert created_row.operator_sub == caller.sub
    if retired:
        assert _receipt(read) is None
    else:
        assert rows[_receipt(read)].path == "/api/v1/operations/groups"
