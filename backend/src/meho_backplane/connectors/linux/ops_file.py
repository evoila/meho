# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""File-content read verbs for :class:`LinuxSshConnector` (#3360).

Two ``safe`` reads over an operator-named, allow-list-confined path:

* ``linux.file.read`` (group ``file``) -- ``head -c``-capped read of an
  allow-listed config / log / sentinel file. Returns a flat scalar dict
  ``{path, content, truncated, size_bytes, exists}``; the content is
  capped by the handler (the JSONFlux reducer only spills set-shaped
  payloads, so a flat dict is never reduced -- an uncapped read would
  ship an arbitrary blob into agent context).
* ``linux.log.tail`` (group ``log``) -- ``tail -n <lines>`` of an
  allow-listed log file. Returns ``{rows, total, path}``; the row list
  spills to a ``result_query`` handle above threshold.

Both confine the operator-named path with :func:`confine_read_path` (the
bind9 ``ensure_path_under_root`` mold) *before* constructing any command,
and ``shlex.quote`` the confined path into a fixed command shape. No
operator value reaches the shell unquoted; a traversal attempt is
rejected before the host is touched.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.linux.ops import (
    DEFAULT_FILE_READ_BYTES,
    DEFAULT_TAIL_LINES,
    MAX_FILE_READ_BYTES,
    MAX_TAIL_LINES,
    SSH_TRANSPORT_NOTE,
    LinuxOp,
    confine_read_path,
    normalise_json_rows,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.linux.connector import LinuxSshConnector, Target

__all__ = [
    "CONTENT_MARKER",
    "FILE_OPS",
    "MISSING_MARKER",
    "build_file_read_command",
    "build_log_tail_command",
    "linux_file_read",
    "linux_log_tail",
    "parse_file_read_output",
]

#: Sentinel the ``file.read`` command prints when the confined path does
#: not exist -- distinguishing "file absent" (the first-boot sentinel was
#: never written) from "file present but empty" honestly.
MISSING_MARKER: str = "===MEHO_FILE_MISSING==="

#: Separates the ``stat`` size line from the file content in the
#: ``file.read`` command's single-round-trip output.
CONTENT_MARKER: str = "===MEHO_CONTENT==="


def _bounded_max_bytes(raw: Any) -> int:
    """Return the validated content byte budget (default + hard cap)."""
    if raw is None:
        return DEFAULT_FILE_READ_BYTES
    value = int(raw)
    if value < 1:
        raise ValueError("max_bytes must be >= 1")
    return min(value, MAX_FILE_READ_BYTES)


def _bounded_lines(raw: Any) -> int:
    """Return the validated tail line budget (default + hard cap)."""
    if raw is None:
        return DEFAULT_TAIL_LINES
    value = int(raw)
    if value < 1:
        raise ValueError("lines must be >= 1")
    return min(value, MAX_TAIL_LINES)


def build_file_read_command(confined_path: str, max_bytes: int) -> str:
    """Build the fixed single-round-trip ``file.read`` command.

    Emits the ``MISSING_MARKER`` when the path is absent, otherwise the
    file size (``stat -c %s``) then the ``CONTENT_MARKER`` then the first
    *max_bytes* bytes (``head -c``). *confined_path* is already confined by
    :func:`confine_read_path`; it is ``shlex.quote``d here defensively.
    *max_bytes* is an ``int`` (bounded by :func:`_bounded_max_bytes`), so
    it is interpolated as a number -- no operator string reaches the shell.
    """
    quoted = shlex.quote(confined_path)
    return (
        f"if [ ! -e {quoted} ]; then printf '%s\\n' {shlex.quote(MISSING_MARKER)}; exit 0; fi; "
        f"stat -c %s {quoted} 2>/dev/null || echo -1; "
        f"printf '%s\\n' {shlex.quote(CONTENT_MARKER)}; "
        f"head -c {int(max_bytes)} {quoted} 2>/dev/null"
    )


def build_log_tail_command(confined_path: str, lines: int) -> str:
    """Build the fixed ``tail -n <lines>`` command for a confined log path."""
    quoted = shlex.quote(confined_path)
    return f"tail -n {int(lines)} {quoted} 2>/dev/null"


def parse_file_read_output(stdout: str, confined_path: str, max_bytes: int) -> dict[str, Any]:
    """Parse the ``file.read`` command output into the response dict.

    Returns ``{path, content, truncated, size_bytes, exists}``:
    ``exists=False`` (with empty content) when the ``MISSING_MARKER`` was
    printed; otherwise the ``stat`` size and the ``head -c`` content.
    ``truncated`` is ``True`` when the on-disk size exceeds the byte budget
    (the content was capped). A ``stat`` failure (``-1``) leaves
    ``size_bytes=None`` and ``truncated`` decided by the content length.
    """
    if stdout.startswith(MISSING_MARKER):
        return {
            "path": confined_path,
            "content": "",
            "truncated": False,
            "size_bytes": None,
            "exists": False,
        }

    marker_at = stdout.find(CONTENT_MARKER)
    if marker_at == -1:
        # No content marker -- the command failed before emitting it.
        return {
            "path": confined_path,
            "content": "",
            "truncated": False,
            "size_bytes": None,
            "exists": True,
        }

    size_line = stdout[:marker_at].strip()
    content = stdout[marker_at + len(CONTENT_MARKER) :]
    content = content[1:] if content.startswith("\n") else content

    size_bytes: int | None
    try:
        parsed = int(size_line)
        size_bytes = parsed if parsed >= 0 else None
    except ValueError:
        size_bytes = None

    if size_bytes is not None:
        truncated = size_bytes > max_bytes
    else:
        truncated = len(content.encode("utf-8", "replace")) >= max_bytes

    return {
        "path": confined_path,
        "content": content,
        "truncated": truncated,
        "size_bytes": size_bytes,
        "exists": True,
    }


async def linux_file_read(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.file.read`` -- read an allow-listed file's content.

    Confines the operator-named ``path`` under the read-root allow-list
    (traversal rejected before any SSH), then ``head -c``-caps the content
    at ``max_bytes`` (default 64 KiB, hard cap 1 MiB). Returns a scalar
    dict; a missing file is reported with ``exists=False`` rather than an
    error, so a missing first-boot sentinel is a legible day-0 signal.
    """
    confined = confine_read_path(params["path"])
    max_bytes = _bounded_max_bytes(params.get("max_bytes"))
    cmd = build_file_read_command(confined, max_bytes)
    proc = await connector._run_command(target, cmd, operator=operator)
    raw = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    stdout = raw if isinstance(raw, str) else ""
    return parse_file_read_output(stdout, confined, max_bytes)


async def linux_log_tail(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.log.tail`` -- tail an allow-listed log file.

    Confines the operator-named ``path`` under the read-root allow-list,
    then runs ``tail -n <lines>`` (default 200, hard cap 10000). Returns
    ``{rows, total, path}``; the row list spills to a ``result_query``
    handle above the JSONFlux threshold.
    """
    confined = confine_read_path(params["path"])
    lines = _bounded_lines(params.get("lines"))
    cmd = build_log_tail_command(confined, lines)
    proc = await connector._run_command(target, cmd, operator=operator)
    raw = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    stdout = raw if isinstance(raw, str) else ""
    rows = stdout.splitlines()
    return {**normalise_json_rows(rows), "path": confined}


_FILE_READ_OP = LinuxOp(
    op_id="linux.file.read",
    handler_attr="file_read",
    summary="Read an allow-listed config/log/sentinel file's content (byte-capped).",
    description=(
        "Reads the content of a single file whose path is confined under "
        "the read-root allow-list (``/etc``, ``/var/log``, ``/var/lib``, "
        "``/run``, ``/proc``, ``/sys``); a path outside every root, or one "
        "using ``..`` traversal, is rejected before any SSH command runs. "
        "Content is capped at ``max_bytes`` (default 65536, hard cap "
        "1048576) via ``head -c``. Returns ``{path, content, truncated, "
        "size_bytes, exists}``; a missing file reports ``exists=false`` "
        "with empty content rather than erroring, so a first-boot "
        "completion sentinel that was never written is a legible signal."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Absolute path to read, confined under the read-root "
                    "allow-list. Traversal outside a root is rejected."
                ),
            },
            "max_bytes": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_FILE_READ_BYTES,
                "description": "Content byte cap (default 65536).",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "truncated": {"type": "boolean"},
            "size_bytes": {"type": ["integer", "null"]},
            "exists": {"type": "boolean"},
        },
        "required": ["path", "content", "truncated", "exists"],
        "additionalProperties": True,
    },
    group_key="file",
    tags=("read-only", "file", "linux"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to read the content of an allow-listed config, log, or "
            "sentinel file -- most decisively the first-boot completion "
            "sentinel a Tools-less appliance writes (present = the guest "
            "came up; missing = the run declared ready while first-boot "
            "aborted). " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {
            "path": "Absolute path under an allowed read root.",
            "max_bytes": "Optional content cap; defaults to 65536.",
        },
        "output_shape": (
            "Flat dict ``{path, content, truncated, size_bytes, exists}``. "
            "``exists=false`` means the file was absent."
        ),
    },
)


_LOG_TAIL_OP = LinuxOp(
    op_id="linux.log.tail",
    handler_attr="log_tail",
    summary="Tail the last N lines of an allow-listed log file.",
    description=(
        "Runs ``tail -n <lines>`` (default 200, hard cap 10000) over a log "
        "file whose path is confined under the read-root allow-list; a path "
        "outside every root, or using ``..`` traversal, is rejected before "
        "any SSH command runs. Returns ``{rows, total, path}`` where each "
        "row is one log line. Above the JSONFlux threshold (50 rows / 4 KB) "
        "the rows spill to a handle paged with "
        "``result_query(handle_id, offset, limit)``."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "description": ("Absolute log path, confined under the read-root allow-list."),
            },
            "lines": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_TAIL_LINES,
                "description": "Number of trailing lines (default 200).",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "rows": {"type": "array", "items": {"type": "string"}},
            "total": {"type": "integer"},
            "path": {"type": "string"},
        },
        "required": ["rows", "total"],
        "additionalProperties": True,
    },
    group_key="log",
    tags=("read-only", "log", "linux"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to see WHY a first-boot or service log ends the way it "
            "does -- tail the first-boot log to read the last error before a "
            "``set -euo pipefail`` script aborted (a missing NIC, an "
            "unresolvable package mirror, a red config-validate). " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {
            "path": "Absolute log path under an allowed read root.",
            "lines": "Optional line count; defaults to 200.",
        },
        "output_shape": (
            "``{rows, total, path}``; rows are log lines, oldest first. "
            "Large tails spill to a ``result_query`` handle."
        ),
    },
)


#: The file-content read tier composed onto ``LINUX_OPS``.
FILE_OPS: tuple[LinuxOp, ...] = (_FILE_READ_OP, _LOG_TAIL_OP)
