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
import random
import re
import socket
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

#: uvicorn closes an idle kept-open connection after this many seconds
#: when the start command sets no other time.
UVICORN_DEFAULT_KEEP_ALIVE = 5.0

#: The idle time after which a client (httpx, for example) drops a kept-open
#: connection by itself. A server that closes first can race with a client
#: that is just sending a new call on that connection.
CLIENT_IDLE_LIMIT = 5.0

_DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"


def image_keep_alive() -> float:
    """The keep-alive time of the start command in the image (``backend/Dockerfile``)."""
    match = re.search(r"--timeout-keep-alive[ =]+(\d+)", _DOCKERFILE.read_text())
    return float(match.group(1)) if match else UVICORN_DEFAULT_KEEP_ALIVE


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
    closed_sessions: set[str] = field(default_factory=set)
    #: The first answer text of each kind of error, to show what it was.
    samples: dict[str, str] = field(default_factory=dict)
    sessions: int = 0


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


def tool_call(
    name: str, arguments: dict[str, Any] | None = None, call_id: int = 1
) -> dict[str, Any]:
    return envelope("tools/call", {"name": name, "arguments": arguments or {}}, call_id)


async def post(
    client: httpx.AsyncClient, body: dict[str, Any], session_id: str | None
) -> tuple[str, httpx.Response | None]:
    """Send one message. Return ``(outcome, response)``.

    The outcome is ``answer``, ``rpc_error:<code>``, ``http_<status>`` or
    ``closed`` (the connection ended and no answer came).
    """
    headers = {"MCP-Protocol-Version": PROTOCOL_VERSION}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    try:
        response = await client.post("/mcp", json=body, headers=headers)
    except httpx.TransportError as exc:
        return f"closed:{type(exc).__name__}", None
    if "id" not in body:
        return (
            "answer" if response.status_code == 202 else f"http_{response.status_code}"
        ), response
    if response.status_code != 200:
        return f"http_{response.status_code}", response
    payload = response.json()
    if "error" in payload:
        return f"rpc_error:{payload['error']['code']}", response
    return "answer", response


async def open_session(client: httpx.AsyncClient) -> tuple[str | None, str]:
    """``initialize`` then ``notifications/initialized``. Return ``(session id, outcome)``."""
    init = envelope(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "parallel-test", "version": "0"},
        },
        0,
    )
    outcome, response = await post(client, init, None)
    if response is None or outcome != "answer":
        return None, outcome
    session_id = response.headers.get("mcp-session-id")
    if not session_id:
        return None, "no_session_id"
    outcome, _ = await post(client, envelope("notifications/initialized", None, None), session_id)
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
    pooled = shared is not None

    async def _send(body: dict[str, Any], session_id: str | None) -> tuple[str, Any]:
        if pooled:
            return await post(shared, body, session_id)  # type: ignore[arg-type]
        async with new_client(server, pooled=False) as one_shot:
            return await post(one_shot, body, session_id)

    async def _open() -> tuple[str | None, str]:
        if pooled:
            return await open_session(shared)  # type: ignore[arg-type]
        async with new_client(server, pooled=False) as one_shot:
            return await open_session(one_shot)

    session_id, outcome = await _open()
    if session_id is None or outcome != "answer":
        tally.outcomes[f"open:{outcome}"] += 1
        tally.closed_sessions.add(session_id or "no-session")
        return
    await asyncio.sleep(random.uniform(*pause))
    results = await asyncio.gather(*(_send(call, session_id) for call in calls))
    for result_outcome, response in results:
        tally.outcomes[result_outcome] += 1
        if result_outcome != "answer" and response is not None:
            tally.samples.setdefault(result_outcome, response.text[:300])
        if result_outcome.startswith("closed"):
            tally.closed_sessions.add(session_id)
