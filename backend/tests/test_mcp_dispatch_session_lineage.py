# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An operation called over MCP carries the MCP session id on its audit row (#4672).

A ``tools/call`` of ``call_operation`` writes two audit rows: the MCP tool
row (``method=MCP``) and the operation row (``method=DISPATCH``). Before
#4672 only the tool row carried the ``Mcp-Session-Id`` header's value as
``agent_session_id``, so a session replay showed the tool calls but not
what ran against the targets. These tests go through the real ``POST
/mcp`` route and check the operation row for a call that ran, one that
failed, one a deny grant refused, and one an MCP tool ran inside
(``meho_status`` reading from Vault). They also check that the two rows
share one ``request_id``, that a call without the header stays NULL, and
that the session replay now returns the operation rows.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.audit_query import replay_session
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission, AuditLog
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations.ingest.boot_stamp import BOOT_STAMP_OPERATOR_SUB
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from tests.mcp_test_fixtures import (
    MCP_ADMIN_SCOPE,
    OPERATOR_TENANT_ID,
    build_operator,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
    seeded_operator_tenant,  # noqa: F401 — pytest-discovered fixture
)

_CONNECTOR = "stub-1.x"
_OK_OP = "stub.thing.get"
_FAILING_OP = "stub.thing.fail"
_TOOL_ROW_PATH = "/mcp/tools/call/call_operation"


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
async def stub_ops(mcp_client: tuple[TestClient, dict[str, Operator]]) -> AsyncIterator[None]:
    """Register the stub connector with one op that works and one that fails.

    Depends on ``mcp_client`` so the app's startup (which checks the
    connector catalog) runs before the registry is replaced.
    """
    clear_registry()
    reset_dispatcher_caches()
    service = AsyncMock()
    service.encode_one.return_value = [0.0] * EMBEDDING_DIMENSION
    service.encode.return_value = [[0.0] * EMBEDDING_DIMENSION]
    service.dimension = EMBEDDING_DIMENSION
    register_connector_v2(product="stub", version="", impl_id="", cls=_StubConnector)
    for op_id, handler in ((_OK_OP, _ok_handler), (_FAILING_OP, _failing_handler)):
        await register_typed_operation(
            product="stub",
            version="1.x",
            impl_id="stub",
            op_id=op_id,
            handler=handler,
            summary=f"Stub op {op_id}.",
            description="Used by the MCP session lineage tests.",
            parameter_schema={"type": "object"},
            when_to_use=None,
            embedding_service=service,
        )
    yield
    clear_registry()
    reset_dispatcher_caches()


_AGENT_CLIENT_ID = "agent:scout"


def _agent_operator() -> Operator:
    """An agent principal, like the one in the issue's live case.

    A human operator is default-allow, so a ``deny`` grant only bites an
    agent. The grant is keyed on the agent's client id (#3795).
    """
    return Operator(
        sub="11111111-2222-3333-4444-555555555555",
        name="Scout",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
        client_id=_AGENT_CLIENT_ID,
        scopes=frozenset({MCP_ADMIN_SCOPE}),
    )


@pytest.fixture
def mcp_client() -> Iterator[tuple[TestClient, dict[str, Operator]]]:
    """The production app; the MCP operator is swappable through ``holder["op"]``."""
    holder = {"op": build_operator(TenantRole.OPERATOR)}

    async def _fake_verify() -> Operator:
        return holder["op"]

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client, holder
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


def _call_tool(
    client: TestClient,
    name: str,
    arguments: dict[str, Any],
    *,
    session_id: uuid.UUID | None,
) -> Any:
    headers = {"Mcp-Session-Id": str(session_id)} if session_id is not None else None
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _call_operation(client: TestClient, op_id: str, *, session_id: uuid.UUID | None) -> Any:
    return _call_tool(
        client,
        "call_operation",
        {"connector_id": _CONNECTOR, "op_id": op_id},
        session_id=session_id,
    )


async def _audit_rows() -> list[AuditLog]:
    """Every audit row except the lifespan boot stamp, oldest first."""
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(AuditLog)
            .where(AuditLog.operator_sub != BOOT_STAMP_OPERATOR_SUB)
            .order_by(AuditLog.occurred_at),
        )
        return list(result.scalars().all())


def _one(rows: list[AuditLog], *, method: str, path: str) -> AuditLog:
    matching = [row for row in rows if row.method == method and row.path == path]
    assert len(matching) == 1, [(row.method, row.path) for row in rows]
    return matching[0]


async def _seed_deny_grant(principal_sub: str) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            AgentPermission(
                tenant_id=OPERATOR_TENANT_ID,
                principal_sub=principal_sub,
                op_pattern="stub.*",
                verdict="deny",
                target_scope="*",
                created_by_sub="admin",
            )
        )


# ---------------------------------------------------------------------------
# The operation row carries the MCP session id
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("stub_ops")
async def test_operation_row_carries_the_mcp_session_id(
    mcp_client: tuple[TestClient, dict[str, Operator]],
) -> None:
    """A call that ran: the operation row has the tool row's session id, column and payload."""
    client, _holder = mcp_client
    session_id = uuid.uuid4()

    body = _call_operation(client, _OK_OP, session_id=session_id)
    assert body["result"]["isError"] is False, body

    rows = await _audit_rows()
    tool_row = _one(rows, method="MCP", path=_TOOL_ROW_PATH)
    op_row = _one(rows, method="DISPATCH", path=_OK_OP)
    assert tool_row.agent_session_id == session_id
    assert op_row.payload["result_status"] == "ok"
    assert op_row.agent_session_id == session_id
    assert op_row.payload["agent_session_id"] == str(session_id)


@pytest.mark.usefixtures("stub_ops")
async def test_failed_operation_row_carries_the_mcp_session_id(
    mcp_client: tuple[TestClient, dict[str, Operator]],
) -> None:
    """A call whose handler raised: the ``error`` row has the session id too."""
    client, _holder = mcp_client
    session_id = uuid.uuid4()

    _call_operation(client, _FAILING_OP, session_id=session_id)

    rows = await _audit_rows()
    op_row = _one(rows, method="DISPATCH", path=_FAILING_OP)
    assert op_row.payload["result_status"] == "error"
    assert op_row.agent_session_id == session_id
    assert _one(rows, method="MCP", path=_TOOL_ROW_PATH).agent_session_id == session_id


@pytest.mark.usefixtures("stub_ops", "seeded_operator_tenant")
async def test_refused_operation_row_carries_the_mcp_session_id(
    mcp_client: tuple[TestClient, dict[str, Operator]],
) -> None:
    """A call a deny grant refused: the ``denied`` row has the session id too."""
    client, holder = mcp_client
    holder["op"] = _agent_operator()
    await _seed_deny_grant(_AGENT_CLIENT_ID)
    session_id = uuid.uuid4()

    body = _call_operation(client, _OK_OP, session_id=session_id)
    assert "denied" in str(body), body

    rows = await _audit_rows()
    op_row = _one(rows, method="DISPATCH", path=_OK_OP)
    assert op_row.payload["result_status"] == "denied"
    assert op_row.status_code == 403
    assert op_row.agent_session_id == session_id
    assert _one(rows, method="MCP", path=_TOOL_ROW_PATH).agent_session_id == session_id


async def test_operation_an_mcp_tool_runs_inside_carries_the_mcp_session_id(
    mcp_client: tuple[TestClient, dict[str, Operator]],
) -> None:
    """``meho_status`` reads from Vault through the dispatcher; that row has the session id."""
    client, _holder = mcp_client
    session_id = uuid.uuid4()

    _call_tool(client, "meho_status", {}, session_id=session_id)

    rows = await _audit_rows()
    tool_row = _one(rows, method="MCP", path="/mcp/tools/call/meho_status")
    vault_row = _one(rows, method="DISPATCH", path="vault.kv.read")
    assert tool_row.agent_session_id == session_id
    assert vault_row.agent_session_id == session_id


# ---------------------------------------------------------------------------
# Without the header nothing changes
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("stub_ops")
async def test_without_the_header_the_operation_row_has_no_session_id(
    mcp_client: tuple[TestClient, dict[str, Operator]],
) -> None:
    client, _holder = mcp_client

    _call_operation(client, _OK_OP, session_id=None)

    rows = await _audit_rows()
    assert _one(rows, method="MCP", path=_TOOL_ROW_PATH).agent_session_id is None
    op_row = _one(rows, method="DISPATCH", path=_OK_OP)
    assert op_row.agent_session_id is None
    assert "agent_session_id" not in op_row.payload


# ---------------------------------------------------------------------------
# One request id for the tool row and the operation row
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("stub_ops")
async def test_tool_row_and_operation_row_share_the_request_id(
    mcp_client: tuple[TestClient, dict[str, Operator]],
) -> None:
    client, _holder = mcp_client

    _call_operation(client, _OK_OP, session_id=uuid.uuid4())

    rows = await _audit_rows()
    tool_row = _one(rows, method="MCP", path=_TOOL_ROW_PATH)
    op_row = _one(rows, method="DISPATCH", path=_OK_OP)
    assert tool_row.request_id is not None
    assert op_row.request_id == tool_row.request_id


# ---------------------------------------------------------------------------
# The session replay now returns the operation rows
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("stub_ops")
async def test_session_replay_returns_the_operation_rows(
    mcp_client: tuple[TestClient, dict[str, Operator]],
) -> None:
    """``meho audit replay <session-id>`` builds on ``replay_session``; it now sees both rows."""
    client, _holder = mcp_client
    session_id = uuid.uuid4()

    _call_operation(client, _OK_OP, session_id=session_id)
    _call_operation(client, _FAILING_OP, session_id=session_id)

    async with get_sessionmaker()() as session:
        nodes = await replay_session(session_id, tenant_id=OPERATOR_TENANT_ID, session=session)

    assert sorted((node.method, node.path) for node in nodes) == [
        ("DISPATCH", _FAILING_OP),
        ("DISPATCH", _OK_OP),
        ("MCP", _TOOL_ROW_PATH),
        ("MCP", _TOOL_ROW_PATH),
    ]
    assert all(node.agent_session_id == session_id for node in nodes)
