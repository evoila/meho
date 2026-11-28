# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Safe texts for the dry run that an agent's own preview of ``k8s.apply`` shows.

The cluster can quote any value of the manifest in a warning or a refusal:
an admission check may name the value of a Secret's ``stringData`` that it
does not like. The pattern redaction finds only values that look like a
secret (a token, a key). So :class:`ManifestValues` knows every value that
the preview hides in its echo of this manifest
(``ops_write_preview.apply_manifest_echo``): Secret values, ``env`` and
``httpHeaders`` values, command lines and the last-applied annotation, and
each value the shared params redaction hides. The redaction runs over the
whole list of documents, as for the echo, so a rule for one place in the
list (such as ``manifest.1.metadata.name``) counts too. For a Secret's
``data`` it also knows the base64 text on one line (also for binary
data) and the decoded text; line breaks in the base64 text are skipped,
as the API server skips them. For a Secret's ``stringData`` it also knows
the base64 text of each value, because the API server writes that plain
text into ``data`` as base64.

In a text of the cluster or of MEHO:

* each such value of :data:`SHORTEST_REPLACED` characters or more is
  replaced by the hidden mark, wherever it is;
* when a shorter value stands alone (no letter or digit next to it), the
  whole text is withheld (:data:`WITHHELD`), because replacing a short
  value would also change other words.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable, Iterable
from typing import Any, Final

from meho_backplane.connectors.kubernetes.op_classes import SECRET_VALUE_FIELDS
from meho_backplane.connectors.kubernetes.ops_write_preview import _hide_values
from meho_backplane.redaction.command_line import HIDDEN

__all__ = ["SHORTEST_REPLACED", "WITHHELD", "ManifestValues"]

#: A hidden value this long or longer is replaced where it stands.
SHORTEST_REPLACED: Final[int] = 4

#: The text shown in place of a text that may quote a short hidden value.
WITHHELD: Final[str] = (
    "The text of the cluster is not shown, because it may quote a value that this preview hides."
)

#: Stands for a key that the hidden copy does not have.
_GONE: Final[object] = object()


def _leaves(node: Any, out: set[str]) -> None:
    """Add every scalar value under *node* to *out*, as text."""
    if isinstance(node, dict):
        for value in node.values():
            _leaves(value, out)
    elif isinstance(node, list):
        for value in node:
            _leaves(value, out)
    elif node is not None:
        _add(node, out)


def _add(value: Any, out: set[str]) -> None:
    """Add *value* as the cluster may write it: its text and its JSON form."""
    text = value if isinstance(value, str) else json.dumps(value)
    out.add(text)
    out.add(str(value))
    quoted = json.dumps(text)[1:-1]
    if quoted != text:
        out.add(quoted)


def _hidden_leaves(raw: Any, shown: Any, out: set[str]) -> None:
    """Add to *out* each value of *raw* that the hidden copy *shown* does not show."""
    if isinstance(raw, dict):
        if not isinstance(shown, dict):
            _leaves(raw, out)
            return
        for key, value in raw.items():
            _hidden_leaves(value, shown.get(key, _GONE), out)
    elif isinstance(raw, list):
        if not isinstance(shown, list) or len(shown) != len(raw):
            _leaves(raw, out)
            return
        for value, shown_value in zip(raw, shown, strict=True):
            _hidden_leaves(value, shown_value, out)
    elif raw is not None and raw != shown:
        _add(raw, out)


def _one_line(value: str) -> str:
    """The base64 *value* without line breaks, as the API server reads it."""
    return value.replace("\r", "").replace("\n", "")


def _decoded(value: str) -> str | None:
    """The text that the base64 *value* holds, or ``None``.

    The API server skips line breaks in base64 text (a YAML block value
    ends with one), so they are taken out first.
    """
    try:
        return base64.b64decode(_one_line(value), validate=True).decode("utf-8") or None
    except (binascii.Error, ValueError):
        return None


def _secret_data_forms(value: Any, out: set[str]) -> None:
    """Add the forms the cluster may quote of one Secret ``data`` *value*.

    That is the base64 text on one line, also for binary data, and the
    decoded text when it is UTF-8.
    """
    if not isinstance(value, str):
        return
    _add(_one_line(value), out)
    decoded = _decoded(value)
    if decoded is not None:
        _add(decoded, out)


def _string_data_forms(value: Any, out: set[str]) -> None:
    """Add the base64 text of one Secret ``stringData`` *value*.

    The API server moves ``stringData`` into ``data``, as base64 of the
    UTF-8 text, so the cluster may quote that form.
    """
    if isinstance(value, str):
        _add(base64.b64encode(value.encode("utf-8")).decode("ascii"), out)


def _decoded_secret_data(doc: Any, out: set[str]) -> None:
    """Add the other forms of each Secret value under *doc* that the cluster may quote."""
    if isinstance(doc, list):
        for item in doc:
            _decoded_secret_data(item, out)
        return
    if not isinstance(doc, dict):
        return
    if doc.get("kind") == "Secret":
        for field in SECRET_VALUE_FIELDS:
            values = doc.get(field)
            forms = _string_data_forms if field == "stringData" else _secret_data_forms
            for value in values.values() if isinstance(values, dict) else ():
                forms(value, out)
    for item in doc.values():
        _decoded_secret_data(item, out)


class ManifestValues:
    """The values the preview hides in the echo of one manifest, and how to hide them in a text."""

    def __init__(self, values: Iterable[str]) -> None:
        kept = {value for value in values if value.strip()}
        self._long = sorted(
            (value for value in kept if len(value) >= SHORTEST_REPLACED), key=len, reverse=True
        )
        self._short = [
            re.compile(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])")
            for value in kept
            if len(value) < SHORTEST_REPLACED
        ]

    @classmethod
    def of(
        cls,
        docs: list[dict[str, Any]],
        redact: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    ) -> ManifestValues:
        """The values of *docs* (made plain) that the echo hides, also after *redact*.

        *redact* runs once over the whole list, in its order, as for the
        echo. When it does not give back one document for each, MEHO cannot
        tell which hidden copy is which, so every value counts as hidden.
        """
        out: set[str] = set()
        hidden = redact([_hide_values(doc) for doc in docs])
        shown: list[Any] = hidden if len(hidden) == len(docs) else [_GONE] * len(docs)
        for doc, shown_doc in zip(docs, shown, strict=True):
            _hidden_leaves(doc, shown_doc, out)
            _decoded_secret_data(doc, out)
        return cls(out)

    def hide(self, text: str) -> str:
        """*text* with each hidden value replaced, or :data:`WITHHELD`."""
        for pattern in self._short:
            if pattern.search(text):
                return WITHHELD
        for value in self._long:
            text = text.replace(value, HIDDEN)
        return text
