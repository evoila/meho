# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The audit row and the broadcast event of one call carry the same op id and class.

Both writers use :func:`~meho_backplane.broadcast.events.resolve_op_class`.
The cases:

* an MCP read tool whose name has no read suffix (``list_targets``) is
  ``read`` on the row and on the event, not ``read`` / ``other``;
* an MCP write tool is ``write`` on both;
* a declared class never weakens a credential or audit-query class;
* an op the dispatcher runs inside an agent run keeps its own op id and
  class, even though the agent-run route bound ``agent.run`` / ``write``
  for its own row;
* the registry write ``targets.register`` is ``write`` on both.
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

import meho_backplane.mcp.handlers as handlers_module
import meho_backplane.operations._audit as dispatch_audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.broadcast.events import classify_op, resolve_op_class
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog
from meho_backplane.mcp.registry import ToolDefinition, ToolSurface, register_mcp_tool
from meho_backplane.operations._audit import publish_broadcast, write_audit_row
from tests.mcp_test_fixtures import (
    build_operator,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
    seeded_operator_tenant,  # noqa: F401 — pytest-discovered fixture
)


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Record every event the MCP handler and the dispatcher publish."""
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(handlers_module, "publish_event", _capture)
    monkeypatch.setattr(dispatch_audit_module, "publish_event", _capture)
    return events


@pytest.fixture
def agent_run_labels() -> Iterator[None]:
    """Bind the labels the agent-run route sets for its own row."""
    with structlog.contextvars.bound_contextvars(audit_op_id="agent.run", audit_op_class="write"):
        yield


async def _row(audit_id: uuid.UUID) -> AuditLog:
    async with get_sessionmaker()() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.id == audit_id))
        return result.scalar_one()


async def _mcp_row_and_event(
    client: TestClient,
    tool: str,
    events: list[BroadcastEvent],
) -> tuple[AuditLog, BroadcastEvent]:
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {}},
        },
    )
    assert response.status_code == 200
    mcp_events = [e for e in events if e.op_id == tool]
    assert len(mcp_events) == 1
    event = mcp_events[0]
    return await _row(event.audit_id), event


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
async def test_mcp_read_tool_row_and_event_agree(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811 — the fixture
    captured_events: list[BroadcastEvent],
) -> None:
    """``list_targets`` has no read suffix; both records still say ``read``."""
    client, _op = client_with_operator
    assert classify_op("list_targets") == "other"

    row, event = await _mcp_row_and_event(client, "list_targets", captured_events)

    assert row.payload["op_id"] == event.op_id == "list_targets"
    assert row.payload["op_class"] == event.op_class == "read"


@pytest.mark.asyncio
async def test_mcp_write_tool_row_and_event_agree(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811 — the fixture
    captured_events: list[BroadcastEvent],
) -> None:
    """A write tool whose name has no write suffix is ``write`` on both."""
    client, _op = client_with_operator
    _register_test_tool("acme_widget_register", "write")

    row, event = await _mcp_row_and_event(client, "acme_widget_register", captured_events)

    assert row.payload["op_id"] == event.op_id == "acme_widget_register"
    assert row.payload["op_class"] == event.op_class == "write"


@pytest.mark.asyncio
async def test_mcp_declared_class_never_weakens_credential_class(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811 — the fixture
    captured_events: list[BroadcastEvent],
) -> None:
    """A tool named like a credential read stays ``credential_read`` on both."""
    client, _op = client_with_operator
    _register_test_tool("vault.kv.read", "read")

    row, event = await _mcp_row_and_event(client, "vault.kv.read", captured_events)

    assert row.payload["op_class"] == event.op_class == "credential_read"
    assert "params" not in event.payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "expected_class"),
    [
        ("linux.about", "read"),
        ("vsphere.vm.create", "write"),
        ("targets.register", "write"),
    ],
)
async def test_agent_dispatch_row_and_event_carry_the_dispatched_op(
    agent_run_labels: None,
    captured_events: list[BroadcastEvent],
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
    await publish_broadcast(
        audit_id=audit_id,
        operator=operator,
        descriptor=descriptor,  # type: ignore[arg-type]
        target=None,
        params={},
        result_status="ok",
    )

    row = await _row(audit_id)
    assert len(captured_events) == 1
    event = captured_events[0]
    assert row.path == row.payload["op_id"] == event.op_id == op_id
    assert row.payload["op_class"] == event.op_class == expected_class


def test_resolve_op_class_rules() -> None:
    """Declared class wins, except over a credential or audit-query class."""
    assert resolve_op_class("list_targets", "read") == "read"
    assert resolve_op_class("list_targets") == "other"
    assert resolve_op_class("query_audit", "audit_query") == "audit_query"
    assert resolve_op_class("vault.kv.read", "read") == "credential_read"
    assert resolve_op_class("meho_audit_replay", "read") == "audit_query"
    assert resolve_op_class("targets.register") == "write"
