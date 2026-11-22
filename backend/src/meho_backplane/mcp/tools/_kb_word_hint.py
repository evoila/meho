# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The key-word hint of ``search_knowledge``, and its switch.

It is on only when the ``KB_SEARCH_WORD_HINT`` setting is on (off by
default). While it is off, nothing here changes the tool text or the answer.
When it is on:

* the ``query`` input tells the agent to search with a few key words, and to
  search again with other words when ``word_match`` is ``none``;
* the answer has ``word_match``: ``some`` when at least one stored hit has a
  word score (``bm25_score``), ``none`` when no hit has one. Knowledge repo
  hits do not count.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Final

from meho_backplane.kb.schemas import KbEntrySearchHit
from meho_backplane.mcp.registry import ToolDefinition
from meho_backplane.settings import get_settings

__all__ = ["QUERY_HINT", "with_word_hint", "word_hint_extra"]

#: The ``query`` text while the switch is on.
QUERY_HINT: Final[str] = (
    "Free text; ranked by BM25 and meaning. Search with a few key words "
    "(product, error text, the main nouns), not the whole ticket text. If "
    "`word_match` is `none`, search again with other words."
)


def word_hint_extra(hits: Sequence[KbEntrySearchHit]) -> dict[str, Any]:
    """Return the ``word_match`` key for the answer, or nothing while the switch is off."""
    if not get_settings().kb_search_word_hint:
        return {}
    some = any(hit.bm25_score is not None for hit in hits)
    return {"word_match": "some" if some else "none"}


def with_word_hint(
    inner: Callable[[ToolDefinition, Any], ToolDefinition],
) -> Callable[[ToolDefinition, Any], ToolDefinition]:
    """Return a ``for_settings`` function that runs *inner*, then sets the ``query`` hint."""

    def for_settings(definition: ToolDefinition, operator: Any) -> ToolDefinition:
        shown = inner(definition, operator)
        if not get_settings().kb_search_word_hint:
            return shown
        schema = {
            **shown.inputSchema,
            "properties": {
                **shown.inputSchema["properties"],
                "query": {**shown.inputSchema["properties"]["query"], "description": QUERY_HINT},
            },
        }
        return shown.model_copy(update={"inputSchema": schema})

    return for_settings
