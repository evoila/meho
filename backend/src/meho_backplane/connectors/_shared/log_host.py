"""Reduce an address from a vendor answer to its host name for a log line."""

from __future__ import annotations

import re
from urllib.parse import quote, unquote

import httpx

__all__ = [
    "UNPARSEABLE_ADDRESS",
    "UNREADABLE_LOCATION_ERRORS",
    "UNREADABLE_LOCATION_MESSAGE",
    "host_for_log",
    "host_names_in_text",
    "redirect_status_text",
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

# An address in free text: one with a scheme (``https://...``), one that
# starts with ``//``, or one with a percent-encoded scheme part
# (``https%3A%2F%2F...``). It ends at a space, a quote or an angle bracket.
_TEXT_ADDRESS = re.compile(
    r"""[A-Za-z][A-Za-z0-9+.-]*://[^\s"'<>]+"""
    r"""|(?<![\w:/])//[^\s"'<>]+"""
    r"""|[A-Za-z][A-Za-z0-9+.-]*%3A%2F%2F[^\s"'<>]+""",
    re.IGNORECASE,
)

# Signs that end a sentence or close a bracket after an address. They stay
# in the text.
_TRAILING_SIGNS = ".,;:!?)]}"


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


def _address_as_host_name(match: re.Match[str]) -> str:
    address = match.group(0)
    kept = address.rstrip(_TRAILING_SIGNS)
    tail = address[len(kept) :]
    if "://" not in kept and not kept.startswith("//"):
        kept = unquote(kept)
    return host_for_log(kept) + tail


# The scheme and the authority at the start of an address.
_SCHEME_AND_AUTHORITY = re.compile(r"(?:[A-Za-z][A-Za-z0-9+.-]*:)?//[^/?#]*")


def _location_forms(location: str) -> list[str]:
    """Return the ways a vendor text can write ``location``, longest first.

    The forms are the whole address and the part from its path on, each as
    sent, percent-decoded, percent-encoded and with ``&amp;`` for ``&``.
    """
    start = _SCHEME_AND_AUTHORITY.match(location)
    values = {location, location[start.end() :] if start else location}
    forms: set[str] = set()
    for value in values | {unquote(value) for value in values}:
        for form in (value, quote(value, safe=""), quote(value, safe="/")):
            forms.update((form, form.replace("&", "&amp;")))
    return sorted((form for form in forms if form.strip("/")), key=len, reverse=True)


def host_names_in_text(text: str, location: str | None) -> str:
    """Return ``text`` with each address in it reduced to its host name.

    ``text`` is the body of a 3xx answer and ``location`` its ``Location``
    header. Each address with a scheme, each address that starts with
    ``//``, and ``location`` as the vendor sent it (also a relative one, or
    only its part from the path on, with ``&amp;`` for ``&`` or with
    percent-encoded characters) becomes the result of :func:`host_for_log`
    for that address. The text around each address stays.
    """
    text = _TEXT_ADDRESS.sub(_address_as_host_name, text)
    if location:
        mark = host_for_log(location)
        for form in _location_forms(location):
            text = text.replace(form, mark)
    return text


def redirect_status_text(exc: BaseException) -> str | None:
    """Return the text for a 3xx answer with host names only, or ``None``.

    ``exc`` is an :exc:`httpx.HTTPStatusError` of a 3xx answer; for any other
    error the result is ``None``. The text holds only the status code and the
    host names of the request address and of the ``Location`` header.
    """
    if not isinstance(exc, httpx.HTTPStatusError) or not 300 <= exc.response.status_code < 400:
        return None
    response = exc.response
    text = f"Redirect response {response.status_code} from {host_for_log(exc.request.url)}"
    location = response.headers.get("Location")
    if location is None:
        return f"{text}, with no Location header."
    return f"{text} to {host_for_log(location)}, not followed."


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
