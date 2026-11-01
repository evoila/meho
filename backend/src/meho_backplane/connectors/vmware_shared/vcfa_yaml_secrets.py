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

* A flow map or list (``{user: a, password: b}``, also a template written
  as one line of JSON, with or without spaces) that holds such a key, at
  any depth: the whole value. A flow map that runs over several lines is
  read as one value and replaced as one. A map that a bare ``{`` or ``[``
  opens (a template written as multi-line JSON) keeps its lines: each is
  checked on its own, and only when a secret is still left after that (a
  row such as ``encrypted: true, default: x``) the whole value is replaced.
* The value of every key with a secret name
  (:func:`~meho_backplane.broadcast.events.is_secret_param_name`), and of
  the ``list`` part of the cloud-init key ``chpasswd`` (it holds plain
  ``user:password`` pairs; the ``users`` part names its passwords). The
  line keeps the key and gets the marker as its value. The lines that
  belong to the value (a ``|`` or ``>`` block, a nested map or list) are
  removed.
* A double-quoted value whose ``\\n`` escapes hold more YAML lines (a
  ``cloudConfig`` written on one line) is checked like a block, and
  replaced as a whole when a line of it holds a secret.
* Every PEM private key block (``-----BEGIN ... PRIVATE KEY-----`` to its
  ``END`` line), under any key.

A secret input keeps its lines: an input under the top-level ``inputs:``
map whose name is a secret name, or any map with ``encrypted: true`` (such
as ``inputs.adminPassword``), in block form or as a flow map. It describes
the input; it does not hold the secret. Only its example, default and
fixed values (``default``, ``const``, ``enum``, ``example``, ``examples``,
``value``) are replaced; a flow map that holds one becomes the marker as a
whole. Keys may be quoted (``"encrypted"``).

:func:`scrub_inputs_schema` does the same for the inputs schema (a JSON
schema): each secret input loses its example and default values.

What stays: ``true``, ``false``, ``null``, ``~``, an empty value, a value
that is already a marker, and a plain reference to an input or a secret
such as ``${input.password}`` or ``${secret.adminPassword}``. Such a value
names a secret; it does not hold one.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from copy import deepcopy
from typing import Any, Final

from meho_backplane.broadcast.events import is_secret_param_name

__all__ = [
    "MARKER_PREFIX",
    "SECRET_MARKER",
    "is_reference",
    "scrub_inputs_schema",
    "scrub_template_yaml",
]

#: Every marker MEHO puts in place of a value starts with this text.
MARKER_PREFIX: Final[str] = "[REDACTED"

#: The value put in place of a secret-named value.
SECRET_MARKER: Final[str] = "[REDACTED:param_name]"

#: The line put in place of a PEM private key block.
PRIVATE_KEY_MARKER: Final[str] = "[REDACTED:private_key]"

#: A ``key: value`` line. A quoted or flow value may follow the ``:`` at once
#: (``"key":"value"``, JSON without spaces). A line that starts with ``{`` or
#: ``[`` is a flow map or list, not a key line (see :data:`_FLOW_LINE`).
_KEY_LINE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<lead>[ \t]*(?:-[ \t]+)*)"
    r"(?P<key>\"[^\"\n]*\"|'[^'\n]*'|[^\s#'\"\-:{\[][^:#\n]*?|-[^\s:#][^:#\n]*?)"
    r"[ \t]*:(?:(?:[ \t]+|(?=[\"'{\[]))(?P<value>.*?))?[ \t\r]*$"
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
#: The characters that open and close a flow map or list.
_FLOW_OPEN: Final[str] = "{["
_FLOW_CLOSE: Final[str] = "}]"
#: What may follow the ``:`` of a key inside a flow map (a quote: JSON
#: without spaces, ``{"password":"x"}``).
_AFTER_FLOW_COLON: Final[tuple[str, ...]] = (
    *("", " ", "\t", "\r", "\n", ",", "{", "[", "}", "]"),
    '"',
    "'",
)
#: A line that is (a list item holding) a flow map or list, or a double-quoted
#: value on its own.
_FLOW_LINE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<lead>[ \t]*(?:-[ \t]+)*)(?P<flow>[{\[\"].*?)\r?$"
)
#: One escape inside a double-quoted value.
_ESCAPE: Final[re.Pattern[str]] = re.compile(r"\\(.)")
_ESCAPED_CHARS: Final[dict[str, str]] = {"n": "\n", "r": "\r", "t": "\t"}
#: The cloud-init key whose ``list`` part holds plain ``user:password``
#: lines. Its ``users`` part names each password; ``expire`` is no secret.
_CHPASSWD: Final[str] = "chpasswd"
_CHPASSWD_SECRET_KEYS: Final[frozenset[str]] = frozenset({"list"})
#: The key that marks a map as the definition of a secret input.
_ENCRYPTED: Final[str] = "encrypted"
#: The mark of a definition that gives a value (see :func:`_flow_has_secret_key`).
_FIXED: Final[str] = "fixed"
#: A YAML true, bare or quoted (YAML 1.1 also reads ``yes`` and ``on`` as true).
_TRUE_VALUE: Final[re.Pattern[str]] = re.compile(r"(?P<q>[\"']?)(?i:true|yes|on)(?P=q)")
#: The top-level ``inputs:`` line, also quoted or opening a multi-line ``{``.
_INPUTS_LINE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<q>[\"']?)inputs(?P=q)[ \t]*:[ \t]*\{?[ \t]*(?:#.*)?\r?$"
)
#: An ``encrypted: true`` row of a block map; the key or the value may be quoted,
#: and a trailing comma (multi-line JSON) is allowed.
_ENCRYPTED_LINE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*(?P<q>[\"']?)encrypted(?P=q)[ \t]*:[ \t]*"
    + _TRUE_VALUE.pattern.replace("(?P<q>", "(?P<v>").replace("(?P=q)", "(?P=v)")
    + r"[ \t]*,?[ \t]*(?:#.*)?\r?$"
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


def _scalar_value(raw: str) -> str:
    """The value of a key line without a trailing comment.

    A quoted value ends at its closing quote; a ``#`` inside it is text.
    """
    if raw and raw[0] in "\"'":
        return raw[: _flow_scalar_end(raw, 0)]
    value = raw.split(" #", 1)[0].strip()
    return "" if value.startswith("#") else value


class _Scrub:
    """One pass over the lines of a template."""

    def __init__(self, text: str) -> None:
        self.lines = text.split("\n")
        self.out: list[str] = []
        self.count = 0
        #: The open input definitions: (index after the last line, key column).
        self.definitions: list[tuple[int, int]] = []
        #: The open cloud-init ``chpasswd`` blocks, in the same form.
        self.chpasswd: list[tuple[int, int]] = []
        #: The lines of the top-level ``inputs:`` map and the column of its keys.
        self.inputs = _input_column(self.lines)

    def run(self) -> str:
        self._run_range(0, len(self.lines))
        return "\n".join(self.out)

    def _run_range(self, start: int, stop: int) -> int:
        """Scrub the lines from *start* to *stop*; return the index after the last one read."""
        index = start
        while index < stop:
            self.definitions = [scope for scope in self.definitions if scope[0] > index]
            self.chpasswd = [scope for scope in self.chpasswd if scope[0] > index]
            following = self._key_line(index)
            if following is None:
                following = _scrub_pem(self.lines, index, self.out)
                if following is not None:
                    self.count += 1
            if following is None:
                following = self._flow_line(index)
            if following is None:
                self.out.append(self.lines[index])
                index += 1
                continue
            index = following
        return index

    def _hide(self, hidden: str) -> None:
        self.out.append(hidden)
        self.count += 1

    def _secret_key(self, key: str, key_col: int) -> bool:
        if is_secret_param_name(key):
            return True
        if key.lower() in _CHPASSWD_SECRET_KEYS and _inside(key_col, self.chpasswd):
            return True
        return key in _DEFINITION_VALUE_KEYS and _inside(key_col, self.definitions)

    def _is_input(self, index: int, key_col: int) -> bool:
        """True when the key at *index* names one input of ``inputs:``."""
        if self.inputs is None:
            return False
        first, end, col = self.inputs
        return first <= index < end and key_col == col

    def _secret_input(self, index: int, key: str, key_col: int) -> int | None:
        """Open an input definition at *index* when the input is secret.

        A secret input is a map with an ``encrypted: true`` row, wherever it
        sits, or an input of the top-level ``inputs:`` map with a secret
        name. Return the index after its lines, or ``None`` when the key
        opens no such map.
        """
        end = _value_lines_end(self.lines, index + 1, key_col, nested=True)
        rows = _map_rows(self.lines[index + 1 : end])
        if not rows:
            return None
        if any(_ENCRYPTED_LINE.match(row) for row in rows):
            return end
        if self._is_input(index, key_col) and is_secret_param_name(key):
            return end
        return None

    def _key_line(self, index: int) -> int | None:
        """Scrub a secret-named key at *index*; return the next index, or ``None``."""
        match = _KEY_LINE.match(self.lines[index])
        if match is None:
            return None
        key_col = len(match["lead"])
        key = _key_name(match["key"])
        value = _scalar_value((match["value"] or "").strip())
        opens_map = not value or value == "{"
        definition_end = self._secret_input(index, key, key_col) if opens_map else None
        if definition_end is not None:
            self.definitions.append((definition_end, key_col))
            return None
        if key.lower() == _CHPASSWD and opens_map:
            self.chpasswd.append(
                (_value_lines_end(self.lines, index + 1, key_col, nested=True), key_col)
            )
            return None
        line_end = "\r" if self.lines[index].endswith("\r") else ""
        hidden = f"{match['lead']}{match['key']}: '{SECRET_MARKER}'{line_end}"
        span = _flow_span(self.lines, index, value)
        if span is not None:
            if value in ("{", "["):
                return self._bare_flow(index, span, key, key_col, hidden)
            if self._secret_key(key, key_col) or _flow_has_secret_key(span[0], root=key):
                self._hide(hidden)
                return span[1]
            return None
        secret = (
            self._secret_key(key, key_col)
            or _flow_has_secret_key(value, root=key)
            or _escaped_text_has_secret(value)
        )
        if not secret or _KEPT_VALUE.fullmatch(value):
            return None
        end = _value_lines_end(self.lines, index + 1, key_col, nested=not value)
        if not value and end == index + 1:
            return None
        self._hide(hidden)
        return end

    def _bare_flow(
        self, index: int, span: tuple[str, int], key: str, key_col: int, hidden: str
    ) -> int:
        """Scrub a flow map or list that a bare ``{`` or ``[`` opens at *index*.

        A template written as multi-line JSON has this shape. Its lines are
        first checked one by one, like block YAML, so every line without a
        secret stays as written. When a secret is still left after that (a
        row such as ``encrypted: true, default: x`` or ``"password":"x"``),
        or when *key* itself is secret, the whole value becomes the marker.
        Returns the index after the closing bracket.
        """
        text, end = span
        opening = text.split("\n", 1)[0]
        mark, count = len(self.out), self.count
        if not self._secret_key(key, key_col):
            self.out.append(self.lines[index])
            end = max(end, self._run_range(index + 1, end))
            if not _flow_has_secret_key("\n".join([opening, *self.out[mark + 1 :]]), root=key):
                return end
            del self.out[mark:]
            self.count = count
        self._hide(hidden)
        return end

    def _flow_line(self, index: int) -> int | None:
        """Replace a flow map, list or quoted line with a secret; return the next index.

        Such a line has no key of its own: a list item ``- {password: x}``, a
        template written as one line of JSON, or a double-quoted cloud-init
        text with ``\\n`` escapes.
        """
        match = _FLOW_LINE.match(self.lines[index])
        if match is None:
            return None
        flow = match["flow"]
        line_end = "\r" if self.lines[index].endswith("\r") else ""
        hidden = f"{match['lead']}'{SECRET_MARKER}'{line_end}"
        span = _flow_span(self.lines, index, flow)
        if span is not None:
            if flow in ("{", "["):
                return self._bare_flow(index, span, "", len(match["lead"]), hidden)
            if _flow_has_secret_key(span[0]):
                self._hide(hidden)
                return span[1]
            return None
        if not (_flow_has_secret_key(flow) or _escaped_text_has_secret(flow)):
            return None
        self._hide(hidden)
        return index + 1


def _inside(key_col: int, scopes: list[tuple[int, int]]) -> bool:
    """True when a key at *key_col* is nested in one of the open *scopes*."""
    return any(key_col > col for _end, col in scopes)


def _flow_scalar_end(text: str, start: int) -> int:
    """The index after the scalar that starts at *start* of a flow map or list.

    A quoted scalar ends at its closing quote. A plain one ends at a comma,
    a bracket, a ``:`` that ends a key, or a comment; a ``${...}`` reference
    inside it is kept whole, brackets and all.
    """
    quote = text[start]
    index = start + 1 if quote in "\"'" else start
    while index < len(text):
        char = text[index]
        if quote in "\"'":
            if char == "\\" and quote == '"':
                index += 2
                continue
            if char == quote:
                if quote == "'" and text[index + 1 : index + 2] == "'":
                    index += 2
                    continue
                return index + 1
        elif char == "$" and text[index + 1 : index + 2] == "{":
            close = text.find("}", index)
            index = len(text) if close < 0 else close + 1
            continue
        elif (
            char in ",{}[]"
            or (char == ":" and text[index + 1 : index + 2] in _AFTER_FLOW_COLON)
            or (char == "#" and index > start and text[index - 1] in " \t\r\n")
        ):
            break
        index += 1
    return index


def _flow_skip(text: str, index: int) -> int:
    """The index of the next token of a flow map after spaces, commas and comments."""
    while index < len(text):
        char = text[index]
        if char in " \t\r\n,":
            index += 1
        elif char == "#" and (index == 0 or text[index - 1] in " \t\r\n"):
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline
        else:
            break
    return index


def _flow_token(text: str, index: int) -> tuple[str, int, bool]:
    """The scalar at *index* of a flow map: (text, next index, True when it is a key).

    A quoted scalar followed by ``:`` is a key whatever comes next (JSON
    without spaces: ``"encrypted":true``). A plain one needs a space, a
    comma, a bracket or a quote after the ``:``; ``http://x`` is a value.
    """
    end = max(_flow_scalar_end(text, index), index + 1)
    colon = end
    while colon < len(text) and text[colon] in " \t":
        colon += 1
    if text[colon : colon + 1] == ":" and (
        text[index] in "\"'" or text[colon + 1 : colon + 2] in _AFTER_FLOW_COLON
    ):
        return _key_name(text[index:end]), colon + 1, True
    return text[index:end], end, False


def _flow_entries(text: str, root: str) -> Iterator[tuple[tuple[str, ...], str, str]]:
    """Every ``key: value`` of a flow map, with the keys of the maps around it.

    *root* is the key whose value the text is. A nested map or list gives
    its own key an empty value, then names the frame of the entries in it.
    A list item without a key is skipped.
    """
    frames: list[str] = []
    key: str | None = None
    index = _flow_skip(text, 0)
    while index < len(text):
        char = text[index]
        if char in _FLOW_OPEN or char in _FLOW_CLOSE:
            if key is not None:
                yield (tuple(frames), key, "")
            if char in _FLOW_OPEN:
                frames.append(root if not frames else key or "")
            elif frames:
                frames.pop()
            key = None
            index += 1
        else:
            token, index, is_key = _flow_token(text, index)
            if is_key:
                key = token
            else:
                if key is not None:
                    yield (tuple(frames), key, token)
                key = None
        index = _flow_skip(text, index)
    if key is not None:
        yield (tuple(frames), key, "")


def _flow_has_secret_key(text: str, *, root: str = "") -> bool:
    """True when *text* is a flow map or list (under the key *root*) with a secret.

    That is a secret-named key with a value, the ``list`` of a ``chpasswd``
    map, or a map with ``encrypted: true`` and a default, example or fixed
    value, at any depth.
    """
    if not text or text[0] not in _FLOW_OPEN:
        return False
    marks: dict[tuple[str, ...], set[str]] = {}
    for path, key, held in _flow_entries(text, root):
        if key.lower() == _ENCRYPTED and _TRUE_VALUE.fullmatch(held):
            marks.setdefault(path, set()).add(_ENCRYPTED)
        elif _KEPT_VALUE.fullmatch(held):
            continue
        elif is_secret_param_name(key) or _chpasswd_list(key, path):
            return True
        elif key in _DEFINITION_VALUE_KEYS:
            marks.setdefault(path, set()).add(_FIXED)
        if marks.get(path) == {_ENCRYPTED, _FIXED}:
            return True
    return False


def _chpasswd_list(key: str, path: tuple[str, ...]) -> bool:
    """True for the ``list`` of a cloud-init ``chpasswd`` map, at any depth in it."""
    return key.lower() in _CHPASSWD_SECRET_KEYS and any(name.lower() == _CHPASSWD for name in path)


def _flow_depth(text: str, depth: int = 0) -> int:
    """How many flow maps or lists are still open after *text*."""
    index = _flow_skip(text, 0)
    while index < len(text):
        char = text[index]
        if char in _FLOW_OPEN or char in _FLOW_CLOSE:
            depth += 1 if char in _FLOW_OPEN else -1
            index += 1
        elif char == ":":
            index += 1
        else:
            index = max(_flow_scalar_end(text, index), index + 1)
        index = _flow_skip(text, index)
    return depth


def _flow_span(lines: list[str], index: int, opening: str) -> tuple[str, int] | None:
    """The lines of a flow map or list that *opening* starts and does not close.

    Returns the joined text and the index after its last line, or ``None``
    when *opening* is no flow value or closes on its own line. The text is
    read until every bracket is closed, or to the end.
    """
    if not opening or opening[0] not in _FLOW_OPEN:
        return None
    depth = _flow_depth(opening)
    if depth <= 0:
        return None
    end = index + 1
    while end < len(lines) and depth > 0:
        depth = _flow_depth(lines[end], depth)
        end += 1
    return "\n".join([opening, *lines[index + 1 : end]]), end


def _escaped_text_has_secret(value: str) -> bool:
    """True when *value* is a double-quoted scalar whose ``\\n`` lines hold a secret."""
    if len(value) < 2 or value[0] != '"' or value[-1] != '"' or "\\n" not in value:
        return False
    inner = _ESCAPE.sub(lambda found: _ESCAPED_CHARS.get(found[1], found[1]), value[1:-1])
    return scrub_template_yaml(inner)[1] > 0


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


def is_reference(value: Any) -> bool:
    """True when *value* names a secret or input (``${secret.<name>}``) or holds none.

    Such a value may travel in a call. ``None``, a bool and a marker count
    too; a number or any other text is a value.
    """
    if value is None or isinstance(value, bool):
        return True
    return isinstance(value, str) and _KEPT_VALUE.fullmatch(value.strip()) is not None


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
