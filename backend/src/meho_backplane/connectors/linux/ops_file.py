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

Both check the operator-named path with :func:`confine_read_path` (the
bind9 ``ensure_path_under_root`` mold) *before* constructing any command,
and ``shlex.quote`` the confined path into a fixed command shape. No
operator value reaches the shell unquoted; a traversal attempt is
rejected before the host is touched.

Then both ask the host for the real path (``realpath``, which follows
every symlink), run :func:`confine_read_path` again on that real path,
and read only the real path. So a symlink under an allowed root cannot
point the read at a file outside the allowed roots or at a key file.
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
    PathConfinementError,
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
    "build_real_path_command",
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


def build_real_path_command(confined_path: str) -> str:
    """Build the command that prints the real path of *confined_path*.

    Prints the ``MISSING_MARKER`` when nothing exists at the path (also for
    a symlink whose target is gone), otherwise the output of ``realpath``,
    which follows every symlink. *confined_path* is already checked by
    :func:`confine_read_path`; it is ``shlex.quote``d here too.
    """
    quoted = shlex.quote(confined_path)
    return (
        f"if [ -e {quoted} ]; then realpath -- {quoted}; "
        f"else printf '%s\\n' {shlex.quote(MISSING_MARKER)}; fi"
    )


async def _real_read_path(
    connector: LinuxSshConnector,
    target: Target,
    confined_path: str,
    operator: Operator | None,
) -> str | None:
    """Return the checked real path of *confined_path*, or ``None`` when it is missing.

    Asks the host for the real path and runs :func:`confine_read_path` on
    it, so the file that is read is the file that was checked. Any doubt
    (an error exit, no line, more than one line) raises
    :class:`PathConfinementError`.
    """
    proc = await connector._run_command(
        target, build_real_path_command(confined_path), operator=operator
    )
    stdout = getattr(proc, "stdout", None)
    lines = stdout.splitlines() if isinstance(stdout, str) else []
    if lines == [MISSING_MARKER]:
        return None
    if getattr(proc, "exit_status", None) != 0 or len(lines) != 1:
        raise PathConfinementError(f"could not resolve the real path of {confined_path!r}")
    return confine_read_path(lines[0])


def build_file_read_command(confined_path: str, max_bytes: int) -> str:
    """Build the fixed single-round-trip ``file.read`` command.

    Emits the ``MISSING_MARKER`` when the path is absent, otherwise the
    file size (``stat -c %s``) then the ``CONTENT_MARKER`` then the first
    *max_bytes* bytes (``head -c``). *confined_path* is the real path,
    already checked by :func:`confine_read_path`; it is ``shlex.quote``d
    here defensively.
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
    (traversal rejected before any SSH), checks the real path the host
    reports, then ``head -c``-caps the content of the real path at
    ``max_bytes`` (default 64 KiB, hard cap 1 MiB). Returns a scalar dict;
    a missing file is reported with ``exists=False`` rather than an error,
    so a missing first-boot sentinel is a legible day-0 signal.
    """
    confined = confine_read_path(params["path"])
    max_bytes = _bounded_max_bytes(params.get("max_bytes"))
    real_path = await _real_read_path(connector, target, confined, operator)
    if real_path is None:
        return parse_file_read_output(MISSING_MARKER, confined, max_bytes)
    cmd = build_file_read_command(real_path, max_bytes)
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
    checks the real path the host reports, then runs ``tail -n <lines>``
    (default 200, hard cap 10000) on the real path. Returns
    ``{rows, total, path}``; a missing file gives no rows. The row list
    spills to a ``result_query`` handle above the JSONFlux threshold.
    """
    confined = confine_read_path(params["path"])
    lines = _bounded_lines(params.get("lines"))
    real_path = await _real_read_path(connector, target, confined, operator)
    if real_path is None:
        return {**normalise_json_rows([]), "path": confined}
    cmd = build_log_tail_command(real_path, lines)
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
        "``/run``, plus a few named ``/proc`` and ``/sys`` files such as "
        "``/proc/meminfo``); a path outside every root, or one using ``..`` "
        "traversal, is rejected before any SSH command runs. The real path "
        "the host reports (symlinks followed) must pass the same check, and "
        "files that hold keys, tokens or password hashes are refused. "
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
        "any SSH command runs. The real path the host reports (symlinks "
        "followed) must pass the same check, and files that hold keys, "
        "tokens or password hashes are refused. Returns ``{rows, total, path}`` where each "
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
