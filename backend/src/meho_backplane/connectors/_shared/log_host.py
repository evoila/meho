"""Reduce an address from a vendor answer to its host name for a log line."""

from __future__ import annotations

import re
from http import HTTPStatus

import httpx

__all__ = [
    "FIRST_REQUEST_URL_EXTENSION",
    "REDIRECT_TEXT_WITHHELD",
    "UNPARSEABLE_ADDRESS",
    "UNREADABLE_LOCATION_ERRORS",
    "UNREADABLE_LOCATION_MESSAGE",
    "followed_redirect_status_text",
    "host_for_log",
    "is_redirect_status",
    "origin_for_log",
    "redirect_status_text",
    "redirect_text_withheld",
    "spec_source_for_log",
    "unreadable_location_error",
]

#: The one fixed mark a log line holds when there is no host name to show.
UNPARSEABLE_ADDRESS = "<unparseable-location>"

#: The fixed text of the error when httpx cannot read a ``Location`` address.
UNREADABLE_LOCATION_MESSAGE = "MEHO could not read the address in the Location header"

#: The key of :attr:`httpx.Response.extensions` and :attr:`httpx.Request.extensions`
#: that holds the first request address when the HTTP client followed a redirect.
FIRST_REQUEST_URL_EXTENSION = "meho_first_request_url"

#: The fixed text that stands in place of the vendor's text of a 3xx answer
#: other than a 304 that MEHO does not follow: in the answer, the audit row,
#: the log and the trace.
REDIRECT_TEXT_WITHHELD = "MEHO does not pass on the text of a redirect answer it does not follow."

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


def origin_for_log(address: str | httpx.URL) -> str:
    """Return only ``scheme://host`` of ``address``, or one fixed mark.

    Use it for an address of MEHO's own settings or requests, where the
    scheme helps the reader. The result never holds the path, the query, the
    fragment, the port or the user part. A value that has no scheme or no valid
    host name gives :data:`UNPARSEABLE_ADDRESS`.
    """
    try:
        url = address if isinstance(address, httpx.URL) else httpx.URL(address)
    except (httpx.InvalidURL, UnicodeError):
        return UNPARSEABLE_ADDRESS
    host = host_for_log(url)
    if host == UNPARSEABLE_ADDRESS or not url.scheme:
        return UNPARSEABLE_ADDRESS
    if ":" in host:
        host = f"[{host}]"
    return f"{url.scheme}://{host}"


def spec_source_for_log(uri: str) -> str:
    """Return ``scheme://host`` for a spec at a web address, else ``file``.

    A local or packaged spec has no address worth logging, and its path is not
    written.
    """
    if uri.lower().startswith(("http://", "https://")):
        return origin_for_log(uri)
    return "file"


def is_redirect_status(status_code: object) -> bool:
    """Return ``True`` when *status_code* is a 3xx HTTP status."""
    return isinstance(status_code, int) and 300 <= status_code < 400


def redirect_text_withheld(status_code: object) -> bool:
    """Return ``True`` when MEHO does not pass on the vendor's text of a *status_code* answer.

    This holds for a 3xx answer other than a ``304 Not Modified``. A 304
    asks for no redirect, so its answer and trace are built as for any other
    status.
    """
    return is_redirect_status(status_code) and status_code != HTTPStatus.NOT_MODIFIED


def redirect_status_text(exc: BaseException) -> str | None:
    """Return the text for a 3xx answer with host names only, or ``None``.

    ``exc`` is an :exc:`httpx.HTTPStatusError` of a 3xx answer; for any other
    error the result is ``None``. The text holds only the status code and the
    host names of the request address and of the ``Location`` header.
    """
    if not isinstance(exc, httpx.HTTPStatusError) or not is_redirect_status(
        exc.response.status_code
    ):
        return None
    response = exc.response
    text = f"Redirect response {response.status_code} from {host_for_log(exc.request.url)}"
    location = response.headers.get("Location")
    if location is None:
        return f"{text}, with no Location header."
    return f"{text} to {host_for_log(location)}, not followed."


def followed_redirect_status_text(exc: BaseException) -> str | None:
    """Return the text for an error answer that came after a followed redirect, or ``None``.

    ``exc`` is an :exc:`httpx.HTTPStatusError` whose answer carries the mark
    :data:`FIRST_REQUEST_URL_EXTENSION`; for any other error the result is
    ``None``. The address of the followed hop is the vendor's choice, and so
    is the reason phrase of its status line. The text holds only the status
    code, the fixed phrase of that code from :class:`http.HTTPStatus` (none
    for a code it does not know) and the host name of that address.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    response = exc.response
    if response.extensions.get(FIRST_REQUEST_URL_EXTENSION) is None:
        return None
    code = response.status_code
    try:
        status = f"{code} {HTTPStatus(code).phrase}"
    except ValueError:
        status = str(code)
    kind = "Client error" if code < 500 else "Server error"
    return (
        f"{kind} '{status}' from {host_for_log(exc.request.url)}, "
        "after a redirect on the same host."
    )


def unreadable_location_error(exc: Exception, request: httpx.Request) -> Exception:
    """Return an error of the same class as ``exc``, with one fixed text.

    ``exc`` is one of :data:`UNREADABLE_LOCATION_ERRORS`. The caller raises
    this error, with :data:`UNREADABLE_LOCATION_MESSAGE` as its text, in
    place of ``exc``. A ``UnicodeEncodeError`` or ``UnicodeDecodeError``
    also names the codec.
    """
    if isinstance(exc, httpx.RemoteProtocolError):
        return type(exc)(UNREADABLE_LOCATION_MESSAGE, request=request)
    if isinstance(exc, UnicodeEncodeError):
        return type(exc)(exc.encoding, "", 0, 1, UNREADABLE_LOCATION_MESSAGE)
    if isinstance(exc, UnicodeDecodeError):
        return type(exc)(exc.encoding, b"", 0, 1, UNREADABLE_LOCATION_MESSAGE)
    if isinstance(exc, UnicodeTranslateError):
        return type(exc)("", 0, 1, UNREADABLE_LOCATION_MESSAGE)
    return type(exc)(UNREADABLE_LOCATION_MESSAGE)
