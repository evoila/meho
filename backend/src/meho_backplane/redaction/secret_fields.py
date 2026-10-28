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
  (JSON ``"name": "value"``, Python ``'name': 'value'`` and escaped JSON
  ``\\"name\\": \\"value\\"``) and replaces the value when the name is a
  secret name.

Numbers, booleans and ``null`` under a secret name stay (``bind_secret_id:
true`` is a flag, not a secret). A value that an earlier step already
masked (``[REDACTED:...]``, a trace marker, ``***``) stays, so its label is
kept.

Used by the dispatch error path (``operations/_errors.py`` and
``operations/_vendor_text.py``) and by the flight-recorder trace scrub
(``flight_recorder/_content.py``). The normal result path does not use it:
there a field like ``password`` is removed only for a credential read
(``docs/codebase/redaction.md``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

__all__ = ["SECRET_FIELD_MARKER", "scrub_secret_fields", "scrub_secret_fields_in_text"]

#: The text put in place of a secret-named value. The same text as the
#: broadcast scrub, so a reader sees one word for "a field with a secret name".
SECRET_FIELD_MARKER: Final[str] = "[REDACTED:param_name]"

_NAME: Final[str] = r"(?P<name>[A-Za-z0-9_.\-]{1,128})"
_MAX: Final[str] = "4096"

#: Quoted name-value pairs. Each pattern names the value ``value``.
_QUOTED_PAIRS: Final[tuple[re.Pattern[str], ...]] = (
    # Escaped JSON inside a JSON string: \"name\": \"value\"
    re.compile(r'\\"' + _NAME + r'\\"\s*:\s*\\"(?P<value>(?:(?!\\").){1,' + _MAX + r'})\\"'),
    # JSON: "name": "value" (a value may hold \" and other escapes)
    re.compile(r'"' + _NAME + r'"\s*:\s*"(?P<value>(?:[^"\\]|\\.){1,' + _MAX + r'})"'),
    # Python repr: 'name': 'value'
    re.compile(r"'" + _NAME + r"'\s*:\s*'(?P<value>(?:[^'\\]|\\.){1," + _MAX + r"})'"),
)

#: A value some earlier step already masked: ``[REDACTED:...]``, the trace
#: markers ``[MEHO-REDACTED:...]`` / ``[MEHO-OMITTED:...]``, ``[redacted]``,
#: and a connector's own ``***`` or ``***REDACTED***``. It stays, so the
#: reader still sees which step masked it.
_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(
    r"\[(?:MEHO-)?(?:REDACTED|OMITTED):[^\[\]\s'\"]+\]|\[redacted\]|\*{2,}|\*+REDACTED\*+"
)


def _is_secret_name(name: str) -> bool:
    # Imported here: the broadcast package imports the redaction package, so
    # a module-level import would make a cycle.
    from meho_backplane.broadcast.events import is_secret_param_name

    return is_secret_param_name(name)


def _replace_value(match: re.Match[str]) -> str:
    value = match.group("value")
    if not _is_secret_name(match.group("name")) or _PLACEHOLDER.fullmatch(value):
        return match.group(0)
    start, end = match.span("value")
    offset = match.start()
    whole = match.group(0)
    return whole[: start - offset] + SECRET_FIELD_MARKER + whole[end - offset :]


def scrub_secret_fields_in_text(text: str) -> str:
    """Return *text* with the value of each quoted secret-named pair replaced."""
    for pattern in _QUOTED_PAIRS:
        text = pattern.sub(_replace_value, text)
    return text


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
        return _PLACEHOLDER.fullmatch(value) is None
    return isinstance(value, (Mapping, Sequence)) and not isinstance(value, (bytes, bytearray))
