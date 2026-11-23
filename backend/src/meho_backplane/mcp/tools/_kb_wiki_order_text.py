# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The tool text of ``search_knowledge`` about the order of the hits.

It follows the ``KB_WIKI_INTERLEAVE`` switch (off by default). While it is
off, nothing here changes the tool text or the answer. When it is on, the
hits come in turns (store hit, knowledge repo hit, ...), so the text no
longer says "best first" for the whole list. It says each list is best
first, and the cut text says the hits at the end of the list were left out.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from meho_backplane.mcp.registry import ToolDefinition
from meho_backplane.settings import get_settings

__all__ = [
    "INTERLEAVED_ORDER",
    "PARTIAL_REMEDIATION_IN_TURNS",
    "PLAIN_ORDER",
    "with_wiki_order_text",
]

#: The words of the full and the short text while the switch is off.
PLAIN_ORDER: Final[str] = "one hit per entry, best first"

#: The same words while the switch is on.
INTERLEAVED_ORDER: Final[str] = (
    "one hit per entry, store hits and knowledge repo hits in turns (each list best first)"
)

#: The cut text while the switch is on: the last places of the turn list go first.
PARTIAL_REMEDIATION_IN_TURNS: Final[str] = (
    "{omitted} hits at the end of the list were left out to keep this response "
    "under {limit} bytes. Narrow the query or add filters to find them."
)


def with_wiki_order_text(
    inner: Callable[[ToolDefinition, Any], ToolDefinition],
) -> Callable[[ToolDefinition, Any], ToolDefinition]:
    """Return a ``for_settings`` function that runs *inner*, then names the turn order."""

    def for_settings(definition: ToolDefinition, operator: Any) -> ToolDefinition:
        shown = inner(definition, operator)
        if not get_settings().kb_wiki_interleave:
            return shown
        text = shown.description.replace(PLAIN_ORDER, INTERLEAVED_ORDER)
        return shown.model_copy(update={"description": text})

    return for_settings
