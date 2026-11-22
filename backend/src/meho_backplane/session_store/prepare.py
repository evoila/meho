# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The CPU work of one upload: unpack, compare, check for secrets, pack.

:func:`prepare_upload` runs in a worker process (:mod:`.worker`), not on
the event loop of the server. So a large upload never holds up other
requests. It takes bytes and plain values and returns plain values, so
both can pass between processes. It does no I/O.

This module imports only the pure parts of the session store, so a new
worker process starts fast.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from meho_backplane.session_store.archive import read_archive, write_archive
from meho_backplane.session_store.plan import FileState, plan_upload, sha256_hex
from meho_backplane.session_store.secret_check import check_part, ends_inside_private_key
from meho_backplane.session_store.tool_calls import (
    MAX_TOOL_CALLS,
    CliCall,
    ToolCall,
    cli_calls_in,
    tool_calls_in,
)

__all__ = ["CheckedArchive", "PreparedUpload", "prepare_upload"]


@dataclass(frozen=True)
class CheckedArchive:
    """The archive of the checked tails, ready for the object storage."""

    data: bytes
    sha256: str
    quarantined: bool
    redactions: int
    files: dict[str, Any]
    """``{path: {offset, added, size, sha256}}`` for the upload row."""


@dataclass(frozen=True)
class PreparedUpload:
    """What one upload adds, after the secret check."""

    states: dict[str, FileState]
    conflicts: list[str]
    new_bytes: int
    archive: CheckedArchive | None
    """``None`` when the upload has no new bytes or has conflicts."""
    tool_calls: list[ToolCall] = field(default_factory=list)
    """The MCP tool calls in the checked tails of the ``.jsonl`` files."""
    cli_calls: list[CliCall] = field(default_factory=list)
    """The ``meho`` CLI commands in the checked tails of the ``.jsonl`` files."""


def prepare_upload(
    data: bytes,
    *,
    max_files: int,
    max_unpacked_bytes: int,
    stored: Mapping[str, Mapping[str, Any]],
    earlier: Iterable[Mapping[str, Mapping[str, Any]]],
    final: bool,
) -> PreparedUpload:
    """Unpack *data*, find the new tails, check them and pack them again.

    *stored* and *earlier* are the file states of the stored session and
    of its uploads (:func:`.plan.plan_upload`). Raises
    :class:`.archive.ArchiveError` for an archive MEHO refuses.
    """
    files = read_archive(data, max_files=max_files, max_unpacked_bytes=max_unpacked_bytes)
    plan = plan_upload(stored, earlier, files, final=final)
    if plan.conflicts or not plan.tails:
        return PreparedUpload(plan.states, plan.conflicts, plan.new_bytes, None)
    checked: dict[str, bytes] = {}
    meta: dict[str, Any] = {}
    redactions = 0
    unclear = False
    tool_calls: list[ToolCall] = []
    cli_calls: list[CliCall] = []
    for path, (offset, tail) in plan.tails.items():
        # The upload holds the whole file, so a key that starts in an
        # earlier upload is seen in the stored part before the tail.
        part = check_part(tail, inside_private_key=ends_inside_private_key(files[path], offset))
        checked[path] = part.content
        redactions += sum(part.redactions.values())
        unclear = unclear or part.unclear
        meta[path] = {"offset": offset, "added": len(tail), **plan.states[path].as_json()}
        if path.endswith(".jsonl"):
            limit = MAX_TOOL_CALLS - len(tool_calls)
            tool_calls.extend(tool_calls_in(part.content, limit=limit))
            cli_limit = MAX_TOOL_CALLS - len(cli_calls)
            cli_calls.extend(cli_calls_in(part.content, limit=cli_limit))
    packed = write_archive(checked)
    archive = CheckedArchive(packed, sha256_hex(packed), unclear, redactions, meta)
    return PreparedUpload(
        plan.states, plan.conflicts, plan.new_bytes, archive, tool_calls, cli_calls
    )
