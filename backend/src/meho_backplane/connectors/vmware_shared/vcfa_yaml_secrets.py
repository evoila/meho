# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hide secret values in cloud template YAML and in its inputs schema.

``*.blueprint.get`` returns the template YAML and the inputs schema. A
template can hold a password or a private key (``remoteAccess.password``,
``privateKey``, a ``passwd:`` or ``chpasswd:`` part inside a ``cloudConfig``
block, the default of a password input). This module replaces such values
with a marker before they leave MEHO.

The YAML is checked line by line (:func:`scrub_template_yaml`), not as
parsed YAML, for two reasons:

* A ``cloudConfig`` block is a text value that holds more YAML. Its lines
  are checked like the lines around it.
* Every other line comes back exactly as written (comments and order too),
  so an agent can change the YAML it read and send it back.

What is replaced:

* A one-line flow map or list (``{user: a, password: b}``, also a template
  written as one line of JSON) that holds such a key: the whole value.
* The value of every key with a secret name
  (:func:`~meho_backplane.broadcast.events.is_secret_param_name`), and of
  the cloud-init key ``chpasswd`` (its ``list`` or ``users`` part holds
  plain ``user:password`` pairs). The line keeps the key and gets the
  marker as its value. The lines that belong to the value (a ``|`` or
  ``>`` block, a nested map or list) are removed.
* Every PEM private key block (``-----BEGIN ... PRIVATE KEY-----`` to its
  ``END`` line), under any key.

A secret input keeps its lines: an input under the top-level ``inputs:``
map whose name is a secret name, or which has ``encrypted: true`` (such as
``inputs.adminPassword``). It describes the input; it does not hold the
secret. Only its example, default and fixed values (``default``,
``const``, ``enum``, ``example``, ``examples``, ``value``) are replaced.

:func:`scrub_inputs_schema` does the same for the inputs schema (a JSON
schema): each secret input loses its example and default values.

What stays: ``true``, ``false``, ``null``, ``~``, an empty value, a value
that is already a marker, and a plain reference to an input or a secret
such as ``${input.password}`` or ``${secret.adminPassword}``. Such a value
names a secret; it does not hold one.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Final

from meho_backplane.broadcast.events import is_secret_param_name

__all__ = ["MARKER_PREFIX", "SECRET_MARKER", "scrub_inputs_schema", "scrub_template_yaml"]

#: Every marker MEHO puts in place of a value starts with this text.
MARKER_PREFIX: Final[str] = "[REDACTED"

#: The value put in place of a secret-named value.
SECRET_MARKER: Final[str] = "[REDACTED:param_name]"

#: The line put in place of a PEM private key block.
PRIVATE_KEY_MARKER: Final[str] = "[REDACTED:private_key]"

_KEY_LINE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<lead>[ \t]*(?:-[ \t]+)*)"
    r"(?P<key>\"[^\"\n]*\"|'[^'\n]*'|[^\s#'\"\-:][^:#\n]*?|-[^\s:#][^:#\n]*?)"
    r"[ \t]*:(?:[ \t]+(?P<value>.*?))?[ \t\r]*$"
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
#: In the YAML, an input definition can also give a fixed ``value``.
_DEFINITION_VALUE_KEYS: Final[frozenset[str]] = _SCHEMA_VALUE_KEYS | {"value"}
#: A key inside a one-line flow map or list (``{user: a, password: b}``).
_FLOW_KEY: Final[re.Pattern[str]] = re.compile(
    r"[{,\[][ \t]*(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)'|(?P<plain>[^\s{}\[\],:'\"]+))[ \t]*:"
    r"(?=[ \t]*(?P<value>'[^']*'|\"[^\"]*\"|\$\{[^}]*\}|[^,{}\[\]]*))"
)
#: A line that is (a list item holding) a flow map or list.
_FLOW_LINE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<lead>[ \t]*(?:-[ \t]+)*)(?P<flow>[{\[].*?)\r?$"
)
#: Cloud-init keys whose whole value is secret, in lower case. ``chpasswd``
#: holds ``user:password`` lines (``list``) or users with a ``password``.
_CLOUD_INIT_SECRET_KEYS: Final[frozenset[str]] = frozenset({"chpasswd"})
_INPUTS_LINE: Final[re.Pattern[str]] = re.compile(r"^inputs[ \t]*:[ \t]*(?:#.*)?\r?$")
_ENCRYPTED_LINE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*encrypted[ \t]*:[ \t]*(?i:true)[ \t]*(?:#.*)?\r?$"
)


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


def _map_rows(children: list[str]) -> list[str]:
    """The top-level lines of the nested map in *children* (empty for a list)."""
    rows = [line for line in children if line.strip()]
    if not rows or rows[0].lstrip().startswith("-"):
        return []
    top = _indent(rows[0])
    return [line for line in rows if _indent(line) == top]


def _input_column(lines: list[str]) -> tuple[int, int, int] | None:
    """Where the inputs sit: (first line, index after the last line, column)."""
    for index, line in enumerate(lines):
        if _INPUTS_LINE.match(line):
            end = _value_lines_end(lines, index + 1, 0, nested=True)
            rows = [row for row in lines[index + 1 : end] if row.strip()]
            return (index + 1, end, _indent(rows[0])) if rows else None
    return None


class _Scrub:
    """One pass over the lines of a template."""

    def __init__(self, text: str) -> None:
        self.lines = text.split("\n")
        self.out: list[str] = []
        self.count = 0
        #: The open input definitions: (index after the last line, key column).
        self.definitions: list[tuple[int, int]] = []
        #: The lines of the top-level ``inputs:`` map and the column of its keys.
        self.inputs = _input_column(self.lines)

    def run(self) -> str:
        index = 0
        while index < len(self.lines):
            self.definitions = [scope for scope in self.definitions if scope[0] > index]
            following = self._key_line(index)
            if following is None:
                following = _scrub_pem(self.lines, index, self.out)
            if following is None:
                following = _scrub_flow_line(self.lines, index, self.out)
            if following is None:
                self.out.append(self.lines[index])
                index += 1
                continue
            self.count += 1
            index = following
        return "\n".join(self.out)

    def _secret_key(self, key: str, key_col: int) -> bool:
        if is_secret_param_name(key) or key.lower() in _CLOUD_INIT_SECRET_KEYS:
            return True
        return key in _DEFINITION_VALUE_KEYS and any(
            key_col > col for _end, col in self.definitions
        )

    def _is_input(self, index: int, key_col: int) -> bool:
        """True when the key at *index* names one input of ``inputs:``."""
        if self.inputs is None:
            return False
        first, end, col = self.inputs
        return first <= index < end and key_col == col

    def _secret_input(self, index: int, key: str, key_col: int) -> int | None:
        """Open an input definition at *index* when the input is secret.

        Return the index after its lines, or ``None`` when it is no secret
        input with nested lines.
        """
        if not self._is_input(index, key_col):
            return None
        end = _value_lines_end(self.lines, index + 1, key_col, nested=True)
        rows = _map_rows(self.lines[index + 1 : end])
        if not rows:
            return None
        if is_secret_param_name(key) or any(_ENCRYPTED_LINE.match(row) for row in rows):
            return end
        return None

    def _key_line(self, index: int) -> int | None:
        """Scrub a secret-named key at *index*; return the next index, or ``None``."""
        match = _KEY_LINE.match(self.lines[index])
        if match is None:
            return None
        key_col = len(match["lead"])
        key = _key_name(match["key"])
        value = (match["value"] or "").split(" #", 1)[0].strip()
        if value.startswith("#"):
            value = ""
        definition_end = None if value else self._secret_input(index, key, key_col)
        if definition_end is not None:
            self.definitions.append((definition_end, key_col))
            return None
        secret = self._secret_key(key, key_col) or _flow_has_secret_key(value)
        if not secret or _KEPT_VALUE.fullmatch(value):
            return None
        end = _value_lines_end(self.lines, index + 1, key_col, nested=not value)
        if not value and end == index + 1:
            return None
        line_end = "\r" if self.lines[index].endswith("\r") else ""
        self.out.append(f"{match['lead']}{match['key']}: '{SECRET_MARKER}'{line_end}")
        return end


def _flow_has_secret_key(value: str) -> bool:
    """True when *value* is a one-line flow map or list with a secret-named key."""
    if value[:1] not in "{[":
        return False
    for found in _FLOW_KEY.finditer(value):
        name = found["dq"] if found["dq"] is not None else found["sq"] or found["plain"] or ""
        secret = is_secret_param_name(name) or name.lower() in _CLOUD_INIT_SECRET_KEYS
        if secret and not _KEPT_VALUE.fullmatch(found["value"].strip()):
            return True
    return False


def _scrub_flow_line(lines: list[str], index: int, out: list[str]) -> int | None:
    """Replace a flow map or list line with a secret-named key; return the next index.

    Such a line has no key of its own: a list item ``- {password: x}``, or a
    template written as one line of JSON.
    """
    match = _FLOW_LINE.match(lines[index])
    if match is None or not _flow_has_secret_key(match["flow"]):
        return None
    line_end = "\r" if lines[index].endswith("\r") else ""
    out.append(f"{match['lead']}'{SECRET_MARKER}'{line_end}")
    return index + 1


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


def _hide_schema_values(node: dict[str, Any]) -> int:
    """Replace the example and default values of one input; return the count."""
    count = 0
    for key in _SCHEMA_VALUE_KEYS:
        if node.get(key) is not None:
            node[key] = SECRET_MARKER
            count += 1
    return count


def _scrub_schema_node(node: Any, *, secret: bool) -> int:
    """Walk one schema node; hide the values of every secret input in it."""
    if not isinstance(node, dict):
        return 0
    count = _hide_schema_values(node) if secret else 0
    properties = node.get("properties")
    if isinstance(properties, dict):
        for name, child in properties.items():
            child_secret = (
                secret
                or is_secret_param_name(str(name))
                or (isinstance(child, dict) and child.get("encrypted") is True)
            )
            count += _scrub_schema_node(child, secret=child_secret)
    count += _scrub_schema_node(node.get("items"), secret=secret)
    return count


def scrub_inputs_schema(schema: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Return a copy of *schema* with secret input values replaced, and the count.

    An input is secret when its name is a secret name or it has
    ``encrypted: true``. Its ``default``, ``const``, ``enum``, ``example``
    and ``examples`` become a marker. Inputs nested in an object
    (``properties``) or a list (``items``) are checked too.
    """
    copy = deepcopy(schema)
    return copy, _scrub_schema_node(copy, secret=False)
