# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the address-pinning httpx transports (F13, meho-internal#275).

The pinning transport closes the SSRF check/use gap: a destination guard
that screens a hostname and then hands the *name* to httpx leaves httpx
free to re-resolve at connect time, so a resolver that changes its answer
between the screen and the connect can steer the socket at a blocked
address. The transport removes that gap by screening inside
``connect_tcp`` and dialing only a validated address from that same
resolution.

Coverage matrix:

* The pinning backend dials the resolver-returned address literal, not
  the hostname it was handed (async + sync).
* A resolver returning ``None`` (passthrough — allowlisted literal /
  fail-open name) dials the original host unchanged.
* A resolver that raises (a blocked destination) never opens a socket.
* Multiple screened addresses are tried in order (IPv4/IPv6 fallback):
  the first reachable one wins; all-unreachable surfaces the last error.
* ``connect_unix_socket`` and ``sleep`` delegate untouched.
* The transport builders swap only the pool's network backend and carry
  the requested TLS ``verify`` context.
* A client handed the explicit pinned transport dials the target
  directly — an ambient ``HTTPS_PROXY`` does not interpose (pinning
  governs the real dial).
* Following a redirect to a new origin re-screens that origin at the
  socket, so a blocked redirect target is refused at connect.
"""

from __future__ import annotations

import ssl
import threading
from collections.abc import Iterable, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpcore
import httpx
import pytest

from meho_backplane.connectors._shared.pinned_transport import (
    _PinnedAsyncBackend,
    _PinnedSyncBackend,
    build_pinned_async_transport,
    build_pinned_sync_transport,
)


class _RecordingSyncStream(httpcore.NetworkStream):
    """A no-op sync stream stand-in returned by the fake backend."""

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return b""

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        return None

    def close(self) -> None:
        return None

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        return self

    def get_extra_info(self, info: str) -> object:
        return None


class _FakeSyncBackend(httpcore.NetworkBackend):
    """Records the (host, port) each ``connect_tcp`` was asked to dial.

    ``fail_hosts`` names address literals that should raise
    :class:`httpcore.ConnectError` (to exercise the multi-address fallback
    loop); everything else "connects" and returns a stream.
    """

    def __init__(self, fail_hosts: set[str] | None = None) -> None:
        self.dialed: list[tuple[str, int]] = []
        self.unix_paths: list[str] = []
        self._fail_hosts = fail_hosts or set()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        self.dialed.append((host, port))
        if host in self._fail_hosts:
            raise httpcore.ConnectError(f"refused {host}")
        return _RecordingSyncStream()

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        self.unix_paths.append(path)
        return _RecordingSyncStream()

    def sleep(self, seconds: float) -> None:
        return None


class _RecordingAsyncStream(httpcore.AsyncNetworkStream):
    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return b""

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return self

    def get_extra_info(self, info: str) -> object:
        return None


class _FakeAsyncBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, fail_hosts: set[str] | None = None) -> None:
        self.dialed: list[tuple[str, int]] = []
        self.unix_paths: list[str] = []
        self.slept: list[float] = []
        self._fail_hosts = fail_hosts or set()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.dialed.append((host, port))
        if host in self._fail_hosts:
            raise httpcore.ConnectError(f"refused {host}")
        return _RecordingAsyncStream()

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.unix_paths.append(path)
        return _RecordingAsyncStream()

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


class _BlockedError(Exception):
    """A resolver's destination-blocked error stand-in."""


# ---------------------------------------------------------------------------
# Sync backend
# ---------------------------------------------------------------------------


def test_sync_pins_to_resolved_address() -> None:
    inner = _FakeSyncBackend()
    backend = _PinnedSyncBackend(lambda host: ["93.184.216.34"], inner)
    backend.connect_tcp("api.example.com", 443)
    assert inner.dialed == [("93.184.216.34", 443)]


def test_sync_passthrough_when_resolver_returns_none() -> None:
    inner = _FakeSyncBackend()
    backend = _PinnedSyncBackend(lambda host: None, inner)
    backend.connect_tcp("allowlisted.internal", 8443)
    assert inner.dialed == [("allowlisted.internal", 8443)]


def test_sync_blocked_resolver_opens_no_socket() -> None:
    inner = _FakeSyncBackend()

    def _resolver(host: str) -> Sequence[str] | None:
        raise _BlockedError("no")

    backend = _PinnedSyncBackend(_resolver, inner)
    with pytest.raises(_BlockedError):
        backend.connect_tcp("evil.example.com", 443)
    assert inner.dialed == []


def test_sync_multi_address_fallback_tries_in_order() -> None:
    # First screened address is unreachable; the pin falls through to the
    # second (IPv6-then-IPv4 style fallback).
    inner = _FakeSyncBackend(fail_hosts={"2001:db8::1"})
    backend = _PinnedSyncBackend(lambda host: ["2001:db8::1", "93.184.216.34"], inner)
    backend.connect_tcp("dual.example.com", 443)
    assert inner.dialed == [("2001:db8::1", 443), ("93.184.216.34", 443)]


def test_sync_all_addresses_unreachable_raises_last_error() -> None:
    inner = _FakeSyncBackend(fail_hosts={"10.0.0.1", "10.0.0.2"})
    backend = _PinnedSyncBackend(lambda host: ["10.0.0.1", "10.0.0.2"], inner)
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("h.example.com", 443)
    assert inner.dialed == [("10.0.0.1", 443), ("10.0.0.2", 443)]


def test_sync_unix_socket_and_sleep_delegate() -> None:
    inner = _FakeSyncBackend()
    backend = _PinnedSyncBackend(lambda host: ["1.2.3.4"], inner)
    backend.connect_unix_socket("/tmp/x.sock")
    backend.sleep(0.0)
    assert inner.unix_paths == ["/tmp/x.sock"]


# ---------------------------------------------------------------------------
# Async backend
# ---------------------------------------------------------------------------


async def test_async_pins_to_resolved_address() -> None:
    inner = _FakeAsyncBackend()
    backend = _PinnedAsyncBackend(lambda host: ["93.184.216.34"], inner)
    await backend.connect_tcp("api.example.com", 443)
    assert inner.dialed == [("93.184.216.34", 443)]


async def test_async_passthrough_when_resolver_returns_none() -> None:
    inner = _FakeAsyncBackend()
    backend = _PinnedAsyncBackend(lambda host: None, inner)
    await backend.connect_tcp("allowlisted.internal", 8443)
    assert inner.dialed == [("allowlisted.internal", 8443)]


async def test_async_blocked_resolver_opens_no_socket() -> None:
    inner = _FakeAsyncBackend()

    def _resolver(host: str) -> Sequence[str] | None:
        raise _BlockedError("no")

    backend = _PinnedAsyncBackend(_resolver, inner)
    with pytest.raises(_BlockedError):
        await backend.connect_tcp("evil.example.com", 443)
    assert inner.dialed == []


async def test_async_multi_address_fallback_tries_in_order() -> None:
    inner = _FakeAsyncBackend(fail_hosts={"2001:db8::1"})
    backend = _PinnedAsyncBackend(lambda host: ["2001:db8::1", "93.184.216.34"], inner)
    await backend.connect_tcp("dual.example.com", 443)
    assert inner.dialed == [("2001:db8::1", 443), ("93.184.216.34", 443)]


async def test_async_unix_socket_and_sleep_delegate() -> None:
    inner = _FakeAsyncBackend()
    backend = _PinnedAsyncBackend(lambda host: ["1.2.3.4"], inner)
    await backend.connect_unix_socket("/tmp/x.sock")
    await backend.sleep(0.5)
    assert inner.unix_paths == ["/tmp/x.sock"]
    assert inner.slept == [0.5]


# ---------------------------------------------------------------------------
# Transport builders
# ---------------------------------------------------------------------------


def test_build_sync_transport_swaps_backend_and_keeps_verify() -> None:
    ctx = ssl.create_default_context()
    transport = build_pinned_sync_transport(lambda host: None, verify=ctx)
    assert isinstance(transport._pool._network_backend, _PinnedSyncBackend)
    assert transport._pool._ssl_context is ctx


def test_build_async_transport_swaps_backend_and_keeps_verify() -> None:
    ctx = ssl.create_default_context()
    transport = build_pinned_async_transport(lambda host: None, verify=ctx)
    assert isinstance(transport._pool._network_backend, _PinnedAsyncBackend)
    assert transport._pool._ssl_context is ctx


# ---------------------------------------------------------------------------
# Integration: real loopback sockets
# ---------------------------------------------------------------------------


class _RecordingHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.server.received_hosts.append(self.headers.get("Host"))  # type: ignore[attr-defined]
        location = self.server.redirect_to  # type: ignore[attr-defined]
        if location is not None:
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()
            return
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return None


class _LoopbackServer:
    def __init__(self, redirect_to: str | None = None) -> None:
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
        self._httpd.received_hosts = []  # type: ignore[attr-defined]
        self._httpd.redirect_to = redirect_to  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def received_hosts(self) -> list[str]:
        return self._httpd.received_hosts  # type: ignore[attr-defined,no-any-return]

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def loopback_server() -> Iterable[_LoopbackServer]:
    server = _LoopbackServer()
    try:
        yield server
    finally:
        server.stop()


def test_pinned_client_ignores_ambient_proxy_and_dials_target(
    loopback_server: _LoopbackServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ambient HTTPS_PROXY must not interpose on the pinned transport.

    A client handed an explicit ``transport=`` sends every request through
    that transport and skips httpx's environment proxy mounts. With a bogus
    proxy set, the pinned client still reaches the loopback target directly
    (the pin governs the real dial), and the target sees the original Host.
    """
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")  # would fail if honoured
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    transport = build_pinned_sync_transport(lambda host: ["127.0.0.1"])
    with httpx.Client(transport=transport, trust_env=True) as client:
        resp = client.get(f"http://pinned.test:{loopback_server.port}/x")
    assert resp.status_code == 200
    # Host header carries the original hostname, not the pinned IP.
    assert loopback_server.received_hosts == [f"pinned.test:{loopback_server.port}"]


def test_pinned_client_screens_redirect_hop_at_socket() -> None:
    """A cross-origin redirect re-screens the new origin at connect time.

    The resolver permits ``a.test`` (→ loopback server) but blocks
    ``b.test``. httpx follows the 302, opens a fresh connection to
    ``b.test``, and the pin refuses it at ``connect_tcp`` — so the socket
    to the blocked origin never opens.
    """
    server_b = _LoopbackServer()
    server_a = _LoopbackServer(redirect_to=f"http://b.test:{server_b.port}/next")
    try:

        def _resolver(host: str) -> Sequence[str] | None:
            if host == "a.test":
                return ["127.0.0.1"]
            raise _BlockedError(f"blocked {host}")

        transport = build_pinned_sync_transport(_resolver)
        with (
            httpx.Client(transport=transport, follow_redirects=True) as client,
            pytest.raises(_BlockedError),
        ):
            client.get(f"http://a.test:{server_a.port}/start")
        # A got the initial request; B was never dialed.
        assert server_a.received_hosts == [f"a.test:{server_a.port}"]
        assert server_b.received_hosts == []
    finally:
        server_a.stop()
        server_b.stop()
