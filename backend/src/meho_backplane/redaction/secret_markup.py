# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hide secret-named values in ``key: value`` lines and in XML text.

:mod:`.secret_text` reads a text value as YAML and hides each secret-named
value the parser returns. Two forms are not such values, and this module
hides them in the text:

* :func:`hide_secret_lines` reads ``key: value`` lines. With
  ``comments_only=True`` (a text that reads as YAML) it reads only comment
  lines, such as a commented-out ``# dbPassword: ...``. Without it (a text
  that does not read as YAML) it reads every line, and a secret-named key
  whose value is a block (``password: |`` or an empty value with indented
  lines below it) loses the indented lines too.
* :func:`hide_secret_xml` reads XML tags: the text of an element with a
  secret name (``<password>...</password>``, also with child elements), an
  attribute with a secret name (``dbPassword="..."``), and the ``value``
  attribute of an element whose ``name`` or ``key`` attribute is a secret
  name (``<add key="DbPassword" value="..."/>``).

The name rule is the shared
:func:`~meho_backplane.broadcast.events.is_secret_param_name`, and for the
``value`` of a pair :func:`~meho_backplane.broadcast.events.is_secret_pair_name`.
A hidden value becomes :data:`~.secret_fields.SECRET_FIELD_MARKER`, without
quotes. An empty value, ``true``, ``false``, ``null`` and a value an earlier
step already masked stay. An element with no closing tag is not touched.

Both scans read each part of the text a fixed number of times, so the time
they take grows in line with the size of the text.
"""

from __future__ import annotations

import bisect
import re
from typing import Final

from meho_backplane.redaction.secret_fields import SECRET_FIELD_MARKER, _is_placeholder

__all__ = ["hide_secret_lines", "hide_secret_xml"]

#: One ``key: value`` line: an indent, then an optional ``#`` (a comment), an
#: optional ``- `` (a list item), the key and a colon that ends the key.
_FIELD_LINE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<indent>[ \t]*+)(?P<comment>#[ \t#]*+)?(?:-[ \t]++)?"
    r"(?P<key>[A-Za-z0-9_.\-]{1,128}+)[ \t]*+:(?=[ \t\r]|$)[ \t]*+(?P<value>[^\r\n]*)",
    re.MULTILINE,
)

#: A YAML block value: ``|`` or ``>``, with an optional chomping and indent sign.
_BLOCK_START: Final[re.Pattern[str]] = re.compile(r"[|>][-+0-9]*(?:[ \t]+#.*)?")

#: Values under a secret name that are no secret.
_KEPT_WORDS: Final[frozenset[str]] = frozenset(
    {"true", "false", "yes", "no", "on", "off", "null", "~"}
)

_XML_NAME: Final[str] = r"[A-Za-z_][\w.\-]*+(?::[A-Za-z_][\w.\-]*+)?"

#: One XML start, end or empty tag. A quoted attribute value may hold ``>``.
_XML_TAG: Final[re.Pattern[str]] = re.compile(
    r"<(?P<close>/)?(?P<name>" + _XML_NAME + r")"
    r"(?P<attrs>(?:[^<>\"']|\"[^\"]*+\"|'[^']*+')*+)>"
)

#: One attribute of a tag, with its quoted value.
_XML_ATTR: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w.\-:])(?P<name>" + _XML_NAME + r")\s*+=\s*+"
    r"(?:\"(?P<double>[^\"]*+)\"|'(?P<single>[^']*+)')"
)

#: The attributes that name the ``value`` attribute of a pair element.
_PAIR_NAME_ATTRS: Final[tuple[str, ...]] = ("name", "key")


def _is_secret_name(name: str) -> bool:
    # Imported here: the broadcast package imports the redaction package.
    from meho_backplane.broadcast.events import is_secret_param_name

    return is_secret_param_name(name)


def _is_secret_pair_name(name: str) -> bool:
    from meho_backplane.broadcast.events import is_secret_pair_name

    return is_secret_pair_name(name)


def _keeps(value: str) -> bool:
    """True when *value* under a secret name is no secret and stays."""
    core = value.strip()
    if len(core) >= 2 and core[0] == core[-1] and core[0] in "\"'":
        core = core[1:-1].strip()
    return core == "" or core.lower() in _KEPT_WORDS or _is_placeholder(core)


def _line_end(text: str, index: int) -> int:
    """The index of the line break that ends the line at *index*, or the end of *text*."""
    end = text.find("\n", index)
    return len(text) if end == -1 else end


def _content_end(text: str, line_end: int) -> int:
    """*line_end* without the ``\r`` of a ``\r\n`` line break."""
    return line_end - 1 if line_end > 0 and text[line_end - 1] == "\r" else line_end


def _block_end(text: str, line_end: int, column: int) -> int:
    """The end of the lines after *line_end* that are indented deeper than *column*.

    Blank lines inside the block count; blank lines after its last line do
    not. Returns *line_end* when no such line follows.
    """
    end = line_end
    cursor = line_end
    while cursor < len(text):
        next_end = _line_end(text, cursor + 1)
        line = text[cursor + 1 : next_end].rstrip("\r")
        if line.strip():
            if len(line) - len(line.lstrip(" \t")) <= column:
                break
            end = next_end
        cursor = next_end
    return end


def _line_value_span(text: str, match: re.Match[str]) -> tuple[int, int, str] | None:
    """The span of the value of a secret-named line and its new text; None when it stays."""
    value = match["value"].rstrip(" \t\r")
    start = match.start("value")
    if match["comment"] is None and (value == "" or _BLOCK_START.fullmatch(value)):
        line_end = _line_end(text, match.start())
        column = match.start("key") - match.start()
        block_end = _block_end(text, line_end, column)
        if block_end == line_end and value == "":
            return None
        new = SECRET_FIELD_MARKER if value else f" {SECRET_FIELD_MARKER}"
        return start, _content_end(text, block_end), new
    if _keeps(value):
        return None
    return start, start + len(value), SECRET_FIELD_MARKER


def hide_secret_lines(text: str, *, comments_only: bool) -> str:
    """Return *text* with the value of each secret-named ``key: value`` line hidden.

    With *comments_only*, only lines that start with ``#`` are read.
    """
    if ":" not in text:
        return text
    parts: list[str] = []
    cursor = 0
    for match in _FIELD_LINE.finditer(text):
        if match.start() < cursor or (comments_only and match["comment"] is None):
            continue
        if not _is_secret_name(match["key"]):
            continue
        span = _line_value_span(text, match)
        if span is None:
            continue
        start, end, new = span
        parts.append(text[cursor:start])
        parts.append(new)
        cursor = end
    if not parts:
        return text
    parts.append(text[cursor:])
    return "".join(parts)


def _local_name(name: str) -> str:
    return name.rpartition(":")[2]


def _attribute_spans(attrs: str, offset: int) -> list[tuple[int, int]]:
    """The spans of the attribute values in *attrs* that are to be hidden."""
    values: dict[str, tuple[str, int, int]] = {}
    spans: list[tuple[int, int]] = []
    for attr in _XML_ATTR.finditer(attrs):
        group = "double" if attr["double"] is not None else "single"
        name = attr["name"]
        value = attr[group]
        start, end = offset + attr.start(group), offset + attr.end(group)
        values[name] = (value, start, end)
        if _is_secret_name(_local_name(name)) and not _keeps(value):
            spans.append((start, end))
    pair_value = values.get("value")
    if pair_value is not None and not _keeps(pair_value[0]):
        for name_attr in _PAIR_NAME_ATTRS:
            named = values.get(name_attr)
            if named is not None and _is_secret_pair_name(named[0]):
                spans.append(pair_value[1:])
                break
    return spans


class _Closings:
    """The closing tags of a text by name, for a quick look-up of the next one."""

    def __init__(self, tags: list[re.Match[str]]) -> None:
        self.starts: dict[str, list[int]] = {}
        self.ends: dict[str, list[int]] = {}
        for tag in tags:
            if tag["close"]:
                self.starts.setdefault(tag["name"], []).append(tag.start())
                self.ends.setdefault(tag["name"], []).append(tag.end())

    def after(self, name: str, index: int) -> tuple[int, int] | None:
        """The span of the first ``</name>`` at or after *index*; None when there is none."""
        starts = self.starts.get(name, [])
        at = bisect.bisect_left(starts, index)
        if at == len(starts):
            return None
        return starts[at], self.ends[name][at]


def _element_spans(text: str) -> list[tuple[int, int]]:
    """The spans of the attribute values and element texts that are to be hidden."""
    tags = list(_XML_TAG.finditer(text))
    closings = _Closings(tags)
    spans: list[tuple[int, int]] = []
    cursor = 0
    for tag in tags:
        if tag.start() < cursor or tag["close"]:
            continue
        attrs = tag["attrs"]
        spans.extend(_attribute_spans(attrs, tag.start("attrs")))
        name = tag["name"]
        if attrs.rstrip().endswith("/") or not _is_secret_name(_local_name(name)):
            continue
        closing = closings.after(name, tag.end())
        if closing is None:
            continue
        if not _keeps(text[tag.end() : closing[0]]):
            spans.append((tag.end(), closing[0]))
        cursor = closing[1]
    return spans


def hide_secret_xml(text: str) -> str:
    """Return *text* with secret-named values in its XML tags hidden."""
    if "<" not in text:
        return text
    spans = _element_spans(text)
    if not spans:
        return text
    parts: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        parts.append(text[cursor:start])
        parts.append(SECRET_FIELD_MARKER)
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)
