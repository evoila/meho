"""Reduce an address from a vendor answer to its host name for a log line."""

from __future__ import annotations

import re

import httpx

__all__ = ["UNPARSEABLE_ADDRESS", "host_for_log"]

#: The one fixed mark a log line holds when there is no host name to show.
UNPARSEABLE_ADDRESS = "<unparseable-location>"

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
