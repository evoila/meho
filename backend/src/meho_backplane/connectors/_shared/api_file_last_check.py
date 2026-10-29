# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The last check of :mod:`.api_file_secrets`: read the copy the way a person reads it.

The checks before this one parse the file and replace every secret value
they find. This check runs last, on the checked copy, after the general
secret-redaction rules too. It asks one question of every text in the
copy: once the tags and escapes are gone, does a secret name still have a
value? If yes, that part is held back whole. The check only holds back
more; it never brings back what an earlier check held back.

How a text is read (:func:`secret_in_text`):

1. Flatten it. Decode HTML entities (``&#73;``), backslash escapes
   (``\\u0049``, ``\\x49``) and URL percent escapes (``%49``), and repeat
   until nothing changes. The text is read three ways: with its tags, with
   the tags removed, and with a space for each tag.
2. Search the flattened text for a secret name with a value: the name,
   maybe a quote, then ``:``, ``=`` or ``=>``, maybe a quote, then a
   value (any word or number). After a name and only white space, the
   value counts only when it looks like a token (:func:`_is_token`), so
   prose ("the credentials by ...") stays. A value that already is a
   placeholder (``[REDACTED:...]``) does not count. A name is secret by
   the rule the text check before uses
   (:func:`~meho_backplane.connectors._shared.api_file_values.is_secret_name_lowercased`).
   A full stop after a name ends a sentence; it is not part of the name.
3. Look inside. Every JSON or YAML document in the flattened text is
   parsed, and each key and text value in it is checked the same way. A
   secret-named key whose value is not a placeholder is a secret too.

What is held back, as one part:

* in a JSON or YAML file (:func:`hold_back_in_data`): a text value; the
  value under a key that reads as a secret name; a whole mapping whose key
  text shows a secret with a value;
* in an HTML or XML file: an attribute value, the content of a
  ``<script>`` or ``<style>`` block, and the page text. Page text that is
  split over several tags (``<span>sessionId</span>: ...``) is joined in
  document order first (:func:`hold_back_pieces`). Then the texts around
  the secret are held back, as few as the check can find;
* in an HTML file, the value of a label-and-value pair whose label names
  a secret: the cell after a table cell, the ``<dd>`` after a ``<dt>``,
  the element after a ``<label>``. The value counts whatever its length
  or punctuation; the token rule is for free text only
  (:func:`~meho_backplane.connectors._shared.api_file_markup.last_check_html`).

A held-back part becomes the placeholder. It is counted, and the reason
(:data:`~meho_backplane.connectors._shared.api_file_values.SECRET_NAME_AFTER_FLATTENING`)
is kept on the scrubber, never the text. A text the check cannot finish
reading (encoded too many times, nested too deep) is held back too.

The check reads at most a fixed multiple of the copy's size
(:class:`Work`). A copy that needs more raises
:exc:`LastCheckTooCostlyError`, and the file is not returned.
"""

from __future__ import annotations

import functools
import html
import json
import re
from collections.abc import Sequence
from typing import Any, Final
from urllib.parse import unquote

import yaml

from meho_backplane.connectors._shared.api_file_values import (
    REDACTED,
    SECRET_NAME_AFTER_FLATTENING,
    TOO_DEEP,
    Scrubber,
    WithheldError,
    dump_json,
    is_secret_name_lowercased,
    json_documents,
)

__all__ = [
    "LastCheckTooCostlyError",
    "Work",
    "hold_back_in_data",
    "hold_back_pieces",
    "is_placeholder",
    "last_check_json",
    "names_a_secret",
    "secret_in_text",
]

_YAML_LOADER: Final[type[yaml.SafeLoader]] = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

#: How many times a text is decoded at most. Each round makes the text
#: shorter; a text that still changes after this many rounds is held back.
_DECODE_ROUNDS: Final[int] = 8

#: How deep the check looks into documents inside documents.
_MAX_DEPTH: Final[int] = 8

#: How much text the check may read: this many times the copy's size, plus
#: a floor for small copies.
_WORK_FACTOR: Final[int] = 128
_WORK_FLOOR: Final[int] = 4 * 1024 * 1024

#: How many characters next to a border between two texts the check reads
#: at most to find the texts that hold a secret together.
_WINDOW: Final[int] = 256

#: An HTML or XML tag, a comment, a declaration or a processing instruction.
_TAG: Final[re.Pattern[str]] = re.compile(r"<!--.*?-->|<[!/?]?[A-Za-z][^<>]*>", re.DOTALL)

#: A backslash escape: ``\\uXXXX``, ``\\UXXXXXXXX``, ``\\xXX``, a backslash at
#: the end of a line (YAML joins the lines) or a backslash and any character.
_ESCAPE: Final[re.Pattern[str]] = re.compile(
    r"\\(?:u([0-9A-Fa-f]{4})|U([0-9A-Fa-f]{8})|x([0-9A-Fa-f]{2})|(\r?\n[ \t]*)|(.))", re.DOTALL
)
_ESCAPED: Final[dict[str, str]] = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "N": "\x85",
    "_": "\xa0",
    "L": "\u2028",
    "P": "\u2029",
}

#: A name: words joined by ``.`` or ``-``, maybe after leading dashes
#: (``--password``). It never ends in a dot, so ``password.`` at the end of
#: a sentence is the name ``password`` and then a full stop.
_NAME_CHARS: Final[str] = r"A-Za-z0-9_.\-"
_NAME: Final[re.Pattern[str]] = re.compile(
    rf"(?<![{_NAME_CHARS}])[.\-]*+[A-Za-z0-9_]++(?:[.\-]++[A-Za-z0-9_]++)*+"
)

#: A placeholder that an earlier step wrote: the API file check's own, the
#: general redaction rules' ``[REDACTED:<rule>]``, the trace markers.
_PLACEHOLDER: Final[str] = (
    r"\[(?:MEHO-)?(?:REDACTED|OMITTED):[^\[\]\s'\"]+\](?![A-Za-z0-9_])|\[redacted\]"
)
_PLACEHOLDER_VALUE: Final[re.Pattern[str]] = re.compile(_PLACEHOLDER)

#: What makes the text after a name a value: maybe quotes, ``:``, ``=`` or
#: ``=>``, maybe quotes, then a word or a number within a short run of
#: characters. A placeholder is no value.
_QUOTES: Final[str] = "[\"'`]*+"
_VALUE_AFTER_NAME: Final[re.Pattern[str]] = re.compile(
    _QUOTES + r"\s*+(?:=>|[:=])\s*+" + _QUOTES + r"(?!" + _PLACEHOLDER + r")\S{0,63}?[A-Za-z0-9]"
)

#: The text after a name and only white space: maybe quotes, then the run
#: of characters a token can hold (base64 and its URL-safe form). Whether
#: the run is a token is :func:`_is_token`'s call.
_TOKEN_AFTER_SPACE: Final[re.Pattern[str]] = re.compile(
    _QUOTES + r"\s++" + _QUOTES + r"(?P<run>[A-Za-z0-9+/=_\-]++)"
)

#: How long a run must be to count as a token: hex or letters mixed with
#: digits from 16 characters, any other base64 run from 20.
_TOKEN_MIN: Final[int] = 16
_BASE64_MIN: Final[int] = 20

_HEX: Final[re.Pattern[str]] = re.compile(rf"[0-9A-Fa-f]{{{_TOKEN_MIN},}}")
_LETTERS_AND_DIGITS: Final[re.Pattern[str]] = re.compile(rf"[A-Za-z0-9]{{{_TOKEN_MIN},}}")
_HAS_LETTER: Final[re.Pattern[str]] = re.compile(r"[A-Za-z]")
_HAS_DIGIT: Final[re.Pattern[str]] = re.compile(r"[0-9]")


class LastCheckTooCostlyError(ValueError):
    """The last check would read more than its budget allows; the file is not returned."""


class Work:
    """How much text the last check may still read for one copy (:data:`_WORK_FACTOR`)."""

    def __init__(self, size: int) -> None:
        self._left = _WORK_FACTOR * size + _WORK_FLOOR

    def spend(self, size: int) -> None:
        """Count *size* characters read; raise :exc:`LastCheckTooCostlyError` past the budget."""
        self._left -= size
        if self._left < 0:
            raise LastCheckTooCostlyError


@functools.lru_cache(maxsize=65536)
def _is_secret_word(name: str) -> bool:
    return is_secret_name_lowercased(name)


def is_placeholder(value: Any) -> bool:
    """True when *value* is a placeholder an earlier step wrote."""
    return isinstance(value, str) and _PLACEHOLDER_VALUE.fullmatch(value.strip()) is not None


def _unescape(match: re.Match[str]) -> str:
    code = match.group(1) or match.group(2) or match.group(3)
    if code is not None:
        number = int(code, 16)
        return chr(number) if number <= 0x10FFFF else match.group(0)
    if match.group(4) is not None:
        return ""
    char = match.group(5)
    return _ESCAPED.get(char, char)


def _decode(text: str, tag_gap: str | None, work: Work) -> str:
    """*text* with every escape decoded; the tags removed (``tag_gap``) or kept (``None``)."""
    for _ in range(_DECODE_ROUNDS):
        work.spend(len(text))
        flat = text if tag_gap is None else _TAG.sub(tag_gap, text)
        flat = unquote(_ESCAPE.sub(_unescape, html.unescape(flat)), errors="replace")
        if flat == text:
            return text
        text = flat
    raise WithheldError(TOO_DEEP)


def _readings(pieces: Sequence[str], work: Work) -> set[str]:
    """The ways a person can read *pieces*, texts that follow each other in a document.

    The texts joined as they are and, when there are several, with a space
    between them; each with its tags, without its tags, and with a space
    for each tag; every escape decoded.
    """
    joined = ["".join(pieces)]
    if len(pieces) > 1:
        joined.append(" ".join(pieces))
    readings: set[str] = set()
    for text in joined:
        kept = _decode(text, None, work)
        readings.add(kept)
        if "<" in kept:
            readings.add(_decode(text, "", work))
            readings.add(_decode(text, " ", work))
    return readings


def _is_token(run: str) -> bool:
    """True when *run*, a run without spaces, looks like a token, not a word.

    A token is 16 or more characters of hex, or of letters mixed with
    digits, or 20 or more characters of base64 (``A-Z a-z 0-9 + / = - _``).
    Plain words ("by", "first", "combining") are no token.
    """
    if len(run) >= _BASE64_MIN or _HEX.fullmatch(run):
        return True
    return any(
        _HAS_LETTER.search(part) and _HAS_DIGIT.search(part)
        for part in _LETTERS_AND_DIGITS.findall(run)
    )


def _shows_a_name_with_a_value(reading: str) -> bool:
    for match in _NAME.finditer(reading):
        if not _is_secret_word(match.group()):
            continue
        if _VALUE_AFTER_NAME.match(reading, match.end()):
            return True
        spaced = _TOKEN_AFTER_SPACE.match(reading, match.end())
        if spaced is not None and _is_token(spaced.group("run")):
            return True
    return False


def names_a_secret(name: str, work: Work) -> bool:
    """True when *name*, a key or an attribute name, reads as one secret name once flattened."""
    try:
        readings = _readings((name,), work)
    except WithheldError:
        return True
    for reading in readings:
        word = reading.strip(" \t\r\n\"'`")
        if _NAME.fullmatch(word) and _is_secret_word(word):
            return True
    return False


def secret_in_text(text: str, work: Work, depth: int = 0) -> str | None:
    """Why *text* must be held back once a person reads it, or ``None`` when it may stay."""
    if depth > _MAX_DEPTH:
        return TOO_DEEP
    try:
        readings = _readings((text,), work)
    except WithheldError as exc:
        return exc.reason
    if any(_shows_a_name_with_a_value(reading) for reading in readings):
        return SECRET_NAME_AFTER_FLATTENING
    for reading in readings:
        reason = _secret_inside(reading, work, depth)
        if reason is not None:
            return reason
    return None


def _secret_inside(reading: str, work: Work, depth: int) -> str | None:
    """Check every JSON and YAML document in *reading*, key by key and value by value."""
    work.spend(len(reading))
    try:
        documents: list[Any] = [parsed for _, _, parsed in json_documents(reading)]
        if ":" in reading:
            documents.extend(_yaml_documents(reading, work))
    except WithheldError as exc:
        return exc.reason
    for document in documents:
        if isinstance(document, str) and document.split() == reading.split():
            continue
        reason = _secret_in_data(document, work, depth + 1)
        if reason is not None:
            return reason
    return None


def _yaml_documents(reading: str, work: Work) -> list[Any]:
    """Every YAML document in *reading*; none when it is no YAML (the name search covers it)."""
    work.spend(len(reading))
    try:
        return list(yaml.load_all(reading, Loader=_YAML_LOADER))
    except RecursionError:
        raise WithheldError(TOO_DEEP) from None
    except Exception:  # text that is no YAML is read as text only
        return []


def _secret_in_data(root: Any, work: Work, depth: int) -> str | None:
    """Why parsed data shows a secret, or ``None``: every key and text value, at any depth."""
    seen: set[int] = set()
    stack: list[Any] = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, (dict, list, tuple)):
            if id(node) in seen:
                continue
            seen.add(id(node))
        if isinstance(node, dict):
            for key, value in node.items():
                if names_a_secret(str(key), work) and not is_placeholder(value):
                    return SECRET_NAME_AFTER_FLATTENING
                stack.extend((str(key), value))
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
        elif isinstance(node, str):
            reason = secret_in_text(node, work, depth)
            if reason is not None:
                return reason
    return None


def hold_back_in_data(root: Any, scrubber: Scrubber, work: Work) -> Any:
    """A copy of *root*, parsed JSON or YAML, with every part that shows a secret held back.

    A text value that shows a secret becomes the placeholder. So does the
    value under a key that reads as a secret name, whatever the value is,
    unless it already is a placeholder. A mapping with a key whose text
    shows a secret with a value becomes the placeholder whole. A container
    the data shares (a YAML alias) is checked once and stays shared.
    """
    return _hold_back(root, scrubber, work, {})


def _hold_back(node: Any, scrubber: Scrubber, work: Work, memo: dict[int, Any]) -> Any:
    if isinstance(node, dict):
        if id(node) in memo:
            return memo[id(node)]
        for key in node:
            reason = secret_in_text(str(key), work)
            if reason is not None:
                memo[id(node)] = scrubber.withhold(reason)
                return memo[id(node)]
        mapping: dict[Any, Any] = {}
        memo[id(node)] = mapping
        for key, value in node.items():
            if names_a_secret(str(key), work) and not is_placeholder(value):
                mapping[key] = scrubber.withhold(SECRET_NAME_AFTER_FLATTENING)
            else:
                mapping[key] = _hold_back(value, scrubber, work, memo)
        return mapping
    if isinstance(node, (list, tuple)):
        if id(node) in memo:
            return memo[id(node)]
        items: list[Any] = []
        memo[id(node)] = items
        items.extend(_hold_back(item, scrubber, work, memo) for item in node)
        return items
    if isinstance(node, str):
        reason = secret_in_text(node, work)
        return node if reason is None else scrubber.withhold(reason)
    return node


def last_check_json(copy: str, scrubber: Scrubber, work: Work) -> str:
    """*copy*, a checked JSON file, with every part that shows a secret held back."""
    before = scrubber.count
    data = hold_back_in_data(json.loads(copy), scrubber, work)
    return copy if scrubber.count == before else dump_json(data)


def hold_back_pieces(pieces: list[str], scrubber: Scrubber, work: Work) -> None:
    """Hold back the texts in *pieces* that show a secret, alone or read on with the next ones.

    *pieces* are the texts of an HTML or XML page in document order; the
    list is changed in place. Each text is checked alone first
    (:func:`secret_in_text`). Then the texts are joined, as a person reads
    them across tags. When the joined texts show a secret name with a value,
    the check finds the texts that hold it by halves: it holds back the
    few texts next to the border where the secret crosses, or, when the
    secret spans more than :data:`_WINDOW` characters there, every text in
    that half. So the joined page never shows a secret afterwards.
    """
    for index, piece in enumerate(pieces):
        reason = secret_in_text(piece, work)
        if reason is not None:
            pieces[index] = scrubber.withhold(reason)
    _hold_back_across(pieces, 0, len(pieces), scrubber, work)


def _crosses(pieces: Sequence[str], work: Work) -> bool:
    try:
        return any(_shows_a_name_with_a_value(reading) for reading in _readings(pieces, work))
    except WithheldError:
        return True


def _hold_back_across(
    pieces: list[str], start: int, end: int, scrubber: Scrubber, work: Work
) -> None:
    """Hold back texts in ``pieces[start:end]`` until their joined text shows no secret."""
    if end - start < 2 or not _crosses(pieces[start:end], work):
        return
    middle = (start + end) // 2
    _hold_back_across(pieces, start, middle, scrubber, work)
    _hold_back_across(pieces, middle, end, scrubber, work)
    if not _crosses(pieces[start:end], work):
        return
    reach = 1
    while True:
        low, high = max(start, middle - reach), min(end, middle + reach)
        if _crosses(pieces[low:high], work):
            _withhold_texts(pieces, low, high, scrubber)
            if not _crosses(pieces[start:end], work):
                return
        if (low, high) == (start, end) or _span(pieces, low, high) > 2 * _WINDOW:
            _withhold_texts(pieces, start, end, scrubber)
            return
        reach *= 2


def _span(pieces: Sequence[str], start: int, end: int) -> int:
    return sum(len(piece) for piece in pieces[start:end])


def _withhold_texts(pieces: list[str], start: int, end: int, scrubber: Scrubber) -> None:
    """Hold back every text in ``pieces[start:end]`` that is not blank, as one part."""
    first = True
    for index in range(start, end):
        if not pieces[index].strip() or pieces[index] == REDACTED:
            continue
        if first:
            pieces[index] = scrubber.withhold(SECRET_NAME_AFTER_FLATTENING)
            first = False
        else:
            scrubber.count += 1
            pieces[index] = REDACTED
