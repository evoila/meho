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
* An unquoted value runs to the end of the line. After ``=`` (a properties,
  env or shell file) any text is a value, ``password=[x]`` too. After ``:``
  a ``{`` or ``[`` starts a YAML or JSON map or list: its own fields are
  read as fields of their own.
* After ``name: |`` or ``name: >`` (a YAML block text), the more indented
  lines below are the value and are hidden too.

A field without a value (``password:``, ``token: ''``) keeps its line.
A text that is a whole JSON object is read as JSON by the callers, not here.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Final

from meho_backplane.broadcast.events import is_secret_param_name
from meho_backplane.redaction.command_line import HIDDEN, is_secret_shaped_name

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


@lru_cache(maxsize=4096)
def _is_secret_name(name: str) -> bool:
    if _SHELL_VARIABLE.fullmatch(name):
        return is_secret_shaped_name(name)
    return is_secret_param_name(name)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _hide_line(line: str) -> tuple[str, bool]:
    """*line* with its secret values hidden, and whether a YAML block text starts below it."""
    pos = 0
    while (found := _FIELD_NAME.search(line, pos)) is not None:
        start = pos = found.end()
        if not _is_secret_name(found["name"]):
            continue
        quoted = _QUOTED.match(line, start)
        if quoted is not None and (quoted.end() == len(line) or line[quoted.end()] in _AFTER_QUOTE):
            value = quoted["double"] if quoted["double"] is not None else quoted["single"]
            if value.strip():
                line = f"{line[: start + 1]}{HIDDEN}{line[quoted.end() - 1 :]}"
            pos = start + 2 + len(HIDDEN if value.strip() else value)
            continue
        rest = line[start:]
        if not rest.strip() or (found["sep"] == ":" and rest[0] in "{["):
            continue
        if found["sep"] == ":" and _BLOCK_MARKER.fullmatch(rest.rstrip()) is not None:
            return line, True
        return f"{line[:start]}{HIDDEN}", False
    return line, False


def hide_file_text(text: str) -> str:
    """*text* with the value of every secret-named field hidden; other text as it is."""
    out: list[str] = []
    block_indent: int | None = None
    for raw in text.splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        end = raw[len(line) :]
        if block_indent is not None:
            if not line.strip() or _indent(line) > block_indent:
                out.append(f"{line[: _indent(line)]}{HIDDEN}{end}" if line.strip() else raw)
                continue
            block_indent = None
        hidden, block = _hide_line(line)
        if block:
            block_indent = _indent(line)
        out.append(f"{hidden}{end}")
    return "".join(out)
