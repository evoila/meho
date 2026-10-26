# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The audit row of one call carries the right op id and class.

The audit writers use :func:`~meho_backplane.broadcast.events.resolve_op_class`.
The cases:

* an MCP read tool whose name has no read suffix (``list_targets``) is
  ``read`` on the row, not ``other``;
* an MCP write tool is ``write``;
* the audit-query tool is ``audit_query`` and records aggregate detail;
* a declared class never weakens a credential or audit-query class;
* an op the dispatcher runs inside an agent run keeps its own op id and
  class, even though the agent-run route bound ``agent.run`` / ``write``
  for its own row;
* the registry write ``targets.register`` is ``write``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.events import classify_op, resolve_op_class
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog
from meho_backplane.mcp.registry import ToolDefinition, ToolSurface, register_mcp_tool
from meho_backplane.operations._audit import write_audit_row
from tests.mcp_test_fixtures import (
    build_operator,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
    seeded_operator_tenant,  # noqa: F401 — pytest-discovered fixture
)


@pytest.fixture
def agent_run_labels() -> Iterator[None]:
    """Bind the labels the agent-run route sets for its own row."""
    with structlog.contextvars.bound_contextvars(audit_op_id="agent.run", audit_op_class="write"):
        yield


async def _row(audit_id: uuid.UUID) -> AuditLog:
    async with get_sessionmaker()() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.id == audit_id))
        return result.scalar_one()


async def _mcp_row(
    client: TestClient,
    tool: str,
    arguments: dict[str, Any] | None = None,
) -> AuditLog:
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        },
    )
    assert response.status_code == 200
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.path == f"/mcp/tools/call/{tool}")
        )
        rows = result.scalars().all()
    assert len(rows) == 1
    return rows[0]


async def _noop_handler(_operator: Operator, _arguments: dict[str, Any]) -> dict[str, Any]:
    return {}


def _register_test_tool(name: str, op_class: str) -> None:
    register_mcp_tool(
        ToolDefinition(
            feature=None,
            surface=ToolSurface.WORKING,
            name=name,
            description="Test tool (op class agreement).",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
            required_role=TenantRole.READ_ONLY,
            op_class=op_class,
        ),
        _noop_handler,
    )


@pytest.mark.asyncio
async def test_mcp_read_tool_row_has_the_class(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811 — the fixture
) -> None:
    """``list_targets`` has no read suffix; the row still says ``read``."""
    client, _op = client_with_operator
    assert classify_op("list_targets") == "other"

    row = await _mcp_row(client, "list_targets")

    assert row.payload["op_id"] == "list_targets"
    assert row.payload["op_class"] == "read"


@pytest.mark.asyncio
async def test_mcp_write_tool_row_has_the_class(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811 — the fixture
) -> None:
    """A write tool whose name has no write suffix is ``write`` on the row."""
    client, _op = client_with_operator
    _register_test_tool("acme_widget_register", "write")

    row = await _mcp_row(client, "acme_widget_register")

    assert row.payload["op_id"] == "acme_widget_register"
    assert row.payload["op_class"] == "write"


@pytest.mark.asyncio
async def test_mcp_declared_class_never_weakens_credential_class(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811 — the fixture
) -> None:
    """A tool named like a credential read stays ``credential_read`` on the row."""
    client, _op = client_with_operator
    _register_test_tool("vault.kv.read", "read")

    row = await _mcp_row(client, "vault.kv.read")

    assert row.payload["op_class"] == "credential_read"


@pytest.mark.asyncio
@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_mcp_audit_query_tool_records_aggregate_detail(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811 — the fixture
) -> None:
    """The audit-query tool's audit row records aggregate detail."""
    client, _op = client_with_operator

    row = await _mcp_row(
        client,
        "query_audit",
        arguments={"principal": "someone", "op_id": "vault.*"},
    )

    assert row.payload["op_class"] == "audit_query"
    assert row.payload["broadcast_detail_effective"] == "aggregate"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "expected_class"),
    [
        ("linux.about", "read"),
        ("vsphere.vm.create", "write"),
        ("targets.register", "write"),
    ],
)
async def test_agent_dispatch_row_carries_the_dispatched_op(
    agent_run_labels: None,
    op_id: str,
    expected_class: str,
) -> None:
    """Inside an agent run the dispatch row names the op, not ``agent.run``."""
    operator = build_operator(TenantRole.OPERATOR)
    descriptor = SimpleNamespace(
        op_id=op_id,
        source_kind="typed",
        product="acme",
        version="1.x",
        impl_id="acme-1",
    )
    audit_id = uuid.uuid4()

    await write_audit_row(
        audit_id=audit_id,
        event_class=AuditEventClass.OPERATION_EXECUTION,
        operator=operator,
        descriptor=descriptor,  # type: ignore[arg-type]
        target=None,
        params_hash="",
        result_status="ok",
        duration_ms=1.0,
    )

    row = await _row(audit_id)
    assert row.path == row.payload["op_id"] == op_id
    assert row.payload["op_class"] == expected_class


def test_resolve_op_class_rules() -> None:
    """Declared class wins, except over a credential or audit-query class."""
    assert resolve_op_class("list_targets", "read") == "read"
    assert resolve_op_class("list_targets") == "other"
    assert resolve_op_class("query_audit", "audit_query") == "audit_query"
    assert resolve_op_class("vault.kv.read", "read") == "credential_read"
    assert resolve_op_class("meho_audit_replay", "read") == "audit_query"
    assert resolve_op_class("targets.register") == "write"
