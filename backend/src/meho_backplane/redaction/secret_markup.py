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
  goes on over more lines. In a commented-out line, the comment lines below
  whose text after the ``#`` is indented deeper than the key are hidden
  with the value. A byte order mark at the start of the text is skipped.
  A quoted value ends at its closing quote, also
  when it holds an escaped quote (``\\"`` or ``''``). A note after the value
  (``# rotate monthly``) stays, and so does a comment line below the key
  that is not part of a block value or of a quoted value that is still
  open. A secret-named value in such a comment line is hidden too.
* :func:`hide_secret_xml` reads XML tags: the text of an element with a
  secret name (``<password>...</password>``, also with child elements), an
  attribute with a secret name (``dbPassword="..."``), and the ``value``
  attribute and the text of an element whose ``name`` or ``key`` attribute
  is a secret name (``<add key="DbPassword" value="..."/>``), and the text
  of a ``<value>`` child of an element whose ``<name>`` child is a secret
  name. The name of such a pair is read as XML text first: CDATA and
  character references count. In a JSON text, each string is read on its
  own, as text: its escapes are read before its XML, and only the hidden
  value changes in the JSON. In a YAML text, each value (with its key on
  the same entry) and each comment is read on its own, so a start tag and
  a closing tag in two different values are no element. A text that reads
  as neither is read as XML whole. A closing tag closes the
  nearest open element with its name, so a child with the same name does
  not end its parent. Tags inside CDATA are text. The text of an XML
  comment is read on its own, in one pass: comments do not nest.

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

import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Final

import yaml

from meho_backplane.redaction.secret_fields import SECRET_FIELD_MARKER, _is_placeholder

__all__ = ["hide_secret_lines", "hide_secret_xml"]

#: One ``key: value`` line: a byte order mark at the start of the text, an
#: indent, then an optional ``#`` (a comment), an optional ``- `` (a list
#: item), the key (bare, or in double or single quotes) and a colon that ends
#: the key.
_FIELD_LINE: Final[re.Pattern[str]] = re.compile(
    r"^(?:\A\ufeff)?(?P<indent>[ \t]*+)(?P<comment>#[ \t#]*+)?(?:-[ \t]++)?"
    r"(?P<key_token>\"(?P<double_key>[^\"\r\n]{1,128}+)\"|'(?P<single_key>[^'\r\n]{1,128}+)'"
    r"|(?P<key>[A-Za-z0-9_.\-]{1,128}+))"
    r"[ \t]*+:(?=[ \t\r]|$)[ \t]*+(?P<value>[^\r\n]*)",
    re.MULTILINE,
)

#: A YAML comment after a value: ``#`` at the start or after white space.
#: The white space before it is found by stepping back, so a long run of
#: spaces is read once.
_TRAILING_NOTE: Final[re.Pattern[str]] = re.compile(r"(?<![^ \t])#")

#: A value in quotes that close on the line: ``\"`` inside double quotes and
#: ``''`` inside single quotes are escaped quotes, not the closing quote.
_QUOTED_VALUE: Final[re.Pattern[str]] = re.compile(r"\"(?:[^\"\\]|\\.)*+\"|'(?:[^']|'')*+'")

#: The rest of a quoted value that goes on over more lines, up to and with
#: its closing quote, by the quote that opened it.
_QUOTE_CLOSE: Final[dict[str, re.Pattern[str]]] = {
    '"': re.compile(r"(?:[^\"\\]|\\.)*+\""),
    "'": re.compile(r"(?:[^']|'')*+'"),
}

#: A string in a JSON text.
_JSON_STRING: Final[re.Pattern[str]] = re.compile(r"\"(?:[^\"\\]|\\.)*+\"")

#: ``<`` written as an escape in a JSON string.
_JSON_ESCAPED_LT: Final[re.Pattern[str]] = re.compile(r"\\u003[cC]")

#: The character of each short escape in a JSON string.
_JSON_ESCAPES: Final[dict[str, str]] = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}

#: The parser of the YAML step, the fast one when it is built in.
_LOADER: Final[Any] = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

#: A YAML comment: ``#`` at the start of a line or after white space, to the
#: end of the line.
_YAML_COMMENT: Final[re.Pattern[str]] = re.compile(r"(?:^|(?<=[ \t]))#[^\r\n]*+", re.MULTILINE)

#: The indicator of a YAML block value, such as ``|``, ``>-`` or ``|2+``.
_BLOCK_INDICATOR: Final[re.Pattern[str]] = re.compile(r"[|>][0-9+\-]{0,2}")

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

#: A CDATA section, a character reference or a predefined entity in XML text.
_XML_TEXT_PART: Final[re.Pattern[str]] = re.compile(
    r"<!\[CDATA\[(?P<cdata>.*?)\]\]>|&#(?:(?P<decimal>[0-9]{1,7})|x(?P<hex>[0-9A-Fa-f]{1,6}));"
    r"|&(?P<entity>lt|gt|amp|quot|apos);",
    re.DOTALL,
)

_XML_ENTITIES: Final[dict[str, str]] = {
    "lt": "<",
    "gt": ">",
    "amp": "&",
    "quot": '"',
    "apos": "'",
}

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


def _deeper_lines(text: str, line_end: int, column: int) -> list[tuple[int, int, bool]]:
    """The lines after *line_end* that are indented deeper than *column*.

    Each line is (start, end without the line break, the line is a comment).
    Blank lines are left out.
    """
    lines: list[tuple[int, int, bool]] = []
    cursor = line_end
    while cursor < len(text):
        start = cursor + 1
        cursor = _line_end(text, start)
        end = _content_end(text, cursor)
        content = text[start:end].lstrip(" \t")
        if not content:
            continue
        if end - start - len(content) <= column:
            break
        lines.append((start, end, content.startswith("#")))
    return lines


def _deeper_comment_lines(text: str, line_end: int, column: int) -> list[tuple[int, int, bool]]:
    """The comment lines after *line_end* whose text after the ``#`` is deeper than *column*.

    They hold the rest of a commented-out value: a block value or a value
    that goes on over more lines. Each line is (start, end without the line
    break, False). Blank lines and lines with only ``#`` are left out.
    """
    lines: list[tuple[int, int, bool]] = []
    cursor = line_end
    while cursor < len(text):
        start = cursor + 1
        cursor = _line_end(text, start)
        end = _content_end(text, cursor)
        line = text[start:end]
        if not line.lstrip(" \t"):
            continue
        if not line.lstrip(" \t").startswith("#"):
            break
        content = line.lstrip(" \t#")
        if not content:
            continue
        if len(line) - len(content) <= column:
            break
        lines.append((start, end, False))
    return lines


def _split_value(value: str) -> tuple[str, str]:
    """*value* as (the value itself, the note after it with its white space).

    A value in quotes that close on the line ends at the closing quote. Any
    other value ends where a YAML comment starts.
    """
    if value[:1] in ('"', "'"):
        quoted = _QUOTED_VALUE.match(value)
        if quoted is not None:
            return value[: quoted.end()], value[quoted.end() :]
        return value, ""
    note = _TRAILING_NOTE.search(value)
    if note is None:
        return value, ""
    end = len(value[: note.start()].rstrip(" \t"))
    return value[:end], value[end:]


def _mark_quoted_lines(
    text: str, lines: list[tuple[int, int, bool]], value: str
) -> list[tuple[int, int, bool]]:
    """*lines* with each line inside a quoted value that is still open marked as no comment.

    A quoted value that does not close on the line of its key goes on until
    its closing quote, so a line in it that starts with ``#`` is part of the
    value.
    """
    if value[:1] not in ('"', "'") or _QUOTED_VALUE.match(value) is not None:
        return lines
    closing: re.Pattern[str] | None = _QUOTE_CLOSE[value[0]]
    marked: list[tuple[int, int, bool]] = []
    for line_start, end, is_comment in lines:
        if closing is None:
            marked.append((line_start, end, is_comment))
            continue
        marked.append((line_start, end, False))
        if closing.match(text, line_start, end) is not None:
            closing = None
    return marked


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
    value (``# rotate monthly``) stays. Below a value that is not a block
    value and not an open quoted value, a comment line stays where it is,
    with its own secret-named value hidden. In a comment, the comment lines
    below whose text after the ``#`` is indented deeper than the key belong
    to the value too.
    """
    value, note = _split_value(match["value"].rstrip(" \t\r"))
    start = match.start("value")
    column = match.start("key_token") - match.start("indent")
    lines: list[tuple[int, int, bool]] = []
    strip = " \t"
    if match["comment"] is not None:
        lines = _deeper_comment_lines(text, _line_end(text, match.start()), column)
        strip = " \t#"
    else:
        lines = _deeper_lines(text, _line_end(text, match.start()), column)
        lines = _mark_quoted_lines(text, lines, value)
        if _BLOCK_INDICATOR.fullmatch(value.strip()) is not None:
            lines = [(line_start, end, False) for line_start, end, _ in lines]
        while lines and lines[-1][2]:
            lines.pop()
    parts = [value] + [
        text[line_start:end].strip(strip) for line_start, end, is_comment in lines if not is_comment
    ]
    if len(parts) == 1:
        if _keeps(value):
            return None
        return start, start + len(value), SECRET_FIELD_MARKER
    if _keeps(" ".join(part for part in parts if part)):
        return None
    lead = "" if text[start - 1] in " \t" else " "
    if note and note[0] not in " \t":
        note = " " + note
    kept = [f"{lead}{SECRET_FIELD_MARKER}{note}"]
    for line_start, end, is_comment in lines:
        if is_comment:
            line_break = "\r\n" if text[line_start - 2 : line_start] == "\r\n" else "\n"
            comment = hide_secret_lines(text[line_start:end], comments_only=True)
            kept.append(line_break + comment)
    return start, lines[-1][1], "".join(kept)


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


def _xml_text_part(part: re.Match[str]) -> str:
    if part["cdata"] is not None:
        return part["cdata"]
    if part["entity"] is not None:
        return _XML_ENTITIES[part["entity"]]
    code = int(part["decimal"]) if part["decimal"] is not None else int(part["hex"], 16)
    return chr(code) if code <= sys.maxunicode else part[0]


def _xml_text(raw: str) -> str:
    """*raw* XML text as the text it means: CDATA and references read."""
    if "&" not in raw and "<" not in raw:
        return raw
    return _XML_TEXT_PART.sub(_xml_text_part, raw)


def _attribute_spans(attrs: str, offset: int) -> tuple[list[tuple[int, int]], bool]:
    """The spans of the attribute values in *attrs* that are to be hidden.

    Also True when a ``name`` or ``key`` attribute is a secret name: the
    ``value`` attribute and the text of such an element are a secret value.
    """
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
    pair_named = any(
        (named := values.get(name_attr)) is not None and _is_secret_pair_name(_xml_text(named[0]))
        for name_attr in _PAIR_NAME_ATTRS
    )
    pair_value = values.get("value")
    if pair_named and pair_value is not None and not _keeps(pair_value[0]):
        spans.append(pair_value[1:])
    return spans, pair_named


def _xml_tags(
    text: str, offset: int, spans: list[tuple[int, int]], *, in_comment: bool = False
) -> list[re.Match[str]]:
    """The start, end and empty tags of *text*, outside comments and CDATA.

    The text of each comment is read on its own, and the spans to hide in it
    go to *spans* (shifted by *offset*). Comments do not nest, so in the text
    of a comment ``<!--`` is plain text. A comment or CDATA section that does
    not close runs to the end of *text*.
    """
    tags: list[re.Match[str]] = []
    cursor = 0
    while (start := text.find("<", cursor)) != -1:
        if not in_comment and text.startswith(_COMMENT_START, start):
            inner = start + len(_COMMENT_START)
            end = text.find(_COMMENT_END, inner)
            stop = len(text) if end == -1 else end
            spans.extend(_element_spans(text[inner:stop], offset + inner, in_comment=True))
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


@dataclass(slots=True)
class _OpenElement:
    """An element whose closing tag is not read yet."""

    name: str
    #: Where the text of the element starts.
    text_start: int
    #: The text of the element is a secret value.
    secret: bool
    #: A ``<name>`` child holds a secret name.
    secret_name_child: bool = False
    #: The text spans of the ``<value>`` children.
    value_spans: list[tuple[int, int]] = field(default_factory=list)


def _hides(text: str, start: int, end: int) -> bool:
    """True when the element text *text[start:end]* is a value to hide."""
    return end - start > _KEPT_MAX_LENGTH or not _keeps(text[start:end])


def _close(
    text: str, element: _OpenElement, end: int, parent: _OpenElement | None
) -> list[tuple[int, int]]:
    """The spans to hide when *element* closes at *end*; notes a pair child in *parent*."""
    spans: list[tuple[int, int]] = []
    if element.secret and _hides(text, element.text_start, end):
        spans.append((element.text_start, end))
    if element.secret_name_child:
        spans.extend(element.value_spans)
    if parent is not None:
        local = _local_name(element.name).lower()
        if local == "name" and end - element.text_start <= _KEPT_MAX_LENGTH:
            name = _xml_text(text[element.text_start : end]).strip()
            parent.secret_name_child |= _is_secret_pair_name(name)
        elif local == "value" and _hides(text, element.text_start, end):
            parent.value_spans.append((element.text_start, end))
    return spans


def _element_spans(
    text: str, offset: int = 0, *, in_comment: bool = False
) -> list[tuple[int, int]]:
    """The spans of the attribute values and element texts that are to be hidden.

    Each closing tag closes the nearest open element with its name, so a
    child with the same name does not end its parent early. Open elements
    in between have no closing tag and are not touched.
    """
    spans: list[tuple[int, int]] = []
    stack: list[_OpenElement] = []
    open_names: dict[str, int] = {}
    for tag in _xml_tags(text, offset, spans, in_comment=in_comment):
        name = tag["name"]
        if tag["close"]:
            if not open_names.get(name):
                continue
            while True:
                element = stack.pop()
                open_names[element.name] -= 1
                if element.name == name:
                    break
            parent = stack[-1] if stack else None
            closed = _close(text, element, tag.start(), parent)
            spans.extend((offset + start, offset + end) for start, end in closed)
            continue
        attrs = tag["attrs"]
        attr_spans, pair_named = _attribute_spans(attrs, offset + tag.start("attrs"))
        spans.extend(attr_spans)
        if attrs.rstrip().endswith("/"):
            continue
        secret = pair_named or _is_secret_name(_local_name(name))
        stack.append(_OpenElement(name, tag.end(), secret))
        open_names[name] = open_names.get(name, 0) + 1
    return spans


def _merged_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """*spans* in order, without the spans that start inside an earlier one."""
    merged: list[tuple[int, int]] = []
    cursor = 0
    for start, end in sorted(spans, key=lambda span: (span[0], -span[1])):
        if start < cursor:
            continue
        merged.append((start, end))
        cursor = end
    return merged


def _hide_xml(text: str) -> str:
    parts: list[str] = []
    cursor = 0
    for start, end in _merged_spans(_element_spans(text)):
        parts.append(text[cursor:start])
        parts.append(SECRET_FIELD_MARKER)
        cursor = end
    if not parts:
        return text
    parts.append(text[cursor:])
    return "".join(parts)


def _is_json_collection(text: str) -> bool:
    if text.lstrip()[:1] not in ("{", "["):
        return False
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def _decode_json_string(raw: str) -> tuple[str, list[int]]:
    """*raw* (the inside of a JSON string) as text, and where each character starts in *raw*.

    The list has one more entry, the length of *raw*. Each escape, also each
    half of a ``\\u`` surrogate pair, is one character of the text.
    """
    if "\\" not in raw:
        return raw, list(range(len(raw) + 1))
    chars: list[str] = []
    starts: list[int] = []
    index = 0
    while index < len(raw):
        starts.append(index)
        if raw[index] != "\\":
            chars.append(raw[index])
            index += 1
        elif raw[index + 1] == "u":
            chars.append(chr(int(raw[index + 2 : index + 6], 16)))
            index += 6
        else:
            chars.append(_JSON_ESCAPES[raw[index + 1]])
            index += 2
    starts.append(len(raw))
    return "".join(chars), starts


def _hide_xml_in_json_strings(text: str) -> str:
    """*text* (a JSON text) with the XML step run on each of its strings on its own.

    Each string is read as text first, so the quotes of an attribute may be
    escaped. Only the value spans become the marker; the rest of the string
    keeps its escapes, so the text stays JSON.
    """
    parts: list[str] = []
    cursor = 0
    for token in _JSON_STRING.finditer(text):
        start, end = token.start() + 1, token.end() - 1
        if text.find("<", start, end) == -1 and not _JSON_ESCAPED_LT.search(text, start, end):
            continue
        decoded, starts = _decode_json_string(text[start:end])
        for span_start, span_end in _merged_spans(_element_spans(decoded)):
            parts.append(text[cursor : start + starts[span_start]])
            parts.append(SECRET_FIELD_MARKER)
            cursor = start + starts[span_end]
    if not parts:
        return text
    parts.append(text[cursor:])
    return "".join(parts)


def _yaml_nodes(text: str) -> Iterator[tuple[int, int, bool, bool]]:
    """Each node of *text* read as YAML: (start, end, it is a scalar, it is a mapping key).

    Raises :class:`yaml.YAMLError` when *text* is no YAML.
    """
    shift = 1 if text.startswith("\ufeff") else 0
    #: For each open collection: (it is a mapping, its next node is a key).
    levels: list[list[bool]] = []
    for event in yaml.parse(text[shift:], Loader=_LOADER):
        if isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            levels.pop()
            continue
        if not isinstance(event, yaml.NodeEvent):
            continue
        is_key = False
        if levels and levels[-1][0]:
            is_key = levels[-1][1]
            levels[-1][1] = not is_key
        if isinstance(event, yaml.MappingStartEvent):
            levels.append([True, True])
        elif isinstance(event, yaml.SequenceStartEvent):
            levels.append([False, False])
        start, end = shift + event.start_mark.index, shift + event.end_mark.index
        yield start, end, isinstance(event, yaml.ScalarEvent), is_key


def _yaml_value_spans(text: str) -> list[tuple[int, int]] | None:
    """The span of each value of *text* read as YAML; None when it is no YAML.

    A scalar key and its scalar value are one span, so ``key: <tag>...``
    on one entry is read as one text. Every other scalar is a span of its
    own.
    """
    spans: list[tuple[int, int]] = []
    key: tuple[int, int] | None = None
    try:
        for start, end, is_scalar, is_key in _yaml_nodes(text):
            if key is not None and is_scalar:
                start, end = key[0], max(key[1], end)
            elif key is not None:
                spans.append(key)
            key = None
            if is_scalar and is_key:
                key = (start, end)
            elif is_scalar and end > start:
                spans.append((start, end))
    except yaml.YAMLError:
        return None
    if key is not None:
        spans.append(key)
    return spans


def _hide_xml_in_yaml_values(text: str, values: list[tuple[int, int]]) -> str:
    """*text* (a YAML text) with the XML step run on each value and each comment on its own."""
    pieces: list[tuple[int, int]] = []
    cursor = 0
    for start, end in [*values, (len(text), len(text))]:
        if start > cursor:
            pieces.extend(comment.span() for comment in _YAML_COMMENT.finditer(text, cursor, start))
        pieces.append((start, end))
        cursor = max(cursor, end)
    spans: list[tuple[int, int]] = []
    for start, end in pieces:
        if text.find("<", start, end) != -1:
            spans.extend(_element_spans(text[start:end], start))
    parts: list[str] = []
    cursor = 0
    for start, end in _merged_spans(spans):
        parts.append(text[cursor:start])
        parts.append(SECRET_FIELD_MARKER)
        cursor = end
    if not parts:
        return text
    parts.append(text[cursor:])
    return "".join(parts)


def hide_secret_xml(text: str) -> str:
    """Return *text* with secret-named values in its XML tags hidden.

    In a JSON text each string is read on its own, with its escapes read
    first, so a start tag and a closing tag in two different strings are
    no element, and the JSON between them stays. In a YAML text each value
    and each comment is read on its own in the same way. Any other text is
    read as XML whole.
    """
    if "<" not in text and not _JSON_ESCAPED_LT.search(text):
        return text
    if _is_json_collection(text):
        return _hide_xml_in_json_strings(text)
    values = _yaml_value_spans(text)
    if values is None:
        return _hide_xml(text)
    return _hide_xml_in_yaml_values(text, values)
