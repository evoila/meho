# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Find the MCP tool calls in a checked transcript.

A Claude Code transcript is a JSON Lines file. A tool call is a content
block ``{"type": "tool_use", "name": ..., "input": {...}}`` in the
``message`` of a line. The name of an MCP tool is
``mcp__<server>__<tool>``; the part after the last ``__`` is the name MEHO
writes as ``op_id`` on the audit row of the call.

:func:`tool_calls_in` runs in the upload worker (:mod:`.prepare`), so it
does no I/O and imports nothing heavy. It reads the checked bytes: a
known secret is already replaced there.
"""

from __future__ import annotations

import json
from typing import Any, Final

__all__ = ["MAX_TOOL_CALLS", "ToolCall", "tool_calls_in"]

#: The most tool calls MEHO reads from one upload. The rest get no link.
MAX_TOOL_CALLS: Final[int] = 2000

_MCP_PREFIX: Final[str] = "mcp__"

#: One MCP tool call: the tool name and its arguments.
ToolCall = tuple[str, dict[str, Any]]


def _tool_name(name: object) -> str | None:
    if not isinstance(name, str) or not name.startswith(_MCP_PREFIX):
        return None
    server_and_tool = name[len(_MCP_PREFIX) :]
    if "__" not in server_and_tool:
        return None
    tool = server_and_tool.rsplit("__", 1)[1]
    return tool or None


def _calls_of_line(line: bytes) -> list[ToolCall]:
    try:
        entry = json.loads(line)
    except ValueError:
        return []
    message = entry.get("message") if isinstance(entry, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    calls: list[ToolCall] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        tool = _tool_name(block.get("name"))
        arguments = block.get("input")
        if tool is not None and isinstance(arguments, dict):
            calls.append((tool, arguments))
    return calls


def tool_calls_in(content: bytes, *, limit: int = MAX_TOOL_CALLS) -> list[ToolCall]:
    """Return up to *limit* MCP tool calls of the JSON lines in *content*."""
    calls: list[ToolCall] = []
    for line in content.splitlines():
        if len(calls) >= limit:
            break
        if b"tool_use" in line:
            calls.extend(_calls_of_line(line))
    return calls[:limit]
