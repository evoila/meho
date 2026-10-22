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
   When the key holds an object or a list instead of one value, every
   scalar inside it counts, at any depth, except the values of the
   words a schema uses (``type``, ``format``, ``description``, ``$ref``,
   ``example``, ``enum``, ``tags``, ``scopes``, ...: the fixed fields of
   OpenAPI 3.1, Swagger 2 and JSON Schema 2020-12). So ``"password":
   {"type": "string"}`` and a path item named ``/reset-password`` stay as
   they are, while ``"credentials": {"value": "..."}`` and
   ``"licenseKey": ["..."]`` are found. A bare number, ``true``, ``false``
   and ``null`` are never a value here, as in the broadcast scrub.
2. **The content of a secret-named element**, ``<password>...</password>``,
   with or without a namespace prefix or attributes. Comments, CDATA
   sections and child elements inside are part of the content.
3. **The value attribute of an element named by its attributes**:
   ``<property name="password" value="..."/>``, ``<input name="password"
   value="...">``, ``<meta name="password" content="...">``, and the
   content of ``<entry key="password">...</entry>``. An attribute value
   may be in double quotes, in single quotes or bare, as HTML allows.
4. **A PEM private key block**, from ``-----BEGIN ... PRIVATE KEY`` to
   the ``-----END`` line or the end of the file.

Nothing is found for a schema stub: an empty value, a type word such as
``string`` or ``null``, a placeholder in angle brackets or template braces
(``<password>``, ``{{ token }}``, ``${TOKEN}``), a row of ``*``, or the
placeholder this helper writes. So ``password: string`` stays as it is.

:func:`scrub_api_file_secrets` replaces every found value with
:data:`REDACTED`, the placeholder the governed Vault read writes for a
secret-named value (:func:`~meho_backplane.broadcast.events.scrub_secret_named_values`),
and returns the text and the number of replacements. A read that must
return the exact file can refuse it when the count is not zero.
:func:`find_api_file_secrets` returns where each value is and what names
it, never the value itself.
"""

from __future__ import annotations

import re
from typing import Final

from meho_backplane.connectors._shared.api_file_collections import collection_hits
from meho_backplane.connectors._shared.api_file_text import (
    KEY_RE,
    SecretValue,
    decode_key,
    indent_of_line,
    is_secret_name,
    is_stub,
    resolve_value,
)

__all__ = ["REDACTED", "SecretValue", "find_api_file_secrets", "scrub_api_file_secrets"]

#: Written in place of every replaced value: the placeholder the
#: dispatcher writes for a secret-named value of a governed credential
#: read, so a caller sees one vocabulary.
REDACTED: Final[str] = "[REDACTED:param_name]"

_DENSE_VALUE_RE: Final[re.Pattern[str]] = re.compile(r"""[ \t]*['"]?(?P<value>[^\s'"]{8,})""")


#: self-closing tag. The name is checked in Python.
_TAG_RE: Final[re.Pattern[str]] = re.compile(
    r"<(?P<name>[A-Za-z_][\w.-]*(?::[A-Za-z_][\w.-]*)?)(?P<attrs>(?:\s[^<>]*?)?)(?P<close>/?)>"
)
#: One attribute. The value is in double quotes, in single quotes or
#: bare; a bare value ends at whitespace, a quote, ``=``, ``<``, ``>`` or
#: a backtick, the characters HTML keeps out of it.
_ATTR_RE: Final[re.Pattern[str]] = re.compile(
    r"""(?P<name>[\w.:-]+)\s*=\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[^\s"'=<>`]+))"""
)
_ATTR_VALUE_GROUPS: Final[tuple[str, ...]] = ("dq", "sq", "bare")
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

_ANCHOR_RE: Final[re.Pattern[str]] = re.compile(r"(?<![\w.-])&(?P<name>[\w.-]+)")


# ---------------------------------------------------------------------------
# The four finders
# ---------------------------------------------------------------------------


def _find_key_values(text: str, aliases: set[str]) -> list[SecretValue]:
    hits: list[SecretValue] = []
    walked_end = 0
    for match in KEY_RE.finditer(text):
        name = decode_key(match)
        if not is_secret_name(name):
            continue
        lead = match.group("lead")
        bare = match.group("bare") is not None
        after = match.end()
        if match.group("sep") == "=":
            span = resolve_value(text, after, 0, "attr", aliases)
        elif bare and lead is None:
            dense = _DENSE_VALUE_RE.match(text, after)
            span = None if dense is None or is_stub(dense.group("value")) else dense.span("value")
        else:
            mode = "flow" if lead and lead[0] in "{," else "block"
            indent = indent_of_line(text, match.start())
            span = resolve_value(text, after, indent, mode, aliases)
            if span is None and match.start() >= walked_end:
                # One walk covers every nested collection; a key inside
                # an already walked one is not walked again.
                inner, walked_end = collection_hits(text, after, indent, mode, name, aliases)
                hits.extend(inner)
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
        indent = indent_of_line(text, match.start())
        span = resolve_value(text, match.end(), indent, "block", set())
        if span is not None:
            hits.append(SecretValue(span[0], span[1], "field", f"&{name}"))
        else:
            hits.extend(collection_hits(text, match.end(), indent, "block", f"&{name}", set())[0])
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
        value = _attr_value(attr)[1]
        if is_secret_name(value):
            return value
    return None


def _attr_value(attr: re.Match[str]) -> tuple[str, str]:
    """Return the group that holds the value of *attr* (quoted or bare) and the value."""
    group = next(g for g in _ATTR_VALUE_GROUPS if attr.group(g) is not None)
    return group, attr.group(group)


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
                    group, value = _attr_value(attr)
                    if not is_stub(value):
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
        elif not tag.group("close") and is_secret_name(local):
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
    if is_stub(plain):
        return []
    lead = len(text[start:end]) - len(text[start:end].lstrip())
    return [SecretValue(start + lead, end, "element", name)]


def _find_pem_keys(text: str) -> list[SecretValue]:
    return [
        SecretValue(match.start("body"), match.end("body"), "PEM private key", match.group("kind"))
        for match in _PEM_RE.finditer(text)
        if not is_stub(match.group("body"))
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
