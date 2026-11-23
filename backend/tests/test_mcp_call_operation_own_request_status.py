# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An MCP caller reads the state of its own approval request (#6795).

``call_operation(connector_id, op_id, approval_request_id)`` answers with the
state of the caller's own request and runs nothing, behind the
``MCP_OWN_REQUEST_STATUS`` switch (off by default). These tests check:

* with the switch off, the input is not in the tool and a call with it is
  refused as bad input;
* each state gives its answer;
* another person, another tenant and an unknown id get the same answer;
* a wrong connector or operation, and extra inputs, are refused and nothing
  is read;
* under a hosted agent's connector list, a request outside the list is
  refused before any other check, and the answer names nothing outside it;
* a read writes one ``approval.result`` audit row and changes no request;
* a stored result stays inside the response size limit with the added fields;
* an approved secret reveal with ``MCP_REVEAL_FETCH`` on still runs the fetch.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.connectors.schemas import (
    FetchMore,
    FetchMoreDrillIn,
    FetchMoreNativePagination,
    OperationResult,
    ResultHandle,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditLog
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.tools._call_operation_status import read_own_request_over_mcp
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._target_connector_limit import connector_limit
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import create_pending_request
from meho_backplane.operations.delivery_budget import measure_delivered_wire_bytes
from meho_backplane.operations.deny_reasons import CONNECTOR_NOT_ALLOWED
from meho_backplane.settings import get_settings
from meho_backplane.untrusted_text import with_data_notice
from tests._reveal_fetch_helpers import (
    CONNECTOR_ID as REVEAL_CONNECTOR,
)
from tests._reveal_fetch_helpers import (
    READ_OP as REVEAL_OP,
)
from tests._reveal_fetch_helpers import (
    SECRET_VALUE,
    approved_reveal,
    register_vault_read,
)
from tests.mcp_test_fixtures import (
    build_operator,
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

_CONNECTOR = "demo-1.x"
_OP = "demo.vm.delete"
_REQUESTER = build_operator(TenantRole.OPERATOR, scopes=frozenset())
_NOT_FOUND = "approval_request_not_found"


class _Caller:
    operator: Operator = _REQUESTER


def _switch(monkeypatch: pytest.MonkeyPatch, *, status: bool, reveal: bool = False) -> None:
    monkeypatch.setenv("MCP_OWN_REQUEST_STATUS", "true" if status else "false")
    monkeypatch.setenv("MCP_REVEAL_FETCH", "true" if reveal else "false")
    get_settings.cache_clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    _Caller.operator = _REQUESTER

    async def _fake_verify() -> Operator:
        op = _Caller.operator
        structlog.contextvars.bind_contextvars(operator_sub=op.sub, tenant_id=str(op.tenant_id))
        return op

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


async def _park(
    owner: Operator = _REQUESTER, *, connector_id: str = _CONNECTOR, op_id: str = _OP
) -> uuid.UUID:
    params = {"vm": "vm-1"}
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=owner,
            connector_id=connector_id,
            op_id=op_id,
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
        )
        await session.commit()
        return request.id


async def _set(request_id: uuid.UUID, **fields: Any) -> None:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        for name, value in fields.items():
            setattr(row, name, value)
        await session.commit()


async def _count(model: Any, *where: Any) -> int:
    async with get_sessionmaker()() as session:
        return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def _result_rows() -> Any:
    return _count(AuditLog, AuditLog.path == "approval.result")


def _call(client: TestClient, request_id: uuid.UUID | str, **overrides: Any) -> Any:
    arguments = {
        "connector_id": _CONNECTOR,
        "op_id": _OP,
        "approval_request_id": str(request_id),
        **overrides,
    }
    return post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "call_operation", "arguments": arguments},
        },
    )


def _answer(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    structured: dict[str, Any] = response.json()["result"]["structuredContent"]
    return structured


def _listed_call_operation(client: TestClient) -> dict[str, Any]:
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
    return tools["call_operation"]


async def test_switch_off_lists_nothing_and_refuses_the_input(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, status=False)
    assert "approval_request_id" not in str(_listed_call_operation(client))
    request_id = await _park()
    before = await _result_rows()

    response = _call(client, request_id)

    assert response.json()["error"]["code"] == -32602, response.text
    assert await _result_rows() == before


async def test_switch_on_lists_the_input_and_its_text(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, status=True)
    listed = _listed_call_operation(client)
    assert "approval_request_id" in listed["inputSchema"]["properties"]
    assert "see the state of that request" in listed["description"]


async def test_both_switches_on_keep_one_input_with_both_texts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, status=True, reveal=True)
    listed = _listed_call_operation(client)
    text = listed["inputSchema"]["properties"]["approval_request_id"]["description"]
    assert "own approval request" in text
    assert "reveal_secret" in text


async def test_a_waiting_request_gets_the_awaiting_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, status=True)
    request_id = await _park()
    requests_before = await _count(ApprovalRequest)

    answer = _answer(_call(client, request_id))

    assert answer["status"] == "awaiting_approval"
    assert answer["extras"]["error_code"] == "awaiting_approval"
    assert answer["extras"]["approval_request_id"] == str(request_id)
    assert await _count(ApprovalRequest) == requests_before


@pytest.mark.parametrize(
    ("fields", "state"),
    [
        ({}, "not_started"),
        ({"resumed_at": datetime.now(UTC)}, "started"),
    ],
    ids=["not-started", "started"],
)
async def test_an_approved_request_that_has_not_finished_says_it_runs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fields: dict[str, Any], state: str
) -> None:
    _switch(monkeypatch, status=True)
    request_id = await _park()
    await _set(request_id, status=ApprovalRequestStatus.APPROVED.value, **fields)

    answer = _answer(_call(client, request_id))

    assert answer["status"] == "pending"
    assert answer["extras"]["result_state"] == state
    assert answer["extras"]["approval_status"] == "approved"
    assert "running" in answer["extras"]["next_step"]


@pytest.mark.parametrize(
    ("envelope", "state"),
    [
        (
            OperationResult(
                status="ok", op_id=_OP, result={"deleted": True}, duration_ms=1.0
            ).model_dump(mode="json"),
            "executed",
        ),
        (
            OperationResult(
                status="error", op_id=_OP, error="vendor said no", duration_ms=1.0
            ).model_dump(mode="json"),
            "failed",
        ),
    ],
    ids=["executed", "failed"],
)
async def test_an_approved_request_that_ran_gives_the_stored_result(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, envelope: dict[str, Any], state: str
) -> None:
    _switch(monkeypatch, status=True)
    request_id = await _park()
    await _set(
        request_id,
        status=ApprovalRequestStatus.APPROVED.value,
        resumed_at=datetime.now(UTC),
        resume_result=envelope,
    )

    answer = _answer(_call(client, request_id))

    assert answer["status"] == envelope["status"]
    assert answer.get("result") == envelope.get("result")
    assert answer["extras"]["result_state"] == state
    assert answer["extras"]["approval_request_id"] == str(request_id)


def _readable_handle() -> ResultHandle:
    return ResultHandle(
        handle_id=uuid.uuid4(),
        summary_md="500 rows",
        schema_={"type": "array"},
        total_rows=500,
        ttl_seconds=3600,
        fetch_more=FetchMore(
            drill_in=FetchMoreDrillIn(
                available=True,
                rationale="Call result_query with the handle id.",
                mcp_tool="result_query",
            ),
            native_pagination=FetchMoreNativePagination(available=False, rationale="none"),
        ),
    )


@pytest.mark.parametrize(
    ("over", "handle", "delivery"),
    [
        (0, None, None),
        (1, None, "unavailable"),
        (1, _readable_handle(), "partial"),
    ],
    ids=["at-limit", "over-limit", "over-limit-with-handle"],
)
async def test_a_stored_result_stays_inside_the_response_size_limit(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    over: int,
    handle: ResultHandle | None,
    delivery: str | None,
) -> None:
    """The stored result fits alone. The fields the read adds decide if it still fits."""
    _switch(monkeypatch, status=True)
    request_id = await _park()
    audit_id = uuid.uuid4()
    stored = OperationResult(
        status="ok",
        op_id=_OP,
        result={"blob": "x" * 4000},
        duration_ms=12.5,
        audit_id=audit_id,
        handle=handle,
    ).model_dump(mode="json")
    added = {
        "approval_request_id": str(request_id),
        "approval_status": "approved",
        "result_state": "executed",
    }
    full_answer = with_data_notice({**stored, "extras": added})
    # The limit is the size of the full answer, or one byte less.
    limit = measure_delivered_wire_bytes(full_answer) - over
    assert measure_delivered_wire_bytes(stored) <= limit
    monkeypatch.setenv("RESULT_COMPLETE_RESPONSE_WIRE_MAX_BYTES", str(limit))
    get_settings.cache_clear()
    await _set(
        request_id,
        status=ApprovalRequestStatus.APPROVED.value,
        resumed_at=datetime.now(UTC),
        resume_result=stored,
    )
    rows_before = await _result_rows()

    answer = _answer(_call(client, request_id))

    assert measure_delivered_wire_bytes(answer) <= limit
    assert answer["status"] == "ok"
    assert answer["audit_id"] == str(audit_id)
    assert answer["delivery"] == delivery
    assert answer["data_notice"] == full_answer["data_notice"]
    if delivery is None:
        assert answer["result"] == stored["result"]
        assert answer["extras"] == added
    else:
        assert answer["result"] is None
        assert answer["extras"]["retry"] is False
        assert answer["extras"]["delivery_max_bytes"] == limit
        assert answer["handle"] == stored["handle"]
    assert await _result_rows() == rows_before + 1


@pytest.mark.parametrize("state", ["rejected", "expired", "withdrawn"])
async def test_a_request_that_did_not_run_is_denied_and_says_to_ask_again(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    _switch(monkeypatch, status=True)
    request_id = await _park()
    await _set(request_id, status=state)

    answer = _answer(_call(client, request_id))

    assert answer["status"] == "denied"
    assert answer["extras"]["error_code"] == f"approval_request_{state}"
    assert "Ask again" in answer["error"]


@pytest.mark.parametrize(
    "overrides",
    [{}, {"connector_id": "other-1.x"}, {"op_id": "demo.vm.list"}],
    ids=["right-operation", "wrong-connector", "wrong-operation"],
)
async def test_another_person_another_tenant_and_unknown_id_get_the_same_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, str]
) -> None:
    """The owner and tenant check runs first, so a wrong operation names nothing."""
    _switch(monkeypatch, status=True)
    request_id = await _park()
    other_tenant = uuid.UUID("00000000-0000-0000-0000-000000006795")
    unknown = uuid.UUID("00000000-0000-0000-0000-000000006796")
    cases = [
        (_REQUESTER.model_copy(update={"sub": "someone-else"}), request_id),
        (_REQUESTER.model_copy(update={"tenant_id": other_tenant}), request_id),
        (_REQUESTER, unknown),
    ]
    before = await _result_rows()
    answers = []
    for caller, asked in cases:
        _Caller.operator = caller
        answer = _answer(_call(client, asked, **overrides))
        assert answer["status"] == "denied"
        assert answer["extras"]["error_code"] == _NOT_FOUND
        answer["extras"] = {k: v for k, v in answer["extras"].items() if k != "approval_request_id"}
        answers.append(answer)
    assert answers[0] == answers[1] == answers[2]
    assert await _result_rows() == before


@pytest.mark.parametrize(
    "overrides",
    [{"connector_id": "other-1.x"}, {"op_id": "demo.vm.list"}],
    ids=["connector", "operation"],
)
async def test_a_wrong_connector_or_operation_is_refused_and_nothing_is_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, str]
) -> None:
    _switch(monkeypatch, status=True)
    request_id = await _park()
    before = await _result_rows()

    answer = _answer(_call(client, request_id, **overrides))

    assert answer["status"] == "denied"
    assert answer["extras"]["error_code"] == "request_wrong_operation"
    assert await _result_rows() == before


_IN_LIST = "other-1.x"


def _arguments(request_id: uuid.UUID, **overrides: str) -> dict[str, Any]:
    return {
        "connector_id": _CONNECTOR,
        "op_id": _OP,
        "approval_request_id": str(request_id),
        **overrides,
    }


@pytest.mark.parametrize(
    ("allowed", "overrides"),
    [
        (frozenset({_IN_LIST}), {}),
        (frozenset(), {}),
        (frozenset({_IN_LIST}), {"connector_id": _IN_LIST}),
        (frozenset({_IN_LIST}), {"op_id": "demo.vm.list"}),
    ],
    ids=["right-operation", "empty-list", "wrong-connector", "wrong-operation"],
)
@pytest.mark.usefixtures("client")
async def test_a_request_outside_the_connector_list_is_refused_and_names_nothing(
    monkeypatch: pytest.MonkeyPatch, allowed: frozenset[str], overrides: dict[str, str]
) -> None:
    _switch(monkeypatch, status=True)
    request_id = await _park()
    before = await _result_rows()

    with connector_limit(allowed):
        answer = await read_own_request_over_mcp(_REQUESTER, _arguments(request_id, **overrides))

    assert answer["status"] == "denied"
    assert answer["extras"]["error_code"] == CONNECTOR_NOT_ALLOWED
    # Only the op_id field may hold what the caller sent.
    text = json.dumps({k: v for k, v in answer.items() if k != "op_id"})
    assert _CONNECTOR not in text
    assert _OP not in text
    assert await _result_rows() == before


@pytest.mark.usefixtures("client")
async def test_a_request_inside_the_connector_list_reads_its_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, status=True)
    request_id = await _park()
    before = await _result_rows()

    with connector_limit(frozenset({_CONNECTOR, _IN_LIST})):
        answer = await read_own_request_over_mcp(_REQUESTER, _arguments(request_id))

    assert answer["status"] == "awaiting_approval"
    assert answer["extras"]["approval_request_id"] == str(request_id)
    assert await _result_rows() == before + 1


@pytest.mark.parametrize(
    "extra",
    [{"params": {"vm": "vm-2"}}, {"target": "some-target"}, {"async": True}],
    ids=["params", "target", "async"],
)
async def test_extra_inputs_are_refused_as_bad_input(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, extra: dict[str, Any]
) -> None:
    _switch(monkeypatch, status=True)
    request_id = await _park()
    before = await _result_rows()

    body = _call(client, request_id, **extra).json()

    assert body["error"]["code"] == -32602, body
    assert body["error"]["data"]["reason"] == "own_request_status_extra_inputs"
    assert await _result_rows() == before


async def test_a_bad_request_id_is_refused_as_bad_input(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, status=True)
    assert _call(client, "not-a-uuid").json()["error"]["code"] == -32602


async def test_a_read_writes_one_audit_row_and_changes_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, status=True)
    request_id = await _park()
    rows_before = await _result_rows()
    requests_before = await _count(ApprovalRequest)

    _answer(_call(client, request_id))

    assert await _result_rows() == rows_before + 1
    assert await _count(ApprovalRequest) == requests_before
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
    assert row is not None
    assert row.status == ApprovalRequestStatus.PENDING.value
    assert row.resumed_at is None
    assert row.resume_result is None


@pytest.fixture
async def vault_read(client: TestClient) -> AsyncIterator[None]:
    clear_registry()
    reset_dispatcher_caches()
    await register_vault_read()
    yield
    clear_registry()
    reset_dispatcher_caches()


@pytest.mark.usefixtures("vault_read")
async def test_an_approved_secret_reveal_still_runs_the_fetch_when_that_switch_is_on(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, status=True, reveal=True)
    request_id = await approved_reveal(_REQUESTER)

    response = _call(client, request_id, connector_id=REVEAL_CONNECTOR, op_id=REVEAL_OP)

    answer = _answer(response)
    assert answer["status"] == "ok"
    assert answer["result"]["data"]["password"] == SECRET_VALUE
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.usefixtures("vault_read")
async def test_an_approved_secret_reveal_with_the_fetch_switch_off_reads_state_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, status=True, reveal=False)
    request_id = await approved_reveal(_REQUESTER)

    response = _call(client, request_id, connector_id=REVEAL_CONNECTOR, op_id=REVEAL_OP)

    answer = _answer(response)
    assert answer["status"] == "pending"
    assert answer["extras"]["result_state"] == "not_started"
    assert "did not run" in answer["extras"]["next_step"]
    assert SECRET_VALUE not in response.text
