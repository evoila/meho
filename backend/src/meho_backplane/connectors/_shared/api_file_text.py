# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keys, values, lines and names in the text of an API description file.

The pieces :mod:`~meho_backplane.connectors._shared.api_file_secrets`
and its collection walk share: what a secret-named key is, what a schema
stub is, where a key stands, where its value starts and ends, and how
the lines around it are read. Nothing here knows the file format; the
rules follow the text, as the module docstring of the scan explains.
"""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass
from typing import Final

from meho_backplane.broadcast.events import is_secret_param_name

#: Secret-named key tokens beyond :func:`is_secret_param_name`: a licence
#: or product key, and the ``Authorization`` header as a field (final
#: token only, so ``authorizationUrl`` is not one).
_EXTRA_FINAL_TOKENS: Final[frozenset[str]] = frozenset({"authorization"})
_EXTRA_TOKEN_PAIRS: Final[frozenset[tuple[str, str]]] = frozenset(
    {("licence", "key"), ("license", "key"), ("product", "key")}
)
_CAMEL_BOUNDARY: Final[re.Pattern[str]] = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

#: Values a schema or an example writes in place of a secret.
_STUB_WORDS: Final[frozenset[str]] = frozenset(
    {"string", "integer", "number", "boolean", "object", "array", "null", "true", "false", "~"}
)

#: A bare number: never a value inside a collection, as in the broadcast

_NAME: Final[str] = r"[\w.-]+"

#: A key and its separator. ``lead`` says where the key stands: at the
#: start of a line (after list dashes or ``?``), or after ``{`` or ``,``.
#: A quoted key may hold an escape (``"password"``). The look-behind
#: keeps a bare key at a token boundary, so ``userPassword`` is one key.
KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?P<lead>^[ \t]*(?:[-?][ \t]+)*|[{,][ \t]*)?"
    r"(?<![\w.-])"
    rf"""(?:"(?P<dq>(?:[^"\\\n]|\\.)+)"|'(?P<sq>(?:[^'\n]|'')+)'|(?P<bare>{_NAME}))"""
    r"\s*(?P<sep>[:=])(?![=>])",
    re.MULTILINE,
)

#: A dense value after a secret-named word in running text: at least 8

#: A YAML block scalar indicator with its chomping or indentation sign
#: and an optional comment, up to the end of the line.
_BLOCK_INDICATOR_RE: Final[re.Pattern[str]] = re.compile(r"[|>][-+0-9]*(?:[ \t]+#[^\n]*)?[ \t]*$")

#: A line that opens a structure, not a scalar: a mapping entry
#: (``type: string``) or a list item (``- a``).
_STRUCTURE_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"""(?:-(?:[ \t]|$)|(?:"[^"\n]*"|'[^'\n]*'|[^\s:"'#]+)[ \t]*:(?:[ \t]|$))"""
)

#: An opening tag with its name and attributes; ``close`` is ``/`` for a

_ALIAS_RE: Final[re.Pattern[str]] = re.compile(r"\*(?P<name>[\w.-]+)")


@dataclass(frozen=True, slots=True)
class SecretValue:
    """Where one secret value sits in the text, and what names it.

    ``start`` and ``end`` bound the value itself (inside its quotes, if
    any). ``kind`` is ``field``, ``element``, ``attribute`` or ``PEM
    private key``; ``name`` is the key, element or attribute name as the
    file writes it, or the key type of a PEM block. The value is never
    kept here.
    """

    start: int
    end: int
    kind: str
    name: str

    @property
    def description(self) -> str:
        """One phrase for an error message: ``the field 'password' at character 20``."""
        if self.kind == "PEM private key":
            return f"a PEM private key ({self.name}) at character {self.start}"
        return f"the {self.kind} {self.name!r} at character {self.start}"


# ---------------------------------------------------------------------------
# Names and stubs
# ---------------------------------------------------------------------------


def is_secret_name(name: str) -> bool:
    """Return whether *name* is a secret-named key, element or attribute."""
    local = name.rsplit(":", 1)[-1]
    if not local:
        return False
    if is_secret_param_name(local):
        return True
    flat = local.replace("-", "_").replace(".", "_")
    tokens = [t for t in _CAMEL_BOUNDARY.sub("_", flat).lower().split("_") if t]
    if not tokens:
        return False
    if tokens[-1] in _EXTRA_FINAL_TOKENS:
        return True
    return any(pair in _EXTRA_TOKEN_PAIRS for pair in itertools.pairwise(tokens))


def is_stub(value: str) -> bool:
    """Return whether a scalar *value* is a schema stub or placeholder, not a secret."""
    stripped = value.strip()
    if not stripped or stripped.lower() in _STUB_WORDS or stripped.startswith("[REDACTED"):
        return True
    if stripped[0] == "<" and stripped[-1] == ">":
        return True
    if stripped.startswith(("{{", "${")) and stripped.endswith("}"):
        return True
    return set(stripped) <= {"*"}


def decode_key(match: re.Match[str]) -> str:
    """Return the key *match* names, with a JSON escape or a doubled quote resolved."""
    if (dq := match.group("dq")) is not None:
        if "\\" not in dq:
            return dq
        try:
            decoded = json.loads(f'"{dq}"')
        except ValueError:
            return dq
        return decoded if isinstance(decoded, str) else dq
    if (sq := match.group("sq")) is not None:
        return sq.replace("''", "'")
    return match.group("bare")


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------


def end_of_line(text: str, pos: int) -> int:
    """Return where the line that holds *pos* ends, before its ``\\r\\n`` or ``\\n``."""
    end = text.find("\n", pos)
    if end < 0:
        return len(text)
    return end - 1 if end > pos and text[end - 1] == "\r" else end


def indent_of_line(text: str, pos: int) -> int:
    """Return the indentation of the line that holds *pos*."""
    start = text.rfind("\n", 0, pos) + 1
    end = start
    while end < len(text) and text[end] in " \t":
        end += 1
    return end - start


def is_comment(stripped: str) -> bool:
    return stripped.startswith(("#", "//", "<!--"))


def deeper_lines_end(text: str, line_end: int, indent: int) -> int:
    """Return the end of the last line below *line_end* indented deeper than *indent*.

    Blank lines inside are kept; the scan stops at the first line that is
    not blank and not indented deeper. Returns *line_end* when no such
    line follows.
    """
    end = line_end
    pos = line_end + 1
    while pos <= len(text):
        next_end = end_of_line(text, pos)
        line = text[pos:next_end]
        if line.strip(" \t\r"):
            if len(line) - len(line.lstrip(" \t")) <= indent:
                break
            end = next_end
        pos = next_end + 1
    return end


def next_content_line(text: str, pos: int) -> int | None:
    """Return where the content of the next line that is not empty or a comment starts."""
    pos = end_of_line(text, pos) + 1
    while pos <= len(text):
        end = end_of_line(text, pos)
        line = text[pos:end]
        stripped = line.strip(" \t\r")
        if stripped and not is_comment(stripped):
            return pos + len(line) - len(line.lstrip(" \t"))
        pos = end + 1
    return None


def _next_value_line(text: str, pos: int) -> int | None:
    """Return where the next line that holds a value starts, or ``None``.

    A line that opens a structure (a mapping entry, a list item, ``{`` or
    ``[``) is not a value: the key has children, as a schema property has.
    """
    start = next_content_line(text, pos)
    if start is None:
        return None
    rest = text[start : end_of_line(text, start)]
    if rest[0] in "{[" or _STRUCTURE_LINE_RE.match(rest):
        return None
    return start


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def quoted_end(text: str, pos: int) -> int:
    """Return the index of the quote that closes the string opened at *pos*.

    A ``"`` string knows backslash escapes; a ``'`` string knows the
    doubled quote. Both may run over line breaks. When no closing quote
    follows, the string ends with its line.
    """
    quote = text[pos]
    i = pos + 1
    while i < len(text):
        c = text[i]
        if quote == '"' and c == "\\":
            i += 2
            continue
        if c == quote:
            if quote == "'" and text[i + 1 : i + 2] == "'":
                i += 2
                continue
            return i
        i += 1
    return end_of_line(text, pos)


FLOW_STOP: Final[frozenset[str]] = frozenset(",}]\n")
_ATTR_STOP: Final[frozenset[str]] = frozenset(" \t\r\n&<>;,)}\"'")


def scalar_end(text: str, pos: int, stops: frozenset[str]) -> int:
    end = pos
    while end < len(text) and text[end] not in stops:
        end += 1
    return end


def resolve_value(
    text: str, pos: int, indent: int, mode: str, aliases: set[str]
) -> tuple[int, int] | None:
    """Return the span of the value that starts at *pos*, or ``None`` for a stub or structure.

    *mode* is ``block`` (a key at the start of a line, YAML or JSON),
    ``flow`` (a key after ``{`` or ``,``) or ``attr`` (a key before
    ``=``). *indent* is the indentation of the key's line. An alias found
    as a value is added to *aliases* so its anchor is resolved too.
    """
    while pos < len(text) and text[pos] in " \t":
        pos += 1
    rest = text[pos : end_of_line(text, pos)]
    if not rest or is_comment(rest):
        if mode == "attr":
            return None
        next_pos = _next_value_line(text, pos)
        return None if next_pos is None else resolve_value(text, next_pos, indent, mode, aliases)
    if rest[0] in "{[":
        return None
    if rest[0] in "\"'":
        close = quoted_end(text, pos)
        return None if is_stub(text[pos + 1 : close]) else (pos + 1, close)
    if mode != "attr" and rest[0] in "!&*|>":
        return _yaml_value(text, pos, indent, mode, aliases)
    return _plain_value(text, pos, indent, mode)


def _yaml_value(
    text: str, pos: int, indent: int, mode: str, aliases: set[str]
) -> tuple[int, int] | None:
    """A value that opens with a YAML marker: tag, anchor, alias or block indicator.

    A tag or an anchor is skipped and the value behind it resolved. An
    alias is a value, and its name is kept for the anchor pass. A block
    scalar is the indented lines below the key.
    """
    rest = text[pos : end_of_line(text, pos)]
    if rest[0] in "!&":
        return resolve_value(text, pos + len(rest.split(None, 1)[0]), indent, mode, aliases)
    if rest[0] == "*":
        alias = _ALIAS_RE.match(rest)
        if alias is None:
            return _plain_value(text, pos, indent, mode)
        aliases.add(alias.group("name"))
        end = scalar_end(text, pos, FLOW_STOP) if mode == "flow" else pos + len(rest)
        return (pos, end)
    if mode != "block" or not _BLOCK_INDICATOR_RE.match(rest):
        return _plain_value(text, pos, indent, mode)
    line_end = pos + len(rest)
    body_end = deeper_lines_end(text, line_end, indent)
    body_start = _block_body_start(text, line_end, body_end)
    if body_start is None or is_stub(text[body_start:body_end]):
        return None
    return (body_start, body_end)


def _block_body_start(text: str, line_end: int, body_end: int) -> int | None:
    """Return where the text of a block scalar starts, or ``None`` for an empty block.

    The block is the lines after *line_end* up to *body_end*. Its first
    line that is not blank starts the text. Every line is text: a
    ``key: value``, a ``- item`` or a ``# comment`` inside a block scalar
    is part of the password, not a mapping, a list or a comment.
    """
    pos = line_end + 1
    while pos <= body_end:
        end = end_of_line(text, pos)
        line = text[pos:end]
        if line.strip(" \t\r"):
            return pos + len(line) - len(line.lstrip(" \t"))
        pos = end + 1
    return None


def _plain_value(text: str, pos: int, indent: int, mode: str) -> tuple[int, int] | None:
    """A bare value: to a stop character, or to the end of its line and the lines below it."""
    if mode == "attr":
        end = scalar_end(text, pos, _ATTR_STOP)
    elif mode == "flow":
        end = scalar_end(text, pos, FLOW_STOP)
    else:
        end = deeper_lines_end(text, end_of_line(text, pos), indent)
    core = text[pos:end].split(" #", 1)[0]
    return None if is_stub(core) else (pos, end)
