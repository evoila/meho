"""Reduce an address from a vendor answer to its host name for a log line."""

from __future__ import annotations

import httpx

__all__ = ["UNPARSEABLE_ADDRESS", "host_for_log"]

#: The one fixed mark a log line holds when there is no host name to show.
UNPARSEABLE_ADDRESS = "<unparseable-location>"


def host_for_log(address: str | httpx.URL) -> str:
    """Return only the host name of ``address``, or one fixed mark.

    The address may come from a vendor system (a ``Location`` header, a
    redirect target). The result never holds the scheme, the path, the query,
    the fragment or the user part. A relative address, or a value ``httpx``
    cannot parse (also a host name that is not valid IDNA), gives
    :data:`UNPARSEABLE_ADDRESS`.
    """
    try:
        host = (address if isinstance(address, httpx.URL) else httpx.URL(address)).host
    except (httpx.InvalidURL, UnicodeError):
        return UNPARSEABLE_ADDRESS
    return host or UNPARSEABLE_ADDRESS
