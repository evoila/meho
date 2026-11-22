# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Find the MCP tool calls and the ``meho`` CLI commands in a checked transcript.

A Claude Code transcript is a JSON Lines file. A tool call is a content
block ``{"type": "tool_use", "id": ..., "name": ..., "input": {...}}`` in the
``message`` of a line. The name of an MCP tool is
``mcp__<server>__<tool>``; the part after the last ``__`` is the name MEHO
writes as ``op_id`` on the audit row of the call.

A ``meho`` CLI command is a ``Bash`` tool call whose ``command`` runs
``meho``. Its answer is a ``{"type": "tool_result", "tool_use_id": ...}``
block in a later line. Each line has a ``timestamp``, so the command ran
between the time of its call line and the time of its answer line
(:class:`CliCall`).

:func:`tool_calls_in` runs in the upload worker (:mod:`.prepare`), so it
does no I/O and imports nothing heavy. It reads the checked bytes: a
known secret is already replaced there.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

__all__ = ["MAX_TOOL_CALLS", "CliCall", "ToolCall", "cli_calls_in", "tool_calls_in"]

#: The most tool calls MEHO reads from one upload. The rest get no link.
MAX_TOOL_CALLS: Final[int] = 2000

_MCP_PREFIX: Final[str] = "mcp__"

#: One MCP tool call: the tool name and its arguments.
ToolCall = tuple[str, dict[str, Any]]

#: ``meho`` as a command word: at the start, or after a space, ``;``, ``&``,
#: ``|``, ``(`` or a path such as ``/usr/local/bin/``.
_MEHO_COMMAND: Final[re.Pattern[str]] = re.compile(r"(?:^|[\s;&|(/])meho(?=\s)")

#: The words of a command line, without quotes and shell signs.
_WORD: Final[re.Pattern[str]] = re.compile(r"[^\s'\"=;&|()]+")


@dataclass(frozen=True)
class CliCall:
    """One ``meho`` command the agent ran in the ``Bash`` tool."""

    words: frozenset[str]
    """The words of the command line (an operation id is one of them)."""
    started_at: datetime
    """The time of the call line."""
    ended_at: datetime
    """The time of the line with its answer."""


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


def _line_time(entry: dict[str, Any]) -> datetime | None:
    stamp = entry.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def _blocks(entry: object) -> list[dict[str, Any]]:
    message = entry.get("message") if isinstance(entry, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _meho_command(block: dict[str, Any]) -> str | None:
    if block.get("type") != "tool_use" or block.get("name") != "Bash":
        return None
    arguments = block.get("input")
    command = arguments.get("command") if isinstance(arguments, dict) else None
    if isinstance(command, str) and _MEHO_COMMAND.search(command):
        return command
    return None


def cli_calls_in(content: bytes, *, limit: int = MAX_TOOL_CALLS) -> list[CliCall]:
    """Return up to *limit* ``meho`` commands of the JSON lines in *content*.

    A command whose answer line is not in *content*, or a line with no
    valid ``timestamp`` (with its time zone), is left out.
    """
    started: dict[str, tuple[str, datetime]] = {}
    calls: list[CliCall] = []
    for line in content.splitlines():
        if len(calls) >= limit:
            break
        if b"tool_use" not in line and b"tool_result" not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        when = _line_time(entry) if isinstance(entry, dict) else None
        if when is None:
            continue
        for block in _blocks(entry):
            command = _meho_command(block)
            if command is not None and isinstance(block.get("id"), str):
                started[block["id"]] = (command, when)
            elif block.get("type") == "tool_result":
                begin = started.pop(str(block.get("tool_use_id")), None)
                if begin is not None and begin[1] <= when:
                    words = frozenset(_WORD.findall(begin[0]))
                    calls.append(CliCall(words=words, started_at=begin[1], ended_at=when))
    return calls[:limit]
