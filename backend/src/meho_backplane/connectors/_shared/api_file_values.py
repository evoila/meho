# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The value walk shared by every format of :mod:`.api_file_secrets`.

:class:`Scrubber` replaces secret values in parsed data (what JSON and
YAML give, and what a text value that holds JSON gives) and keeps the
count of replacements. The markup writers in :mod:`.api_file_markup` use
it for text and attribute values; :mod:`.api_file_secrets` uses it for
whole JSON and YAML files.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

from meho_backplane.broadcast.events import is_secret_param_name

__all__ = ["REDACTED", "ApiFileFormatError", "Scrubber", "dump_json"]

#: Written in place of every replaced value: the placeholder the governed
#: Vault read returns for a secret-named value, so a reader sees one
#: vocabulary.
REDACTED: Final[str] = "[REDACTED:param_name]"

_PEM_PRIVATE_KEY: Final[re.Pattern[str]] = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")


class ApiFileFormatError(ValueError):
    """The file has no known format or does not parse. The message says why."""


def dump_json(data: Any) -> str:
    """Write *data* as compact JSON, close to the minified form appliances serve."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


class Scrubber:
    """Replace secret values in parsed data; keep the count of replacements."""

    def __init__(self, count: int = 0) -> None:
        self.count = count

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
                if is_secret_param_name(str(key)) and value != REDACTED:
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
        """Check one text value: a PEM private key, or JSON inside the text."""
        if _PEM_PRIVATE_KEY.search(value):
            self.count += 1
            return REDACTED
        stripped = value.strip()
        if not stripped or stripped[0] not in "{[":
            return value
        try:
            parsed = json.loads(stripped)
        except ValueError:
            return value
        if not isinstance(parsed, (dict, list)):
            return value
        before = self.count
        checked = self.tree(parsed, {})
        return value if self.count == before else dump_json(checked)
