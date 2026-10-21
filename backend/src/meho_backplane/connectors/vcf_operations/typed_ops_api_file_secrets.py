# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Secret shapes ``vrops.api_file.get`` refuses before it returns a file.

The stored-result redaction (:mod:`meho_backplane.redaction`) finds a
labelled secret only when a bare label sits directly before ``=`` or
``:`` and the value is at least 8 characters with no whitespace
(``password=...``, ``token: ...``). That leaves gaps a file can fall
through:

* A JSON or YAML file writes the label in quotes (``"password": "..."``).
* A short password (``"password": "hunter2"``) or one made of words
  (``"password": "my dog is cute"``) is below the length bound or has
  whitespace in it.
* A licence key has a label the redaction rules do not know
  (``"licenseKey": "..."``).
* A PEM private key has no label at all.

The read must never return a file with a secret in clear text, so it
checks these shapes itself, on the whole file text, and refuses the file
when one is found. A field is a quoted key (``"password"``, ``'token'``)
anywhere in the text, or a bare key at the start of a line (YAML, INI,
properties). Its complete value counts: a quoted value up to its closing
quote, whitespace included, or an unquoted value to the end of the line.
Only a schema stub is let through: an empty value, a JSON schema type
word such as ``string``, ``null``, ``true`` or ``false``, a placeholder in
angle brackets or template braces, or a JSON or YAML object or list
(``{``, ``[``, or a nested mapping on the next lines). So
``"password": {"type": "string"}`` and ``"token": "string"`` stay
accepted. YAML can also write the value after a tag (``!!str``), an
anchor (``&name``), as a block scalar (``|`` or ``>``, the value on the
next lines), as a plain scalar on the next line, or as an alias
(``*name``) of an anchored value elsewhere in the file. The check reads
through each of these to the value it marks and judges that value, so a
password in a block scalar or behind a tag is refused like a plain one.
A bare label in running text (``Use password: ...``) keeps the
redaction's own bound, so a sentence about passwords is not refused.

The check names the shape and where it starts; it never repeats the
matched value.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["find_secret_shape"]

#: The labels the redaction rule ``api_key`` knows, plus the licence and
#: product key labels it lacks and the ``Authorization`` header as a
#: field. More specific members come before the broad bare ``token`` so
#: the reported label is the full one.
_LABEL: Final[str] = (
    r"(?P<label>api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token"
    r"|session[_-]?token|secret(?:[_-]?(?:key|id))?|private[_-]?key|password|passwd|pwd"
    r"|client[_-]?secret|licen[cs]e[_-]?key|product[_-]?key|authorization|token)"
)

#: The complete value of a field: a double-quoted or single-quoted string
#: up to its closing quote (no length bound, whitespace allowed), or an
#: unquoted value up to the end of the line or the next ``,``, ``}`` or
#: ``]`` (one-line JSON). Each branch is one negated character class, so
#: matching stays linear.
_FIELD_VALUE: Final[str] = (
    r"""\s*[=:][ \t]*(?:"(?P<dq>[^"\n]*)"|'(?P<sq>[^'\n]*)'|(?P<line>[^\n,}\]]*))"""
)

#: ``"password": "value"``, ``'x-auth-token': 'value'``: a quoted key
#: anywhere in the text. The key may carry a prefix inside the quotes
#: (``"userPassword"``, ``"vcenterLicenseKey"``), but must end with the
#: label, so ``"passwordPolicy"`` and ``"tokenUrl"`` do not match. The
#: prefix is lazy, so the reported label is the longest one
#: (``auth-token``, not ``token``, for ``"x-auth-token"``).
_QUOTED_FIELD: Final[re.Pattern[str]] = re.compile(
    r"""(?<![\w-])['"][\w.-]*?""" + _LABEL + r"""['"]""" + _FIELD_VALUE,
    re.IGNORECASE,
)

#: ``password: value`` as a YAML, INI or properties key: a bare key at the
#: start of a line, also as a list item (``- password: value``).
_LINE_FIELD: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*(?:-[ \t]+)?[\w.-]*?" + _LABEL + _FIELD_VALUE,
    re.IGNORECASE | re.MULTILINE,
)

#: A bare label in running text, with the redaction's own bound: a value
#: of at least 8 characters with no whitespace. The redaction rule
#: ``api_key`` finds the same text for the labels it knows; this keeps
#: the licence and product key labels covered, and the rest when a
#: redaction policy scopes the rule out for this operation.
_BARE_FIELD: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w-])[\w.-]*?" + _LABEL + r"""\s*[=:]\s*['"]?[^\s'"]{8,}""",
    re.IGNORECASE,
)

#: PEM private keys of every kind (``PRIVATE KEY``, ``RSA PRIVATE KEY``,
#: ``EC PRIVATE KEY``, ``ENCRYPTED PRIVATE KEY``, ``OPENSSH PRIVATE KEY``).
_PEM_PRIVATE_KEY: Final[re.Pattern[str]] = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY", re.IGNORECASE
)

#: Values a schema or an example writes in place of a secret.
_STUB_WORDS: Final[frozenset[str]] = frozenset(
    {"string", "integer", "number", "boolean", "object", "array", "null", "true", "false"}
)

#: A YAML block scalar indicator (``|``, ``>-``, ``|+2``), with an
#: optional trailing comment: the value is on the lines below.
_BLOCK_INDICATOR: Final[re.Pattern[str]] = re.compile(r"[|>][-+0-9]*(?:[ \t]+#.*)?$")

#: A line that opens a nested YAML structure: a mapping entry or a list
#: item. The lines below an empty value are a structure when the first
#: one looks like this, and a plain scalar otherwise.
_MAPPING_OR_ITEM: Final[re.Pattern[str]] = re.compile(r"""^(?:-(?:\s|$)|[\w"'.\-]+\s*:(?:\s|$))""")

#: The anchor *name* and the value on its line, if any.
_ANCHOR_VALUE: Final[str] = r"(?<![\w-])&{name}(?:[ \t]+(?P<value>[^\n]*))?$"


def _is_stub(value: str) -> bool:
    """Return whether a scalar *value* is a schema stub, not a secret."""
    stripped = value.strip()
    if not stripped or stripped.lower() in _STUB_WORDS:
        return True
    if stripped[0] == "<" and stripped[-1] == ">":
        return True
    return stripped.startswith(("{{", "${")) and stripped.endswith("}")


def _block_body(text: str, pos: int) -> list[str]:
    """Return the first lines indented deeper than the line that holds *pos*.

    Blank lines are dropped. A stub is one word, so two lines are enough
    to decide; the scan stops there, or at the next line that is not
    indented deeper than the key line.
    """
    line_start = text.rfind("\n", 0, pos) + 1
    line_end = text.find("\n", pos)
    if line_end < 0:
        return []
    key_line = text[line_start:line_end]
    indent = len(key_line) - len(key_line.lstrip(" \t"))
    body: list[str] = []
    for line in text[line_end + 1 :].split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if len(line) - len(line.lstrip(" \t")) <= indent:
            break
        body.append(stripped)
        if len(body) == 2:
            break
    return body


def _unquoted_is_stub(text: str, pos: int, value: str, *, follow_alias: bool = True) -> bool:
    """Return whether the unquoted *value* found at *pos* in *text* is a stub.

    The value is the rest of the key line. A tag or an anchor in front is
    skipped; a quoted string, a block scalar, an alias, a comment and an
    empty value are read through to the value they stand for.
    """
    stripped = value.strip()
    while stripped[:1] in ("!", "&"):
        stripped = stripped.partition(" ")[2].strip()
    if stripped[:1] in ('"', "'"):
        return _is_stub(stripped[1:].partition(stripped[0])[0])
    if stripped[:1] in ("{", "["):
        return True
    if _BLOCK_INDICATOR.match(stripped):
        return _is_stub(" ".join(_block_body(text, pos)))
    if stripped[:1] == "*":
        return follow_alias and _anchor_is_stub(text, stripped[1:].strip())
    if not stripped or stripped[0] == "#":
        body = [line for line in _block_body(text, pos) if line[0] != "#"]
        if not body or _MAPPING_OR_ITEM.match(body[0]):
            return True
        return _is_stub(" ".join(body))
    return _is_stub(stripped)


def _anchor_is_stub(text: str, name: str) -> bool:
    """Return whether the value anchored as *name* in *text* is a stub.

    A missing anchor, or one on a structure, is not a secret here. A
    chained alias is not followed.
    """
    if not name:
        return True
    anchor = re.search(_ANCHOR_VALUE.format(name=re.escape(name)), text, re.MULTILINE)
    if anchor is None:
        return True
    return _unquoted_is_stub(text, anchor.start(), anchor.group("value") or "", follow_alias=False)


def _first_field_hit(text: str) -> re.Match[str] | None:
    """Return the first field whose value is a secret, or ``None``.

    A quoted key or a line-start key decides by its complete value, stub
    or not. A bare label in running text counts only outside those
    fields, so a line such as ``licenseKey: <licence key>`` is judged as
    a field (a placeholder) and not as prose.
    """
    fields = [match for pattern in (_QUOTED_FIELD, _LINE_FIELD) for match in pattern.finditer(text)]
    hits = []
    for match in fields:
        line_value = match.group("line")
        if line_value is None:
            stub = _is_stub(match.group("dq") or match.group("sq") or "")
        else:
            stub = _unquoted_is_stub(text, match.start("line"), line_value)
        if not stub:
            hits.append(match)
    for match in _BARE_FIELD.finditer(text):
        if not any(field.start() <= match.start() < field.end() for field in fields):
            hits.append(match)
            break
    return min(hits, key=lambda match: match.start(), default=None)


def find_secret_shape(text: str) -> str | None:
    """Return the first secret shape in *text* and where it starts, or ``None``.

    The answer is one phrase, for example ``a PEM private key at
    character 1024`` or ``a secret field named 'licenseKey' at character
    20``. The field name is the label as the file writes it; the value is
    never returned.
    """
    pem = _PEM_PRIVATE_KEY.search(text)
    if pem is not None:
        return f"a PEM private key at character {pem.start()}"
    hit = _first_field_hit(text)
    if hit is None:
        return None
    return f"a secret field named {hit.group('label')!r} at character {hit.start()}"
