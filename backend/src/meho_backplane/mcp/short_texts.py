# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Short tool texts behind the ``MCP_SHORT_TOOL_TEXTS`` switch (#4172).

The tool text on the default list is sent to the model before the user's
question. A small model has a small context window, so the biggest tools
get a short version: a short description and short input texts.

The switch is off by default. With it off, every tool shows its registered
text, unchanged. With it on, a tool that has a :class:`ShortText` shows
that text instead. Only the ``description`` strings change. The input
schema keeps every property, type, enum, default, limit and ``required``
list, so a tool accepts exactly the same calls in both states.

A tool opts in through :attr:`ToolDefinition.for_settings`:
``for_settings=with_short_text(SHORT)``. A tool that already has a
``for_settings`` function passes it as ``then``; it runs first, so a
property it takes away (for example ``create_anyway`` while the
similar-entry check is off) stays away.

The hosted-run tools and the reference docs use the registered tool, so
they keep the full text.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from meho_backplane.mcp.registry import ToolDefinition, _with_maturity_prefix
from meho_backplane.settings import get_settings

__all__ = [
    "ShortText",
    "apply_short_text",
    "input_property",
    "with_short_text",
]

ForSettings = Callable[[ToolDefinition], ToolDefinition]


@dataclass(frozen=True)
class ShortText:
    """The short text of one tool.

    ``description`` replaces the tool description. The registry adds the
    maturity label (``[beta]``) the same way it does for the full text.

    ``inputs`` maps an input path to its short description. A path is the
    property names joined with ``.``; a step into an array goes through its
    ``items`` (``query.filter.value``). ``None`` drops the property's
    description, for an input whose name and schema say enough.
    """

    description: str
    inputs: Mapping[str, str | None] = field(default_factory=dict)


def input_property(schema: dict[str, Any], path: str) -> dict[str, Any] | None:
    """Return the schema of the input at *path* (a node inside *schema*), or ``None``."""
    node = schema
    for name in path.split("."):
        holder = node if "properties" in node else node.get("items", {})
        found = holder.get("properties", {}).get(name)
        if not isinstance(found, dict):
            return None
        node = found
    return node


def apply_short_text(definition: ToolDefinition, short: ShortText) -> ToolDefinition:
    """Return *definition* with the short description and input texts of *short*.

    A path that is not in the schema is skipped: a ``for_settings`` step
    before this one may have taken that input away.
    """
    schema = copy.deepcopy(definition.inputSchema)
    for path, text in short.inputs.items():
        prop = input_property(schema, path)
        if prop is None:
            continue
        if text is None:
            prop.pop("description", None)
        else:
            prop["description"] = text
    plain = definition.model_copy(update={"description": short.description, "inputSchema": schema})
    return _with_maturity_prefix(plain)


def with_short_text(short: ShortText, *, then: ForSettings | None = None) -> ForSettings:
    """Return a ``for_settings`` function that shows *short* while the switch is on.

    *then* is the tool's other ``for_settings`` function, if it has one. It
    runs first, in both states.
    """

    def for_settings(definition: ToolDefinition) -> ToolDefinition:
        shown = then(definition) if then is not None else definition
        if not get_settings().mcp_short_tool_texts:
            return shown
        return apply_short_text(shown, short)

    return for_settings
