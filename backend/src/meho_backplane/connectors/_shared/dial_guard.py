# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One connect-time address check for every connector driver.

A target's address is checked when the target is saved (the target
address check in :mod:`meho_backplane.targets`). This module checks it again at
the moment a connector opens a connection, for every driver: the HTTP
transport, SSH, Postgres, MSSQL, MongoDB, Kubernetes, and the OVF upload
client. It uses the same rules, the same operator allowlist and the same
DNS lookup as the save-time check, so the two can never disagree about an
address.

The rules at connect time:

* An IP literal is checked directly.
* A host name that is listed verbatim on the allowlist is trusted as
  written. The driver dials it by name.
* Any other host name is resolved once. Every answer is checked, and the
  caller dials only those checked addresses: the first one, then the next
  when a connection attempt fails (:func:`connect_checked`). The driver
  never looks the name up a second time, so the address it reaches is the
  address that was checked.
* A host name that does not resolve is refused
  (:class:`DialHostUnresolvedError`). MEHO does not connect to an address
  it could not check. At save time such a name is still accepted, because
  the backplane may not share the target's DNS view yet.

A refusal raises ``TargetDestinationBlockedError`` (a
:class:`ValueError`), so each connector's existing error mapping turns
it into a normal connector error. The message never names the resolved
address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

from aiohttp.abc import ResolveResult

from meho_backplane.targets.ssrf_guard import (
    TARGET_SSRF_ALLOWLIST_ENV,
    TargetDestinationBlockedError,
    _dialed_host,
    is_allowlisted,
    screen_and_resolve,
)

if TYPE_CHECKING:
    from aiohttp import TCPConnector
    from aiohttp.tracing import Trace

__all__ = [
    "DialHostUnresolvedError",
    "connect_checked",
    "screen_connector_dials",
    "screen_dial_addresses",
    "screen_dial_addresses_async",
]

_NUMERIC_FLAGS = socket.AI_NUMERICHOST | socket.AI_NUMERICSERV


class DialHostUnresolvedError(TargetDestinationBlockedError):
    """The host name did not resolve when the connector was about to connect."""


def _unresolved(host: str) -> list[str] | None:
    """Refuse a host name that did not resolve at connect time.

    A separate function so the test suite can keep its synthetic host names
    (``*.invalid``) dialable by name, the same way it stubs the DNS lookup.
    """
    raise DialHostUnresolvedError(
        "target host name does not resolve, so MEHO cannot check its address; "
        "refusing to connect (fix the DNS record, use an IP address, or list "
        f"the exact host name in {TARGET_SSRF_ALLOWLIST_ENV})"
    )


def screen_dial_addresses(host: str) -> list[str] | None:
    """Check *host* at connect time and return the addresses to dial.

    Returns the checked IP addresses, in resolver order. Returns ``None``
    only for a host name listed verbatim on the allowlist: the caller dials
    that name unchanged. Raises ``TargetDestinationBlockedError`` when the
    host is, or resolves to, a blocked address, and
    :class:`DialHostUnresolvedError` when the name does not resolve (or the
    host is empty).
    """
    addresses = screen_and_resolve(host)
    if addresses:
        return addresses
    candidate = host.strip()
    if candidate and is_allowlisted(_dialed_host(candidate)):
        return None
    return _unresolved(host)


async def screen_dial_addresses_async(host: str) -> list[str] | None:
    """Run :func:`screen_dial_addresses` in a worker thread.

    The DNS lookup blocks, so it must not run on the event loop.
    """
    return await asyncio.to_thread(screen_dial_addresses, host)


async def connect_checked[T](
    host: str,
    connect: Callable[[str], Awaitable[T]],
    *,
    retry_on: tuple[type[BaseException], ...] = (OSError,),
) -> T:
    """Check *host*, then call *connect* with one checked address at a time.

    For drivers that take one host and resolve it themselves (asyncssh,
    asyncpg, pytds, pymongo). *connect* gets an IP literal, so the driver
    connects to the checked address and never looks the name up again.

    The name is resolved once. When *connect* fails with one of *retry_on*
    (by default ``OSError``: refused, unreachable, timed out), the next
    checked address from that same answer is tried. When every address
    fails, the last error is raised. Any other error is raised at once. A
    host name listed verbatim on the allowlist is handed to *connect* as
    written, once.
    """
    addresses = await screen_dial_addresses_async(host)
    if addresses is None:
        return await connect(host)
    last_exc: BaseException | None = None
    for address in addresses:
        try:
            return await connect(address)
        except retry_on as exc:
            last_exc = exc
    assert last_exc is not None
    raise last_exc


def _family_of(address: str) -> socket.AddressFamily:
    if ipaddress.ip_address(address).version == 6:
        return socket.AF_INET6
    return socket.AF_INET


def _resolve_results(
    host: str, port: int, addresses: Sequence[str], family: socket.AddressFamily
) -> list[ResolveResult]:
    results = [
        ResolveResult(
            hostname=host,
            host=address,
            port=port,
            family=_family_of(address),
            proto=0,
            flags=_NUMERIC_FLAGS,
        )
        for address in addresses
        if family == socket.AF_UNSPEC or _family_of(address) == family
    ]
    if not results:
        raise OSError(f"target host has no checked address in address family {family!r}")
    return results


def screen_connector_dials(connector: TCPConnector) -> None:
    """Make an aiohttp connector check every host before it dials it.

    For clients that build their own :class:`aiohttp.TCPConnector` (the
    Kubernetes client). aiohttp resolves a host right before it opens a
    new connection, through the connector's ``_resolve_host`` method: the
    request host, the host of a redirect it follows, and a proxy host all
    pass through it. aiohttp answers an IP literal there without asking
    its resolver, and may answer a name from its DNS cache. So this hooks
    that method instead of the resolver: every new connection is checked,
    to a literal or to a name, and a name gets only the checked addresses
    of one fresh lookup. A host name listed verbatim on the allowlist is
    resolved by aiohttp itself. A connection that is already open is
    reused as it is.

    aiohttp has no public seam for this. It is the same kind of private
    seam :mod:`.pinned_transport` uses for httpcore's network backend.
    """
    original = connector._resolve_host
    family: socket.AddressFamily = connector._family

    async def _resolve_host(
        host: str, port: int, traces: Sequence[Trace] | None = None
    ) -> list[ResolveResult]:
        addresses = await screen_dial_addresses_async(host)
        if addresses is None:
            return await original(host, port, traces)
        return _resolve_results(host, port, addresses, family)

    connector._resolve_host = _resolve_host  # type: ignore[method-assign]
