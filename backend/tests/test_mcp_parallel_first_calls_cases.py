# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Odd cases of calls at the same moment, and the log line for a call with no answer (#6829).

The matrix test (``test_mcp_parallel_first_calls.py``) counts answers on a
real local HTTP server. These tests use the same server for the odd cases:

* one call is refused by policy, the others still get their answers;
* one call waits for approval and answers at once, the others still answer;
* one call returns a result handle that ``result_query`` reads in the same session;
* the client closes its side during one call: the others still answer, and the
  server writes ``mcp_call_ended_without_answer`` once with the reason
  ``caller_gone`` and the session id cut to 8 characters.

A call that the server cancels also writes the line (reason ``cancelled``),
and the cancel still goes up.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from structlog.contextvars import merge_contextvars
from structlog.testing import capture_logs

import meho_backplane.mcp.server as mcp_server
import meho_backplane.mcp.tools.operations as operation_tools
import meho_backplane.operations.jsonflux_reducer as jsonflux_reducer_module
import meho_backplane.operations.result_query as result_query_core
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.db.models import Tenant
from meho_backplane.mcp.schemas import PROTOCOL_VERSION, JsonRpcRequest
from meho_backplane.operations.caller_gone import caller_gone_at
from meho_backplane.settings import get_settings
from tests.mcp_live_server import (
    LiveServer,
    live_server,
    new_client,
    open_session,
    post,
    tool_call,
)
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

_LOG_LINE = "mcp_call_ended_without_answer"


@pytest.fixture(autouse=True)
def _broadcast_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    get_settings.cache_clear()


@pytest.fixture
async def session() -> AsyncIterator[tuple[LiveServer, httpx.AsyncClient, str]]:
    """A live server, a pooled client, and a session that is ready for its first calls."""
    async with live_server(0.06) as server, new_client(server, pooled=True) as client:
        session_id, outcome = await open_session(client)
        assert outcome == "answer"
        assert session_id is not None
        yield server, client, session_id


def _payload(response: httpx.Response) -> dict[str, Any]:
    """The JSON a tool put in its first text block."""
    body = response.json()
    assert "error" not in body, body
    return json.loads(body["result"]["content"][0]["text"])  # type: ignore[no-any-return]


@pytest.mark.parametrize("status", ["denied", "awaiting_approval"])
async def test_refused_or_waiting_call_does_not_hurt_the_others(
    session: tuple[LiveServer, httpx.AsyncClient, str],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    """A call that policy refuses, or that waits for a person, answers at once."""
    _server, client, session_id = session

    async def _fake_call(_operator: Any, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": status, "op_id": "stub.thing.get", "duration_ms": 1.0, "extras": {}}

    monkeypatch.setattr(operation_tools, "call_operation", _fake_call)
    calls = [
        tool_call("call_operation", {"connector_id": "stub-1.x", "op_id": "stub.thing.get"}, 1),
        tool_call("list_targets", {}, 2),
        tool_call("meho_broadcast_recent", {}, 3),
    ]
    results = await asyncio.gather(*(post(client, call, session_id) for call in calls))

    assert [outcome for outcome, _ in results] == ["answer"] * 3
    assert _payload(results[0][1])["status"] == status  # type: ignore[arg-type]
    assert "targets" in _payload(results[1][1])  # type: ignore[arg-type]


class _MemoryValkey:
    """Minimal async Valkey shape the result handle store reads and writes."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        del ex
        self.values[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.values.get(name)


async def test_result_handle_from_a_parallel_call_works_in_the_same_session(
    session: tuple[LiveServer, httpx.AsyncClient, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large list answers with a handle, and ``result_query`` reads it in this session."""
    _server, client, session_id = session
    store = ResultHandleStore(_MemoryValkey())  # type: ignore[arg-type]
    monkeypatch.setattr(jsonflux_reducer_module, "get_result_handle_store", lambda: store)
    monkeypatch.setattr(result_query_core, "get_result_handle_store", lambda: store)
    async with get_sessionmaker()() as db, db.begin():
        db.add(Tenant(id=OPERATOR_TENANT_ID, slug="op-test-tenant", name="Operator Test Tenant"))
        for i in range(89):
            db.add(
                TargetORM(
                    tenant_id=OPERATOR_TENANT_ID,
                    name=f"lab-{i:03d}",
                    aliases=[],
                    product="vault",
                    host=f"lab-{i:03d}.example.test",
                    port=443,
                    secret_ref=f"targets/lab-{i:03d}",
                    auth_model="shared_service_account",
                    fingerprint={"product": "vault", "version": "1.0", "reachable": True},
                )
            )

    calls = [
        tool_call("meho_broadcast_recent", {}, 1),
        tool_call("list_targets", {}, 2),
        tool_call("meho_broadcast_recent", {}, 3),
    ]
    results = await asyncio.gather(*(post(client, call, session_id) for call in calls))

    assert [outcome for outcome, _ in results] == ["answer"] * 3
    handle_id = _payload(results[1][1])["handle"]["handle_id"]  # type: ignore[arg-type]
    outcome, response = await post(
        client,
        tool_call("result_query", {"handle_id": handle_id, "offset": 0, "limit": 100}, 4),
        session_id,
    )
    assert outcome == "answer"
    assert len(_payload(response)["rows"]) == 89  # type: ignore[arg-type]


async def test_client_that_closes_during_one_call_leaves_the_others_answered(
    session: tuple[LiveServer, httpx.AsyncClient, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The slow call ends after its client left: one log line, and the others answer."""
    server, client, session_id = session
    started = asyncio.Event()
    seen_gone = asyncio.Event()

    async def _slow(_operator: Any, _params: dict[str, Any] | None) -> dict[str, Any]:
        started.set()
        for _ in range(500):
            if caller_gone_at() is not None:
                break
            await asyncio.sleep(0.01)
        seen_gone.set()
        return {"ok": True}

    monkeypatch.setitem(mcp_server._DISPATCH, "test/slow", _slow)
    host_port = server.base_url.removeprefix("http://")
    host, port = host_port.split(":")
    body = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "test/slow", "params": {}}).encode()
    request = (
        f"POST /mcp HTTP/1.1\r\nHost: {host_port}\r\nContent-Type: application/json\r\n"
        f"Mcp-Session-Id: {session_id}\r\nMCP-Protocol-Version: {PROTOCOL_VERSION}\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode() + body

    with capture_logs(processors=[merge_contextvars]) as logs:
        _reader, writer = await asyncio.open_connection(host, int(port))
        writer.write(request)
        await writer.drain()
        await asyncio.wait_for(started.wait(), 10)
        others = await asyncio.gather(
            *(
                post(client, tool_call(tool, {}, i), session_id)
                for i, tool in enumerate(("list_targets", "meho_broadcast_recent"), start=1)
            )
        )
        writer.close()
        await asyncio.wait_for(seen_gone.wait(), 10)
        await asyncio.sleep(0.2)

    assert [outcome for outcome, _ in others] == ["answer", "answer"]
    lines = [entry for entry in logs if entry["event"] == _LOG_LINE]
    assert len(lines) == 1
    assert lines[0]["reason"] == "caller_gone"
    assert lines[0]["session"] == session_id[:8]
    assert lines[0]["method"] == "test/slow"


async def test_cancelled_call_writes_the_log_line_and_the_cancel_goes_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool call that the server cancels writes one line, then the cancel goes up."""
    inside = asyncio.Event()

    async def _hangs(*_args: Any, **_kwargs: Any) -> Any:
        inside.set()
        await asyncio.Event().wait()

    async def _never_receive() -> dict[str, Any]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover

    monkeypatch.setattr(mcp_server, "_dispatch_no_store", _hangs)
    request = SimpleNamespace(
        headers={"mcp-session-id": "0123456789abcdef"}, receive=_never_receive
    )
    jrpc = JsonRpcRequest(
        jsonrpc="2.0",
        id=1,
        method="tools/call",
        params={"name": "list_targets", "arguments": {}},
    )

    with capture_logs() as logs:
        task = asyncio.create_task(
            mcp_server._dispatch_watching_caller(request, jrpc, False, SimpleNamespace())  # type: ignore[arg-type]
        )
        await asyncio.wait_for(inside.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    lines = [entry for entry in logs if entry["event"] == _LOG_LINE]
    assert len(lines) == 1
    assert lines[0]["reason"] == "cancelled"
    assert lines[0]["tool"] == "list_targets"
    assert lines[0]["method"] == "tools/call"
    assert lines[0]["session"] == "01234567"
