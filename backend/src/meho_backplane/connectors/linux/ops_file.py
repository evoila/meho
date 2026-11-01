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

Then both run one command that opens the file once, reports the path of
the file it opened (``readlink /proc/self/fd/3``, symlinks followed)
and reads from that same open file. The handler runs
:func:`confine_read_path` again on the reported path and returns the
content only when it passes. Path and content come from one open file,
so a symlink under an allowed root cannot point the read at a file
outside the allowed roots or at a key file, and a change of the path or
of a parent directory after the open cannot swap in another file.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
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
    "UNREADABLE_MARKER",
    "ReadOutput",
    "build_file_read_command",
    "build_log_tail_command",
    "linux_file_read",
    "linux_log_tail",
    "parse_read_output",
]

#: Sentinel the read command prints when the confined path does not
#: exist -- distinguishing "file absent" (the first-boot sentinel was
#: never written) from "file present but empty" honestly.
MISSING_MARKER: str = "===MEHO_FILE_MISSING==="

#: Sentinel the read command prints when the file exists but could not be
#: opened for reading. Nothing was read, so there is no path to check.
UNREADABLE_MARKER: str = "===MEHO_FILE_UNREADABLE==="

#: Separates the opened file's path and size from the content in the
#: read command's single-round-trip output.
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


def _opened_file_command(confined_path: str, reader: str) -> str:
    """Build the one command that opens *confined_path*, reports it and reads it.

    Prints the ``MISSING_MARKER`` when nothing exists at the path (also
    for a symlink whose target is gone). Otherwise the shell opens the
    file once (``3<``) and, on that open file: prints the path the kernel
    reports for it (``readlink /proc/self/fd/3``, every symlink followed),
    its size (``stat -L``), the ``CONTENT_MARKER``, and then the output
    of *reader* on the same open file (``<&3``). When the open fails the
    ``UNREADABLE_MARKER`` is printed instead.

    The handler checks the reported path and returns the content only
    when it passes. Path and content come from the same open file, so
    there is no second lookup of the path that a change of the path or
    of a parent directory could redirect. *confined_path* is already
    checked by :func:`confine_read_path` and is ``shlex.quote``d here;
    *reader* is a fixed command with numeric arguments only.
    """
    quoted = shlex.quote(confined_path)
    return (
        f"if [ ! -e {quoted} ]; then printf '%s\\n' {shlex.quote(MISSING_MARKER)}; exit 0; fi; "
        f"{{ readlink /proc/self/fd/3; "
        f"stat -L -c %s /proc/self/fd/3 2>/dev/null || echo -1; "
        f"printf '%s\\n' {shlex.quote(CONTENT_MARKER)}; "
        f"{reader} <&3 2>/dev/null; true; }} 2>/dev/null 3<{quoted} "
        f"|| printf '%s\\n' {shlex.quote(UNREADABLE_MARKER)}"
    )


def build_file_read_command(confined_path: str, max_bytes: int) -> str:
    """Build the fixed single-round-trip ``file.read`` command.

    See :func:`_opened_file_command`; the reader is ``head -c``, so the
    content is capped at *max_bytes*. *max_bytes* is an ``int`` (bounded
    by :func:`_bounded_max_bytes`), so it is interpolated as a number --
    no operator string reaches the shell.
    """
    return _opened_file_command(confined_path, f"head -c {int(max_bytes)}")


def build_log_tail_command(confined_path: str, lines: int) -> str:
    """Build the fixed single-round-trip ``log.tail`` command.

    See :func:`_opened_file_command`; the reader is ``tail -n <lines>``.
    """
    return _opened_file_command(confined_path, f"tail -n {int(lines)}")


@dataclass(frozen=True)
class ReadOutput:
    """The parsed output of the read command.

    ``exists`` is ``False`` when the ``MISSING_MARKER`` was printed.
    ``real_path`` is the path of the opened file as the host reported it,
    or ``None`` when nothing was opened (missing or unreadable file);
    then ``content`` is empty and ``size_bytes`` is ``None``.
    """

    exists: bool
    real_path: str | None
    size_bytes: int | None
    content: str


def parse_read_output(stdout: str, confined_path: str) -> ReadOutput:
    """Parse the read command's output, or raise when it is unclear.

    After the markers, the output is the reported path line, the size
    line, the ``CONTENT_MARKER`` line and the content. Anything else (no
    marker, a missing or extra line before it) raises
    :class:`PathConfinementError`: content without a clear path is never
    returned. A ``stat`` failure (``-1``) leaves ``size_bytes=None``.
    """
    if stdout.startswith(MISSING_MARKER):
        return ReadOutput(exists=False, real_path=None, size_bytes=None, content="")
    if stdout.startswith(UNREADABLE_MARKER):
        return ReadOutput(exists=True, real_path=None, size_bytes=None, content="")

    marker_at = stdout.find(CONTENT_MARKER)
    header_lines = stdout[:marker_at].split("\n") if marker_at != -1 else []
    if len(header_lines) != 3 or header_lines[2] != "" or not header_lines[0]:
        raise PathConfinementError(f"could not tell which file was opened for {confined_path!r}")
    real_path, size_line, _ = header_lines

    content = stdout[marker_at + len(CONTENT_MARKER) :]
    content = content[1:] if content.startswith("\n") else content

    size_bytes: int | None
    try:
        parsed = int(size_line)
        size_bytes = parsed if parsed >= 0 else None
    except ValueError:
        size_bytes = None
    return ReadOutput(exists=True, real_path=real_path, size_bytes=size_bytes, content=content)


async def _read_opened_file(
    connector: LinuxSshConnector,
    target: Target,
    cmd: str,
    confined_path: str,
    operator: Operator | None,
) -> ReadOutput:
    """Run the read command and return its output once the opened file passed the check.

    The path the host reports for the opened file goes through
    :func:`confine_read_path`, so the file that is returned is the file
    that was checked. A file deleted while it was open (the kernel marks
    its path ``(deleted)``), an error exit or an unclear answer raise
    :class:`PathConfinementError`, and the content never leaves this
    function.
    """
    proc = await connector._run_command(target, cmd, operator=operator)
    if getattr(proc, "exit_status", 0) != 0:
        raise PathConfinementError(f"the read of {confined_path!r} did not finish cleanly")
    stdout = getattr(proc, "stdout", None)
    output = parse_read_output(stdout if isinstance(stdout, str) else "", confined_path)
    if output.real_path is not None:
        if output.real_path.endswith(" (deleted)"):
            raise PathConfinementError(f"the file opened for {confined_path!r} was deleted")
        confine_read_path(output.real_path)
    return output


async def linux_file_read(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.file.read`` -- read an allow-listed file's content.

    Confines the operator-named ``path`` under the read-root allow-list
    (traversal rejected before any SSH), then runs one command that opens
    the file, reports the path of the opened file and ``head -c``-caps its
    content at ``max_bytes`` (default 64 KiB, hard cap 1 MiB). The content
    is returned only when the reported path passes the same check. Returns a scalar dict;
    a missing file is reported with ``exists=False`` rather than an error,
    so a missing first-boot sentinel is a legible day-0 signal.
    """
    confined = confine_read_path(params["path"])
    max_bytes = _bounded_max_bytes(params.get("max_bytes"))
    cmd = build_file_read_command(confined, max_bytes)
    output = await _read_opened_file(connector, target, cmd, confined, operator)
    if output.size_bytes is not None:
        truncated = output.size_bytes > max_bytes
    else:
        truncated = len(output.content.encode("utf-8", "replace")) >= max_bytes
    return {
        "path": confined,
        "content": output.content,
        "truncated": truncated if output.real_path is not None else False,
        "size_bytes": output.size_bytes,
        "exists": output.exists,
    }


async def linux_log_tail(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.log.tail`` -- tail an allow-listed log file.

    Confines the operator-named ``path`` under the read-root allow-list,
    then runs one command that opens the file, reports the path of the
    opened file and runs ``tail -n <lines>`` (default 200, hard cap 10000)
    on it. The rows are returned only when the reported path passes the
    same check. Returns
    ``{rows, total, path}``; a missing file gives no rows. The row list
    spills to a ``result_query`` handle above the JSONFlux threshold.
    """
    confined = confine_read_path(params["path"])
    lines = _bounded_lines(params.get("lines"))
    cmd = build_log_tail_command(confined, lines)
    output = await _read_opened_file(connector, target, cmd, confined, operator)
    return {**normalise_json_rows(output.content.splitlines()), "path": confined}


_FILE_READ_OP = LinuxOp(
    op_id="linux.file.read",
    handler_attr="file_read",
    summary="Read an allow-listed config/log/sentinel file's content (byte-capped).",
    description=(
        "Reads the content of a single file whose path is confined under "
        "the read-root allow-list (``/etc``, ``/var/log``, ``/var/lib``, "
        "``/run``, plus a few named ``/proc`` and ``/sys`` files such as "
        "``/proc/meminfo``); a path outside every root, or one using ``..`` "
        "traversal, is rejected before any SSH command runs. The path of the "
        "file the host opened (symlinks followed) must pass the same check, "
        "and files that hold keys, tokens or password hashes are refused. "
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
        "any SSH command runs. The path of the file the host opened (symlinks "
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
