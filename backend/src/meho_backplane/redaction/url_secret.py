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

Every scan is linear: one regex pass finds the URLs, and each URL is
split once.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import unquote_plus

__all__ = ["URL_SECRET", "UrlSecretFinder", "is_secret_query_name"]

#: A URL: a short scheme, ``://``, and every byte up to white space, a
#: quote or an angle bracket. The scheme length is capped, so a long run
#: of letters and dots without ``://`` is not scanned again and again.
_URL: Final = re.compile(r"(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]{0,31}://[^\s'\"<>]+")

#: A value that already is a complete ``[REDACTED:<name>]`` placeholder.
_PLACEHOLDER: Final = re.compile(r"\[REDACTED:[^\]\s'\"]+\]")

_AUTHORITY_END: Final = re.compile(r"[/?#]")
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


def _is_secret_value(value: str) -> bool:
    return bool(value) and _PLACEHOLDER.fullmatch(value) is None


class UrlSecretFinder:
    """Report the user part and the secret query values of each URL in a text."""

    def secret_spans(self, text: str) -> list[tuple[int, int]]:
        """Return ``(start, end)`` of every secret part, left to right."""
        spans: list[tuple[int, int]] = []
        for match in _URL.finditer(text):
            spans.extend(_url_spans(match.group(0), match.start()))
        return spans


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
