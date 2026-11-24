"""A stop during the close of an answer leaves no connection open (loopback).

The shared HTTP client closes an answer when a request hits its time limit,
and on a 3xx answer before the next hop. A stop from outside can come during
that close. These tests run many rounds of this against a local stand-in
server. A hook in the network layer signals when the close starts and holds
it until the stop is sent, so the stop lands in the close in every round.
The time between the start of the close and the stop changes from round to
round. Then the tests check:

* the caller got the stop in every round,
* the server holds no connection open,
* the client's pool has no active connection and no waiting request,
* a normal request on the same client gets an answer at once.

The cases: a streamed read after a time limit, a direct ``client.request``
after a time limit, the close of a 3xx answer before the next hop, and a
direct ``client.stream`` whose caller is stopped while it closes the answer.
A direct ``client.stream`` whose caller reads the body is stopped while
the read closes the answer after its time limit. One more case stops a
direct ``client.stream`` right after the answer arrives, before the caller
gets it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpcore
import httpx
import pytest

from meho_backplane.connectors.adapters.http import _read_capped_json_response
from meho_backplane.targets.ssrf_guard import TARGET_SSRF_ALLOWLIST_ENV
from tests._time_limits import check_time_limit
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


class _CloseHook:
    """Signal when an armed close starts, and hold it until the stop is sent."""

    def __init__(self) -> None:
        self.armed = False
        self.fired = 0
        self.started = asyncio.Event()
        self.stop_sent = asyncio.Event()

    def arm(self) -> None:
        self.armed = True
        self.started.clear()
        self.stop_sent.clear()

    async def on_close(self) -> None:
        if not self.armed:
            return
        self.armed = False
        self.fired += 1
        self.started.set()
        async with asyncio.timeout(5):
            await self.stop_sent.wait()


class _HoldingStream(httpcore.AsyncNetworkStream):
    def __init__(self, inner: httpcore.AsyncNetworkStream, hook: _CloseHook) -> None:
        self._inner = inner
        self._hook = hook

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await self._inner.read(max_bytes, timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        await self._inner.write(buffer, timeout)

    async def aclose(self) -> None:
        await self._hook.on_close()
        await self._inner.aclose()

    async def start_tls(self, *args: Any, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        return _HoldingStream(await self._inner.start_tls(*args, **kwargs), self._hook)

    def get_extra_info(self, info: str) -> Any:
        return self._inner.get_extra_info(info)


class _HoldingBackend(httpcore.AsyncNetworkBackend):
    """Wrap the client's own network layer, so every connection close runs the hook."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend, hook: _CloseHook) -> None:
        self._inner = inner
        self._hook = hook

    async def connect_tcp(self, *args: Any, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        return _HoldingStream(await self._inner.connect_tcp(*args, **kwargs), self._hook)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _pool(client: httpx.AsyncClient) -> Any:
    return client._transport._pool  # type: ignore[attr-defined]


def _pool_active(client: httpx.AsyncClient) -> int:
    """Count pooled connections that are open and not idle (test-only peek)."""
    return sum(1 for c in _pool(client).connections if not c.is_idle() and not c.is_closed())


def _pool_waiting(client: httpx.AsyncClient) -> int:
    """Count requests the pool still holds (test-only peek)."""
    return len(_pool(client)._requests)


def _pool_idle(client: httpx.AsyncClient) -> int:
    return sum(1 for c in _pool(client).connections if c.is_idle() and not c.is_closed())


class _PlainHttpConnector(_ConcreteHttpConnector):
    """The real client build, pointed at the plain-http stand-in server."""

    def _base_url(self, target: Any) -> str:
        return f"http://{target.host}:{target.port}"


async def _ok(client: httpx.AsyncClient) -> None:
    resp = await _read_capped_json_response(client, "GET", "/ok", timeout=httpx.Timeout(5.0))
    assert resp.status_code == 200


_Call = Callable[[httpx.AsyncClient, str, Any], Awaitable[httpx.Response]]


async def _streamed(client: httpx.AsyncClient, url: str, timeout: Any) -> httpx.Response:
    return await _read_capped_json_response(client, "GET", url, timeout=timeout)


async def _direct(client: httpx.AsyncClient, url: str, timeout: Any) -> httpx.Response:
    return await client.request("GET", url, timeout=timeout)


async def _direct_stream(client: httpx.AsyncClient, url: str, timeout: Any) -> httpx.Response:
    # Only the status is read, like the readiness check of a connector; the
    # close at the end of the block runs in the caller.
    async with client.stream("GET", url, timeout=timeout) as resp:
        return resp


async def _direct_stream_read(client: httpx.AsyncClient, url: str, timeout: Any) -> httpx.Response:
    # The caller reads the body. The read hits the time limit, and httpcore
    # starts the close inside the body read, in the caller's task.
    async with client.stream("GET", url, timeout=timeout) as resp:
        async for _ in resp.aiter_bytes():
            pass
        return resp


async def _wait_before_stop(round_no: int) -> None:
    """Wait a different time in each round, while the close is held open."""
    step = round_no % 4
    if step == 1:
        await asyncio.sleep(0)
    elif step == 2:
        for _ in range(round_no % 7 + 2):
            await asyncio.sleep(0)
    elif step == 3:
        await asyncio.sleep((round_no % 5 + 1) / 10_000)


async def _assert_nothing_left_open(client: httpx.AsyncClient, server: _StallServer) -> None:
    # Let the server see the closes. Idle keep-alive connections stay open on
    # both sides on purpose; any other open connection was never closed.
    idle = _pool_idle(client)
    for _ in range(200):
        if server.open_connections <= idle:
            break
        await asyncio.sleep(0.01)
    assert server.open_connections == idle
    assert _pool_active(client) == 0
    assert _pool_waiting(client) == 0
    started = time.monotonic()
    resp = await _read_capped_json_response(client, "GET", "/ok", timeout=httpx.Timeout(1.0))
    assert resp.status_code == 200
    elapsed = time.monotonic() - started
    check_time_limit(elapsed, 1.0, f"the request after the rounds took {elapsed:.2f} s")


async def _run_rounds(call: _Call, path: str, *, read_timeout: float | None) -> None:
    server = _StallServer()
    await server.start()
    conn = _PlainHttpConnector()
    client = await conn._http_client(_make_target(host="127.0.0.1", port=server.port))
    hook = _CloseHook()
    _pool(client)._network_backend = _HoldingBackend(_pool(client)._network_backend, hook)
    timeout: Any = httpx.USE_CLIENT_DEFAULT
    if read_timeout is not None:
        timeout = httpx.Timeout(connect=5.0, read=read_timeout, write=5.0, pool=5.0)
    stops_seen = 0
    try:
        for round_no in range(_ROUNDS):
            # Two idle connections, so the round dials no new connection: a
            # stop during a dial is not the case under test.
            await asyncio.gather(*(_ok(client) for _ in range(2)))
            hook.arm()
            task = asyncio.ensure_future(call(client, f"{path}?round={round_no}", timeout))
            async with asyncio.timeout(10):
                await hook.started.wait()
            # The close has started and waits for this stop.
            await _wait_before_stop(round_no)
            assert not task.done()
            task.cancel()
            hook.stop_sent.set()
            try:
                await task
            except asyncio.CancelledError:
                stops_seen += 1
        assert hook.fired == _ROUNDS
        assert stops_seen == _ROUNDS
        await _assert_nothing_left_open(client, server)
    finally:
        await conn.aclose()
        await server.stop()


@pytest.mark.slow_runner_tolerant
@pytest.mark.asyncio
async def test_stop_during_close_after_time_limit_leaves_nothing_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TARGET_SSRF_ALLOWLIST_ENV, "127.0.0.0/8")
    await _run_rounds(_streamed, "/slow", read_timeout=0.002)


@pytest.mark.slow_runner_tolerant
@pytest.mark.asyncio
async def test_stop_during_close_of_direct_request_leaves_nothing_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TARGET_SSRF_ALLOWLIST_ENV, "127.0.0.0/8")
    await _run_rounds(_direct, "/slow", read_timeout=0.002)


@pytest.mark.slow_runner_tolerant
@pytest.mark.asyncio
async def test_stop_during_close_of_redirect_answer_leaves_nothing_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TARGET_SSRF_ALLOWLIST_ENV, "127.0.0.0/8")
    await _run_rounds(_streamed, "/redirect", read_timeout=None)


@pytest.mark.slow_runner_tolerant
@pytest.mark.asyncio
async def test_stop_during_close_of_direct_stream_leaves_nothing_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TARGET_SSRF_ALLOWLIST_ENV, "127.0.0.0/8")
    await _run_rounds(_direct_stream, "/slow", read_timeout=None)


@pytest.mark.slow_runner_tolerant
@pytest.mark.asyncio
async def test_stop_during_close_in_direct_stream_body_read_leaves_nothing_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TARGET_SSRF_ALLOWLIST_ENV, "127.0.0.0/8")
    await _run_rounds(_direct_stream_read, "/slow", read_timeout=0.002)


@pytest.mark.slow_runner_tolerant
@pytest.mark.asyncio
async def test_stop_right_after_direct_stream_answer_leaves_nothing_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The answer is ready, but the stop reaches the caller before the answer does."""
    monkeypatch.setenv(TARGET_SSRF_ALLOWLIST_ENV, "127.0.0.0/8")
    server = _StallServer()
    await server.start()
    conn = _PlainHttpConnector()
    client = await conn._http_client(_make_target(host="127.0.0.1", port=server.port))
    callers: list[asyncio.Future[Any]] = []

    async def _stop_caller(response: httpx.Response) -> None:
        # Runs in the read, right after the head of the answer arrived. The
        # read returns the open answer without another wait, so the stop
        # reaches the caller after the read ended.
        if response.request.url.path == "/slow":
            callers[-1].cancel()

    client.event_hooks["response"].append(_stop_caller)
    stops_seen = 0
    try:
        for round_no in range(_ROUNDS):
            await asyncio.gather(*(_ok(client) for _ in range(2)))
            callers.append(
                asyncio.ensure_future(
                    _direct_stream(client, f"/slow?round={round_no}", httpx.USE_CLIENT_DEFAULT)
                )
            )
            try:
                async with asyncio.timeout(10):
                    await callers[-1]
            except asyncio.CancelledError:
                stops_seen += 1
        assert stops_seen == _ROUNDS
        client.event_hooks["response"].remove(_stop_caller)
        await _assert_nothing_left_open(client, server)
    finally:
        await conn.aclose()
        await server.stop()
