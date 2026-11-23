"""A stop during the close of an answer leaves no connection open (loopback).

The shared HTTP client reads each answer in a stream. Leaving the stream
closes the answer. A request can hit its time limit and start to close, and a
stop from outside can then cut that close. These tests run many rounds of this
against a local stand-in server and then check three things:

* the server holds no connection open,
* the client's pool has no connection checked out,
* a normal request on the same client gets an answer at once.

A second test does the same for a 3xx answer that the redirect client closes
before the next hop. In every round the caller must still get the stop.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx
import pytest

from meho_backplane.connectors.adapters.http import _read_capped_json_response
from meho_backplane.targets.ssrf_guard import TARGET_SSRF_ALLOWLIST_ENV
from tests.test_connectors_http_adapter import _ConcreteHttpConnector, _make_target

_ROUNDS = 1000


class _StallServer:
    """Asyncio stand-in server that counts the connections it still holds.

    ``/slow`` sends a head and part of a body, then waits. ``/redirect`` answers
    a same-origin 302 with a body that is never finished. ``/ok`` answers at
    once. A connection counts as open until the client closes it.
    """

    def __init__(self) -> None:
        self.open_connections = 0
        self.accepted = 0
        self.rounds_seen: set[int] = set()
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def stop(self) -> None:
        """Shut the listener down and drop every client that is still connected."""
        listener, self._server = self._server, None
        if listener is None:
            return
        listener.close_clients()
        listener.close()
        await listener.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.accepted += 1
        self.open_connections += 1
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                path = head.split(b" ", 2)[1]
                if b"round=" in path:
                    self.rounds_seen.add(int(path.split(b"round=")[1]))
                path = path.split(b"?")[0]
                if path == b"/ok":
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Content-Length: 11\r\n\r\n" + b'{"ok":true}'
                    )
                    await writer.drain()
                    continue
                if path == b"/redirect":
                    writer.write(
                        b"HTTP/1.1 302 Found\r\nLocation: /slow\r\nContent-Length: 100000\r\n\r\n"
                        + b"x" * 1000
                    )
                else:
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n" + b"x" * 1000)
                await writer.drain()
                # Wait until the client closes the connection.
                while await reader.read(4096):
                    pass
                return
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            return
        finally:
            self.open_connections -= 1
            writer.close()


def _pool_checked_out(client: httpx.AsyncClient) -> int:
    """Count pooled connections that are open and not idle (test-only peek)."""
    pool: Any = client._transport._pool  # type: ignore[attr-defined]
    return sum(1 for c in pool.connections if not c.is_idle() and not c.is_closed()) + len(
        pool._requests
    )


class _PlainHttpConnector(_ConcreteHttpConnector):
    """The real client build, pointed at the plain-http stand-in server."""

    def _base_url(self, target: Any) -> str:
        return f"http://{target.host}:{target.port}"


def _pool_idle(client: httpx.AsyncClient) -> int:
    pool: Any = client._transport._pool  # type: ignore[attr-defined]
    return sum(1 for c in pool.connections if c.is_idle() and not c.is_closed())


async def _ok(client: httpx.AsyncClient) -> None:
    resp = await _read_capped_json_response(client, "GET", "/ok", timeout=httpx.Timeout(5.0))
    assert resp.status_code == 200


async def _run_rounds(path: str, *, read_timeout: float | None, spread: float) -> None:
    server = _StallServer()
    await server.start()
    conn = _PlainHttpConnector()
    client = await conn._http_client(_make_target(host="127.0.0.1", port=server.port))
    rng = random.Random(6821)
    stops_seen = 0
    time_limit_first = 0
    try:
        for round_no in range(_ROUNDS):
            # Two idle connections, so the round dials no new connection: a
            # stop during a dial is not the case under test.
            await asyncio.gather(*(_ok(client) for _ in range(2)))
            kwargs: dict[str, Any] = {}
            if read_timeout is not None:
                kwargs["timeout"] = httpx.Timeout(
                    connect=5.0, read=read_timeout, write=5.0, pool=5.0
                )
            task = asyncio.ensure_future(
                _read_capped_json_response(client, "GET", f"{path}?round={round_no}", **kwargs)
            )
            # Stop only after the server has read the request: the connection is
            # then set up, and the stop lands while the answer is read or closed.
            async with asyncio.timeout(10):
                while round_no not in server.rounds_seen:
                    await asyncio.sleep(0)
            await asyncio.sleep(rng.random() * spread)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                stops_seen += 1
            except httpx.TimeoutException:
                # The time limit ended the call before the stop came, so there
                # was nothing left to stop.
                time_limit_first += 1
        # Let the server see the closes. Idle keep-alive connections stay open on
        # both sides on purpose; any other open connection is a leak.
        idle = _pool_idle(client)
        for _ in range(200):
            if server.open_connections <= idle:
                break
            await asyncio.sleep(0.01)
        assert server.open_connections == idle
        assert _pool_checked_out(client) == 0
        started = time.monotonic()
        resp = await _read_capped_json_response(client, "GET", "/ok", timeout=httpx.Timeout(1.0))
        assert resp.status_code == 200
        assert time.monotonic() - started < 1.0
        # Every round ended in the stop, or in the time limit that came first.
        assert stops_seen + time_limit_first == _ROUNDS
        assert stops_seen > _ROUNDS // 2
        if read_timeout is None:
            assert stops_seen == _ROUNDS
    finally:
        await conn.aclose()
        await server.stop()


@pytest.mark.asyncio
async def test_stop_during_close_after_time_limit_leaves_nothing_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TARGET_SSRF_ALLOWLIST_ENV, "127.0.0.0/8")
    await _run_rounds("/slow", read_timeout=0.002, spread=0.003)


@pytest.mark.asyncio
async def test_stop_during_close_of_redirect_answer_leaves_nothing_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TARGET_SSRF_ALLOWLIST_ENV, "127.0.0.0/8")
    await _run_rounds("/redirect", read_timeout=None, spread=0.004)
