# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Whole-file secret redaction for ``vcfa.vra8.api_file.get``.

An API description file or a documentation page can carry a clear-text
secret: an example ``"password": "..."`` in a Swagger file, a
``<password>...</password>`` element in an XML file, a ``password="..."``
attribute or a script block in an HTML page, a PEM private key. The
stored-result redaction of MEHO sees only labelled shapes on one line, so
the read cleans the file itself, before the text is returned.

The scan works on the whole file as text, whatever its format, so a part
cut hides nothing and no format needs its own rules. It replaces every
matched value with :data:`REDACTED`, the placeholder the governed Vault
read uses for a secret-named value, and counts the replacements. Every
other character stays as it is.

What is replaced
================

1. **The value of a secret-named key.** A key is secret-named by the one
   rule MEHO uses for every secret-named parameter and field,
   :func:`~meho_backplane.broadcast.events.is_secret_param_name`
   (``password``, ``pwd``, ``clientSecret``, ``refreshToken``,
   ``x-api-key``, ``private_key``, ``licenseKey``, ...). The key is in
   quotes (JSON, quoted YAML; a name written with escapes,
   ``"pass\\u0077ord"``, is read as the name it spells), bare at the start
   of a line or after a list dash (YAML, a properties file), bare inside a
   flow mapping (``{password: abc}``), or bare before ``=`` (an HTML or XML
   attribute, a query string). The value follows ``:`` or ``=``, on the
   same line or on the next non-empty line (a comment after the colon,
   ``password: # note``, is skipped), and is replaced whole:

   * a quoted string, up to its closing quote, also when it runs over
     several lines (a YAML quoted scalar); an apostrophe inside a
     double-quoted value counts, so does a doubled quote. A quote that
     never closes takes the rest of its line;
   * a bare value, with the spaces inside it (``made up``), up to the end
     of the line, a ``,``, ``;``, ``}``, ``]``, ``<`` or ``>``, or a
     comment (`` #``). A bare value after ``:`` also takes the lines below
     it that are indented more than the key (a YAML plain scalar over
     several lines, ``password: made`` then ``  up``), up to a comment
     line, a list item, a mapping key or a stop character; inside a flow
     mapping the indentation does not matter;
   * a YAML block scalar: the indicator (``|`` or ``>``, with a chomping
     or indentation sign and a comment after it) and the indented lines
     below it. The comment stays.

   A YAML tag or anchor before the value (``!!str made``, ``&a made``)
   stays; the value behind it goes.

   Nothing is replaced for an empty string, ``null``, ``true``, ``false``,
   a comment alone, an object or list (``{``, ``[``: a schema property
   named ``password``), or a key whose children follow on the next lines
   (``password:`` then ``type: string``). A secret-named word in running
   text (``the token: in the header``) is not a key.
2. **The content of a secret-named element.** ``<password>abc</password>``,
   with or without a namespace prefix or attributes, loses everything
   between its tags; the tags stay. The content may hold comments, CDATA
   sections and child elements (``<password>ma<!-- x -->de<b>up</b>
   </password>``): all of it goes. An element whose content is only
   comments or white space keeps it. An element without a closing tag
   loses its text up to the next tag. An element that is not secret-named
   is looked into: its children are checked one by one.
3. **A PEM private key block.** Everything between ``-----BEGIN ...
   PRIVATE KEY-----`` and ``-----END ... PRIVATE KEY-----`` (or the end of
   the file) is replaced, with the line breaks written as they are in the
   file (``\\n`` inside a JSON string too).

The scan fails closed: an example value in a documentation file is
replaced like a real one, and the count in the answer says how many
values changed. When the count is zero, the text is the file as served.
"""

from __future__ import annotations

import re
import sys
from typing import Final

from meho_backplane.broadcast.events import is_secret_param_name

__all__ = ["REDACTED", "redact_secret_values"]

#: Written in place of every replaced value. The same placeholder the
#: dispatcher writes for a secret-named value of a governed credential read
#: (``scrub_secret_named_values``), so a caller sees one vocabulary.
REDACTED: Final[str] = "[REDACTED:param_name]"

_NAME = r"[A-Za-z0-9_][A-Za-z0-9_.-]*"

#: A name in quotes may spell a character as an escape (``w`` for
#: ``w``, as JSON, YAML and JavaScript write it); the name is decoded
#: before it is checked.
_ESCAPE = r"\\(?:u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|x[0-9A-Fa-f]{2})"
_QUOTED_NAME = rf"(?:[A-Za-z0-9_.-]|{_ESCAPE})+"
_ESCAPE_RE: Final[re.Pattern[str]] = re.compile(
    r"\\(?:u([0-9A-Fa-f]{4})|U([0-9A-Fa-f]{8})|x([0-9A-Fa-f]{2}))"
)

#: A bare value runs to the end of its line, a stop character or a `` #``
#: comment, spaces inside included.
_BARE_TAIL = r"""(?:[^\s<>,;}\]]|[ \t]+(?=[^\s#<>,;}\]]))*"""

#: A key, its separator and its value. Three key forms, five value forms;
#: the key's name is checked in Python with :func:`is_secret_param_name`.
#: A quoted value may run over line breaks; an unclosed quote (``open``)
#: takes the rest of its line.
_KEY_VALUE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:"
    rf"""(?P<quote>["'])(?P<quoted_key>{_QUOTED_NAME})(?P=quote)"""
    rf"|(?:^[ \t]*(?:-[ \t]+)?|[{{,][ \t]*)(?P<bare_key>{_NAME})"
    rf"|(?<=[ \t?&])(?P<attr_key>{_NAME})(?=[ \t]*=)"
    r")"
    r"\s*(?P<sep>[:=])[ \t]*(?:#[^\n]*(?=\r?\n|\Z))?(?P<newline>(?:\r?\n[ \t]*)*)"
    r"(?:[!&][^\s,;}\]]*[ \t]+)*"
    r"(?:"
    r'"(?P<double>(?:[^"\\]|\\[\s\S])+)"'
    r"|'(?P<single>(?:[^']|'')+)'"
    r"|(?P<block>[|>][-+0-9]*)[ \t]*(?:#[^\n]*)?(?=\r?\n|\Z)"
    r"|(?P<bare>(?!(?:null|true|false|~)(?=[\s,;}\]<>]|\Z))(?!-[ \t])"
    rf"""[^\s"'<>{{}}\[\],;#|&*!]{_BARE_TAIL})"""
    r"""|(?P<open>["'][^\s"'][^\n]*)"""
    r")",
    re.MULTILINE,
)
_KEY_GROUPS: Final[tuple[str, ...]] = ("quoted_key", "bare_key", "attr_key")
_VALUE_GROUPS: Final[tuple[str, ...]] = ("double", "single", "block", "bare", "open")
#: The value forms that may go on below their first line as a YAML plain scalar.
_PLAIN_GROUPS: Final[frozenset[str]] = frozenset({"bare", "open"})

#: One more line of a plain scalar: not a comment line, not a list item.
_CONTINUATION_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?:\r?\n[ \t]*)+(?!#|-(?:[ \t]|\r?\n|\Z))(?P<line>[^\s<>,;}}\]]{_BARE_TAIL})"
)

#: A value on the next line that is itself a mapping key (``type: string``
#: below ``password:``): the key has children, not a value.
_MAPPING_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"""(?:"[^"\n]*"|'[^'\n]*'|[^\s:"']+)[ \t]*:(?:[ \t]|\r?\n|\Z)"""
)

#: A CDATA section or a comment. Each is matched character by character,
#: so a long text backtracks no more than once.
_CDATA = r"<!\[CDATA\[(?:[^\]]|\](?!\]>))*\]\]>"
_COMMENT = r"<!--(?:[^-]|-(?!->))*-->"

#: The next piece of markup: a CDATA section, a comment, or a tag (opening,
#: closing or self-closing) with its attributes.
_TAG_RE: Final[re.Pattern[str]] = re.compile(
    rf"{_CDATA}|{_COMMENT}"
    r"|<(?P<close>/)?(?P<name>[A-Za-z_][\w.-]*(?::[A-Za-z_][\w.-]*)?)"
    r"""(?P<attrs>(?:"[^"]*"|'[^']*'|[^<>"'])*)>"""
)

#: The pieces of an element's content that are not text: a CDATA section
#: (whose content is text) and a comment (which is not).
_MARKUP_RE: Final[re.Pattern[str]] = re.compile(
    r"<!\[CDATA\[(?P<cdata>.*?)\]\]>|<!--.*?-->", re.DOTALL
)

_PRIVATE_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"(-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----)(.*?)(-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)


def redact_secret_values(text: str) -> tuple[str, int]:
    """Return ``(cleaned_text, replaced_count)`` for the whole file *text*.

    Every secret-named element content, every PEM private key block and
    every value of a secret-named key (see the module docstring) is
    replaced with :data:`REDACTED`; everything else is copied as it is.
    The count is the number of values replaced. A count of zero means the
    text is the file as served.
    """
    text, elements = _redact_elements(text)
    text, keys = _redact_private_keys(text)
    text, values = _redact_key_values(text)
    return text, elements + keys + values


def _element_text(body: str) -> str:
    """The text of an element content: CDATA content kept, comments dropped."""
    return _MARKUP_RE.sub(lambda match: match.group("cdata") or "", body)


def _is_self_closing(match: re.Match[str]) -> bool:
    return match.group("attrs").rstrip().endswith("/")


def _redact_elements(text: str) -> tuple[str, int]:
    """Replace the content of every secret-named element, scanning left to right.

    A tag inside a comment or a CDATA section is not a tag. After an
    element that is not secret-named the scan goes on behind its opening
    tag, so its children are checked one by one.
    """
    out: list[str] = []
    count = 0
    pos = 0
    copied = 0
    while (match := _TAG_RE.search(text, pos)) is not None:
        pos = match.end()
        name = match.group("name")
        if name is None or match.group("close") or _is_self_closing(match):
            continue
        if not is_secret_param_name(name.rsplit(":", 1)[-1]):
            continue
        end = _element_end(text, name, pos)
        content = text[pos:end]
        if content == REDACTED or not _element_text(content).strip():
            continue
        count += 1
        out.append(text[copied:pos])
        out.append(REDACTED)
        copied = pos = end
    out.append(text[copied:])
    return "".join(out), count


def _element_end(text: str, name: str, start: int) -> int:
    """Return where the content of the element *name* opened before *start* ends.

    That is the start of its closing tag, with a nested element of the same
    name counted. Without a closing tag the content is the text up to the
    next tag, or the end of the file.
    """
    depth = 1
    pos = start
    while (match := _TAG_RE.search(text, pos)) is not None:
        pos = match.end()
        tag = match.group("name")
        if tag is None or tag.lower() != name.lower():
            continue
        if match.group("close"):
            depth -= 1
            if depth == 0:
                return match.start()
        elif not _is_self_closing(match):
            depth += 1
    next_tag = text.find("<", start)
    return len(text) if next_tag == -1 else next_tag


def _redact_private_keys(text: str) -> tuple[str, int]:
    count = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal count
        if match.group(2) in ("", REDACTED):
            return match.group(0)
        count += 1
        return f"{match.group(1)}{REDACTED}{match.group(3)}"

    return _PRIVATE_KEY_RE.sub(_replace, text), count


def _unescape(match: re.Match[str]) -> str:
    code = int(next(group for group in match.groups() if group is not None), 16)
    return chr(code) if code <= sys.maxunicode else match.group(0)


def _key_name(match: re.Match[str], group: str) -> str:
    """The name a key spells, with the escapes of a quoted key decoded."""
    name = match.group(group)
    if group == "quoted_key" and "\\" in name:
        return _ESCAPE_RE.sub(_unescape, name)
    return name


def _redact_key_values(text: str) -> tuple[str, int]:
    """Replace the value of every secret-named key, scanning left to right.

    After a key that is not secret-named the scan goes on right behind the
    key, so a secret-named key inside that key's value (``{"example":
    {"password": "abc"}}``) is still found. After a replaced value it goes
    on behind the value.
    """
    out: list[str] = []
    count = 0
    pos = 0
    copied = 0
    while (match := _KEY_VALUE_RE.search(text, pos)) is not None:
        key_group = next(group for group in _KEY_GROUPS if match.group(group) is not None)
        value_group = next(group for group in _VALUE_GROUPS if match.group(group) is not None)
        start, end = match.span(value_group)
        if (
            not is_secret_param_name(_key_name(match, key_group))
            or text[start:end] == REDACTED
            or (match.group("newline") and _MAPPING_KEY_RE.match(text, start))
        ):
            pos = match.end(key_group)
            continue
        kept = ""
        key_start = match.start(key_group)
        if value_group == "block":
            # The comment after the indicator stays; the indented lines go.
            kept = text[end : match.end()]
            end = _block_scalar_end(text, key_start, match.end())
        elif value_group in _PLAIN_GROUPS and match.group("sep") == ":":
            end = _plain_scalar_end(text, key_start, end)
        out.append(text[copied:start])
        out.append(REDACTED + kept)
        count += 1
        copied = pos = end
    out.append(text[copied:])
    return "".join(out), count


def _key_indent(text: str, key_start: int) -> tuple[int, bool]:
    """The indentation of the key's line, and whether the key opens that line.

    A key opens its line when only white space, a list dash or a quote
    comes before it (block context); otherwise it sits in a flow mapping
    or in the middle of a line.
    """
    line_start = text.rfind("\n", 0, key_start) + 1
    lead = text[line_start:key_start]
    indent = len(lead) - len(lead.lstrip(" \t"))
    return indent, lead.strip(" \t\"'") in ("", "-")


def _plain_scalar_end(text: str, key_start: int, end: int) -> int:
    """Return where a bare value ends when it goes on below its first line.

    *end* is where the value's first line ends. The lines below it belong
    to the value (a YAML plain scalar over several lines) while they are
    indented more than the key (in block context), are not a comment line,
    a list item or a mapping key, and the previous line did not stop at a
    stop character.
    """
    indent, block = _key_indent(text, key_start)
    while (match := _CONTINUATION_RE.match(text, end)) is not None:
        line = match.start("line")
        line_indent = line - (text.rfind("\n", 0, line) + 1)
        if (block and line_indent <= indent) or _MAPPING_KEY_RE.match(text, line):
            break
        end = match.end()
    return end


def _block_scalar_end(text: str, key_start: int, line_end: int) -> int:
    """Return where a YAML block scalar ends: before the first line indented like the key.

    *line_end* is the end of the line that holds the key and the block
    indicator; the scalar's lines are the lines below it that are indented
    more than the key.
    """
    indent, _block = _key_indent(text, key_start)
    end = line_end
    pos = line_end
    while pos < len(text):
        if text.startswith("\r\n", pos):
            pos += 2
        elif text[pos] == "\n":
            pos += 1
        if pos >= len(text):
            break
        next_end = text.find("\n", pos)
        if next_end == -1:
            next_end = len(text)
        line = text[pos:next_end]
        if line.strip() and len(line) - len(line.lstrip(" \t")) <= indent:
            break
        end = next_end
        pos = next_end
    return end
