# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Named file sources: one check for "may MEHO fetch a file from this address".

A tenant admin names the places a file may come from. Each place is a
*file source*: a name and a base address (scheme, host, port and an
optional path prefix). An operation that moves a file takes an address and
calls :func:`resolve_file_source`. The address is accepted only when it
lies under one of the sources. Every connector package uses this one
check, so the rules are the same everywhere.

:func:`resolve_file_source` refuses, before any connection:

* a scheme other than ``http`` or ``https``;
* a user name, a password or a fragment in the address;
* an address whose scheme, host and port do not match a source, or whose
  path is not under the source's path prefix on a ``/`` boundary;
* a path with a ``.`` or ``..`` part after it is fully decoded (a part may
  carry ``;`` parameters, and may be encoded more than once), and a path
  that cannot be decoded to the end.

The error says which rule failed. It never holds the address: an address
can carry a token.

The address guard still runs for every source, at connect time.
:func:`file_source_transport` gives an httpx transport for one source. It
connects only to the host and port of that source, checks every address
the host resolves to, and dials only those checked addresses. A source
that says ``private_network_allowed`` accepts private addresses (RFC 1918
and unique-local IPv6). Loopback, link-local and cloud metadata addresses
are always refused. The install-wide address allowlist does not count for
a file source.

Two readers turn the target settings that exist today into sources:

* :func:`file_sources_from_map`: a map of name to base address;
* :func:`file_sources_from_hosts`: a list of ``host`` or ``host:port``
  entries, read as ``https://host:port/``.

See ``docs/codebase/file-sources.md``.
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
import ssl
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import httpx

from meho_backplane.connectors._shared.pinned_transport import build_port_pinned_async_transport
from meho_backplane.targets import ssrf_guard as _guard

__all__ = [
    "FileSource",
    "FileSourceBase",
    "FileSourceError",
    "file_source_resolver",
    "file_source_transport",
    "file_sources_from_hosts",
    "file_sources_from_map",
    "resolve_file_source",
]

_DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}

#: Decoding rounds before a path counts as "cannot be decoded to the end".
_MAX_DECODE_ROUNDS: Final[int] = 8

_BAD_PERCENT = re.compile(r"%(?![0-9a-fA-F]{2})")

#: What each refusal rule says. The texts never hold the address.
_RULES: Final[dict[str, str]] = {
    "unparseable": "the file address cannot be read as a web address",
    "scheme": "the file address must use http or https",
    "userinfo": "the file address must not hold a user name or password",
    "fragment": "the file address must not hold a fragment",
    "path_dots": "the file address path must not hold a '.' or '..' part",
    "path_undecodable": "the file address path cannot be decoded to the end",
    "no_source": "the file address is not under any named file source "
    "(scheme, host or port differ)",
    "path_outside_source": "the file address path is not under the path of the named file source",
    "bad_source": "a named file source setting is not a valid base address",
    "other_host": "the connection goes to a host or port that is not the named file source's",
    "address_blocked": "the named file source host does not resolve, or is not at an address "
    "MEHO may connect to (a private address needs private_network_allowed; loopback, "
    "link-local and cloud metadata addresses are always refused)",
}


class FileSourceError(ValueError):
    """A file address was refused, or a source setting is not valid.

    :attr:`rule` names the rule that failed. The message never holds the
    address, so it is safe to show and to log.
    """

    def __init__(self, rule: str) -> None:
        self.rule = rule
        super().__init__(_RULES[rule])


@dataclass(frozen=True)
class FileSourceBase:
    """The base address of a file source.

    ``path_prefix`` has no trailing ``/``. It is empty when the source covers
    the whole host.
    """

    scheme: str
    host: str
    port: int
    path_prefix: str = ""


@dataclass(frozen=True)
class FileSource:
    """A named place a file may come from.

    ``private_network_allowed`` lets the address guard accept private
    addresses for this source only. It is false unless the tenant admin
    sets it.
    """

    name: str
    base: FileSourceBase
    private_network_allowed: bool = False


# ---------------------------------------------------------------------------
# Reading the source settings
# ---------------------------------------------------------------------------


def _has_control_char(text: str) -> bool:
    return any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in text)


def _split(address: str) -> urllib.parse.SplitResult:
    if not isinstance(address, str) or not address or _has_control_char(address):
        raise FileSourceError("unparseable")
    try:
        return urllib.parse.urlsplit(address)
    except ValueError:
        # The parser message can quote the address; never chain it.
        raise FileSourceError("unparseable") from None


def _origin(parts: urllib.parse.SplitResult) -> tuple[str, str, int]:
    """Return ``(scheme, host, port)`` of *parts*, with the default port filled in."""
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise FileSourceError("scheme")
    if "@" in parts.netloc:
        raise FileSourceError("userinfo")
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        # The parser message quotes the port text; never chain it.
        raise FileSourceError("unparseable") from None
    if not host:
        raise FileSourceError("unparseable")
    return scheme, host.rstrip(".").lower(), port if port is not None else _DEFAULT_PORTS[scheme]


def _parse_base(name: str, address: str, *, private_network_allowed: bool) -> FileSource:
    """Parse one base address of a source setting."""
    try:
        parts = _split(address)
        scheme, host, port = _origin(parts)
        if parts.query or parts.fragment or "#" in address or "?" in address:
            raise FileSourceError("bad_source")
        prefix = parts.path.rstrip("/")
        _check_path(prefix)
    except FileSourceError:
        raise FileSourceError("bad_source") from None
    if not isinstance(name, str) or not name:
        raise FileSourceError("bad_source")
    return FileSource(
        name=name,
        base=FileSourceBase(scheme=scheme, host=host, port=port, path_prefix=prefix),
        private_network_allowed=private_network_allowed,
    )


def file_sources_from_map(
    mapping: Mapping[str, str], *, private_network_allowed: bool = False
) -> list[FileSource]:
    """Read a map of source name to base address, for example ``{"isos": "https://files.example.com/isos/"}``.

    This is the shape of the target settings that name upload or check
    sources. A value that is not a valid base address raises
    :class:`FileSourceError` with the rule ``bad_source``, and so does a
    *mapping* that is not a map.
    """
    if not isinstance(mapping, Mapping):
        raise FileSourceError("bad_source")
    return [
        _parse_base(name, address, private_network_allowed=private_network_allowed)
        for name, address in mapping.items()
    ]


def file_sources_from_hosts(
    entries: Sequence[str], *, private_network_allowed: bool = False
) -> list[FileSource]:
    """Read a list of ``host`` or ``host:port`` entries, each as ``https://host:port/``.

    The name of each source is its entry. An entry that is not a plain host
    (it holds a scheme, a path, a user name, a query or a fragment) raises
    :class:`FileSourceError` with the rule ``bad_source``. So does a single
    string in place of a list: it would read as one source per character.
    """
    if isinstance(entries, (str, bytes)) or not isinstance(entries, Sequence):
        raise FileSourceError("bad_source")
    sources: list[FileSource] = []
    for entry in entries:
        text = entry.strip() if isinstance(entry, str) else ""
        if not text or "/" in text or "://" in text:
            raise FileSourceError("bad_source")
        source = _parse_base(
            text, f"https://{text}/", private_network_allowed=private_network_allowed
        )
        sources.append(source)
    return sources


# ---------------------------------------------------------------------------
# Checking an address
# ---------------------------------------------------------------------------


def _fully_decoded(path: str) -> str:
    """Decode *path* until another round changes nothing.

    Raises ``path_undecodable`` for a ``%`` that is not followed by two hex
    digits, for bytes that are not UTF-8, and for a path that is still
    changing after :data:`_MAX_DECODE_ROUNDS` rounds.
    """
    current = path
    for _ in range(_MAX_DECODE_ROUNDS):
        if _BAD_PERCENT.search(current):
            raise FileSourceError("path_undecodable")
        try:
            decoded = urllib.parse.unquote(current, errors="strict")
        except UnicodeDecodeError:
            raise FileSourceError("path_undecodable") from None
        if decoded == current:
            return current
        current = decoded
    raise FileSourceError("path_undecodable")


def _check_path(path: str) -> None:
    """Refuse a path with a dot part after full decoding."""
    decoded = _fully_decoded(path)
    if _has_control_char(decoded.replace(" ", "")):
        raise FileSourceError("path_undecodable")
    for segment in decoded.replace("\\", "/").split("/"):
        # A part may carry parameters: "..;x" is a "..".
        if segment.split(";", 1)[0] in {".", ".."}:
            raise FileSourceError("path_dots")


def _under(path: str, prefix: str) -> bool:
    return not prefix or path == prefix or path.startswith(prefix + "/")


def resolve_file_source(url: str, sources: Sequence[FileSource]) -> FileSource:
    """Return the source that *url* lies under, or raise :class:`FileSourceError`.

    Runs before any connection. When more than one source fits, the one with
    the longest path prefix wins. The error holds a rule, never the address.
    """
    parts = _split(url)
    scheme, host, port = _origin(parts)
    if parts.fragment or "#" in url:
        raise FileSourceError("fragment")
    _check_path(parts.path)
    origin_matches = [
        s for s in sources if (s.base.scheme, s.base.host, s.base.port) == (scheme, host, port)
    ]
    if not origin_matches:
        raise FileSourceError("no_source")
    fits = [s for s in origin_matches if _under(parts.path, s.base.path_prefix)]
    if not fits:
        raise FileSourceError("path_outside_source")
    return max(fits, key=lambda s: len(s.base.path_prefix))


#: The only ranges a source with ``private_network_allowed`` may open:
#: RFC 1918 IPv4 space and unique-local IPv6. Loopback, link-local (which
#: holds the cloud metadata address) and every other range stay refused.
_PRIVATE_SOURCE_NETWORKS: Final[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)

#: Instance-service addresses that sit inside the ranges above. A file
#: source never reaches them, even with ``private_network_allowed``.
_INSTANCE_SERVICE_NETWORKS: Final[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]] = (
    ipaddress.ip_network("fd00:ec2::/32"),
)


def _may_dial(addr: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_private: bool) -> bool:
    if not _guard._is_blocked(addr):
        return True
    return (
        allow_private
        and not any(addr in network for network in _INSTANCE_SERVICE_NETWORKS)
        and any(addr in network for network in _PRIVATE_SOURCE_NETWORKS)
    )


def _screen_host(host: str, *, allow_private: bool) -> list[str]:
    """Check the host of a file source with the address guard's rules; return the addresses to dial.

    Stricter than the screen for targets, on purpose:

    * the install-wide address allowlist does not count;
    * a host name is always resolved, every answer is checked, and the
      caller dials only the returned addresses, never the name;
    * a host name that does not resolve is refused.

    A public address passes. With *allow_private*, an address in
    :data:`_PRIVATE_SOURCE_NETWORKS` passes too. Every other address is
    refused with ``address_blocked``.
    """
    candidate = host.strip()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if not candidate:
        raise FileSourceError("address_blocked")
    try:
        addrs = [ipaddress.ip_address(candidate)]
    except ValueError:
        addrs = _guard._resolve_addrs(candidate)
    if not addrs or not all(_may_dial(a, allow_private=allow_private) for a in addrs):
        raise FileSourceError("address_blocked")
    return [str(addr) for addr in addrs]


def _host_key(host: str) -> str:
    """The host as the transport dials it: ASCII, lower case, no trailing dot or brackets."""
    text = host.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    with contextlib.suppress(httpx.InvalidURL, UnicodeError):
        text = httpx.URL(scheme="https", host=text, path="/").raw_host.decode("ascii")
    return text.rstrip(".").lower()


def file_source_resolver(source: FileSource) -> Callable[[str, int], list[str]]:
    """Return the connect-time screen for *source*.

    The result fits
    :func:`~meho_backplane.connectors._shared.pinned_transport.build_port_pinned_async_transport`.
    It refuses, with :class:`FileSourceError`:

    * a host or port that is not the host and port of *source* (rule
      ``other_host``), for example the target of a redirect;
    * a host that is, or resolves to, an address a file source may not
      reach (rule ``address_blocked``).

    Every address of the host is checked, and only those addresses are
    dialed. A private address passes only when the source says
    ``private_network_allowed``. The install-wide address allowlist does not
    count.
    """
    allowed = (_host_key(source.base.host), source.base.port)
    allow_private = source.private_network_allowed

    def _screen(host: str, port: int) -> list[str]:
        if (_host_key(host), port) != allowed:
            raise FileSourceError("other_host")
        return _screen_host(host, allow_private=allow_private)

    return _screen


def file_source_transport(
    source: FileSource, *, verify: ssl.SSLContext | str | bool = True
) -> httpx.AsyncHTTPTransport:
    """Return an httpx transport that connects only to *source*, through the address guard.

    Hand it to ``httpx.AsyncClient(transport=...)`` for the download of an
    address that :func:`resolve_file_source` accepted. Every new connection
    runs :func:`file_source_resolver`: it goes only to the host and port of
    *source*, and only to checked addresses. A refused connection raises
    :class:`FileSourceError`. *verify* is passed to httpx unchanged.
    """
    return build_port_pinned_async_transport(file_source_resolver(source), verify=verify)
