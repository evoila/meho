# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Address-pinning httpx transports that close the SSRF check/use gap.

A destination guard that resolves a hostname, screens the answer, and
then hands the *hostname* to httpx leaves a check/use gap: httpx
re-resolves the name at connect time, so a resolver that changes its
answer between the screen and the connect (DNS rebinding, or a
split-horizon flip) can steer the socket at a blocked address the guard
never saw (evoila-bosnia/meho-internal#275, F13).

This module removes the gap by moving the decision to the socket
boundary. A pinning transport wraps httpcore's connection pool with a
network backend whose ``connect_tcp`` resolves-and-screens the host in
one step and then dials **only** a validated address literal from that
same resolution. Because the address the socket reaches is the address
that was screened, there is no second, unscreened resolution to exploit.

What is deliberately *not* touched:

* **TLS SNI / certificate verification.** httpcore derives the TLS
  ``server_hostname`` from the request origin (or the ``sni_hostname``
  request extension), never from the value ``connect_tcp`` was handed,
  so rewriting the TCP target to an IP literal leaves the handshake
  offering the original hostname as SNI and verifying the presented
  cert's CN/SAN against it. Chain, hostname, and any per-target CA pin
  keep biting.
* **The ``Host:`` header**, which httpx builds from the request URL.
* **Connection reuse.** ``connect_tcp`` runs only when the pool opens a
  *new* connection; a warm pooled connection (already pinned to a
  screened address) is reused untouched, and the next new connection
  re-screens.

The screening itself is supplied by the caller as a
:data:`PinnedResolver`: the target-dispatch path passes the target SSRF
guard (:mod:`meho_backplane.targets.ssrf_guard`), the connector
spec-ingest path passes its own stricter fetch guard. Each resolver
owns its own policy (allowlist, fail-open vs fail-closed, error type);
this module only enforces *connect to what you screened*.

**Explicit proxy note.** An httpx client handed an explicit
``transport=`` sends every request through that one transport and does
**not** apply the ambient ``HTTP(S)_PROXY`` mounts it would otherwise
derive from the environment. That is the correct posture here: routing a
vendor dial through an ambient proxy would place the destination
decision on the far side of the proxy, defeating address pinning. MEHO
configures no such proxy; egress restriction, where wanted, is an
independent network boundary (the guard does not replace it).
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Callable, Iterable, Sequence

import httpcore
import httpx

__all__ = [
    "PinnedResolver",
    "build_pinned_async_transport",
    "build_pinned_sync_transport",
]

#: A destination screen for the pinning backend. Given the host httpcore
#: is about to dial, it returns the IP-literal address set the connection
#: may use — screening already applied, so it **raises** the caller's own
#: destination-blocked error when the host is (or resolves to) a
#: forbidden address — or ``None`` to dial the host unchanged
#: (passthrough: an allowlisted hostname literal the policy trusts
#: verbatim, or a name the policy fails open on and lets the stock
#: resolver handle). An empty sequence is treated as passthrough.
PinnedResolver = Callable[[str], Sequence[str] | None]


class _PinnedAsyncBackend(httpcore.AsyncNetworkBackend):
    """Async network backend that dials only resolver-validated addresses.

    Wraps the pool's original backend (so trio/asyncio detection and
    every non-TCP concern are preserved) and overrides ``connect_tcp`` to
    screen the host and pin the connection. The screen is a blocking
    ``getaddrinfo`` call, so it runs in a worker thread — the dispatch
    hot path must not stall the event loop for a DNS round-trip (the same
    reason :func:`assert_public_destination_async` offloads).
    """

    def __init__(self, resolver: PinnedResolver, inner: httpcore.AsyncNetworkBackend) -> None:
        self._resolver = resolver
        self._inner = inner

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await asyncio.to_thread(self._resolver, host)
        if not addresses:
            # Passthrough: an allowlisted hostname literal or a name the
            # policy fails open on — dial by name via the stock backend.
            return await self._inner.connect_tcp(
                host,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )
        last_exc: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_exc = exc
        # Every screened address failed to connect (IPv6-then-IPv4
        # fallback exhausted); surface the last transport error.
        assert last_exc is not None
        raise last_exc

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        # A UDS path names a local file, not a network destination — no
        # host to screen; delegate unchanged.
        return await self._inner.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _PinnedSyncBackend(httpcore.NetworkBackend):
    """Synchronous counterpart of :class:`_PinnedAsyncBackend`.

    Used by the connector spec-ingest fetch, which runs on a stock
    synchronous :class:`httpx.Client`. The screen runs inline — there is
    no event loop to protect on this path.
    """

    def __init__(self, resolver: PinnedResolver, inner: httpcore.NetworkBackend) -> None:
        self._resolver = resolver
        self._inner = inner

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        addresses = self._resolver(host)
        if not addresses:
            return self._inner.connect_tcp(
                host,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )
        last_exc: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for address in addresses:
            try:
                return self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        return self._inner.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


def build_pinned_async_transport(
    resolver: PinnedResolver,
    *,
    verify: ssl.SSLContext | str | bool = True,
) -> httpx.AsyncHTTPTransport:
    """Return an :class:`httpx.AsyncHTTPTransport` that pins via *resolver*.

    ``verify`` is forwarded to the transport's TLS context exactly as
    :class:`httpx.AsyncClient` would forward it (a ``bool``, a CA-bundle
    path, or a pre-built :class:`ssl.SSLContext` — the CA-pin / insecure
    contexts the HTTP adapter builds), so certificate-trust behaviour is
    unchanged; only the TCP target is pinned. The transport's own
    connection pool is preserved (limits, retries, keepalive) — its
    network backend is the sole thing swapped.
    """
    transport = httpx.AsyncHTTPTransport(verify=verify)
    pool = transport._pool
    pool._network_backend = _PinnedAsyncBackend(resolver, pool._network_backend)
    return transport


def build_pinned_sync_transport(
    resolver: PinnedResolver,
    *,
    verify: ssl.SSLContext | str | bool = True,
) -> httpx.HTTPTransport:
    """Return an :class:`httpx.HTTPTransport` that pins via *resolver*.

    Synchronous counterpart of :func:`build_pinned_async_transport`, for
    the spec-ingest fetch's :class:`httpx.Client`.
    """
    transport = httpx.HTTPTransport(verify=verify)
    pool = transport._pool
    pool._network_backend = _PinnedSyncBackend(resolver, pool._network_backend)
    return transport
