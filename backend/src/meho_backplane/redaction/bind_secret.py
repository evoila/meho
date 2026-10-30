# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Find BIND key secrets in config text the way BIND reads it (#4617).

A ``named.conf`` ``key`` block carries the shared secret of a TSIG or
rndc key as ``secret "<base64>";``, and the ``options`` block can carry
``cookie-secret "<hex>";``. BIND reads both values as a string, with or
without quotes, and allows comments (``/* */``, ``//``, ``#``) between
the keyword and the value.

A regex alone cannot tell a ``secret`` word in a comment from the
keyword of a key block. A review of #5342 found a line comment that
held ``secret /*``: the old rule read the ``/*`` as a comment between
keyword and value, so its match ran over the real key block and the
real secret stayed visible. So this module first splits the text into
comments, quoted strings and code the way BIND's lexer does
(``lib/isc/lex.c``: C, C++ and shell comments; a quoted string may
span lines and ``\\"`` is an escaped quote; ``{ } ; / " !`` end a
word). Then it looks for the keyword in code and in comments, and
reads the value only inside the same scope:

* In code, the value may sit after white space and comments. A quoted
  value is the next quoted string and must be base64 or hex. A value
  without quotes runs to the next white space or special character
  and must be followed by ``;`` or a comment, so plain prose such as
  "the secret is in the vault" is left alone.
* In a comment, the value must sit inside the same comment. A
  commented-out key block is still hidden, and a comment can never
  reach past its own end. A comment between a keyword and its value
  may hold a secret of its own; both values are reported.
* Inside a quoted string nothing is a keyword, so a key name such as
  ``"transfer-secret"`` or ``"my secret"`` is left alone, and a ``#``
  or ``//`` inside a string does not start a comment.

Only the value is reported, so the keyword and the comments stay
readable in the output. The engine replaces the reported spans.

Every scan is linear. A comment or quoted string that never closes is
looked for once: from then on its opening mark counts as plain code,
as BIND would stop with an error there. So a payload full of ``/*`` or
``"`` cannot keep the middleware busy.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["BIND_KEY_SECRET", "BindKeySecretFinder"]

#: Longest secret value the finder reports (same bound as the regex rules).
_VALUE_MAX: Final[int] = 4096

_CODE: Final[int] = 0
_COMMENT: Final[int] = 1
_STRING: Final[int] = 2

_PIECE_START = re.compile(r'/\*|//|#|"')
_STRING_END = re.compile(r'(?:[^"\\]|\\[\s\S])*"')

# The keyword as a whole word: before and after it sits white space, a
# BIND special character, a comment delimiter, or the end of the text.
# ``cookie-secret`` is one word; ``transfer-secret`` and ``secrets``
# are not the keyword.
_KEYWORD = re.compile(
    r'(?<![^\s{};!/"#])(?:cookie-)?secret(?![^\s{};!/"#])',
    re.IGNORECASE,
)

_WHITESPACE = re.compile(r"\s*")
_QUOTED_VALUE = re.compile(r'"[A-Za-z0-9+/=\s]{1,' + str(_VALUE_MAX) + '}"')
_BARE_VALUE = re.compile(r'[^\s;{}/*"!#]{1,' + str(_VALUE_MAX) + "}")
# Inside a comment: the value follows the keyword in the same comment.
_COMMENT_VALUE = re.compile(
    r'\s*(?P<quoted>"[A-Za-z0-9+/=\s]{1,'
    + str(_VALUE_MAX)
    + r'}")|\s+(?P<bare>[^\s;{}/*"!#]{1,'
    + str(_VALUE_MAX)
    + r"})(?=\s*;)"
)


class BindKeySecretFinder:
    """Report the spans of BIND key secret values in a text."""

    def secret_spans(self, text: str) -> list[tuple[int, int]]:
        """Return ``(start, end)`` of every secret value, left to right."""
        scopes = _split(text)
        found: list[tuple[int, int]] = []
        for index, (kind, start, end) in enumerate(scopes):
            if kind == _STRING:
                continue
            for keyword in _KEYWORD.finditer(text, start, end):
                if kind == _COMMENT:
                    span = _value_in_comment(text, keyword.end(), end)
                else:
                    span = _value_in_code(text, scopes, index, keyword.end())
                if span is not None:
                    found.append(span)
        # A value in code can sit after a comment that holds a secret of
        # its own, so the values are not found in text order. Sort them,
        # and keep every value that does not overlap an earlier one.
        spans: list[tuple[int, int]] = []
        last_end = 0
        for span in sorted(found):
            if span[0] >= last_end:
                spans.append(span)
                last_end = span[1]
        return spans


def _split(text: str) -> list[tuple[int, int, int]]:
    """Cut *text* into ``(kind, start, end)`` scopes that cover it whole.

    Pieces are taken left to right and never overlap, so a ``"`` inside
    a comment or a ``#`` inside a string belongs to the piece that
    started first, as in BIND.
    """
    scopes: list[tuple[int, int, int]] = []
    code_start = 0
    cursor = 0
    # An opening mark with no closing mark anywhere after it. Every later
    # one of the same kind has no closing mark either, so it is code too.
    unclosed: set[str] = set()
    while (found := _PIECE_START.search(text, cursor)) is not None:
        start, mark = found.start(), found.group()
        end = -1 if mark in unclosed else _piece_end(text, start, mark)
        if end < 0:
            unclosed.add(mark)
            cursor = start + 1
            continue
        if start > code_start:
            scopes.append((_CODE, code_start, start))
        scopes.append((_STRING if mark == '"' else _COMMENT, start, end))
        code_start = cursor = end
    if code_start < len(text):
        scopes.append((_CODE, code_start, len(text)))
    return scopes


def _piece_end(text: str, start: int, mark: str) -> int:
    """Where the comment or string that opens with *mark* at *start* ends; -1 if never."""
    if mark == "/*":
        close = text.find("*/", start + 2)
        return -1 if close < 0 else close + 2
    if mark == '"':
        body = _STRING_END.match(text, start + 1)
        return -1 if body is None else body.end()
    newline = text.find("\n", start)
    return len(text) if newline < 0 else newline


def _skip_whitespace(text: str, pos: int, end: int) -> int:
    """Return the first index at or after *pos* (up to *end*) that is not white space."""
    found = _WHITESPACE.match(text, pos, end)
    return pos if found is None else found.end()


def _value_in_comment(text: str, pos: int, end: int) -> tuple[int, int] | None:
    """The value after a keyword inside a comment, bounded by the comment."""
    found = _COMMENT_VALUE.match(text, pos, end)
    if found is None:
        return None
    return found.span("quoted" if found.group("quoted") is not None else "bare")


def _value_in_code(
    text: str,
    scopes: list[tuple[int, int, int]],
    index: int,
    pos: int,
) -> tuple[int, int] | None:
    """The value after a keyword in code: past white space and comments."""
    kind, _start, end = scopes[index]
    while kind != _STRING:
        if kind == _CODE:
            pos = _skip_whitespace(text, pos, end)
            if pos < end:
                return _bare_value(text, scopes, index, pos)
        index += 1
        if index == len(scopes):
            return None
        kind, pos, end = scopes[index]
    if _QUOTED_VALUE.fullmatch(text, pos, end) is None:
        return None
    return pos, end


def _bare_value(
    text: str,
    scopes: list[tuple[int, int, int]],
    index: int,
    pos: int,
) -> tuple[int, int] | None:
    """A value without quotes at *pos*; ``;`` or a comment must follow."""
    end = scopes[index][2]
    found = _BARE_VALUE.match(text, pos, end)
    if found is None:
        return None
    after = _skip_whitespace(text, found.end(), end)
    if after < end:
        return found.span() if text[after] == ";" else None
    if index + 1 < len(scopes) and scopes[index + 1][0] == _COMMENT:
        return found.span()
    return None


#: The finder the ``bind_key_secret`` rule uses.
BIND_KEY_SECRET: Final[BindKeySecretFinder] = BindKeySecretFinder()
