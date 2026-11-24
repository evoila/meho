# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The file side of ``linux.logs.read``.

The file is opened and checked like in ``linux.log.tail``
(:mod:`.ops_file`). Only the command that reads the open file differs: it
sends the last lines of the file whole, and the lines before them cut
short. Those earlier lines are for the redaction only
(:class:`~meho_backplane.connectors.log_read.LogContext`), so long ones
must not fill the output limit of SSH. Both parts come from one pass over
the file, so a line that is written meanwhile cannot move between them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.linux.ops import confine_read_path
from meho_backplane.connectors.linux.ops_file import (
    ReadOutput,
    _opened_file_command,
    _read_opened_file,
)
from meho_backplane.connectors.linux.ops_inspect import LinuxInspectError
from meho_backplane.connectors.log_read import (
    MAX_CONTEXT_LINE_CHARS,
    REDACTION_CONTEXT_LINES,
    LogContext,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.linux.connector import LinuxSshConnector, Target

__all__ = ["FileLines", "build_log_file_command", "parse_log_file_content", "read_log_file"]

#: Byte cap of the lines that are sent whole.
_MAX_FILE_BYTES = 8 * 1024 * 1024

#: The most bytes at the end of the file that the host looks at.
_MAX_FILE_SCAN_BYTES = 32 * 1024 * 1024

_HEADER = "MEHO_LOG_LINES"

#: a[first] to a[NR] is the page; a[old] to a[first - 1] is the context.
#: A line that leaves the page is cut short: its first characters and the
#: last mark of a private key in it.
_AWK = (
    "function cut(s,  p, q, m) { if (length(s) <= chars) return s; "
    "p = split(s, q, /PRIVATE KEY-----/); s = substr(s, 1, chars); "
    "if (p > 1) { m = q[p - 1]; if (length(m) > 120) m = substr(m, length(m) - 119); "
    's = s " " m "PRIVATE KEY-----" } gsub(/[^ -~]/, "?", s); return s } '
    "BEGIN { first = 1; old = 1 } "
    "{ n = length($0) + 1; all += n; size += n; a[NR] = $0; "
    "while (size > max || NR - first + 1 > rows) { size -= length(a[first]) + 1; "
    "a[first] = cut(a[first]); first++; "
    "if (first - old > context) { delete a[old]; old++ } } } "
    "END { short = all >= scan; "
    # The first of the bytes that were looked at can be inside a line.
    "if (short && old == 1 && NR) { if (first == 1) first = 2; old = 2 } "
    f'print "{_HEADER} " first - old " " short + 0; '
    "for (i = old; i <= NR; i++) print a[i] }"
)


@dataclass(frozen=True)
class FileLines:
    """The last lines of the file, oldest first, and the lines before them."""

    lines: list[str]
    context: LogContext
    cut: bool


def build_log_file_command(
    confined_path: str,
    rows: int,
    max_bytes: int = _MAX_FILE_BYTES,
    scan_bytes: int = _MAX_FILE_SCAN_BYTES,
) -> str:
    """Build the fixed read command; only numbers go into it next to the checked path.

    In the C locale awk counts bytes.
    """
    reader = (
        f"{{ tail -n {int(rows) + REDACTION_CONTEXT_LINES} | tail -c {int(scan_bytes)} "
        f"| LC_ALL=C awk -v rows={int(rows)} -v context={REDACTION_CONTEXT_LINES} "
        f"-v max={int(max_bytes)} -v scan={int(scan_bytes)} -v chars={MAX_CONTEXT_LINE_CHARS} "
        f"'{_AWK}'; }}"
    )
    return _opened_file_command(confined_path, reader)


def parse_log_file_content(content: str, path: str) -> FileLines:
    """Split the content into the lines and their context, or raise when it is unclear."""
    header, _, body = content.partition("\n")
    parts = header.split(" ")
    if len(parts) != 3 or parts[0] != _HEADER or not (parts[1].isdigit() and parts[2].isdigit()):
        raise LinuxInspectError(f"the read of log file {path!r} did not finish cleanly")
    before, short = int(parts[1]), parts[2] != "0"
    lines = [line.removesuffix("\r") for line in body.split("\n")]
    lines.pop()
    if len(lines) < before:
        raise LinuxInspectError(f"the read of log file {path!r} did not finish cleanly")
    context = LogContext(lines[:before], complete=not short or before >= REDACTION_CONTEXT_LINES)
    return FileLines(lines[before:], context, cut=short or before > 0)


def _check_file_was_read(output: ReadOutput, path: str) -> None:
    """Raise when the file gave no content because it was not read."""
    if not output.exists:
        raise LinuxInspectError(f"log file {path!r} does not exist; nothing was read")
    if output.real_path is None:
        raise LinuxInspectError(f"log file {path!r} could not be opened; nothing was read")


async def read_log_file(
    connector: LinuxSshConnector,
    target: Target,
    path: str,
    rows: int,
    operator: Operator | None,
) -> FileLines:
    """Read at most *rows* lines from the end of the file, or raise."""
    confined = confine_read_path(path)
    cmd = build_log_file_command(confined, rows)
    output = await _read_opened_file(connector, target, cmd, confined, operator)
    _check_file_was_read(output, confined)
    read = parse_log_file_content(output.content, confined)
    if not read.lines and read.cut:
        raise LinuxInspectError(
            f"the last line of log file {confined!r} is larger than "
            f"{_MAX_FILE_BYTES // (1024 * 1024)} MiB, the most one read may hold"
        )
    if not read.lines and output.size_bytes != 0:
        raise LinuxInspectError(f"log file {confined!r} gave no content and is not an empty file")
    return read
