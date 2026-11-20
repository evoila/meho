# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hide secret-named values in ``key: value`` lines and in XML text.

:mod:`.secret_text` reads a text value as YAML and hides each secret-named
value the parser returns. Two forms are not such values, and this module
hides them in the text:

* :func:`hide_secret_lines` reads ``key: value`` lines. The key may be in
  double or single quotes. With ``comments_only=True`` (a text that reads
  as YAML) it reads only comment lines, such as a commented-out
  ``# dbPassword: ...``. Without it (a text that does not read as YAML) it
  reads every line, and the lines below a secret-named key that are
  indented deeper are hidden with the value: a block value
  (``password: |``), a value that starts on the next line, and a value that
  goes on over more lines. A note after the value (``# rotate monthly``)
  stays.
* :func:`hide_secret_xml` reads XML tags: the text of an element with a
  secret name (``<password>...</password>``, also with child elements), an
  attribute with a secret name (``dbPassword="..."``), and the ``value``
  attribute of an element whose ``name`` or ``key`` attribute is a secret
  name (``<add key="DbPassword" value="..."/>``). A closing tag closes the
  nearest open element with its name, so a child with the same name does
  not end its parent. Tags inside CDATA are text. The text of an XML
  comment is read on its own.

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

import re
from typing import Final

from meho_backplane.redaction.secret_fields import SECRET_FIELD_MARKER, _is_placeholder

__all__ = ["hide_secret_lines", "hide_secret_xml"]

#: One ``key: value`` line: an indent, then an optional ``#`` (a comment), an
#: optional ``- `` (a list item), the key (bare, or in double or single
#: quotes) and a colon that ends the key.
_FIELD_LINE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<indent>[ \t]*+)(?P<comment>#[ \t#]*+)?(?:-[ \t]++)?"
    r"(?P<key_token>\"(?P<double_key>[^\"\r\n]{1,128}+)\"|'(?P<single_key>[^'\r\n]{1,128}+)'"
    r"|(?P<key>[A-Za-z0-9_.\-]{1,128}+))"
    r"[ \t]*+:(?=[ \t\r]|$)[ \t]*+(?P<value>[^\r\n]*)",
    re.MULTILINE,
)

#: A YAML comment after a value: ``#`` at the start or after white space.
_TRAILING_NOTE: Final[re.Pattern[str]] = re.compile(r"(?:^|[ \t]++)#")

#: Values under a secret name that are no secret.
_KEPT_WORDS: Final[frozenset[str]] = frozenset(
    {"true", "false", "yes", "no", "on", "off", "null", "~"}
)

#: Longer than any kept word or masked value, so such a value is hidden.
_KEPT_MAX_LENGTH: Final[int] = 256

_XML_NAME: Final[str] = r"[A-Za-z_][\w.\-]*+(?::[A-Za-z_][\w.\-]*+)?"

#: One XML start, end or empty tag. A quoted attribute value may hold ``>``
#: but not ``<``, as in XML, so a tag never reaches past the next ``<``.
_XML_TAG: Final[re.Pattern[str]] = re.compile(
    r"<(?P<close>/)?(?P<name>" + _XML_NAME + r")"
    r"(?P<attrs>(?:[^<>\"']|\"[^\"<]*+\"|'[^'<]*+')*+)>"
)

#: One attribute of a tag, with its quoted value.
_XML_ATTR: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w.\-:])(?P<name>" + _XML_NAME + r")\s*+=\s*+"
    r"(?:\"(?P<double>[^\"]*+)\"|'(?P<single>[^']*+)')"
)

#: The attributes that name the ``value`` attribute of a pair element.
_PAIR_NAME_ATTRS: Final[tuple[str, ...]] = ("name", "key")

_COMMENT_START: Final[str] = "<!--"
_COMMENT_END: Final[str] = "-->"
_CDATA_START: Final[str] = "<![CDATA["
_CDATA_END: Final[str] = "]]>"


def _is_secret_name(name: str) -> bool:
    # Imported here: the broadcast package imports the redaction package.
    from meho_backplane.broadcast.events import is_secret_param_name

    return is_secret_param_name(name)


def _is_secret_pair_name(name: str) -> bool:
    from meho_backplane.broadcast.events import is_secret_pair_name

    return is_secret_pair_name(name)


def _keeps(value: str) -> bool:
    """True when *value* under a secret name is no secret and stays."""
    if len(value) > _KEPT_MAX_LENGTH:
        return False
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


def _split_value(value: str) -> tuple[str, str]:
    """*value* as (the value itself, the note after it with its white space).

    A value in quotes that close on the line ends at the closing quote. Any
    other value ends where a YAML comment starts.
    """
    if value[:1] in ('"', "'"):
        close = value.find(value[0], 1)
        if close != -1:
            return value[: close + 1], value[close + 1 :]
        return value, ""
    note = _TRAILING_NOTE.search(value)
    if note is None:
        return value, ""
    return value[: note.start()], value[note.start() :]


def _key_name(match: re.Match[str]) -> str:
    for group in ("key", "double_key", "single_key"):
        if match[group] is not None:
            return match[group]
    return ""


def _line_value_span(text: str, match: re.Match[str]) -> tuple[int, int, str] | None:
    """The span of the value of a secret-named line and its new text; None when it stays.

    Outside a comment, the lines below the key that are indented deeper
    belong to the value (a block value such as ``password: |``, or a value
    that goes on over more lines) and are hidden with it. A note after the
    value (``# rotate monthly``) stays.
    """
    value, note = _split_value(match["value"].rstrip(" \t\r"))
    start = match.start("value")
    block_end = line_end = _line_end(text, match.start())
    if match["comment"] is None:
        column = match.start("key_token") - match.start()
        block_end = _block_end(text, line_end, column)
    if block_end == line_end:
        if _keeps(value):
            return None
        return start, start + len(value), SECRET_FIELD_MARKER
    lead = "" if text[start - 1] in " \t" else " "
    if note and note[0] not in " \t":
        note = " " + note
    return start, _content_end(text, block_end), f"{lead}{SECRET_FIELD_MARKER}{note}"


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
        if not _is_secret_name(_key_name(match)):
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


def _xml_tags(text: str, offset: int, spans: list[tuple[int, int]]) -> list[re.Match[str]]:
    """The start, end and empty tags of *text*, outside comments and CDATA.

    The text of each comment is read on its own, and the spans to hide in it
    go to *spans* (shifted by *offset*). A comment or CDATA section that does
    not close runs to the end of *text*.
    """
    tags: list[re.Match[str]] = []
    cursor = 0
    while (start := text.find("<", cursor)) != -1:
        if text.startswith(_COMMENT_START, start):
            inner = start + len(_COMMENT_START)
            end = text.find(_COMMENT_END, inner)
            stop = len(text) if end == -1 else end
            spans.extend(_element_spans(text[inner:stop], offset + inner))
            cursor = stop + len(_COMMENT_END)
        elif text.startswith(_CDATA_START, start):
            end = text.find(_CDATA_END, start + len(_CDATA_START))
            cursor = len(text) if end == -1 else end + len(_CDATA_END)
        elif (tag := _XML_TAG.match(text, start)) is not None:
            tags.append(tag)
            cursor = tag.end()
        else:
            cursor = start + 1
    return tags


def _element_spans(text: str, offset: int = 0) -> list[tuple[int, int]]:
    """The spans of the attribute values and element texts that are to be hidden.

    Each closing tag closes the nearest open element with its name, so a
    child with the same name does not end its parent early. Open elements
    in between have no closing tag and are not touched.
    """
    spans: list[tuple[int, int]] = []
    #: The open elements: (name, start of their text, the name is a secret name).
    stack: list[tuple[str, int, bool]] = []
    open_names: dict[str, int] = {}
    for tag in _xml_tags(text, offset, spans):
        name = tag["name"]
        if tag["close"]:
            if not open_names.get(name):
                continue
            while True:
                open_name, text_start, secret = stack.pop()
                open_names[open_name] -= 1
                if open_name == name:
                    break
            long = tag.start() - text_start > _KEPT_MAX_LENGTH
            if secret and (long or not _keeps(text[text_start : tag.start()])):
                spans.append((offset + text_start, offset + tag.start()))
            continue
        attrs = tag["attrs"]
        spans.extend(_attribute_spans(attrs, offset + tag.start("attrs")))
        if attrs.rstrip().endswith("/"):
            continue
        stack.append((name, tag.end(), _is_secret_name(_local_name(name))))
        open_names[name] = open_names.get(name, 0) + 1
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
    for start, end in sorted(spans, key=lambda span: (span[0], -span[1])):
        if start < cursor:
            continue
        parts.append(text[cursor:start])
        parts.append(SECRET_FIELD_MARKER)
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)
