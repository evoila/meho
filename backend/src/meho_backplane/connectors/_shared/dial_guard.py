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
  caller dials only one of those checked addresses. The driver never looks
  the name up a second time, so the address it reaches is the address that
  was checked.
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
from typing import TYPE_CHECKING

from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import ThreadedResolver

from meho_backplane.targets.ssrf_guard import (
    TARGET_SSRF_ALLOWLIST_ENV,
    TargetDestinationBlockedError,
    _dialed_host,
    is_allowlisted,
    screen_and_resolve,
)

if TYPE_CHECKING:
    from aiohttp import TCPConnector

__all__ = [
    "DialHostUnresolvedError",
    "ScreeningResolver",
    "dial_host",
    "install_screening_resolver",
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


async def dial_host(host: str) -> str:
    """Return the one host a single-address driver should dial for *host*.

    The first checked address, or *host* itself when it is a host name
    listed verbatim on the allowlist. For drivers that take one host and
    resolve it themselves (asyncssh, asyncpg, pytds, pymongo): handing them
    an IP literal means they connect to the checked address and never look
    the name up again.
    """
    addresses = await screen_dial_addresses_async(host)
    return addresses[0] if addresses else host


def _family_of(address: str) -> socket.AddressFamily:
    if ipaddress.ip_address(address).version == 6:
        return socket.AF_INET6
    return socket.AF_INET


class ScreeningResolver(AbstractResolver):
    """aiohttp resolver that answers only with checked addresses.

    aiohttp asks its resolver for every new connection to a host name, so
    each new connection is checked again. aiohttp does not ask the resolver
    for an IP literal; callers check that once with
    :func:`screen_dial_addresses_async` (an IP literal cannot change).
    """

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        addresses = await screen_dial_addresses_async(host)
        if addresses is None:
            return await ThreadedResolver().resolve(host, port, family)
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

    async def close(self) -> None:
        return None


def install_screening_resolver(connector: TCPConnector) -> None:
    """Make an existing aiohttp connector resolve through :class:`ScreeningResolver`.

    For clients that build their own :class:`aiohttp.TCPConnector` with no
    way to pass a resolver (the Kubernetes client). aiohttp has no public
    setter, so this replaces the connector's resolver attribute, the same
    kind of seam :mod:`.pinned_transport` uses for httpcore's network
    backend.
    """
    connector._resolver = ScreeningResolver()
