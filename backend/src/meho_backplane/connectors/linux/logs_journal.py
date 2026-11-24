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

A marker starts the read *at* its entry (``--cursor``). That entry was
returned before, so it is dropped here. When the journal does not give it
first, the journal no longer holds the place, and the read fails: it
never starts again somewhere else without a word.
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
from meho_backplane.connectors.log_read import LogEntry, LogReadError, parse_since, parse_until

if TYPE_CHECKING:
    from meho_backplane.connectors.linux.connector import LinuxSshConnector, Target

__all__ = [
    "JOURNAL_MARKER_PATTERN",
    "JOURNAL_SOURCE",
    "JournalEntries",
    "JournalQuery",
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
    """The entries read, oldest first, and whether the journal holds more."""

    entries: list[LogEntry]
    cut: bool
    warning: str


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


def build_journal_command(
    query: JournalQuery, rows: int, max_bytes: int = _MAX_JOURNAL_BYTES
) -> str:
    """Build the fixed ``journalctl -o json`` command.

    Every value is checked and ``shlex.quote``d, in the same word as its
    option, so it can never be read as an option. ``--all`` keeps long
    fields. The output is cut after *rows* entries and after *max_bytes*
    bytes, always at a whole line, and a cut ends with the cut marker.
    Otherwise the last line is the exit code of ``journalctl``.
    """
    parts = ["journalctl", "-o", "json", "--all", "--no-pager"]
    if not query.forward:
        parts.append("--reverse")
    if query.unit is not None:
        parts += ["-u", shlex.quote(validate_unit_name(query.unit))]
    if query.marker is not None:
        parts.append("--cursor=" + shlex.quote(query.marker))
    if query.since is not None:
        parts.append("--since=" + shlex.quote(_journal_time(query.since)))
    if query.until is not None:
        parts.append("--until=" + shlex.quote(_journal_time(query.until)))
    cap = int(max_bytes)
    # head passes one byte more than the cap, so awk sees an output over
    # the cap. In the C locale awk counts bytes. Each entry is one line
    # that starts with "{".
    cut = (
        f"LC_ALL=C awk -v max={cap} -v rows={int(rows)} "
        f'\'{{ n += length($0) + 1; if (substr($0, 1, 1) == "{{") k++; '
        f'if (n > max || k > rows) {{ print "{_CUT_MARKER}"; exit }} print }}\''
    )
    return (
        f"command -v journalctl >/dev/null 2>&1 || exit {_NO_BINARY_EXIT}; "
        f"{{ {' '.join(parts)}; printf '\\n{_RC_PREFIX}%s\\n' \"$?\"; }} "
        f"| head -c {cap + 1} | {cut}"
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
    lines = [line for line in stdout.split("\n") if line.strip()]
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


def _drop_marker_entry(entries: list[LogEntry], marker: str) -> list[LogEntry]:
    if not entries or entries[0].marker != marker:
        raise LinuxInspectError(
            "the journal does not give the entry of this marker (it was rotated away, belongs "
            "to another source, or lies after 'until'), so the read cannot go on from it; "
            "start again with a time, and entries between may be missing"
        )
    return entries[1:]


async def read_journal(
    connector: LinuxSshConnector,
    target: Target,
    query: JournalQuery,
    rows: int,
    operator: Operator | None,
) -> JournalEntries:
    """Read at most *rows* entries, or raise when the journal could not be read."""
    wanted = rows + (1 if query.marker is not None else 0)
    cmd = build_journal_command(query, wanted)
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
        if query.marker is not None:
            entries = _drop_marker_entry(entries, query.marker)
        elif warning and not entries:
            raise LinuxInspectError("journalctl returned no entries")
    except LinuxInspectError as exc:
        if warning:
            raise LinuxInspectError(f"{exc}; journalctl said: {warning}") from None
        raise
    if cut and not entries:
        raise LinuxInspectError(
            f"the next journal entry is larger than {_MAX_JOURNAL_BYTES // (1024 * 1024)} MiB, "
            "the most one read may hold; read behind it with a later 'since' or an earlier 'until'"
        )
    return JournalEntries(entries if query.forward else entries[::-1], cut, warning)
