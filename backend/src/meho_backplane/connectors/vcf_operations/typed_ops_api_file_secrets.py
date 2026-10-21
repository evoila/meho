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
angle brackets or template braces, or a value that starts a YAML or JSON
structure (``{``, ``[``, a block scalar, an anchor, an alias, a tag or a
comment). So ``"password": {"type": "string"}`` and ``"token": "string"``
stay accepted. A bare label in running text (``Use password: ...``) keeps
the redaction's own bound, so a sentence about passwords is not refused.

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

#: First characters of an unquoted value that starts a structure, not a
#: secret: a JSON or YAML flow object or list, a YAML block scalar, an
#: anchor, an alias, a tag, or a comment.
_STRUCTURE_STARTS: Final[str] = "{[|>&*!#"


def _is_stub(value: str, *, quoted: bool) -> bool:
    """Return whether a field *value* is a schema stub, not a secret."""
    stripped = value.strip()
    if not stripped or stripped.lower() in _STUB_WORDS:
        return True
    if stripped[0] == "<" and stripped[-1] == ">":
        return True
    if stripped.startswith(("{{", "${")) and stripped.endswith("}"):
        return True
    return not quoted and stripped[0] in _STRUCTURE_STARTS


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
        quoted = match.group("line") is None
        value = match.group("dq") or match.group("sq") or match.group("line") or ""
        if not _is_stub(value, quoted=quoted):
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
