# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The audit status of a ``call_operation`` that fails before it runs (#4751).

An unknown op, an unknown connector, or params that do not match the
schema stop the call before any operation runs. The dispatcher writes no
operation row then, so the MCP tool-call row is the only record. That
row's ``status_code`` follows the result status (the same rule as
``isError``), and its payload carries the result status and the
``extras.error_code``. With the wrapper rows retired, the request usage
event carries the real outcome.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.main import app as production_app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.handlers import _operation_result_audit
from meho_backplane.mcp.tools import operations as operation_tools
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations.ingest.boot_stamp import BOOT_STAMP_OPERATOR_SUB
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    build_operator,
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

from ._request_usage_helpers import read_usage_events

_CONNECTOR = "stub-1.x"
_OK_OP = "stub.thing.get"
_BOOM_OP = "stub.thing.boom"
_CALL_PATH = "/mcp/tools/call/call_operation"


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


async def _boom_handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("stub handler failed")


@pytest.fixture
async def stub_op(client: TestClient) -> AsyncIterator[None]:
    """Register a stub connector with one typed op that needs a ``name`` param.

    Depends on *client* so the registry is replaced after the app started.
    """
    clear_registry()
    reset_dispatcher_caches()
    service = AsyncMock()
    service.encode_one.return_value = [0.0] * EMBEDDING_DIMENSION
    service.encode.return_value = [[0.0] * EMBEDDING_DIMENSION]
    service.dimension = EMBEDDING_DIMENSION
    register_connector_v2(product="stub", version="", impl_id="", cls=_StubConnector)
    await register_typed_operation(
        product="stub",
        version="1.x",
        impl_id="stub",
        op_id=_OK_OP,
        handler=_ok_handler,
        summary="Stub op.",
        description="Used by the audit status tests.",
        parameter_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        when_to_use=None,
        embedding_service=service,
    )
    await register_typed_operation(
        product="stub",
        version="1.x",
        impl_id="stub",
        op_id=_BOOM_OP,
        handler=_boom_handler,
        summary="Stub op that fails.",
        description="Used by the audit status tests.",
        parameter_schema={"type": "object", "properties": {}},
        when_to_use=None,
        embedding_service=service,
    )
    yield
    clear_registry()
    reset_dispatcher_caches()


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Production app with a fixed MCP operator."""
    op = build_operator(TenantRole.OPERATOR)

    async def _fake_verify() -> Operator:
        structlog.contextvars.bind_contextvars(operator_sub=op.sub, tenant_id=str(op.tenant_id))
        return op

    production_app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(production_app) as test_client:
            yield test_client
    finally:
        production_app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


@pytest.fixture
def cutover_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Turn request usage on and retire the wrapper rows."""
    monkeypatch.setenv("REQUEST_USAGE_RETENTION_DAYS", "30")
    monkeypatch.setenv("AUDIT_WRAPPER_ROWS_RETIRED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _call(client: TestClient, arguments: dict[str, Any], tool: str = "call_operation") -> Any:
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert "result" in body, body
    return body["result"]


async def _tool_rows(path: str = _CALL_PATH) -> list[AuditLog]:
    """The MCP tool-call rows for *path*, oldest first."""
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(AuditLog)
            .where(AuditLog.operator_sub != BOOT_STAMP_OPERATOR_SUB, AuditLog.path == path)
            .order_by(AuditLog.occurred_at),
        )
        return list(result.scalars().all())


@pytest.mark.usefixtures("stub_op")
@pytest.mark.parametrize(
    ("arguments", "status_code", "error_code"),
    [
        ({"connector_id": _CONNECTOR, "op_id": "stub.thing.missing"}, 404, "unknown_op"),
        ({"connector_id": "nope-9.x", "op_id": _OK_OP}, 404, "unknown_connector"),
        ({"connector_id": _CONNECTOR, "op_id": _OK_OP, "params": {}}, 400, "invalid_params"),
    ],
    ids=["unknown_op", "unknown_connector", "invalid_params"],
)
async def test_failed_before_run_row_has_error_status(
    client: TestClient,
    arguments: dict[str, Any],
    status_code: int,
    error_code: str,
) -> None:
    result = _call(client, arguments)
    assert result["isError"] is True

    rows = await _tool_rows()
    assert len(rows) == 1
    assert rows[0].status_code == status_code
    assert rows[0].payload["result_status"] == "error"
    assert rows[0].payload["error_code"] == error_code


@pytest.mark.usefixtures("stub_op")
async def test_ok_call_row_keeps_200(client: TestClient) -> None:
    result = _call(client, {"connector_id": _CONNECTOR, "op_id": _OK_OP, "params": {"name": "a"}})
    assert result["isError"] is False

    rows = await _tool_rows()
    assert len(rows) == 1
    assert rows[0].status_code == 200
    assert rows[0].payload["result_status"] == "ok"
    assert "error_code" not in rows[0].payload


@pytest.mark.usefixtures("stub_op")
async def test_error_after_run_row_matches_dispatch_row(client: TestClient) -> None:
    """A call that ran and failed: the tool-call row says what the operation row says."""
    result = _call(client, {"connector_id": _CONNECTOR, "op_id": _BOOM_OP})
    assert result["isError"] is True

    dispatch_rows = await _tool_rows(_BOOM_OP)
    assert len(dispatch_rows) == 1
    assert dispatch_rows[0].method == "DISPATCH"
    rows = await _tool_rows()
    assert len(rows) == 1
    assert rows[0].payload["result_status"] == "error"
    assert rows[0].status_code == dispatch_rows[0].status_code == 500


@pytest.mark.usefixtures("cutover_on", "stub_op")
async def test_cutover_error_after_run_writes_no_tool_row(client: TestClient) -> None:
    """With the wrapper rows retired, the dispatcher's row is the only record."""
    _call(client, {"connector_id": _CONNECTOR, "op_id": _BOOM_OP})

    assert await _tool_rows() == []
    dispatch_rows = await _tool_rows(_BOOM_OP)
    assert [row.status_code for row in dispatch_rows] == [500]


@pytest.mark.parametrize(
    ("status", "error_code", "status_code"),
    [
        ("awaiting_approval", "awaiting_approval", 200),
        ("denied", None, 403),
        ("rate_limited", "rate_limited", 429),
    ],
)
async def test_other_outcomes_get_their_status(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    error_code: str | None,
    status_code: int,
) -> None:
    extras = {"error_code": error_code} if error_code else {}

    async def _fake_call(_operator: Operator, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": status, "op_id": _OK_OP, "duration_ms": 1.0, "extras": extras}

    monkeypatch.setattr(operation_tools, "call_operation", _fake_call)
    _call(client, {"connector_id": _CONNECTOR, "op_id": _OK_OP})

    rows = await _tool_rows()
    assert len(rows) == 1
    assert rows[0].status_code == status_code
    assert rows[0].payload["result_status"] == status


@pytest.mark.usefixtures("stub_op")
async def test_preview_of_unknown_op_row_has_404(client: TestClient) -> None:
    result = _call(
        client,
        {"connector_id": _CONNECTOR, "op_id": "stub.thing.missing"},
        tool="preview_operation",
    )
    assert result["isError"] is True

    rows = await _tool_rows("/mcp/tools/call/preview_operation")
    assert len(rows) == 1
    assert rows[0].status_code == 404
    assert rows[0].payload["error_code"] == "unknown_op"


@pytest.mark.usefixtures("cutover_on", "stub_op")
async def test_cutover_usage_event_has_the_real_status(client: TestClient) -> None:
    """With the wrapper rows retired, the usage event holds the 4xx outcome."""
    _call(client, {"connector_id": _CONNECTOR, "op_id": "stub.thing.missing"})

    assert await _tool_rows() == []
    events = [event for event in await read_usage_events() if event.transport == "mcp"]
    calls = [event for event in events if "call_operation" in event.route]
    assert [event.outcome for event in calls] == ["not_found"]


def test_other_tools_keep_200() -> None:
    assert _operation_result_audit("meho_runbook_next", {"status": "error"}) == (200, {})
