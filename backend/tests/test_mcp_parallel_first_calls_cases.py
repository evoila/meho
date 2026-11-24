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
  ``caller_gone`` and the session id cut to 8 characters. A call that is still
  running at that moment finishes with its own answer.

A call that the server cancels also writes the line (reason ``cancelled``),
and the cancel still goes up. The same holds after the handler returned:
while ``initialize`` waits for its audit row, and while the answer is sent.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from structlog.contextvars import merge_contextvars
from structlog.testing import capture_logs

import meho_backplane.mcp.server as mcp_server
import meho_backplane.mcp.tools.operations as operation_tools
import meho_backplane.operations.jsonflux_reducer as jsonflux_reducer_module
import meho_backplane.operations.result_query as result_query_core
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.db.models import Tenant
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.schemas import PROTOCOL_VERSION
from meho_backplane.operations.caller_gone import caller_gone_at
from meho_backplane.settings import get_settings
from tests.mcp_live_server import (
    LiveServer,
    envelope,
    live_server,
    new_client,
    open_session,
    post,
    tool_call,
)
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    build_operator,
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

_LOG_LINE = "mcp_call_ended_without_answer"


@pytest.fixture(autouse=True)
def _broadcast_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # A local port where nothing listens.
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://127.0.0.1:1")
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
        tool_call("call_operation", {"connector_id": "stub-1.x", "op_id": "stub.thing.get"}),
        tool_call("list_targets"),
        tool_call("meho_broadcast_recent"),
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
                    name=f"target-{i:03d}",
                    aliases=[],
                    product="vault",
                    host="127.0.0.1",
                    port=443,
                    secret_ref=f"targets/target-{i:03d}",
                    auth_model="shared_service_account",
                    fingerprint={"product": "vault", "version": "1.0", "reachable": True},
                )
            )

    calls = [
        tool_call("meho_broadcast_recent"),
        tool_call("list_targets"),
        tool_call("meho_broadcast_recent"),
    ]
    results = await asyncio.gather(*(post(client, call, session_id) for call in calls))

    assert [outcome for outcome, _ in results] == ["answer"] * 3
    handle_id = _payload(results[1][1])["handle"]["handle_id"]  # type: ignore[arg-type]
    outcome, response = await post(
        client,
        tool_call("result_query", {"handle_id": handle_id, "offset": 0, "limit": 100}),
        session_id,
    )
    assert outcome == "answer"
    assert len(_payload(response)["rows"]) == 89  # type: ignore[arg-type]


def _raw_request(server: LiveServer, body: dict[str, Any], session_id: str | None) -> bytes:
    """The bytes of one ``POST /mcp``, for a connection the test closes by itself."""
    content = json.dumps(body).encode()
    session = f"Mcp-Session-Id: {session_id}\r\n" if session_id else ""
    return (
        f"POST /mcp HTTP/1.1\r\nHost: {server.base_url.removeprefix('http://')}\r\n"
        f"Content-Type: application/json\r\n{session}"
        f"MCP-Protocol-Version: {PROTOCOL_VERSION}\r\n"
        f"Content-Length: {len(content)}\r\n\r\n"
    ).encode() + content


async def _send_raw(
    server: LiveServer, body: dict[str, Any], session_id: str | None
) -> asyncio.StreamWriter:
    port = int(server.base_url.rsplit(":", 1)[1])
    _reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(_raw_request(server, body, session_id))
    await writer.drain()
    return writer


async def _wait_until(done: Callable[[], Any]) -> None:
    async with asyncio.timeout(10):
        while not done():
            await asyncio.sleep(0.01)


def _lines(logs: list[Any]) -> list[Any]:
    return [entry for entry in logs if entry["event"] == _LOG_LINE]


async def test_client_that_closes_during_one_call_leaves_the_others_answered(
    session: tuple[LiveServer, httpx.AsyncClient, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One log line for the call whose client left. A call still running then answers."""
    server, client, session_id = session
    started = asyncio.Event()
    held_started = asyncio.Event()
    release = asyncio.Event()

    async def _slow(_operator: Any, _params: dict[str, Any] | None) -> dict[str, Any]:
        started.set()
        await _wait_until(caller_gone_at)
        return {"ok": True}

    async def _held(_operator: Any, _params: dict[str, Any] | None) -> dict[str, Any]:
        held_started.set()
        await release.wait()
        return {"held": True}

    monkeypatch.setitem(mcp_server._DISPATCH, "test/slow", _slow)
    monkeypatch.setitem(mcp_server._DISPATCH, "test/held", _held)

    with capture_logs(processors=[merge_contextvars]) as logs:
        writer = await _send_raw(server, envelope("test/slow", {}, 900001), session_id)
        held = asyncio.create_task(post(client, envelope("test/held", {}, 900002), session_id))
        other = asyncio.create_task(post(client, tool_call("list_targets"), session_id))
        await asyncio.wait_for(asyncio.gather(started.wait(), held_started.wait()), 10)
        writer.close()
        await _wait_until(lambda: _lines(logs))
        await asyncio.sleep(0.1)
        assert not held.done()
        release.set()
        held_outcome, held_response = await asyncio.wait_for(held, 10)
        other_outcome, _ = await asyncio.wait_for(other, 10)

    assert (held_outcome, other_outcome) == ("answer", "answer")
    assert held_response is not None
    assert held_response.json()["result"] == {"held": True}
    [line] = _lines(logs)
    assert line["reason"] == "caller_gone"
    assert line["method"] == "test/slow"
    assert line["session"] == session_id[:8]
    # The log context holds the full id. It must not reach this line.
    assert session_id not in json.dumps(line, default=str)
    assert any(e.get("mcp_session_id") == session_id for e in logs)


@pytest.fixture
def session_open_row(monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Event, asyncio.Event]:
    """Audit of new sessions is on, and its write waits. Events: it started, let it end."""
    started, release = asyncio.Event(), asyncio.Event()

    async def _write(**_kwargs: Any) -> None:
        started.set()
        await release.wait()

    monkeypatch.setenv("AUDIT_CLIENT_IDENTITY", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(mcp_server, "write_mcp_session_open_row", _write)
    return started, release


_INITIALIZE = envelope(
    "initialize",
    {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "parallel-test", "version": "0"},
    },
    900003,
)


async def test_client_that_closes_after_the_handler_returned_leaves_the_log_line(
    session_open_row: tuple[asyncio.Event, asyncio.Event],
) -> None:
    """The client leaves while ``initialize`` waits for its audit row."""
    started, release = session_open_row
    # The app sets up its logging at start, so the capture starts after that.
    async with live_server(0.06) as server:
        with capture_logs(processors=[merge_contextvars]) as logs:
            writer = await _send_raw(server, _INITIALIZE, None)
            await asyncio.wait_for(started.wait(), 10)
            writer.close()
            await _wait_until(
                lambda: [e for e in logs if e["event"] == "caller_gone_during_request"]
            )
            assert not _lines(logs)
            release.set()
            await _wait_until(lambda: _lines(logs))

    [line] = _lines(logs)
    assert line["reason"] == "caller_gone"
    assert line["method"] == "initialize"
    # The session id the server had just given, cut to 8 characters.
    assert len(line["session"]) == len(line["mcp_session_id"]) == 8


async def _post_to_app(
    body: dict[str, Any],
    send: Callable[[Any], Awaitable[None]],
    receive_after_body: Callable[[], Awaitable[dict[str, Any]]],
    session_id: str | None,
) -> None:
    """Run one ``POST /mcp`` through the whole app, with the given ASGI channels."""
    content = json.dumps(body).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"mcp-protocol-version", PROTOCOL_VERSION.encode()),
    ]
    if session_id:
        headers.append((b"mcp-session-id", session_id.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 80),
    }
    body_sent = False

    async def _receive() -> dict[str, Any]:
        nonlocal body_sent
        if body_sent:
            return await receive_after_body()
        body_sent = True
        return {"type": "http.request", "body": content, "more_body": False}

    await app(scope, _receive, send)


@pytest.fixture
def _login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        app.dependency_overrides,
        verify_mcp_jwt_and_bind,
        lambda: build_operator(TenantRole.OPERATOR),
    )


async def _wait_for_ever(*_args: Any, **_kwargs: Any) -> Any:
    await asyncio.Event().wait()


@pytest.mark.usefixtures("_login")
@pytest.mark.parametrize("moment", ["handler", "audit row", "send"])
async def test_cancelled_call_writes_the_log_line_and_the_cancel_goes_up(
    monkeypatch: pytest.MonkeyPatch,
    session_open_row: tuple[asyncio.Event, asyncio.Event],
    moment: str,
) -> None:
    """A cancel in the handler, after it, or during the send: one line, and it goes up."""
    started, _release = session_open_row
    body = tool_call("list_targets")
    session_id: str | None = str(uuid.uuid4())

    async def _hangs(*_args: Any) -> dict[str, Any]:
        started.set()
        return await _wait_for_ever()  # type: ignore[no-any-return]

    async def _answers(*_args: Any) -> dict[str, Any]:
        return {}

    async def _send(message: Any) -> None:
        if moment == "send" and message["type"] == "http.response.body":
            await _hangs()

    if moment == "audit row":
        body, session_id = _INITIALIZE, None
    else:
        handler = _hangs if moment == "handler" else _answers
        monkeypatch.setitem(mcp_server._DISPATCH, "tools/call", handler)

    with capture_logs(processors=[merge_contextvars]) as logs:
        task = asyncio.create_task(_post_to_app(body, _send, _wait_for_ever, session_id))
        await asyncio.wait_for(started.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    [line] = _lines(logs)
    assert line["reason"] == "cancelled"
    assert line["method"] == body["method"]
    assert line["tool"] == (None if moment == "audit row" else "list_targets")
    if session_id:
        assert line["session"] == session_id[:8]
        assert session_id not in json.dumps(line, default=str)
    else:
        assert len(line["session"]) == 8


@pytest.mark.usefixtures("_login")
async def test_client_that_leaves_during_the_send_leaves_the_log_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server drops the answer when the client left before it was sent."""
    sending, gone = asyncio.Event(), asyncio.Event()

    async def _answers(*_args: Any) -> dict[str, Any]:
        return {}

    async def _send(message: Any) -> None:
        if message["type"] == "http.response.body":
            sending.set()
            await gone.wait()

    async def _disconnect() -> dict[str, Any]:
        await sending.wait()
        asyncio.get_running_loop().call_soon(gone.set)
        return {"type": "http.disconnect"}

    monkeypatch.setitem(mcp_server._DISPATCH, "tools/call", _answers)
    with capture_logs(processors=[merge_contextvars]) as logs:
        await asyncio.wait_for(
            _post_to_app(tool_call("list_targets"), _send, _disconnect, None), 10
        )

    [line] = _lines(logs)
    assert line["reason"] == "caller_gone"
    assert line["tool"] == "list_targets"
    assert line["session"] is None


@pytest.mark.usefixtures("_login")
async def test_client_gone_when_the_handler_returns_leaves_the_log_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server notes the disconnect as the handler returns, before the watcher ran."""
    gone = asyncio.Event()
    written: list[Any] = []

    async def _answers(*_args: Any) -> dict[str, Any]:
        # Like the server: the connection is marked closed in the same turn.
        gone.set()
        return {}

    async def _send(message: Any) -> None:
        # Like the server: nothing is written once the client has gone.
        if not gone.is_set():
            written.append(message)

    async def _disconnect() -> dict[str, Any]:
        await gone.wait()
        return {"type": "http.disconnect"}

    monkeypatch.setitem(mcp_server._DISPATCH, "tools/call", _answers)
    with capture_logs(processors=[merge_contextvars]) as logs:
        await asyncio.wait_for(
            _post_to_app(tool_call("list_targets"), _send, _disconnect, None), 10
        )

    assert written == []
    [line] = _lines(logs)
    assert line["reason"] == "caller_gone"
    assert line["tool"] == "list_targets"


@pytest.mark.usefixtures("_login")
async def test_cancel_while_a_refusal_is_sent_writes_the_log_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal for a missing session id is an answer too; a cancel during its send is logged."""
    monkeypatch.setenv("MCP_REQUIRE_SESSION_ID", "true")
    get_settings.cache_clear()
    sending = asyncio.Event()

    async def _send(message: Any) -> None:
        if message["type"] == "http.response.body":
            sending.set()
            await _wait_for_ever()

    with capture_logs(processors=[merge_contextvars]) as logs:
        task = asyncio.create_task(
            _post_to_app(tool_call("list_targets"), _send, _wait_for_ever, None)
        )
        await asyncio.wait_for(sending.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    get_settings.cache_clear()

    [line] = _lines(logs)
    assert line["reason"] == "cancelled"
    assert line["tool"] == "list_targets"
    assert line["session"] is None
