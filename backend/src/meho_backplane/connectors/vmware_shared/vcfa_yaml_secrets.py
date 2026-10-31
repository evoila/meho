# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hide secret values in cloud template YAML, line by line.

``*.blueprint.get`` returns the template YAML. A template can hold a
password or a private key (``remoteAccess.password``, ``privateKey``, a
``passwd:`` line inside a ``cloudConfig`` block). This module replaces such
values with a marker before the YAML leaves MEHO.

It works on lines, not on the parsed YAML, for two reasons:

* A ``cloudConfig`` block is a text value that holds more YAML. Its lines
  are checked like the lines around it.
* Every other line comes back exactly as written (comments and order too),
  so an agent can change the YAML it read and send it back.

What is replaced:

* The value of every key with a secret name
  (:func:`~meho_backplane.broadcast.events.is_secret_param_name`). The line
  keeps the key and gets the marker as its value. The lines that belong to
  the value (a ``|`` or ``>`` block, a nested map or list) are removed.
* Every PEM private key block (``-----BEGIN ... PRIVATE KEY-----`` to its
  ``END`` line), under any key.

One kind of secret-named key keeps its lines: an input definition such as
``inputs.adminPassword`` (a map with a ``type`` key). It describes the
input; it does not hold the secret. Only its example and default values
(``default``, ``const``, ``enum``, ``example``, ``examples``) are replaced.

What stays: ``true``, ``false``, ``null``, ``~``, an empty value, a value
that is already a marker, and a plain reference to an input or a secret
such as ``${input.password}`` or ``${secret.adminPassword}``. Such a value
names a secret; it does not hold one.
"""

from __future__ import annotations

import re
from typing import Final

from meho_backplane.broadcast.events import is_secret_param_name

__all__ = ["MARKER_PREFIX", "SECRET_MARKER", "scrub_template_yaml"]

#: Every marker MEHO puts in place of a value starts with this text.
MARKER_PREFIX: Final[str] = "[REDACTED"

#: The value put in place of a secret-named value.
SECRET_MARKER: Final[str] = "[REDACTED:param_name]"

#: The line put in place of a PEM private key block.
PRIVATE_KEY_MARKER: Final[str] = "[REDACTED:private_key]"

_KEY_LINE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<lead>[ \t]*(?:-[ \t]+)*)"
    r"(?P<key>\"[^\"\n]*\"|'[^'\n]*'|[^\s#'\"\-:][^:#\n]*?|-[^\s:#][^:#\n]*?)"
    r"[ \t]*:(?:[ \t]+(?P<value>.*?))?[ \t]*$"
)
_PEM_BEGIN: Final[re.Pattern[str]] = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----")
_PEM_END: Final[re.Pattern[str]] = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----")
_KEPT_VALUE: Final[re.Pattern[str]] = re.compile(
    r"(?i:true|false|yes|no|on|off|null|~)"
    r"|(?P<q>['\"]?)\$\{\s*(?:input|secret|env|propgroup)\.[A-Za-z0-9_.\-]+\s*\}(?P=q)"
    r"|(?P<m>['\"]?)\[REDACTED:[^\]\s]+\](?P=m)"
)


#: The keys of an input definition that can hold a value.
_SCHEMA_VALUE_KEYS: Final[frozenset[str]] = frozenset(
    {"default", "const", "enum", "example", "examples"}
)
_TYPE_LINE: Final[re.Pattern[str]] = re.compile(r"^[ \t]*type[ \t]*:")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _key_name(raw: str) -> str:
    return raw[1:-1] if raw[:1] in "\"'" else raw


def _value_lines_end(lines: list[str], start: int, key_col: int, *, nested: bool) -> int:
    """The index after the last line that belongs to the value of a key.

    A line belongs to the value when it is indented deeper than the key.
    With an empty value (*nested*), a list written at the key's own indent
    (``key:`` then ``- item``) belongs to it too. A blank line belongs to the
    value only when a later line still does.
    """
    end = start
    index = start
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        col = _indent(line)
        if col > key_col or (nested and col == key_col and line.lstrip().startswith("- ")):
            index += 1
            end = index
            continue
        break
    return end


def _is_input_definition(children: list[str]) -> bool:
    """True when the nested lines are a map with a ``type`` key at its top."""
    rows = [line for line in children if line.strip()]
    if not rows or rows[0].lstrip().startswith("-"):
        return False
    top = _indent(rows[0])
    return any(_indent(line) == top and _TYPE_LINE.match(line) for line in rows)


class _Scrub:
    """One pass over the lines of a template."""

    def __init__(self, text: str) -> None:
        self.lines = text.split("\n")
        self.out: list[str] = []
        self.count = 0
        #: The open input definitions: (index after the last line, key column).
        self.definitions: list[tuple[int, int]] = []

    def run(self) -> str:
        index = 0
        while index < len(self.lines):
            self.definitions = [scope for scope in self.definitions if scope[0] > index]
            following = self._key_line(index)
            if following is None:
                following = _scrub_pem(self.lines, index, self.out)
            if following is None:
                self.out.append(self.lines[index])
                index += 1
                continue
            self.count += 1
            index = following
        return "\n".join(self.out)

    def _secret_key(self, key: str, key_col: int) -> bool:
        if is_secret_param_name(key):
            return True
        return key in _SCHEMA_VALUE_KEYS and any(key_col > col for _end, col in self.definitions)

    def _key_line(self, index: int) -> int | None:
        """Scrub a secret-named key at *index*; return the next index, or ``None``."""
        match = _KEY_LINE.match(self.lines[index])
        if match is None:
            return None
        key_col = len(match["lead"])
        if not self._secret_key(_key_name(match["key"]), key_col):
            return None
        value = (match["value"] or "").split(" #", 1)[0].strip()
        if value.startswith("#"):
            value = ""
        if _KEPT_VALUE.fullmatch(value):
            return None
        end = _value_lines_end(self.lines, index + 1, key_col, nested=not value)
        if not value and end == index + 1:
            return None
        if not value and _is_input_definition(self.lines[index + 1 : end]):
            self.definitions.append((end, key_col))
            return None
        self.out.append(f"{match['lead']}{match['key']}: '{SECRET_MARKER}'")
        return end


def _scrub_pem(lines: list[str], index: int, out: list[str]) -> int | None:
    """Replace a PEM private key block that starts at *index*; return the next index."""
    begin = _PEM_BEGIN.search(lines[index])
    if begin is None:
        return None
    prefix = lines[index][: begin.start()]
    end = index
    while end < len(lines) and _PEM_END.search(lines[end]) is None:
        end += 1
    marker = PRIVATE_KEY_MARKER if not prefix.strip() else f"'{PRIVATE_KEY_MARKER}'"
    out.append(prefix + marker)
    return min(end + 1, len(lines))


def scrub_template_yaml(text: str) -> tuple[str, int]:
    """Return *text* with its secret values replaced, and how many were replaced."""
    scrub = _Scrub(text)
    copy = scrub.run()
    return copy, scrub.count
