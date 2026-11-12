# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A person on MCP gets the value of an approved secret reveal (#6273).

``call_operation(connector_id, op_id, approval_request_id)`` runs the
approved ``reveal_secret`` request once for the person who asked, behind the
``MCP_REVEAL_FETCH`` switch (off by default). These tests check:

* with the switch off, ``call_operation`` lists no such input and no text
  about it, and a call that sends it is refused as bad input;
* with it on, an MCP client fetches the value once; the HTTP answer says
  ``Cache-Control: no-store``; a second fetch is refused with no value;
* a wrong operation, extra inputs, another person and an agent are refused
  and do not use up the one-time permission;
* a person's ``reveal_secret: true`` on a plain read runs at once, with the
  switch on or off, and its HTTP answer says no-store; the same call
  without ``reveal_secret`` does not;
* a reveal call cannot run in the background: ``reveal_secret: true``
  with ``async: true`` is refused as bad input, and no run starts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID

import pytest
import structlog
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import OperationRun
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.approved_reveal import fetch_approved_reveal
from meho_backplane.settings import get_settings
from tests._reveal_fetch_helpers import (
    CONNECTOR_ID,
    MINT_OP,
    PLAIN_OP,
    READ_OP,
    SECRET_VALUE,
    approved_reveal,
    register_mint_op,
    register_plain_read,
    register_vault_read,
    run_without_value,
)
from tests.mcp_test_fixtures import (
    build_operator,
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

#: The person who asked, on the default working surface (no ``mcp:admin``).
_REQUESTER = build_operator(TenantRole.OPERATOR, scopes=frozenset())


class _Caller:
    """The operator the MCP route sees; a test can switch it."""

    operator: Operator = _REQUESTER


def _switch(monkeypatch: pytest.MonkeyPatch, *, on: bool, short: bool = False) -> None:
    monkeypatch.setenv("MCP_REVEAL_FETCH", "true" if on else "false")
    monkeypatch.setenv("MCP_SHORT_TOOL_TEXTS", "true" if short else "false")
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


@pytest.fixture
async def vault_read(client: TestClient) -> AsyncIterator[None]:
    """The ``vault.kv.read`` stand-in, registered after the app started."""
    clear_registry()
    reset_dispatcher_caches()
    await register_vault_read()
    await register_plain_read()
    await register_mint_op()
    yield
    clear_registry()
    reset_dispatcher_caches()


def _fetch(client: TestClient, request_id: UUID, **overrides: Any) -> Any:
    arguments = {
        "connector_id": CONNECTOR_ID,
        "op_id": READ_OP,
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


def _listed_call_operation(client: TestClient) -> dict[str, Any]:
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 200, response.text
    tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
    return tools["call_operation"]


async def _value_still_there(request_id: UUID) -> None:
    """The refused calls did not use up the permission: the owner still gets the value."""
    result = await fetch_approved_reveal(_REQUESTER, request_id)
    assert result.status == "ok", result.error
    assert result.result is not None
    assert result.result["data"]["password"] == SECRET_VALUE


def _refusal(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    assert "pragma" not in response.headers
    assert SECRET_VALUE not in response.text
    result = response.json()["result"]
    assert result["isError"] is True
    structured: dict[str, Any] = result["structuredContent"]
    assert structured["status"] == "denied"
    return structured


@pytest.mark.usefixtures("vault_read")
async def test_switch_off_lists_nothing_and_refuses_the_input(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, on=False)
    listed = _listed_call_operation(client)
    assert "approval_request_id" not in str(listed)

    request_id = await approved_reveal(_REQUESTER)
    response = _fetch(client, request_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["error"]["code"] == -32602, body
    assert "approval_request_id" in body["error"]["message"]
    assert SECRET_VALUE not in response.text
    await _value_still_there(request_id)


@pytest.mark.usefixtures("vault_read")
@pytest.mark.parametrize("short", [False, True], ids=["full-text", "short-text"])
async def test_switch_on_lists_the_input_and_its_text(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, short: bool
) -> None:
    _switch(monkeypatch, on=True, short=short)
    listed = _listed_call_operation(client)
    assert "approval_request_id" in listed["inputSchema"]["properties"]
    assert "call again with the same `connector_id` and `op_id`" in listed["description"]
    assert "15 minutes" in listed["description"]


@pytest.mark.usefixtures("vault_read")
async def test_mcp_client_fetches_the_value_once_and_a_second_fetch_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, on=True)
    request_id = await approved_reveal(_REQUESTER)

    first = _fetch(client, request_id)
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    assert first.headers["pragma"] == "no-cache"
    result = first.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["status"] == "ok"
    assert result["structuredContent"]["result"]["data"]["password"] == SECRET_VALUE

    second = _refusal(_fetch(client, request_id))
    assert second["extras"]["error_code"] == "reveal_already_used"
    assert second["extras"]["approval_request_id"] == str(request_id)
    assert "already fetched" in second["error"]


@pytest.mark.usefixtures("vault_read")
async def test_a_fetch_of_a_new_credential_says_no_store(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An approved reveal of an operation that makes a new credential says no-store."""
    _switch(monkeypatch, on=True)
    request_id = await approved_reveal(_REQUESTER, None)

    response = _fetch(client, request_id, op_id=MINT_OP)
    assert response.status_code == 200, response.text
    structured = response.json()["result"]["structuredContent"]
    assert structured["status"] == "ok", structured
    assert SECRET_VALUE in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


@pytest.mark.usefixtures("vault_read")
@pytest.mark.parametrize("how", ["failed", "refused"])
async def test_a_fetch_whose_run_gives_no_value_keeps_usual_headers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """The approved run ends with an error or is refused: no value, no no-store."""
    _switch(monkeypatch, on=True)
    request_id = await run_without_value(monkeypatch, _REQUESTER, how)

    response = _fetch(client, request_id)
    assert response.status_code == 200, response.text
    assert response.json()["result"]["structuredContent"]["status"] != "ok"
    assert SECRET_VALUE not in response.text
    assert "pragma" not in response.headers
    assert response.headers.get("cache-control") != "no-store"


@pytest.mark.usefixtures("vault_read")
async def test_a_wrong_operation_is_refused_and_keeps_the_permission(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, on=True)
    request_id = await approved_reveal(_REQUESTER)

    refused = _refusal(_fetch(client, request_id, op_id="vault.kv.list"))

    assert refused["extras"]["error_code"] == "reveal_wrong_operation"
    await _value_still_there(request_id)


@pytest.mark.usefixtures("vault_read")
@pytest.mark.parametrize(
    "extra",
    [{"params": {"path": "other"}}, {"target": "some-target"}, {"async": True}],
    ids=["params", "target", "async"],
)
async def test_extra_inputs_are_refused_as_bad_input(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, extra: dict[str, Any]
) -> None:
    _switch(monkeypatch, on=True)
    request_id = await approved_reveal(_REQUESTER)

    response = _fetch(client, request_id, **extra)

    body = response.json()
    assert body["error"]["code"] == -32602, body
    assert body["error"]["data"]["reason"] == "reveal_fetch_extra_inputs"
    await _value_still_there(request_id)


@pytest.mark.usefixtures("vault_read")
async def test_a_bad_request_id_is_refused_as_bad_input(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, on=True)
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "call_operation",
                "arguments": {
                    "connector_id": CONNECTOR_ID,
                    "op_id": READ_OP,
                    "approval_request_id": "not-a-uuid",
                },
            },
        },
    )
    assert response.json()["error"]["code"] == -32602


@pytest.mark.usefixtures("vault_read")
async def test_an_unknown_request_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, on=True)
    refused = _refusal(_fetch(client, UUID("00000000-0000-0000-0000-000000006273")))
    assert refused["extras"]["error_code"] == "approval_request_not_found"


@pytest.mark.usefixtures("vault_read")
@pytest.mark.parametrize(
    ("caller", "error_code"),
    [
        (_REQUESTER.model_copy(update={"sub": "someone-else"}), "not_request_owner"),
        (
            _REQUESTER.model_copy(update={"principal_kind": PrincipalKind.AGENT}),
            "reveal_agent_refused",
        ),
    ],
    ids=["another-person", "agent"],
)
async def test_another_person_or_an_agent_is_refused(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caller: Operator,
    error_code: str,
) -> None:
    _switch(monkeypatch, on=True)
    request_id = await approved_reveal(_REQUESTER)
    _Caller.operator = caller

    response = _fetch(client, request_id)

    assert _refusal(response)["extras"]["error_code"] == error_code
    await _value_still_there(request_id)


@pytest.mark.usefixtures("vault_read")
@pytest.mark.parametrize("switch_on", [False, True], ids=["switch-off", "switch-on"])
@pytest.mark.parametrize("reveal", [True, False], ids=["reveal", "no-reveal"])
async def test_an_immediate_reveal_says_no_store(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, reveal: bool, switch_on: bool
) -> None:
    _switch(monkeypatch, on=switch_on)
    params: dict[str, Any] = {"reveal_secret": True} if reveal else {}
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "call_operation",
                "arguments": {"connector_id": CONNECTOR_ID, "op_id": PLAIN_OP, "params": params},
            },
        },
    )

    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"] is False, result
    assert (SECRET_VALUE in response.text) is reveal
    if reveal:
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
    else:
        assert response.headers.get("cache-control") != "no-store"
        assert "pragma" not in response.headers


@pytest.mark.usefixtures("vault_read")
@pytest.mark.parametrize("switch_on", [False, True], ids=["switch-off", "switch-on"])
async def test_a_reveal_call_cannot_run_in_the_background(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, switch_on: bool
) -> None:
    _switch(monkeypatch, on=switch_on)
    async with get_sessionmaker()() as session:
        before = await session.scalar(select(func.count()).select_from(OperationRun))
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "call_operation",
                "arguments": {
                    "connector_id": CONNECTOR_ID,
                    "op_id": PLAIN_OP,
                    "params": {"reveal_secret": True},
                    "async": True,
                },
            },
        },
    )

    body = response.json()
    assert body["error"]["code"] == -32602, body
    assert body["error"]["data"]["reason"] == "reveal_secret_with_async"
    assert "without `async`" in body["error"]["message"]
    assert SECRET_VALUE not in response.text
    async with get_sessionmaker()() as session:
        after = await session.scalar(select(func.count()).select_from(OperationRun))
    assert after == before
