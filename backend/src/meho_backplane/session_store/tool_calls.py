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
(:class:`CliCall`). An upload can end between the call line and its
answer line. The next upload then holds the answer in its new part, and
:func:`cli_calls_in` finds the call line in the part an earlier upload
stored (``earlier``).

:func:`tool_calls_in` runs in the upload worker (:mod:`.prepare`), so it
does no I/O and imports nothing heavy. It reads the checked bytes: a
known secret is already replaced there.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from meho_backplane.session_store.secret_check import check_part

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

#: The most command names in a command path (``vcf-automation catalog item list``).
_MAX_PATH_WORDS: Final[int] = 6

#: The flags that can come before a command name and take the next word as
#: their value (``meho --work-ref CHG-1 vmware vm list``): the global flags
#: of the CLI and the group flags of ``meho vcf-automation``. A flag written
#: as ``--work-ref=CHG-1`` holds its value itself.
_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {"--config", "--work-ref", "--profile", "--plane", "--fqdn"}
)

#: The shell signs that end one command (``;``, ``&&``, ``|`` ...).
_SHELL_SIGNS: Final[frozenset[str]] = frozenset("();<>|&")


@dataclass(frozen=True)
class CliCall:
    """One ``meho`` command the agent ran in the ``Bash`` tool."""

    words: frozenset[str]
    """The words of the command line (an operation id is one of them)."""
    paths: frozenset[str]
    """The command paths the line may run: for each ``meho`` word, the
    first one to six words after it that are not flags or flag values, up to
    the end of that command, joined by a space
    (``vmware``, ``vmware vm``, ``vmware vm list``, ...). The CLI sends the
    path of the running command, so its audit rows carry one of them."""
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


def _shell_words(command: str) -> list[str]:
    """Split *command* as the shell does: quotes removed, ``=`` kept, shell signs apart."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return command.split()


def _command_names(words: list[str]) -> list[str]:
    """Return the first command names in *words*, without flags and their values."""
    names: list[str] = []
    skip_value = False
    for word in words:
        if skip_value:
            skip_value = False
        elif word and set(word) <= _SHELL_SIGNS:
            break
        elif word.startswith("-"):
            skip_value = word in _VALUE_FLAGS
        else:
            names.append(word)
            if len(names) == _MAX_PATH_WORDS:
                break
    return names


def _command_paths(words: list[str]) -> frozenset[str]:
    paths: set[str] = set()
    for index, word in enumerate(words):
        if word != "meho" and not word.endswith("/meho"):
            continue
        names = _command_names(words[index + 1 :])
        paths.update(" ".join(names[: end + 1]) for end in range(len(names)))
    return frozenset(paths)


def _cli_call(command: str, started_at: datetime, ended_at: datetime) -> CliCall:
    return CliCall(
        words=frozenset(_WORD.findall(command)),
        paths=_command_paths(_shell_words(command)),
        started_at=started_at,
        ended_at=ended_at,
    )


def _entry_of(line: bytes) -> tuple[datetime, list[dict[str, Any]]] | None:
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    when = _line_time(entry) if isinstance(entry, dict) else None
    if when is None:
        return None
    return when, _blocks(entry)


def _start_in_earlier(earlier: bytes, call_id: str) -> tuple[str, datetime] | None:
    """Find the ``meho`` call line of *call_id* in the part an earlier upload stored.

    The line is checked again (:func:`.secret_check.check_part`), as the
    earlier upload did, before its command is read.
    """
    at = earlier.rfind(json.dumps(call_id).encode())
    if at < 0:
        return None
    begin = earlier.rfind(b"\n", 0, at) + 1
    end = earlier.find(b"\n", at)
    line = check_part(earlier[begin : len(earlier) if end < 0 else end]).content
    parsed = _entry_of(line)
    if parsed is None:
        return None
    when, blocks = parsed
    for block in blocks:
        command = _meho_command(block)
        if command is not None and block.get("id") == call_id:
            return command, when
    return None


def cli_calls_in(
    content: bytes, *, limit: int = MAX_TOOL_CALLS, earlier: bytes = b""
) -> list[CliCall]:
    """Return up to *limit* ``meho`` commands whose answer line is in *content*.

    The call line is in *content* or in *earlier* (the part of the same file
    that an earlier upload stored). A command whose answer line is not in
    *content*, or a line with no valid ``timestamp`` (with its time zone), is
    left out.
    """
    started: dict[str, tuple[str, datetime] | None] = {}
    calls: list[CliCall] = []
    for line in content.splitlines():
        if len(calls) >= limit:
            break
        if b"tool_use" not in line and b"tool_result" not in line:
            continue
        parsed = _entry_of(line)
        if parsed is None:
            continue
        when, blocks = parsed
        for block in blocks:
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                command = _meho_command(block)
                # Every call of this part is noted, so the answer of a
                # call that is not ``meho`` is not looked up in *earlier*.
                started[block["id"]] = (command, when) if command is not None else None
            elif block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                call_id = block["tool_use_id"]
                if call_id in started:
                    begin = started.pop(call_id)
                else:
                    begin = _start_in_earlier(earlier, call_id) if earlier else None
                if begin is not None and begin[1] <= when:
                    calls.append(_cli_call(begin[0], begin[1], when))
    return calls[:limit]
