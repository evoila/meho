# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Replace the values of secret-named fields, in data and in text (#5031).

The Tier-1 engine (:mod:`.engine`) finds a secret by its shape inside one
string: ``password=...``, ``Bearer ...``. A vendor answer often holds a secret
in a field instead: ``{"password": "..."}``. The value has no label of its
own, so the engine keeps it. The same happens when that answer comes back as
text, for example in a vendor error body: ``"password": "..."`` has a quote
between the name and the colon, so the ``api_key`` pattern does not see it.

This module closes both cases with one name rule: the shared
:func:`~meho_backplane.broadcast.events.is_secret_param_name`, which the
broadcast scrub and the approval echo already use.

* :func:`scrub_secret_fields` walks a dict / list / str. A dict entry with a
  secret name gets :data:`SECRET_FIELD_MARKER` in place of its value (a whole
  sub-tree, too). Each string goes through :func:`scrub_secret_fields_in_text`.
* :func:`scrub_secret_fields_in_text` finds quoted name-value pairs in text
  and replaces the value when the name is a secret name. The name and the
  value may each use ``"`` or ``'`` (JSON ``"name": "value"``, Python
  ``'name': 'value'`` and Python's ``'name': "it's"``), and the pair may be
  escaped JSON inside a JSON string (``\\"name\\": \\"value\\"``).
  The value may also be an object or a list (``"credentials": {"value":
  "..."}``, ``"passwords": ["..."]``): the whole object or list is then
  replaced, as the walk does for data. :func:`iter_quoted_secret_fields`
  lists those pairs; the capture check (``tests/test_capture_secret_scan.py``)
  uses it too.

The value is read by a small scanner, not by a pattern: it walks the text
from the opening quote to the closing quote and steps over each escape, so
a quote inside the value does not end it, and a value has no length limit.
An object or a list is walked to its closing bracket; a bracket inside a
quoted string does not count. A value with no closing quote or bracket (a
diagnostic cut by a length cap) is replaced up to the end of the text.

Numbers, booleans and ``null`` under a secret name stay (``bind_secret_id:
true`` is a flag, not a secret). A value that an earlier step already
masked (``[REDACTED:...]``, a trace marker, ``***``) stays, so its label is
kept.

Used by the dispatch error path (``operations/_errors.py`` and
``operations/_vendor_text.py``) and by the flight-recorder trace scrub
(``flight_recorder/_content.py``). The normal result path does not use it:
there ``operations/_secret_values.py`` hides a field like ``password`` in
every answer, with the fields an operation declared as plain left in place
(``docs/codebase/redaction.md``).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Final, NamedTuple

__all__ = [
    "SECRET_FIELD_MARKER",
    "QuotedSecretField",
    "iter_quoted_secret_fields",
    "scrub_secret_fields",
    "scrub_secret_fields_in_text",
]

#: The text put in place of a secret-named value. The same text as the
#: broadcast scrub, so a reader sees one word for "a field with a secret name".
SECRET_FIELD_MARKER: Final[str] = "[REDACTED:param_name]"

_NAME: Final[str] = r"[A-Za-z0-9_.\-]{1,128}"

#: The start of a quoted name-value pair, up to and including what opens
#: the value: the quote of a string (``"``, ``'`` or escaped JSON's ``\"``)
#: or the bracket of an object or a list (``{`` or ``[``). The name is in
#: ``"`` or ``'``, or escaped JSON's ``\"``. The value itself is read by a
#: scanner (:func:`_plain_value_end`, :func:`_escaped_value_end`,
#: :func:`_bracket_value_end`).
_PAIR_START: Final[re.Pattern[str]] = re.compile(
    r"\\\"(?P<escaped_name>" + _NAME + r")\\\"\s*:\s*(?P<escaped_open>\\\"|[{\[])"
    r"|(?P<name_quote>[\"'])(?P<name>" + _NAME + r")(?P=name_quote)\s*:\s*(?P<open>[\"'{\[])"
)

_BRACKETS: Final = "{["

#: A value some earlier step already masked: ``[REDACTED:...]``, the trace
#: markers ``[MEHO-REDACTED:...]`` / ``[MEHO-OMITTED:...]`` and ``[redacted]``.
#: A connector's own ``***`` or ``***REDACTED***`` is checked in
#: :func:`_is_placeholder`. A masked value stays, so the reader still sees
#: which step masked it.
_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(
    r"\[(?:MEHO-)?(?:REDACTED|OMITTED):[^\[\]\s'\"]+\]|\[redacted\]"
)


def _is_placeholder(value: str) -> bool:
    core = value.strip("*")
    if core == "":
        return len(value) >= 2
    if core == "REDACTED":
        return value.startswith("*") and value.endswith("*")
    return _PLACEHOLDER.fullmatch(value) is not None


def _is_secret_name(name: str) -> bool:
    # Imported here: the broadcast package imports the redaction package, so
    # a module-level import would make a cycle.
    from meho_backplane.broadcast.events import is_secret_param_name

    return is_secret_param_name(name)


class QuotedSecretField(NamedTuple):
    """A pair with a secret name: ``text[start:end]`` is the value.

    For a quoted string, ``start`` is after the opening quote and ``end`` is
    the closing quote. For an object or a list (``bracketed``), ``start`` is
    the opening bracket and ``end`` is after the closing bracket, so the
    value holds its brackets. ``escaped`` says the pair is escaped JSON
    inside a JSON string.
    """

    name: str
    value: str
    start: int
    end: int
    bracketed: bool = False
    escaped: bool = False


def _plain_value_end(text: str, start: int, quote: str) -> int:
    """The index of the closing *quote*, stepping over ``\\x`` escapes; -1 when none."""
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
        elif char == quote:
            return index
        else:
            index += 1
    return -1


def _escaped_value_end(text: str, start: int) -> int:
    """The index of the closing ``\\"`` of an escaped JSON value; -1 when none.

    Inside a JSON string, the inner JSON's own escapes are escaped once more:
    an inner ``\\"`` (a quote in the value) is ``\\\\\\"``, an inner ``\\\\`` is
    ``\\\\\\\\``, an inner ``\\n`` is ``\\\\n``. Each is stepped over as one unit.
    """
    index = start
    while index < len(text):
        if text[index] != "\\":
            index += 1
            continue
        following = text[index + 1 : index + 2]
        if following == '"':
            return index
        index += 2
        if following == "\\" and text[index : index + 2] in ("\\\\", '\\"'):
            index += 2
    return -1


def _bracket_value_end(text: str, start: int, *, escaped: bool) -> int:
    """The index after the bracket that closes the object or list at *start*; -1 when none.

    A quoted string inside is stepped over whole, so a bracket in a string
    does not count. In escaped JSON the strings are delimited by ``\\"``.
    """
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            if escaped and text[index + 1 : index + 2] == '"':
                end = _escaped_value_end(text, index + 2)
                index = len(text) if end == -1 else end + 2
            else:
                index += 2
        elif not escaped and char in ('"', "'"):
            end = _plain_value_end(text, index + 1, char)
            index = len(text) if end == -1 else end + 1
        else:
            if char in _BRACKETS:
                depth += 1
            elif char in "}]":
                depth -= 1
                if depth == 0:
                    return index + 1
            index += 1
    return -1


def _value_end(text: str, match: re.Match[str], *, bracketed: bool, escaped: bool) -> int:
    start = match.end()
    if bracketed:
        return _bracket_value_end(text, start - 1, escaped=escaped)
    if escaped:
        return _escaped_value_end(text, start)
    return _plain_value_end(text, start, match.group("open"))


def iter_quoted_secret_fields(text: str) -> Iterator[QuotedSecretField]:
    """Yield each pair in *text* whose name is a secret name, in order.

    A pair inside the value of an earlier secret-named pair is not yielded
    again. A value with no closing quote or bracket runs to the end of *text*.
    """
    cursor = 0
    for match in _PAIR_START.finditer(text):
        if match.start() < cursor:
            continue
        escaped = match.group("escaped_name") is not None
        name = match.group("escaped_name" if escaped else "name")
        if not _is_secret_name(name):
            continue
        opener = match.group("escaped_open" if escaped else "open")
        bracketed = opener in _BRACKETS
        start = match.end() - 1 if bracketed else match.end()
        end = _value_end(text, match, bracketed=bracketed, escaped=escaped)
        if end == -1:
            end = len(text)
        yield QuotedSecretField(name, text[start:end], start, end, bracketed, escaped)
        cursor = end


def scrub_secret_fields_in_text(text: str) -> str:
    """Return *text* with the value of each quoted secret-named pair replaced.

    A quoted value keeps its quotes around the marker. An object or a list is
    replaced whole by the quoted marker, as the walk does for data.
    """
    parts: list[str] = []
    cursor = 0
    for field in iter_quoted_secret_fields(text):
        if _is_empty(field) or _is_placeholder(field.value):
            continue
        parts.append(text[cursor : field.start])
        if field.bracketed:
            quote = '\\"' if field.escaped else '"'
            parts.append(f"{quote}{SECRET_FIELD_MARKER}{quote}")
        else:
            parts.append(SECRET_FIELD_MARKER)
        cursor = field.end
    parts.append(text[cursor:])
    return "".join(parts)


def _is_empty(field: QuotedSecretField) -> bool:
    """Nothing to hide: ``""``, ``{}`` or ``[]``."""
    if field.bracketed:
        return field.value[1:-1].strip() == "" and field.value[-1:] in "}]"
    return field.value == ""


def scrub_secret_fields(node: Any) -> Any:
    """Return a copy of *node* with every secret-named value replaced.

    A dict entry whose key is a secret name and whose value is a string, a
    dict or a list gets :data:`SECRET_FIELD_MARKER`. Every other string goes
    through :func:`scrub_secret_fields_in_text`. Other values stay as they are.
    """
    if isinstance(node, str):
        return scrub_secret_fields_in_text(node)
    if isinstance(node, Mapping):
        return {
            key: (
                SECRET_FIELD_MARKER
                if _is_secret_name(str(key)) and _can_hold_secret(value)
                else scrub_secret_fields(value)
            )
            for key, value in node.items()
        }
    if isinstance(node, Sequence) and not isinstance(node, (bytes, bytearray)):
        return [scrub_secret_fields(item) for item in node]
    return node


def _can_hold_secret(value: Any) -> bool:
    if isinstance(value, str):
        return not _is_placeholder(value)
    return isinstance(value, (Mapping, Sequence)) and not isinstance(value, (bytes, bytearray))
