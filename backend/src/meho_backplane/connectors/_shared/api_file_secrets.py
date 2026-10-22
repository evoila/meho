# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Find and replace secret values in an API description file, whatever its format.

An API description file or a documentation page an appliance serves can
carry a clear-text secret: an example ``"password": "..."`` in a Swagger
file, a ``<password>...</password>`` element in an XML file, a
``password="..."`` attribute or a script block in an HTML page, a PEM
private key. The stored-result redaction of MEHO finds only a labelled
secret on one line (``password=...``), so the reads that return such a
file check the text themselves first, with this helper.

The scan works on the whole file as text. It does not parse the file, so
the format does not matter and a secret that a part cut splits in two is
found all the same. Hand-written rules per format do not hold up; one
text scan does. What it finds:

1. **The value of a secret-named key.** A key is secret-named by the one
   rule MEHO uses for every secret-named field,
   :func:`~meho_backplane.broadcast.events.is_secret_param_name`
   (``password``, ``pwd``, ``clientSecret``, ``x-auth-token``,
   ``private_key``, ``credentials``, ...), plus a licence key, a product
   key and ``Authorization``. The key is in quotes (JSON, also with a
   ``\\u`` escape inside), or bare at the start of a line (YAML, a
   properties file), after a ``{`` or ``,`` (a flow mapping, a script
   object) or before ``=`` (an attribute, a script, a query string). The
   value follows ``:`` or ``=`` on the same line or, when the line ends
   there or holds only a comment, on the next line that is not empty and
   not a comment. A quoted value runs to its closing quote, also over a
   line break. A bare value runs to the end of its line, and on over the
   lines indented deeper than the key. A YAML block scalar (``|``, ``>``)
   is the indented lines below the key. A tag (``!!str``) or an anchor
   (``&name``) in front is skipped. An alias (``*name``) is a value, and
   so is the value the anchor of that name marks anywhere in the file.
   A bare secret-named word in running text (``Use password: ...``)
   counts only before a dense value of at least 8 characters without
   spaces, so a sentence about passwords stays as it is.
2. **The content of a secret-named element**, ``<password>...</password>``,
   with or without a namespace prefix or attributes. Comments, CDATA
   sections and child elements inside are part of the content.
3. **The value attribute of an element named by its attributes**:
   ``<property name="password" value="..."/>``, ``<input name="password"
   value="...">``, ``<meta name="password" content="...">``, and the
   content of ``<entry key="password">...</entry>``.
4. **A PEM private key block**, from ``-----BEGIN ... PRIVATE KEY`` to
   the ``-----END`` line or the end of the file.

Nothing is found for a schema stub: an empty value, a type word such as
``string`` or ``null``, a placeholder in angle brackets or template braces
(``<password>``, ``{{ token }}``, ``${TOKEN}``), a row of ``*``, the
placeholder this helper writes, or an object or list (``{``, ``[``, or a
mapping or list on the lines below the key). So ``"password": {"type":
"string"}`` and ``password: string`` stay as they are.

:func:`scrub_api_file_secrets` replaces every found value with
:data:`REDACTED`, the placeholder the governed Vault read writes for a
secret-named value (:func:`~meho_backplane.broadcast.events.scrub_secret_named_values`),
and returns the text and the number of replacements. A read that must
return the exact file can refuse it when the count is not zero.
:func:`find_api_file_secrets` returns where each value is and what names
it, never the value itself.
"""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass
from typing import Final

from meho_backplane.broadcast.events import is_secret_param_name

__all__ = ["REDACTED", "SecretValue", "find_api_file_secrets", "scrub_api_file_secrets"]

#: Written in place of every replaced value: the placeholder the
#: dispatcher writes for a secret-named value of a governed credential
#: read, so a caller sees one vocabulary.
REDACTED: Final[str] = "[REDACTED:param_name]"

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

_NAME: Final[str] = r"[\w.-]+"

#: A key and its separator. ``lead`` says where the key stands: at the
#: start of a line (after list dashes or ``?``), or after ``{`` or ``,``.
#: A quoted key may hold an escape (``"password"``). The look-behind
#: keeps a bare key at a token boundary, so ``userPassword`` is one key.
_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?P<lead>^[ \t]*(?:[-?][ \t]+)*|[{,][ \t]*)?"
    r"(?<![\w.-])"
    rf"""(?:"(?P<dq>(?:[^"\\\n]|\\.)+)"|'(?P<sq>(?:[^'\n]|'')+)'|(?P<bare>{_NAME}))"""
    r"\s*(?P<sep>[:=])(?![=>])",
    re.MULTILINE,
)

#: A dense value after a secret-named word in running text: at least 8
#: characters, no space, no quote. The stored-result redaction's own bound.
_DENSE_VALUE_RE: Final[re.Pattern[str]] = re.compile(r"""[ \t]*['"]?(?P<value>[^\s'"]{8,})""")

#: A YAML block scalar indicator with its chomping or indentation sign
#: and an optional comment, up to the end of the line.
_BLOCK_INDICATOR_RE: Final[re.Pattern[str]] = re.compile(r"[|>][-+0-9]*(?:[ \t]+#[^\n]*)?[ \t]*$")

#: A line that opens a structure, not a scalar: a mapping entry
#: (``type: string``) or a list item (``- a``).
_STRUCTURE_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"""(?:-(?:[ \t]|$)|(?:"[^"\n]*"|'[^'\n]*'|[^\s:"'#]+)[ \t]*:(?:[ \t]|$))"""
)

#: An opening tag with its name and attributes; ``close`` is ``/`` for a
#: self-closing tag. The name is checked in Python.
_TAG_RE: Final[re.Pattern[str]] = re.compile(
    r"<(?P<name>[A-Za-z_][\w.-]*(?::[A-Za-z_][\w.-]*)?)(?P<attrs>(?:\s[^<>]*?)?)(?P<close>/?)>"
)
_ATTR_RE: Final[re.Pattern[str]] = re.compile(
    r"""(?P<name>[\w.:-]+)\s*=\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)')"""
)
#: Attributes that name an element (``name="password"``) and the
#: attributes that then hold its value. Every naming attribute is
#: checked: ``id="login-field" name="password"`` names a password.
_NAMING_ATTRS: Final[frozenset[str]] = frozenset({"name", "key", "id", "property"})
_VALUE_ATTRS: Final[frozenset[str]] = frozenset({"value", "content"})

_PEM_RE: Final[re.Pattern[str]] = re.compile(
    r"-----BEGIN (?P<kind>[A-Z0-9 ]*PRIVATE KEY)-{0,5}[ \t]*\r?\n?"
    r"(?P<body>.*?)"
    r"(?=\s*-----END |\Z)",
    re.DOTALL,
)

_ALIAS_RE: Final[re.Pattern[str]] = re.compile(r"\*(?P<name>[\w.-]+)")
_ANCHOR_RE: Final[re.Pattern[str]] = re.compile(r"(?<![\w.-])&(?P<name>[\w.-]+)")


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


def _is_secret_name(name: str) -> bool:
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


def _is_stub(value: str) -> bool:
    """Return whether a scalar *value* is a schema stub or placeholder, not a secret."""
    stripped = value.strip()
    if not stripped or stripped.lower() in _STUB_WORDS or stripped.startswith("[REDACTED"):
        return True
    if stripped[0] == "<" and stripped[-1] == ">":
        return True
    if stripped.startswith(("{{", "${")) and stripped.endswith("}"):
        return True
    return set(stripped) <= {"*"}


def _decode_key(match: re.Match[str]) -> str:
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


def _line_end(text: str, pos: int) -> int:
    """Return where the line that holds *pos* ends, before its ``\\r\\n`` or ``\\n``."""
    end = text.find("\n", pos)
    if end < 0:
        return len(text)
    return end - 1 if end > pos and text[end - 1] == "\r" else end


def _indent_of_line(text: str, pos: int) -> int:
    """Return the indentation of the line that holds *pos*."""
    start = text.rfind("\n", 0, pos) + 1
    end = start
    while end < len(text) and text[end] in " \t":
        end += 1
    return end - start


def _is_comment(stripped: str) -> bool:
    return stripped.startswith(("#", "//", "<!--"))


def _deeper_lines_end(text: str, line_end: int, indent: int) -> int:
    """Return the end of the last line below *line_end* indented deeper than *indent*.

    Blank lines inside are kept; the scan stops at the first line that is
    not blank and not indented deeper. Returns *line_end* when no such
    line follows.
    """
    end = line_end
    pos = line_end + 1
    while pos <= len(text):
        next_end = _line_end(text, pos)
        line = text[pos:next_end]
        if line.strip(" \t\r"):
            if len(line) - len(line.lstrip(" \t")) <= indent:
                break
            end = next_end
        pos = next_end + 1
    return end


def _next_value_line(text: str, pos: int) -> int | None:
    """Return where the next line that holds a value starts, or ``None``.

    Empty lines and comment lines are skipped. A line that opens a
    structure (a mapping entry, a list item, ``{`` or ``[``) is not a
    value: the key has children, as a schema property has.
    """
    pos = _line_end(text, pos) + 1
    while pos <= len(text):
        end = _line_end(text, pos)
        line = text[pos:end]
        stripped = line.strip(" \t\r")
        if stripped and not _is_comment(stripped):
            if stripped[0] in "{[" or _STRUCTURE_LINE_RE.match(stripped):
                return None
            return pos + len(line) - len(line.lstrip(" \t"))
        pos = end + 1
    return None


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def _quoted_end(text: str, pos: int) -> int:
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
    return _line_end(text, pos)


_FLOW_STOP: Final[frozenset[str]] = frozenset(",}]\n")
_ATTR_STOP: Final[frozenset[str]] = frozenset(" \t\r\n&<>;,)}\"'")


def _scalar_end(text: str, pos: int, stops: frozenset[str]) -> int:
    end = pos
    while end < len(text) and text[end] not in stops:
        end += 1
    return end


def _resolve_value(
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
    rest = text[pos : _line_end(text, pos)]
    if not rest or _is_comment(rest):
        if mode == "attr":
            return None
        next_pos = _next_value_line(text, pos)
        return None if next_pos is None else _resolve_value(text, next_pos, indent, mode, aliases)
    if rest[0] in "{[":
        return None
    if rest[0] in "\"'":
        close = _quoted_end(text, pos)
        return None if _is_stub(text[pos + 1 : close]) else (pos + 1, close)
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
    rest = text[pos : _line_end(text, pos)]
    if rest[0] in "!&":
        return _resolve_value(text, pos + len(rest.split(None, 1)[0]), indent, mode, aliases)
    if rest[0] == "*":
        alias = _ALIAS_RE.match(rest)
        if alias is None:
            return _plain_value(text, pos, indent, mode)
        aliases.add(alias.group("name"))
        end = _scalar_end(text, pos, _FLOW_STOP) if mode == "flow" else pos + len(rest)
        return (pos, end)
    if mode != "block" or not _BLOCK_INDICATOR_RE.match(rest):
        return _plain_value(text, pos, indent, mode)
    line_end = pos + len(rest)
    body_end = _deeper_lines_end(text, line_end, indent)
    body_start = _next_value_line(text, pos) if body_end > line_end else None
    if body_start is None or _is_stub(text[body_start:body_end]):
        return None
    return (body_start, body_end)


def _plain_value(text: str, pos: int, indent: int, mode: str) -> tuple[int, int] | None:
    """A bare value: to a stop character, or to the end of its line and the lines below it."""
    if mode == "attr":
        end = _scalar_end(text, pos, _ATTR_STOP)
    elif mode == "flow":
        end = _scalar_end(text, pos, _FLOW_STOP)
    else:
        end = _deeper_lines_end(text, _line_end(text, pos), indent)
    core = text[pos:end].split(" #", 1)[0]
    return None if _is_stub(core) else (pos, end)


# ---------------------------------------------------------------------------
# The four finders
# ---------------------------------------------------------------------------


def _find_key_values(text: str, aliases: set[str]) -> list[SecretValue]:
    hits: list[SecretValue] = []
    for match in _KEY_RE.finditer(text):
        name = _decode_key(match)
        if not _is_secret_name(name):
            continue
        lead = match.group("lead")
        bare = match.group("bare") is not None
        after = match.end()
        if match.group("sep") == "=":
            span = _resolve_value(text, after, 0, "attr", aliases)
        elif bare and lead is None:
            dense = _DENSE_VALUE_RE.match(text, after)
            span = None if dense is None or _is_stub(dense.group("value")) else dense.span("value")
        else:
            mode = "flow" if lead and lead[0] in "{," else "block"
            span = _resolve_value(text, after, _indent_of_line(text, match.start()), mode, aliases)
        if span is not None:
            hits.append(SecretValue(span[0], span[1], "field", name))
    return hits


def _find_anchor_values(text: str, aliases: set[str]) -> list[SecretValue]:
    """The values the anchors of the aliased *aliases* mark, anywhere in *text*."""
    hits: list[SecretValue] = []
    if not aliases:
        return hits
    for match in _ANCHOR_RE.finditer(text):
        name = match.group("name")
        if name not in aliases:
            continue
        indent = _indent_of_line(text, match.start())
        span = _resolve_value(text, match.end(), indent, "block", set())
        if span is not None:
            hits.append(SecretValue(span[0], span[1], "field", f"&{name}"))
    return hits


def _element_content(
    text: str, tag: re.Match[str], closings: dict[str, int | None]
) -> tuple[int, int] | None:
    """The span from the end of the opening *tag* to its closing tag, or ``None`` without one.

    A tag that never closes is not an element: ``"<password>"`` is the
    placeholder a schema example writes. *closings* remembers, per tag
    name, where the last closing tag was found (or that none follows), so
    many open tags of one name do not each scan to the end of the text.
    """
    name = tag.group("name").lower()
    start = tag.end()
    known = closings.get(name, -1)
    if known is None or known >= start:
        return None if known is None else (start, known)
    close = re.compile(rf"</{re.escape(name)}\s*>", re.IGNORECASE).search(text, start)
    closings[name] = close_at = None if close is None else close.start()
    return None if close_at is None else (start, close_at)


def _secret_naming_attr(attrs: dict[str, re.Match[str]]) -> str | None:
    """Return the secret name a naming attribute gives the element, or ``None``.

    Every naming attribute is checked, in order, so an ``id`` before a
    ``name="password"`` does not hide it.
    """
    for key, attr in attrs.items():
        if key not in _NAMING_ATTRS:
            continue
        value = attr.group("dq") if attr.group("dq") is not None else attr.group("sq")
        if _is_secret_name(value):
            return value
    return None


def _find_elements(text: str) -> list[SecretValue]:
    hits: list[SecretValue] = []
    closings: dict[str, int | None] = {}
    for tag in _TAG_RE.finditer(text):
        attrs = {
            attr.group("name").lower(): attr for attr in _ATTR_RE.finditer(tag.group("attrs") or "")
        }
        local = tag.group("name").rsplit(":", 1)[-1]
        named_by_attr = _secret_naming_attr(attrs)
        if named_by_attr is not None:
            for key, attr in attrs.items():
                if key in _VALUE_ATTRS:
                    group = "dq" if attr.group("dq") is not None else "sq"
                    if not _is_stub(attr.group(group)):
                        offset = tag.start("attrs")
                        hits.append(
                            SecretValue(
                                offset + attr.start(group),
                                offset + attr.end(group),
                                "attribute",
                                attr.group("name"),
                            )
                        )
                    break
            else:
                if not tag.group("close"):
                    hits.extend(_content_hit(text, tag, named_by_attr, closings))
        elif not tag.group("close") and _is_secret_name(local):
            hits.extend(_content_hit(text, tag, local, closings))
    return hits


def _content_hit(
    text: str, tag: re.Match[str], name: str, closings: dict[str, int | None]
) -> list[SecretValue]:
    span = _element_content(text, tag, closings)
    if span is None:
        return []
    start, end = span
    plain = re.sub(r"<!--.*?-->|<!\[CDATA\[|\]\]>|<[^<>]*>", " ", text[start:end], flags=re.DOTALL)
    if _is_stub(plain):
        return []
    lead = len(text[start:end]) - len(text[start:end].lstrip())
    return [SecretValue(start + lead, end, "element", name)]


def _find_pem_keys(text: str) -> list[SecretValue]:
    return [
        SecretValue(match.start("body"), match.end("body"), "PEM private key", match.group("kind"))
        for match in _PEM_RE.finditer(text)
        if not _is_stub(match.group("body"))
    ]


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def find_api_file_secrets(text: str) -> list[SecretValue]:
    """Return every secret value in *text*, in order, without overlap.

    A value inside another one (a ``"password"`` key inside a
    ``<credentials>`` element, a PEM block inside a ``private_key`` block
    scalar) is reported once, as the outer value.
    """
    aliases: set[str] = set()
    found = _find_pem_keys(text) + _find_elements(text) + _find_key_values(text, aliases)
    found += _find_anchor_values(text, aliases)
    hits: list[SecretValue] = []
    for hit in sorted(found, key=lambda h: (h.start, -h.end)):
        if hits and hit.start < hits[-1].end:
            continue
        hits.append(hit)
    return hits


def scrub_api_file_secrets(text: str) -> tuple[str, int]:
    """Return *text* with every secret value replaced by :data:`REDACTED`, and the count.

    Every other character stays as it is. A count of zero means the text
    is the file as served.
    """
    hits = find_api_file_secrets(text)
    if not hits:
        return text, 0
    pieces: list[str] = []
    copied = 0
    for hit in hits:
        pieces.append(text[copied : hit.start])
        pieces.append(REDACTED)
        copied = hit.end
    pieces.append(text[copied:])
    return "".join(pieces), len(hits)
