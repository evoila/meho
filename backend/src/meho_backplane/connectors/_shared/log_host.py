"""Reduce an address from a vendor answer to its host name for a log line."""

from __future__ import annotations

import re

import httpx

__all__ = [
    "UNPARSEABLE_ADDRESS",
    "UNREADABLE_LOCATION_ERRORS",
    "UNREADABLE_LOCATION_MESSAGE",
    "host_for_log",
    "unreadable_location_error",
]

#: The one fixed mark a log line holds when there is no host name to show.
UNPARSEABLE_ADDRESS = "<unparseable-location>"

#: The fixed text of the error when httpx cannot read a ``Location`` address.
UNREADABLE_LOCATION_MESSAGE = "MEHO could not read the address in the Location header"

#: What httpx raises while it builds the request for a ``Location`` address.
UNREADABLE_LOCATION_ERRORS = (httpx.RemoteProtocolError, httpx.InvalidURL, UnicodeError)

# ASCII letters, digits, "_", ".", "-" and the ":" of an IPv6 address. The
# check reads ``URL.raw_host``, the ASCII (IDNA) form of the host, so a valid
# non-ASCII name passes. ``httpx`` keeps other characters, such as a
# backslash and the text after it, in the host.
_HOST_NAME = re.compile(rb"[A-Za-z0-9_.:-]+")


def host_for_log(address: str | httpx.URL) -> str:
    """Return only the host name of ``address``, or one fixed mark.

    The address may come from a vendor system (a ``Location`` header, a
    redirect target). The result never holds the scheme, the path, the query,
    the fragment or the user part. A relative address, or a value ``httpx``
    cannot parse (also a host name that is not valid IDNA), or a host part
    with any character a host name does not have, gives
    :data:`UNPARSEABLE_ADDRESS`.
    """
    try:
        url = address if isinstance(address, httpx.URL) else httpx.URL(address)
        host = url.host
    except (httpx.InvalidURL, UnicodeError):
        return UNPARSEABLE_ADDRESS
    if not _HOST_NAME.fullmatch(url.raw_host):
        return UNPARSEABLE_ADDRESS
    return host


def unreadable_location_error(exc: Exception, request: httpx.Request) -> Exception:
    """Return an error of the same class as ``exc``, with one fixed text.

    ``exc`` is one of :data:`UNREADABLE_LOCATION_ERRORS`. The caller raises
    this error, with :data:`UNREADABLE_LOCATION_MESSAGE` as its text, in
    place of ``exc``. A ``UnicodeEncodeError`` or ``UnicodeDecodeError``
    also names the codec.
    """
    cls = type(exc)
    if isinstance(exc, httpx.RemoteProtocolError):
        return cls(UNREADABLE_LOCATION_MESSAGE, request=request)
    if isinstance(exc, UnicodeEncodeError):
        return cls(exc.encoding, "", 0, 1, UNREADABLE_LOCATION_MESSAGE)
    if isinstance(exc, UnicodeDecodeError):
        return cls(exc.encoding, b"", 0, 1, UNREADABLE_LOCATION_MESSAGE)
    if isinstance(exc, UnicodeTranslateError):
        return cls("", 0, 1, UNREADABLE_LOCATION_MESSAGE)
    return cls(UNREADABLE_LOCATION_MESSAGE)
