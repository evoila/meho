# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hide the secret values in the text of a config file, line by line.

A ConfigMap often holds whole files: YAML, a properties file, an env file
or a shell script. :func:`hide_file_text` reads every ``name: value`` and
``name=value`` field of each line, also many fields on one line
(``A=1 DB_PASS=x``, ``{"user": "a", "password": "x"}``), and replaces the
value of each field with a secret name by :data:`HIDDEN`:

* A capital-letter name is read the way a shell variable is
  (:func:`~meho_backplane.redaction.command_line.is_secret_shaped_name`,
  so ``DB_PASS`` counts). Any other name uses the name rule of
  :func:`~meho_backplane.broadcast.events.is_secret_param_name`.
* A quoted value is the text inside its quotes, commas and brackets
  included. Text glued right after the closing quote is part of the value.
* An unquoted value runs to the end of the line. After ``=`` (a properties
  or env file) any text is a value, ``password=[x]`` too. After ``:`` a
  ``{`` or ``[`` starts a YAML or JSON map or list: its own fields are read
  as fields of their own.
* In a shell script (a text that starts with ``#!``), the value after ``=``
  is one shell word, quotes and escapes included: the command after it
  stays (``DB_PASS=x ./run --mode fast`` keeps ``./run --mode fast``). A
  space right after the ``=`` gives the variable an empty value.

A value may go on below its line. These lines are hidden too, each one
whole, with its indent kept:

* After ``name:``, the lines indented more than the line of the name: a YAML
  block text (``name: |``, the marker stays), a plain YAML text that goes on
  on the next lines, or a value that starts on the next line. When nothing
  follows ``name:`` on its line, YAML list items at the same indent
  (``- x``) are the value too, and a map below it (its first line is a
  field, ``file: x``) has its fields read on their own.
* After a value that ends with a ``\\`` (a properties file or a shell
  script), the next line.
* After a quote that is not closed on its line, the lines up to the one
  that closes it.

A field without a value (``password:`` with no lines below it,
``token: ''``) keeps its line. A text that is a whole JSON object is read
as JSON by the callers, not here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Final

from meho_backplane.broadcast.events import is_secret_param_name
from meho_backplane.redaction.command_line import HIDDEN, SHELL_WORD, is_secret_shaped_name

__all__ = ["hide_file_text"]

#: The name and the separator of one field: ``name:``, ``name=``, ``"name":``.
_FIELD_NAME = re.compile(
    r"""(?<![A-Za-z0-9_.-])(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)\\?["']?[ \t]*(?P<sep>[:=])[ \t]*"""
)

#: A quoted value at the start of the rest of a line.
_QUOTED = re.compile(r""""(?P<double>(?:[^"\\]|\\.)*)"|'(?P<single>[^']*)'""")

#: What may follow a closing quote: the value ends there.
_AFTER_QUOTE: Final[str] = " \t,;}]"

#: A YAML block text marker: ``|``, ``>``, with its options and a comment.
_BLOCK_MARKER = re.compile(r"[|>][-+0-9]*[ \t]*(?:#.*)?")

#: A variable name written the way a shell variable is: capital letters.
_SHELL_VARIABLE = re.compile(r"[A-Z][A-Z0-9_]*")

#: A YAML list item at the start of a line (after its indent).
_LIST_ITEM = re.compile(r"-(?:[ \t]|$)")

#: A line that starts a field of a YAML map, also as a list item: ``file: x``,
#: ``- name: x``.
_MAP_FIELD = re.compile(
    r"""[ \t]*(?:-[ \t]+)?["']?[A-Za-z_][A-Za-z0-9_.-]*["']?[ \t]*:(?:[ \t]|$)"""
)


@dataclass(frozen=True)
class _GoesOn:
    """How the value of the last hidden field of a line goes on below it."""

    #: Lines indented more than this are the value; ``None``: no such lines.
    indent: int | None = None
    #: Lines at :attr:`indent` that start a YAML list item are the value too.
    list_items: bool = False
    #: The next line is the value: the line ended with a ``\\``.
    backslash: bool = False
    #: Lines up to the one that holds this quote are the value.
    quote: str | None = None
    #: Nothing followed ``name:`` on its line: when the first line below starts
    #: a map field, the value is a map whose fields are read on their own.
    may_be_map: bool = False


@lru_cache(maxsize=4096)
def _is_secret_name(name: str) -> bool:
    if _SHELL_VARIABLE.fullmatch(name):
        return is_secret_shaped_name(name)
    return is_secret_param_name(name)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _ends_with_backslash(text: str) -> bool:
    """True when *text* ends with an odd number of ``\\``: the line goes on."""
    return (len(text) - len(text.rstrip("\\"))) % 2 == 1


def _open_quote(word: str) -> str | None:
    """The quote a shell word leaves open at its end, or ``None``."""
    quote: str | None = None
    escaped = False
    for char in word:
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote is None and char in "\"'":
            quote = char
        elif char == quote:
            quote = None
    return quote


def _unquoted_goes_on(value: str, *, sep: str, line: str, shell: bool) -> _GoesOn:
    """How an unquoted *value* that ends its line goes on below it."""
    quote = _open_quote(value) if shell else None
    if not shell and value[:1] in "\"'" and value[0] not in value[1:]:
        quote = value[0]
    return _GoesOn(
        indent=_indent(line) if sep == ":" else None,
        backslash=_ends_with_backslash(value),
        quote=quote,
    )


def _hide_quoted(line: str, start: int) -> tuple[str, int] | None:
    """*line* with the quoted value at *start* hidden, and where to read on; ``None``: no quote."""
    quoted = _QUOTED.match(line, start)
    if quoted is None or (quoted.end() < len(line) and line[quoted.end()] not in _AFTER_QUOTE):
        return None
    value = quoted["double"] if quoted["double"] is not None else quoted["single"]
    if not value.strip():
        return line, start + 2 + len(value)
    return f"{line[: start + 1]}{HIDDEN}{line[quoted.end() - 1 :]}", start + 2 + len(HIDDEN)


def _hide_unquoted(
    line: str, start: int, sep: str, *, shell: bool
) -> tuple[str, int | None, _GoesOn | None]:
    """*line* with the unquoted value at *start* hidden.

    Also returned: where to read on in the line (``None`` when the value ends
    the line) and how the value goes on below it.
    """
    rest = line[start:]
    if not rest.strip():
        if sep == ":":
            return line, None, _GoesOn(indent=_indent(line), list_items=True, may_be_map=True)
        return line, start, None
    if sep == ":" and rest[0] in "{[":
        return line, start, None
    if sep == ":" and _BLOCK_MARKER.fullmatch(rest.rstrip()) is not None:
        return line, None, _GoesOn(indent=_indent(line))
    if shell and sep == "=":
        word = SHELL_WORD.match(line, start)
        if word is None:
            return line, start, None
        if word.end() < len(line):
            return f"{line[:start]}{HIDDEN}{line[word.end() :]}", start + len(HIDDEN), None
        rest = word[0]
    return f"{line[:start]}{HIDDEN}", None, _unquoted_goes_on(rest, sep=sep, line=line, shell=shell)


def _empty_assignment(found: re.Match[str]) -> bool:
    """True for a shell ``NAME=`` with a space after the ``=``: the value is empty."""
    return found["sep"] == "=" and found.end() > found.end("sep")


def _hide_line(line: str, *, shell: bool) -> tuple[str, _GoesOn | None]:
    """*line* with its secret values hidden, and how the last hidden value goes on below it."""
    pos = 0
    while (found := _FIELD_NAME.search(line, pos)) is not None:
        start = pos = found.end()
        if not _is_secret_name(found["name"]) or (shell and _empty_assignment(found)):
            continue
        quoted = _hide_quoted(line, start)
        if quoted is not None:
            line, pos = quoted
            continue
        line, read_on, goes_on = _hide_unquoted(line, start, found["sep"], shell=shell)
        if read_on is None:
            return line, goes_on
        pos = read_on
    return line, None


def _goes_on(state: _GoesOn, line: str) -> bool:
    """True when *line* is a part of the value *state* describes."""
    if state.backslash or state.quote is not None:
        return True
    if state.indent is None:
        return False
    if not line.strip() or _indent(line) > state.indent:
        return True
    return (
        state.list_items
        and _indent(line) == state.indent
        and _LIST_ITEM.match(line, state.indent) is not None
    )


def _after(state: _GoesOn, line: str) -> _GoesOn | None:
    """How the value goes on after *line*, a part of it."""
    if state.may_be_map and line.strip():
        return replace(state, may_be_map=False)
    if state.quote is not None:
        return None if state.quote in line else state
    if state.backslash:
        if _ends_with_backslash(line):
            return state
        return replace(state, backslash=False) if state.indent is not None else None
    return state


def hide_file_text(text: str) -> str:
    """*text* with the value of every secret-named field hidden; other text as it is."""
    shell = text.startswith("#!")
    out: list[str] = []
    state: _GoesOn | None = None
    for raw in text.splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        end = raw[len(line) :]
        if state is not None and state.may_be_map and _MAP_FIELD.match(line):
            state = None
        if state is not None and _goes_on(state, line):
            out.append(f"{line[: _indent(line)]}{HIDDEN}{end}" if line.strip() else raw)
            state = _after(state, line)
            continue
        hidden, state = _hide_line(line, shell=shell)
        out.append(f"{hidden}{end}")
    return "".join(out)
