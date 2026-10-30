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
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jsonschema
import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import OperationRun, OperationRunOrigin, OperationRunStatus
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.operations.delivery_budget import measure_delivered_wire_bytes
from meho_backplane.operations.operation_run_service import (
    MAX_RUN_WAIT_SECONDS,
    OperationRunService,
)
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 -- pytest-discovered fixture
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

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
    wait = schema["properties"]["wait_seconds"]
    assert wait["type"] == "integer"
    assert wait["minimum"] == 0
    assert wait["maximum"] == MAX_RUN_WAIT_SECONDS
    assert schema["required"] == ["connector_id", "op_id"]


# ---------------------------------------------------------------------------
# wait_seconds -- a check that waits for the run to end (#5264)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_one_waiting_check_gets_the_result_of_a_run_that_ends_during_the_wait(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start a real governed write, then one check with ``wait_seconds`` gets its result."""
    # A large recheck interval: only the end of the local task can wake the wait.
    monkeypatch.setattr(OperationRunService, "wait_recheck_seconds", 30.0)
    client, _op = client_with_operator
    base = {"connector_id": _CONNECTOR, "op_id": _OP}
    started = _structured(
        _call(
            client, {**base, "params": {"kind": "vault-role", "name": "wait-node"}, "async": True}
        )
    )
    run_id = started["extras"]["run_id"]

    before = time.monotonic()
    final = _structured(_call(client, {**base, "run_id": run_id, "wait_seconds": 20}, req_id=2))

    assert final["status"] == "ok", final
    assert uuid.UUID(final["audit_id"])
    assert final["extras"]["run_status"] == OperationRunStatus.SUCCEEDED.value
    assert time.monotonic() - before < 15


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_waiting_check_answers_running_when_the_time_is_up(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(OperationRunService, "wait_recheck_seconds", 0.2)
    client, _op = client_with_operator
    run_id = await _insert_run(status=OperationRunStatus.RUNNING)

    before = time.monotonic()
    payload = _structured(
        _call(
            client,
            {"connector_id": _CONNECTOR, "op_id": _OP, "run_id": str(run_id), "wait_seconds": 1},
        )
    )
    assert payload["status"] == "running"
    assert "next_step" in payload["extras"]
    assert time.monotonic() - before >= 1


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.parametrize("wait_seconds", [0, None])
async def test_no_wait_answers_at_once(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    wait_seconds: int | None,
) -> None:
    """``wait_seconds`` 0 or not sent: one read, no pause, as before."""

    async def no_wait(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a check without wait_seconds must not wait")

    monkeypatch.setattr(OperationRunService, "wait_until_done", no_wait)
    client, _op = client_with_operator
    run_id = await _insert_run(status=OperationRunStatus.RUNNING)
    arguments: dict[str, Any] = {"connector_id": _CONNECTOR, "op_id": _OP, "run_id": str(run_id)}
    if wait_seconds is not None:
        arguments["wait_seconds"] = wait_seconds
    assert _structured(_call(client, arguments))["status"] == "running"


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.parametrize("bad", [MAX_RUN_WAIT_SECONDS + 1, -1, 1.5, "10", True])
def test_bad_wait_seconds_is_refused(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    bad: Any,
) -> None:
    client, _op = client_with_operator
    arguments = {"connector_id": _CONNECTOR, "op_id": _OP, "run_id": str(uuid.uuid4())}
    response = _call(client, {**arguments, "wait_seconds": bad})
    body = response.json()
    assert body["error"]["code"] == -32602, body
    assert "inputSchema" in body["error"]["message"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_wait_seconds_above_the_limit_names_the_limit(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    arguments = {"connector_id": _CONNECTOR, "op_id": _OP, "run_id": str(uuid.uuid4())}
    response = _call(client, {**arguments, "wait_seconds": MAX_RUN_WAIT_SECONDS + 1})
    body = response.json()
    assert body["error"]["code"] == -32602, body
    assert f"maximum of {MAX_RUN_WAIT_SECONDS}" in body["error"]["message"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_wait_seconds_without_run_id_is_refused(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    response = _call(client, {"connector_id": _CONNECTOR, "op_id": _OP, "wait_seconds": 5})
    assert _invalid_params_reason(response) == "wait_without_run_id"


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_wrong_run_is_refused_before_any_wait(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_wait(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a wrong run_id must be refused before the wait")

    monkeypatch.setattr(OperationRunService, "wait_until_done", no_wait)
    client, _op = client_with_operator
    other_op_run = await _insert_run(status=OperationRunStatus.RUNNING, op_id="topology.other")
    response = _call(
        client,
        {"connector_id": _CONNECTOR, "op_id": _OP, "run_id": str(other_op_run), "wait_seconds": 10},
    )
    assert _invalid_params_reason(response) == "unknown_run"
