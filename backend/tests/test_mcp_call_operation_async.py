# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Async mode for the MCP ``call_operation`` tool (#3695).

``call_operation`` with ``async: true`` starts the call in the background
(the same ``operation_run`` runner as ``POST /api/v1/operations/call`` with
``async: true``) and answers at once with ``status="pending"`` and
``extras.run_id``. ``call_operation`` with the same ``connector_id`` /
``op_id`` and that ``run_id`` reads the run back. Every answer must still
match the tool's declared ``outputSchema``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import jsonschema
import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import OperationRun, OperationRunOrigin, OperationRunStatus
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.operations.delivery_budget import measure_delivered_wire_bytes
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 -- pytest-discovered fixture
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

_PUBLISH_NODES = "meho_backplane.topology.nodes.publish_event"
_CONNECTOR = "topology-graph-1.x"
_OP = "topology.create_node"


def _call(client: TestClient, arguments: dict[str, Any], *, req_id: int = 1) -> Any:
    return post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": "call_operation", "arguments": arguments},
        },
    )


def _structured(response: Any, *, is_error: bool = False) -> dict[str, Any]:
    """Return the structured result and check it against the outputSchema.

    ``is_error`` is the expected MCP ``isError`` flag. It follows the
    envelope ``status`` (#3675): a failed run answers ``status="error"``,
    so the flag is true; a pending, running or finished run keeps it false.
    """
    assert response.status_code == 200
    body = response.json()
    assert "error" not in body, body
    result = body["result"]
    assert result["isError"] is is_error, result
    structured: dict[str, Any] = result["structuredContent"]
    assert structured == json.loads(result["content"][0]["text"])
    entry = mcp_registry.get_tool("call_operation")
    assert entry is not None
    jsonschema.validate(
        instance=structured,
        schema=entry[0].outputSchema,
        cls=jsonschema.Draft202012Validator,
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )
    return structured


def _invalid_params_reason(response: Any) -> str:
    body = response.json()
    assert body["error"]["code"] == -32602, body
    reason: str = body["error"]["data"]["reason"]
    return reason


async def _insert_run(
    *,
    status: OperationRunStatus,
    op_id: str = _OP,
    error: str | None = None,
    result: dict[str, Any] | None = None,
    tenant_id: uuid.UUID = OPERATOR_TENANT_ID,
) -> uuid.UUID:
    now = datetime.now(UTC)
    ended = status in (OperationRunStatus.FAILED, OperationRunStatus.SUCCEEDED)
    async with get_sessionmaker()() as session:
        run = OperationRun(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            identity_sub="op-1",
            origin=OperationRunOrigin.DIRECT.value,
            connector_id=_CONNECTOR,
            op_id=op_id,
            status=status.value,
            error=error,
            result=result,
            started_at=now - timedelta(minutes=5),
            ended_at=now if ended else None,
        )
        session.add(run)
        await session.commit()
        return run.id


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_async_call_returns_pending_then_the_stored_result(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Start a real governed write in async mode, then read its result by run_id."""
    client, _op = client_with_operator
    base = {"connector_id": _CONNECTOR, "op_id": _OP}
    with patch(_PUBLISH_NODES, new=AsyncMock()):
        started = _structured(
            _call(
                client,
                {**base, "params": {"kind": "vault-role", "name": "async-node"}, "async": True},
            )
        )
        assert started["status"] == "pending"
        assert started["audit_id"] is None
        run_id = started["extras"]["run_id"]
        assert uuid.UUID(run_id)
        assert started["extras"]["async"] is True

        final: dict[str, Any] = {}
        for attempt in range(100):
            final = _structured(_call(client, {**base, "run_id": run_id}, req_id=attempt + 2))
            if final["status"] not in ("pending", "running"):
                break
            await asyncio.sleep(0.05)

    assert final["status"] == "ok", final
    assert uuid.UUID(final["audit_id"])
    assert final["extras"]["run_id"] == run_id
    assert final["extras"]["run_status"] == OperationRunStatus.SUCCEEDED.value


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_running_run_reports_running_with_start_time(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    run_id = await _insert_run(status=OperationRunStatus.RUNNING)
    payload = _structured(
        _call(client, {"connector_id": _CONNECTOR, "op_id": _OP, "run_id": str(run_id)})
    )
    assert payload["status"] == "running"
    assert payload["extras"]["started_at"] is not None
    assert payload["duration_ms"] >= 5 * 60 * 1000 - 1000
    assert "next_step" in payload["extras"]


def _stored_ok_result_just_under_the_ceiling(limit: int) -> dict[str, Any]:
    """Return a stored ``ok`` result that fits *limit* with less than 100 bytes to spare.

    This is what the background runner stores: a result that already passed
    the wire ceiling when the call finished. The run details the poll adds
    are bigger than the room left, so the poll answer would break the limit
    if it were not checked again.
    """
    audit_id = uuid.uuid4()

    def _dump(padding: int) -> dict[str, Any]:
        return OperationResult(
            status="ok",
            op_id=_OP,
            result={"blob": "x" * padding},
            duration_ms=12.5,
            audit_id=audit_id,
        ).model_dump(mode="json")

    # Every padding byte goes on the wire twice: once in the text block and
    # once in the structured block.
    padding = (limit - measure_delivered_wire_bytes(_dump(0))) // 2
    stored = _dump(padding)
    size = measure_delivered_wire_bytes(stored)
    assert limit - 100 < size <= limit, size
    return stored


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_finished_run_near_the_ceiling_stays_within_the_limit(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """The run details the poll adds must not push the answer over the wire limit."""
    client, _op = client_with_operator
    limit = get_settings().result_complete_response_wire_max_bytes
    stored = _stored_ok_result_just_under_the_ceiling(limit)
    run_id = await _insert_run(status=OperationRunStatus.SUCCEEDED, result=stored)

    payload = _structured(
        _call(client, {"connector_id": _CONNECTOR, "op_id": _OP, "run_id": str(run_id)})
    )

    assert measure_delivered_wire_bytes(payload) <= limit
    assert payload["status"] == "ok"
    assert payload["audit_id"] == stored["audit_id"]
    assert payload["delivery"] == "unavailable"
    assert payload["result"] is None
    assert payload["extras"]["retry"] is False
    assert payload["extras"]["delivery_max_bytes"] == limit
    assert payload["extras"]["delivered_bytes"] > limit


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_finished_run_with_room_keeps_its_result_and_run_details(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    stored = OperationResult(
        status="ok",
        op_id=_OP,
        result={"node": "small"},
        duration_ms=1.0,
        audit_id=uuid.uuid4(),
        extras={"from_call": True},
    ).model_dump(mode="json")
    run_id = await _insert_run(status=OperationRunStatus.SUCCEEDED, result=stored)

    payload = _structured(
        _call(client, {"connector_id": _CONNECTOR, "op_id": _OP, "run_id": str(run_id)})
    )

    assert payload["status"] == "ok"
    assert payload["result"] == {"node": "small"}
    assert payload["audit_id"] == stored["audit_id"]
    assert payload["delivery"] is None
    assert payload["extras"]["from_call"] is True
    assert payload["extras"]["run_id"] == str(run_id)
    assert payload["extras"]["run_status"] == OperationRunStatus.SUCCEEDED.value
    assert payload["extras"]["ended_at"] is not None


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_failed_run_reports_an_error(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    run_id = await _insert_run(status=OperationRunStatus.FAILED, error="worker lost its lease")
    payload = _structured(
        _call(client, {"connector_id": _CONNECTOR, "op_id": _OP, "run_id": str(run_id)}),
        is_error=True,
    )
    assert payload["status"] == "error"
    assert payload["extras"]["error_code"] == "run_failed"
    assert "worker lost its lease" in payload["error"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_unknown_mismatched_or_foreign_run_is_unknown_run(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    base = {"connector_id": _CONNECTOR, "op_id": _OP}
    other_op_run = await _insert_run(status=OperationRunStatus.RUNNING, op_id="topology.other")
    foreign_run = await _insert_run(status=OperationRunStatus.RUNNING, tenant_id=uuid.uuid4())

    for run_id in (uuid.uuid4(), other_op_run, foreign_run):
        response = _call(client, {**base, "run_id": str(run_id)})
        assert _invalid_params_reason(response) == "unknown_run"


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_async_with_run_id_is_refused(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    response = _call(
        client,
        {"connector_id": _CONNECTOR, "op_id": _OP, "async": True, "run_id": str(uuid.uuid4())},
    )
    assert _invalid_params_reason(response) == "async_with_run_id"


def test_async_controls_are_declared_on_the_input_schema() -> None:
    """``async`` / ``run_id`` are optional; the required set does not change."""
    entry = mcp_registry.get_tool("call_operation")
    assert entry is not None
    schema = entry[0].inputSchema
    assert schema["properties"]["async"]["type"] == "boolean"
    assert schema["properties"]["run_id"]["format"] == "uuid"
    assert schema["required"] == ["connector_id", "op_id"]
