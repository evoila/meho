# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""YAML loading for console imports: no anchors and no aliases.

An alias (``*name``) counts as a full copy of the block it names, so a few
nested aliases in a small file stand for a very large document. The console
imports (targets and topology) use :func:`load_yaml_no_alias`, which
refuses anchors and aliases before any node is built. The file must write
each value out.
"""

from __future__ import annotations

from typing import Any

import yaml

__all__ = ["YamlAliasRefusedError", "load_yaml_no_alias"]


class YamlAliasRefusedError(yaml.YAMLError):  # type: ignore[misc]  # yaml has no stubs here
    """The text holds a YAML anchor or alias."""

    def __init__(self, line: int) -> None:
        super().__init__(
            f"anchors (`&`) and aliases (`*`) are not accepted (line {line}); write each value out"
        )


class _NoAliasLoader(yaml.SafeLoader):  # type: ignore[misc]  # yaml has no stubs here
    def compose_node(self, parent: yaml.Node | None, index: Any) -> yaml.Node:
        event = self.peek_event()
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise YamlAliasRefusedError(event.start_mark.line + 1)
        return super().compose_node(parent, index)


def load_yaml_no_alias(text: str) -> Any:
    """Parse *text* like ``yaml.safe_load``, but refuse anchors and aliases.

    Raises:
        yaml.YAMLError: The text is not valid YAML, or holds an anchor or alias.
    """
    loader = _NoAliasLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()
