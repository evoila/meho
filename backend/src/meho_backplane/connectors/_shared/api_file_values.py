# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The value walk shared by every format of :mod:`.api_file_secrets`.

:class:`Scrubber` replaces secret values in parsed data (what JSON and
YAML give, and what a text value that holds JSON gives) and keeps the
count of replacements. The markup writers in :mod:`.api_file_markup` use
it for text and attribute values; :mod:`.api_file_secrets` uses it for
whole JSON and YAML files.

A caller can give the scrubber one more rule for text values
(``text_rule``): a function that takes one text value and returns the
changed value and how many places changed. It runs on every text value
that is not replaced whole, so a rule that scans text (MEHO's general
secret-redaction rules, for example) changes the value before the file
is written back. The writer then quotes or escapes the changed value as
the format needs, and the file keeps its shape.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Final

from meho_backplane.broadcast.events import (
    is_secret_param_name,
    is_secret_param_name_lowercased,
)

__all__ = [
    "REDACTED",
    "ApiFileFormatError",
    "RepeatedKeyError",
    "Scrubber",
    "TextRule",
    "dump_json",
    "is_secret_name",
    "is_secret_name_lowercased",
    "load_json",
]

#: A rule for one text value: returns the changed value and the count of
#: places changed.
TextRule = Callable[[str], tuple[str, int]]

#: Written in place of every replaced value: the placeholder the governed
#: Vault read returns for a secret-named value, so a reader sees one
#: vocabulary.
REDACTED: Final[str] = "[REDACTED:param_name]"

_PEM_PRIVATE_KEY: Final[re.Pattern[str]] = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")


#: The end of a name that holds a login session id (``sessionId``,
#: ``session_id``, ``X-Session-Id``, ``JSESSIONID``), with case, ``-``,
#: ``_`` and ``.`` removed. Some appliances use the session id as the
#: login token, so an API file example of it is a secret. MEHO's general
#: rule for secret names does not count it: there a ``session_id`` is
#: often MEHO's own agent session, which is no secret.
_SESSION_ID_NAME: Final[str] = "sessionid"


def _ends_in_session_id(name: str) -> bool:
    return (
        name.replace("-", "").replace("_", "").replace(".", "").lower().endswith(_SESSION_ID_NAME)
    )


def is_secret_name(name: str) -> bool:
    """True when the value under *name* is a secret in an API file.

    MEHO's general rule (:func:`is_secret_param_name`) plus a name that
    ends in a session id.
    """
    return is_secret_param_name(name) or _ends_in_session_id(name)


def is_secret_name_lowercased(name: str) -> bool:
    """:func:`is_secret_name` for a name whose case is lost (HTML)."""
    return is_secret_param_name_lowercased(name) or _ends_in_session_id(name)


class ApiFileFormatError(ValueError):
    """The file has no known format or does not parse. The message says why."""


def dump_json(data: Any) -> str:
    """Write *data* as compact JSON, close to the minified form appliances serve."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


class RepeatedKeyError(ValueError):
    """One JSON object names a key twice; the parser would keep one value and drop the other."""


def load_json(text: str) -> Any:
    """Parse *text* as JSON, whole: an object that names a key twice is refused.

    :func:`json.loads` keeps the last value under a repeated key and drops
    the others without a word. A check of the parsed copy then says
    nothing about a dropped value, and a count of replacements stays at
    zero while the text still holds it. Raises :exc:`RepeatedKeyError`,
    a :exc:`ValueError`, for a key named twice in one object at any
    depth; any other :exc:`ValueError` is the parser's own.
    """
    return json.loads(text, object_pairs_hook=_object_without_repeats)


def _object_without_repeats(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key, value in pairs:
        if key in mapping:
            raise RepeatedKeyError(key)
        mapping[key] = value
    return mapping


class Scrubber:
    """Replace secret values in parsed data; keep the count of replacements."""

    def __init__(self, count: int = 0, *, text_rule: TextRule | None = None) -> None:
        self.count = count
        self._text_rule = text_rule

    def fresh(self) -> Scrubber:
        """A scrubber with the same text rule and a count of zero, to check one part on its own."""
        return Scrubber(text_rule=self._text_rule)

    def tree(self, node: Any, memo: dict[int, Any]) -> Any:
        """Return a copy of *node* with every secret value replaced.

        *memo* keeps one copy per container, so a container the file
        shares (a YAML alias) is walked once and stays shared in the
        copy; the YAML writer then writes an alias for it, not another
        copy.
        """
        if isinstance(node, dict):
            if id(node) in memo:
                return memo[id(node)]
            mapping: dict[Any, Any] = {}
            memo[id(node)] = mapping
            for key, value in node.items():
                if is_secret_name(str(key)) and value != REDACTED:
                    mapping[key] = REDACTED
                    self.count += 1
                else:
                    mapping[key] = self.tree(value, memo)
            return mapping
        if isinstance(node, (list, tuple)):
            if id(node) in memo:
                return memo[id(node)]
            items: list[Any] = []
            memo[id(node)] = items
            items.extend(self.tree(item, memo) for item in node)
            return items
        if isinstance(node, str):
            return self.text(node)
        return node

    def text(self, value: str) -> str:
        """Check one text value: a PEM private key, JSON inside the text, the text rule.

        JSON inside the text comes back written from the parsed copy,
        also when nothing was replaced. JSON that names a key twice in
        one object, at any depth, is withheld whole and counted once:
        the parser would keep one value and drop the other, so neither
        the parsed copy nor the count would say anything about the
        dropped one, and a caller that keeps the original text (the
        fail-closed HTML block) would keep it unseen. The text rule runs
        on the values inside such JSON, not on the written JSON text, so
        the text stays JSON.
        """
        if _PEM_PRIVATE_KEY.search(value):
            self.count += 1
            return REDACTED
        stripped = value.strip()
        if stripped and stripped[0] in "{[":
            try:
                parsed = load_json(stripped)
            except RepeatedKeyError:
                self.count += 1
                return REDACTED
            except ValueError:
                parsed = None
            if isinstance(parsed, (dict, list)):
                return dump_json(self.tree(parsed, {}))
        if self._text_rule is None:
            return value
        changed, count = self._text_rule(value)
        self.count += count
        return changed
