# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A real local HTTP server for the MCP app, and a client that opens sessions.

The in-process ``TestClient`` cannot show a closed TCP connection. These
helpers start the app under uvicorn on ``127.0.0.1`` and a free port, in
the test's own event loop, with the same login override the other MCP
tests use. ``McpClient`` talks to it over real sockets, either with a
new connection for each call or with a pool of kept-open connections.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import random
import socket
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import structlog
import uvicorn

from meho_backplane.auth.operator import TenantRole
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.schemas import PROTOCOL_VERSION
from tests.mcp_test_fixtures import build_operator

#: The idle time after which a client (httpx, for example) drops a kept-open
#: connection by itself. A server that closes first can race with a client
#: that is just sending a new call on that connection.
CLIENT_IDLE_LIMIT = 5.0

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"

#: Every request gets its own JSON-RPC id, also across sessions.
_call_ids = itertools.count(1)


def image_keep_alive(dockerfile: str | None = None) -> float:
    """The keep-alive time of the image's start command, as uvicorn reads it.

    The start command is the last ``CMD`` of *dockerfile* (default: the text
    of ``backend/Dockerfile``). Without the flag this is uvicorn's default.
    """
    lines = (DOCKERFILE.read_text() if dockerfile is None else dockerfile).splitlines()
    command = json.loads([line for line in lines if line.startswith("CMD ")][-1][4:])
    assert command[0] == "uvicorn"
    context = uvicorn.main.make_context("uvicorn", command[1:])
    return float(context.params["timeout_keep_alive"])


@dataclass
class LiveServer:
    """The address of a running local server, and the idle limit of its clients."""

    base_url: str
    client_idle_limit: float


@asynccontextmanager
async def live_server(scale: float = 1.0) -> AsyncIterator[LiveServer]:
    """Run the app under uvicorn on ``127.0.0.1`` until the block ends.

    The server keeps idle connections open for the time the image's start
    command sets, times *scale*. The clients drop an idle connection after
    :data:`CLIENT_IDLE_LIMIT` times *scale*. A small *scale* makes a fast
    test with the same ratio between the two.
    """
    keep_alive = image_keep_alive() * scale
    operator = build_operator(TenantRole.OPERATOR)

    async def _fake_verify() -> Any:
        structlog.contextvars.bind_contextvars(
            operator_sub=operator.sub, tenant_id=str(operator.tenant_id)
        )
        return operator

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    # uvicorn types the keep-alive time as whole seconds but accepts a float.
    config = uvicorn.Config(
        app,
        log_config=None,
        lifespan="on",
        timeout_keep_alive=keep_alive,  # type: ignore[arg-type]
        # A test that fails during a call must not wait for that call for ever.
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if serving.done():
                serving.result()
            await asyncio.sleep(0.01)
        yield LiveServer(
            base_url=f"http://127.0.0.1:{port}", client_idle_limit=CLIENT_IDLE_LIMIT * scale
        )
    finally:
        server.should_exit = True
        await serving
        sock.close()
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


@dataclass
class Tally:
    """What happened to the requests of one group of sessions."""

    outcomes: Counter[str] = field(default_factory=Counter)
    #: The ``X-Request-Id`` of every request that got no answer, and the
    #: session id when the client knows it. They find the server log lines.
    lost_requests: set[str] = field(default_factory=set)
    lost_sessions: set[str] = field(default_factory=set)
    #: The first answer text of each kind of error, to show what it was.
    samples: dict[str, str] = field(default_factory=dict)
    sessions: int = 0

    def lost(self) -> dict[str, int]:
        """The outcomes that are no answer. A JSON-RPC error is an answer too."""
        return {k: v for k, v in self.outcomes.items() if is_lost(k)}

    def note(self, outcome: str, request_id: str, session_id: str | None, text: str) -> None:
        """Count *outcome*; keep the ids of a request that got no answer."""
        self.outcomes[outcome] += 1
        if outcome != "answer" and text:
            self.samples.setdefault(outcome, text[:300])
        if is_lost(outcome):
            self.lost_requests.add(request_id)
            if session_id:
                self.lost_sessions.add(session_id)

    def log_lines(self, logs: list[Any]) -> list[Any]:
        """The server log lines in *logs* of the requests and sessions that got no answer."""
        return [
            entry
            for entry in logs
            if entry.get("request_id") in self.lost_requests
            or entry.get("mcp_session_id") in self.lost_sessions
        ]


def is_lost(outcome: str) -> bool:
    return outcome != "answer" and not outcome.startswith("rpc_error:")


def new_client(server: LiveServer, *, pooled: bool) -> httpx.AsyncClient:
    """A client for *server*. Pooled keeps connections open, as most libraries do."""
    limits = httpx.Limits(
        max_connections=1000,
        max_keepalive_connections=1000 if pooled else 0,
        keepalive_expiry=server.client_idle_limit,
    )
    return httpx.AsyncClient(base_url=server.base_url, limits=limits, timeout=60.0)


def envelope(method: str, params: dict[str, Any] | None, call_id: int | None) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    if call_id is not None:
        body["id"] = call_id
    return body


def tool_call(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    params = {"name": name, "arguments": arguments or {}}
    return envelope("tools/call", params, next(_call_ids))


def _read_answer(response: httpx.Response, call_id: Any) -> str:
    """Name the answer to the request *call_id*: ``answer``, ``rpc_error:<code>`` or ``bad_answer``.

    A reply that is no JSON-RPC 2.0 response, or that carries another id,
    is a ``bad_answer``: the client still waits for its own reply.
    """
    try:
        payload = response.json()
    except ValueError:
        return "bad_answer:no_json"
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        return "bad_answer:no_jsonrpc"
    reply_id = payload.get("id")
    if type(reply_id) is not type(call_id) or reply_id != call_id:
        return "bad_answer:other_id"
    if ("result" in payload) == ("error" in payload):
        return "bad_answer:result_or_error"
    if "result" in payload:
        return "answer" if isinstance(payload["result"], dict) else "bad_answer:result"
    error = payload["error"]
    if (
        not isinstance(error, dict)
        or type(error.get("code")) is not int
        or not isinstance(error.get("message"), str)
    ):
        return "bad_answer:error"
    return f"rpc_error:{error['code']}"


async def post(
    client: httpx.AsyncClient,
    body: dict[str, Any],
    session_id: str | None,
    request_id: str | None = None,
) -> tuple[str, httpx.Response | None]:
    """Send one message. Return ``(outcome, response)``.

    The outcome is ``answer``, ``rpc_error:<code>``, ``http_<status>``,
    ``bad_answer:<why>`` or ``closed:<error>`` (the connection ended and no
    answer came). *request_id* is sent as ``X-Request-Id``; the server puts
    it on its log lines.
    """
    headers = {"MCP-Protocol-Version": PROTOCOL_VERSION}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    if request_id:
        headers["X-Request-Id"] = request_id
    try:
        response = await client.post("/mcp", json=body, headers=headers)
    except httpx.TransportError as exc:
        return f"closed:{type(exc).__name__}", None
    expected = 200 if "id" in body else 202
    if response.status_code != expected:
        return f"http_{response.status_code}", response
    return _read_answer(response, body["id"]) if "id" in body else "answer", response


async def open_session(
    client: httpx.AsyncClient, request_id: str | None = None
) -> tuple[str | None, str]:
    """``initialize`` then ``notifications/initialized``. Return ``(session id, outcome)``."""
    init = envelope(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "parallel-test", "version": "0"},
        },
        next(_call_ids),
    )
    outcome, response = await post(client, init, None, request_id)
    if response is None or outcome != "answer":
        return None, outcome
    session_id = response.headers.get("mcp-session-id")
    if not session_id:
        return None, "no_session_id"
    ready = envelope("notifications/initialized", None, None)
    outcome, _ = await post(client, ready, session_id, request_id)
    return session_id, outcome


async def run_session(
    server: LiveServer,
    shared: httpx.AsyncClient | None,
    calls: list[dict[str, Any]],
    pause: tuple[float, float],
    tally: Tally,
) -> None:
    """One session: open it, wait, send every call in *calls* at the same moment."""
    tally.sessions += 1

    @asynccontextmanager
    async def _client() -> AsyncIterator[httpx.AsyncClient]:
        if shared is not None:
            yield shared
            return
        async with new_client(server, pooled=False) as one_shot:
            yield one_shot

    async def _send(body: dict[str, Any], session_id: str) -> None:
        request_id = uuid.uuid4().hex
        async with _client() as client:
            outcome, response = await post(client, body, session_id, request_id)
        tally.note(outcome, request_id, session_id, response.text if response else "")

    open_id = uuid.uuid4().hex
    async with _client() as client:
        session_id, outcome = await open_session(client, open_id)
    if session_id is None or outcome != "answer":
        tally.note(f"open:{outcome}", open_id, session_id, "")
        return
    await asyncio.sleep(random.uniform(*pause))
    await asyncio.gather(*(_send(call, session_id) for call in calls))
