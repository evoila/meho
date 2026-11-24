# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The journal side of ``linux.logs.read``.

``linux.journal.tail`` cuts each message and drops the cursor of each
entry. This read keeps both: the text filter must see whole messages, and
``__CURSOR`` is the journal's own continuation marker.

Two directions, and no ``journalctl -n`` in either: what ``-n`` means
together with ``--since`` differs between systemd versions. The lines are
counted here instead.

* With ``since`` the read goes forward, oldest entry first.
* Without ``since`` it goes back from the end (``--reverse``), newest
  entry first, and the entries are turned round afterwards. So the byte
  cap drops the oldest entries, not the newest.

A marker starts the read *behind* its entry (``--after-cursor``), so the
entry takes nothing from the byte cap of the page. A read of its own then
asks for the entry itself. When the journal does not give it, the journal
no longer holds the place, and the read fails: it never starts again
somewhere else without a word.

The entries before the page are read too (:func:`build_context_command`),
for the redaction only. They have their own byte cap.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.linux.ops_host import validate_unit_name
from meho_backplane.connectors.linux.ops_inspect import LinuxInspectError, _journal_message
from meho_backplane.connectors.log_read import (
    REDACTION_CONTEXT_LINES,
    LogEntry,
    LogReadError,
    bound_context_line,
    parse_since,
    parse_until,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.linux.connector import LinuxSshConnector, Target

__all__ = [
    "JOURNAL_MARKER_PATTERN",
    "JOURNAL_SOURCE",
    "JournalEntries",
    "JournalQuery",
    "build_context_command",
    "build_journal_command",
    "journal_query",
    "parse_journal_entries",
    "read_journal",
]

JOURNAL_SOURCE = "journal"

#: Form of ``__CURSOR``: ``s=...;i=...;b=...`` (names and hex values).
JOURNAL_MARKER_PATTERN = r"[a-z]=[0-9a-zA-Z]{1,64}(?:;[a-z]=[0-9a-zA-Z]{1,64}){1,15}"
_MARKER_RE = re.compile(JOURNAL_MARKER_PATTERN)

#: Byte cap of the journal output read from the host.
_MAX_JOURNAL_BYTES = 8 * 1024 * 1024

#: Caps of the read of the entries before a page: the bytes the host looks
#: at, the bytes it sends, and the size from which an entry is sent short.
_MAX_CONTEXT_SCAN_BYTES = 64 * 1024 * 1024
_MAX_CONTEXT_BYTES = 12 * 1024 * 1024
_MAX_CONTEXT_ENTRY_BYTES = 16 * 1024

#: More than the line with the exit code has.
_EXIT_LINE_BYTES = 64

_NO_ENTRIES = "-- No entries --"
_NO_BINARY_EXIT = 127
_RC_PREFIX = "MEHO_JOURNAL_RC="
_CUT_MARKER = "MEHO_JOURNAL_CUT"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class JournalQuery:
    """What to read: a unit (or all), the start, the end and the direction."""

    unit: str | None
    since: datetime | None
    until: datetime | None
    marker: str | None
    forward: bool


@dataclass(frozen=True)
class JournalEntries:
    """The entries read, oldest first, and whether the journal holds more.

    ``context`` are the lines of the entries before them.
    """

    entries: list[LogEntry]
    cut: bool
    warning: str
    context: list[str]


def _is_marker(value: str | None) -> bool:
    return value is not None and _MARKER_RE.fullmatch(value) is not None


def journal_query(params: dict[str, Any]) -> JournalQuery:
    """Check the params of a journal read, or raise before any command runs."""
    source = params["source"]
    unit = None if source == JOURNAL_SOURCE else validate_unit_name(source)
    since, until = params.get("since"), params.get("until")
    if _is_marker(since):
        return JournalQuery(unit, None, parse_until(until), since, forward=True)
    if _is_marker(until):
        if since is not None:
            raise LogReadError(
                "'until' takes a marker only without 'since': it reads the part before the marker"
            )
        return JournalQuery(unit, None, None, until, forward=False)
    start, end = parse_since(since), parse_until(until)
    return JournalQuery(unit, start, end, None, forward=start is not None)


def _journal_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S.%f UTC")


def _journalctl(query: JournalQuery, *cursor_options: str) -> str:
    parts = ["journalctl", "-o", "json", "--all", "--no-pager"]
    if not query.forward:
        parts.append("--reverse")
    if query.unit is not None:
        parts += ["-u", shlex.quote(validate_unit_name(query.unit))]
    parts += cursor_options
    if query.since is not None:
        parts.append("--since=" + shlex.quote(_journal_time(query.since)))
    if query.until is not None:
        parts.append("--until=" + shlex.quote(_journal_time(query.until)))
    return " ".join(parts)


def _capped(journalctl: str, *, scan_bytes: int, max_bytes: int, rows: int, short: str) -> str:
    # Each entry is one line that starts with "{", and only entries count.
    # head passes more than scan_bytes, so awk sees entries over it and the
    # exit code line fits behind entries under it. In the C locale awk
    # counts bytes. *short* may make an entry shorter before it is sent.
    awk = (
        f"LC_ALL=C awk -v scan={scan_bytes} -v max={max_bytes} -v rows={rows} "
        f'\'substr($0, 1, 1) == "{{" {{ k++; s += length($0) + 1; {short}n += length($0) + 1; '
        f'if (s > scan || n > max || k > rows) {{ print "{_CUT_MARKER}"; exit }} }} '
        f"{{ print }}'"
    )
    return (
        f"command -v journalctl >/dev/null 2>&1 || exit {_NO_BINARY_EXIT}; "
        f"{{ {journalctl}; printf '\\n{_RC_PREFIX}%s\\n' \"$?\"; }} "
        f"| head -c {scan_bytes + _EXIT_LINE_BYTES} | {awk}"
    )


def build_journal_command(
    query: JournalQuery, rows: int, max_bytes: int = _MAX_JOURNAL_BYTES
) -> str:
    """Build the fixed ``journalctl -o json`` command of a page.

    Every value is checked and ``shlex.quote``d, in the same word as its
    option, so it can never be read as an option. ``--all`` keeps long
    fields. The output is cut after *rows* entries and after *max_bytes*
    bytes, always at a whole line, and a cut ends with the cut marker.
    Otherwise the last line is the exit code of ``journalctl``.
    """
    cursor = () if query.marker is None else ("--after-cursor=" + shlex.quote(query.marker),)
    return _capped(
        _journalctl(query, *cursor),
        scan_bytes=int(max_bytes),
        max_bytes=int(max_bytes),
        rows=int(rows),
        short="",
    )


def build_context_command(unit: str | None, marker: str, rows: int) -> str:
    """Build the command that reads the entry of *marker* and those before it.

    The newest comes first. A large entry is sent short: its cursor, and
    the last mark of a private key in it. (A message in the form of a list
    of bytes is sent whole: a mark in it is not text.) So large entries
    before a page cannot fill the output.
    """
    query = JournalQuery(unit, None, None, None, forward=False)
    short = (
        f'if (length($0) > {_MAX_CONTEXT_ENTRY_BYTES} && !/"MESSAGE" ?: ?\\[/) {{ '
        'c = match($0, /"__CURSOR" ?: ?"[^"]*"/) ? substr($0, RSTART, RLENGTH) : "\\"x\\":0"; '
        'm = ""; p = split($0, q, /PRIVATE KEY-----/); if (p > 1) { '
        "m = q[p - 1]; if (length(m) > 120) m = substr(m, length(m) - 119); "
        'gsub(/[^A-Za-z0-9 -]/, " ", m); m = m "PRIVATE KEY-----" } '
        '$0 = "{" c ",\\"MESSAGE\\":\\"" m "\\"}" } '
    )
    return _capped(
        _journalctl(query, "--cursor=" + shlex.quote(marker)),
        scan_bytes=_MAX_CONTEXT_SCAN_BYTES,
        max_bytes=_MAX_CONTEXT_BYTES,
        rows=int(rows),
        short=short,
    )


def _entry_time(value: Any) -> str | None:
    try:
        return (_EPOCH + timedelta(microseconds=int(value))).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _entry(line: str) -> LogEntry:
    """Turn one output line into an entry; a line that is not JSON keeps its text."""
    try:
        fields = json.loads(line)
    except ValueError:
        fields = None
    if not isinstance(fields, dict):
        return LogEntry(time=None, line=line)
    cursor = fields.get("__CURSOR")
    return LogEntry(
        time=_entry_time(fields.get("__REALTIME_TIMESTAMP")),
        line=_journal_message(fields.get("MESSAGE")) or "",
        marker=cursor if isinstance(cursor, str) else None,
    )


def parse_journal_entries(stdout: str) -> tuple[list[LogEntry], bool]:
    """Parse the output into ``(entries, cut)``, or raise.

    The output ends with the cut marker or with exit code ``0``. Anything
    else raises: a failed read is never served as entries.
    """
    lines = [line for line in stdout.split("\n") if line.strip() and line != _NO_ENTRIES]
    last = lines.pop() if lines else ""
    if last == _CUT_MARKER:
        return [_entry(line) for line in lines], True
    if not last.startswith(_RC_PREFIX):
        raise LinuxInspectError("the journal read did not finish cleanly; the journal was not read")
    if last != f"{_RC_PREFIX}0":
        raise LinuxInspectError(
            f"journalctl failed (exit {last[len(_RC_PREFIX) :]}); the journal was not read"
        )
    return [_entry(line) for line in lines], False


async def _run(
    connector: LinuxSshConnector, target: Target, cmd: str, operator: Operator | None
) -> tuple[list[LogEntry], bool, str]:
    """Run *cmd* and return ``(entries, cut, warning)``, in the order of the output."""
    proc = await connector._run_command(target, cmd, operator=operator)
    status = getattr(proc, "exit_status", 0)
    if status == _NO_BINARY_EXIT:
        raise LinuxInspectError("host has no journalctl; cannot read the journal")
    if status not in (0, None):
        raise LinuxInspectError("journalctl did not finish cleanly; the journal was not read")
    stdout, stderr = getattr(proc, "stdout", None), getattr(proc, "stderr", None)
    warning = stderr.strip()[:500] if isinstance(stderr, str) else ""
    try:
        entries, cut = parse_journal_entries(stdout if isinstance(stdout, str) else "")
    except LinuxInspectError as exc:
        if warning:
            raise LinuxInspectError(f"{exc}; journalctl said: {warning}") from None
        raise
    return entries, cut, warning


async def _read_before(
    connector: LinuxSshConnector,
    target: Target,
    unit: str | None,
    marker: str,
    operator: Operator | None,
    rows: int,
) -> list[str] | None:
    """Return the lines of the entry of *marker* and of *rows* entries before it.

    ``None`` when the journal does not give the entry of *marker*.
    """
    cmd = build_context_command(unit, marker, rows + 1)
    entries, _, _ = await _run(connector, target, cmd, operator)
    if not entries or entries[0].marker != marker:
        return None
    return [bound_context_line(entry.line) for entry in reversed(entries)]


_MARKER_GONE = (
    "the journal does not give the entry of this marker (it was rotated away or belongs "
    "to another source), so the read cannot go on from it; start again with a time, and "
    "entries between may be missing"
)


async def _read_context(
    connector: LinuxSshConnector,
    target: Target,
    query: JournalQuery,
    entries: list[LogEntry],
    cut: bool,
    operator: Operator | None,
) -> list[str]:
    """Check the marker of *query* and return the lines before *entries* (oldest first)."""
    if query.marker is not None and query.forward:
        before = await _read_before(
            connector, target, query.unit, query.marker, operator, REDACTION_CONTEXT_LINES
        )
        if before is None:
            raise LinuxInspectError(_MARKER_GONE)
        return before
    if query.marker is not None and (
        await _read_before(connector, target, query.unit, query.marker, operator, 0) is None
    ):
        raise LinuxInspectError(_MARKER_GONE)
    if not entries or not (query.forward or cut):
        return []
    oldest = entries[0].marker
    if oldest is None:
        return []
    before = await _read_before(
        connector, target, query.unit, oldest, operator, REDACTION_CONTEXT_LINES
    )
    if before is None:
        raise LinuxInspectError(
            "the journal was rotated during the read, so it was not read whole; read it again"
        )
    return before[:-1]


async def read_journal(
    connector: LinuxSshConnector,
    target: Target,
    query: JournalQuery,
    rows: int,
    operator: Operator | None,
) -> JournalEntries:
    """Read at most *rows* entries, or raise when the journal could not be read."""
    cmd = build_journal_command(query, rows)
    entries, cut, warning = await _run(connector, target, cmd, operator)
    if query.marker is None and warning and not entries and not cut:
        raise LinuxInspectError(f"journalctl returned no entries; journalctl said: {warning}")
    if not query.forward:
        entries.reverse()
    context = await _read_context(connector, target, query, entries, cut, operator)
    if cut and not entries:
        raise LinuxInspectError(
            f"the next journal entry is larger than {_MAX_JOURNAL_BYTES // (1024 * 1024)} MiB, "
            "the most one read may hold; read behind it with a later 'since' or an earlier 'until'"
        )
    return JournalEntries(entries, cut, warning, context)
