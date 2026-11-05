# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Find the secret parts of a URL in text.

A signed download link works as a key until it ends. Its secret parts
are the user part before ``@`` (``user:password``) and the
values of some query parameters:

* S3: ``X-Amz-Signature``, ``X-Amz-Credential``, ``X-Amz-Security-Token``
* Azure SAS: ``sig``
* Google Cloud Storage: ``X-Goog-Signature``, ``X-Goog-Credential``
* CloudFront and others: ``Signature``, and any ``token`` / ``secret`` /
  ``password`` / ``credential`` / ``api_key`` parameter

The finder reports only these parts, so the host, the path and the
other parameters (such as the expiry time) stay readable. A secret in
the path of a link cannot be found by name and is not reported.

Every scan is linear: one regex pass finds the URLs, and each link in a
URL match is read once, by its start and end in the text.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import unquote_plus

__all__ = ["URL_SECRET", "UrlSecretFinder", "is_secret_query_name"]

#: A URL: a short scheme, ``://``, and every byte up to white space, a
#: double quote or an angle bracket. An apostrophe is a valid URL byte
#: (RFC 3986 sub-delims), so it stays in the URL; :func:`_links` splits
#: the match where the next link in single quotes starts, and cuts a
#: link written in single quotes at its closing quote.
#: The scheme length is capped, so a long run of letters and dots
#: without ``://`` is not scanned again and again.
_URL: Final = re.compile(r"(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]{0,31}://[^\s\"<>]+")

#: The opening quote of a link in single quotes, inside a URL match
#: (``'https://a','https://b'`` is one match of :data:`_URL`).
_QUOTED_LINK_START: Final = re.compile(r"'[A-Za-z][A-Za-z0-9+.\-]{0,31}://")

_PLACEHOLDER_START: Final = "[REDACTED:"
_PLACEHOLDER_NAME_STOP: Final = frozenset("]'\"") | frozenset(" \t\n\r\f\v")

_AUTHORITY_END: Final = re.compile(r"[/?#]")

#: A byte that marks a URL part. Text after the closing quote of a link
#: has none of them.
_URL_PART_MARK: Final = re.compile(r"[@/?&=#]")
_CAMEL_BOUNDARY: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NAME_SPLIT: Final = re.compile(r"[^A-Za-z0-9]+")

#: The last word of a query parameter name that holds a secret.
_SECRET_LAST_WORDS: Final[frozenset[str]] = frozenset(
    {
        "sig",
        "signature",
        "credential",
        "credentials",
        "token",
        "secret",
        "password",
        "passwd",
        "pwd",
        "apikey",
    }
)


def is_secret_query_name(name: str) -> bool:
    """True when the query parameter *name* holds a secret value.

    The name is split into words at ``-``, ``_``, ``.`` and camelCase,
    and the last word decides (``X-Amz-Signature``, ``sig``,
    ``X-Goog-Credential``, ``accessToken``). ``api_key`` counts too.
    """
    words = [w for w in _NAME_SPLIT.split(_CAMEL_BOUNDARY.sub("_", name).lower()) if w]
    if not words:
        return False
    return words[-1] in _SECRET_LAST_WORDS or words[-2:] == ["api", "key"]


def _is_placeholder(value: str) -> bool:
    """True when *value* is one complete ``[REDACTED:<name>]`` placeholder."""
    if not value.startswith(_PLACEHOLDER_START) or not value.endswith("]"):
        return False
    name = value[len(_PLACEHOLDER_START) : -1]
    return bool(name) and _PLACEHOLDER_NAME_STOP.isdisjoint(name)


def _is_secret_value(value: str) -> bool:
    return bool(value) and not _is_placeholder(value)


class UrlSecretFinder:
    """Report the user part and the secret query values of each URL in a text."""

    def secret_spans(self, text: str) -> list[tuple[int, int]]:
        """Return ``(start, end)`` of every secret part, left to right."""
        spans: list[tuple[int, int]] = []
        for match in _URL.finditer(text):
            for url, offset in _links(text, match):
                spans.extend(_url_spans(url, offset))
        return spans


def _links(text: str, match: re.Match[str]) -> list[tuple[str, int]]:
    """Return each link of *match* with its offset in *text*.

    A match holds more than one link when links in single quotes follow
    each other without white space (``['https://a','https://b']``). The
    match is split at each apostrophe that opens the next link, and each
    link in single quotes is cut at its closing quote (see
    :func:`_close_quote`). A link that is not in quotes keeps every
    apostrophe. The match is walked by position, so each byte is read
    once.
    """
    start = match.start()
    end = match.end()
    quoted = start > 0 and text[start - 1] == "'"
    links: list[tuple[str, int]] = []
    while True:
        opening = _QUOTED_LINK_START.search(text, start, end)
        stop = end if opening is None else opening.start()
        links.append((_close_quote(text[start:stop], quoted), start))
        if opening is None:
            return links
        start = opening.start() + 1
        quoted = True


def _close_quote(url: str, quoted: bool) -> str:
    """Cut a link in single quotes at its closing quote.

    The closing quote is the last apostrophe, but only when no URL part
    follows it (no ``@ / ? & = #``). Otherwise the apostrophe is inside
    the link (``/it's/file``, ``?q=it's&sig=...``) and the whole part
    stays one link.
    """
    if not quoted:
        return url
    quote = url.rfind("'")
    if quote == -1 or _URL_PART_MARK.search(url, quote + 1) is not None:
        return url
    return url[:quote]


def _url_spans(url: str, offset: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    rest = url.index("://") + 3
    # The host part ends at the first ``/``, ``?`` or ``#`` (RFC 3986).
    end_mark = _AUTHORITY_END.search(url, rest)
    authority_end = len(url) if end_mark is None else end_mark.start()
    at = url.rfind("@", rest, authority_end)
    if at != -1 and _is_secret_value(url[rest:at]):
        spans.append((offset + rest, offset + at))
    fragment = url.find("#", authority_end)
    query_end = len(url) if fragment == -1 else fragment
    question = url.find("?", authority_end, query_end)
    if question == -1:
        return spans
    pos = question + 1
    while pos <= query_end:
        amp = url.find("&", pos, query_end)
        piece_end = query_end if amp == -1 else amp
        equals = url.find("=", pos, piece_end)
        if equals != -1:
            # ``&amp;`` is how HTML and XML text writes ``&``.
            name = unquote_plus(url[pos:equals]).removeprefix("amp;")
            if is_secret_query_name(name) and _is_secret_value(url[equals + 1 : piece_end]):
                spans.append((offset + equals + 1, offset + piece_end))
        pos = piece_end + 1
    return spans


URL_SECRET: Final[UrlSecretFinder] = UrlSecretFinder()
