# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The shared shape of a governed log read (operation group ``logs``).

Every connector that reads logs takes the same parameters (``source``,
``since``, ``until``, ``lines``, ``filter``) and answers with the same
shape: rows of ``{time, line, line_truncated}``, ``total``, ``truncated``,
the time span that was read, and the source's own continuation marker
when it has one. The connector never builds a result handle: the normal
result reduction does that when the rows are many.

This module holds what the connectors share: the schemas, the time
parsing, and the steps from source entries to the answer, in this order:

1. :func:`redact_entries` hides secrets in the entries as one text, so a
   secret that spans lines (a PEM key) is found whole. The connector gives
   it the lines before the entries too (:class:`LogContext`): an entry can
   be in the middle of a key that started earlier.
2. :func:`take_page` applies the text filter and the line limit. It also
   says which entries were looked at.
3. :func:`build_log_answer` builds the answer.

What a ``source`` means, and how the entries are read, stays in each
connector.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from meho_backplane.redaction import apply_connector_boundary_redaction
from meho_backplane.redaction.secret_text import hide_secret_text

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = [
    "DEFAULT_LOG_LINES",
    "LOG_READ_RESPONSE_SCHEMA",
    "MAX_CONTEXT_LINE_CHARS",
    "MAX_LINE_CHARS",
    "MAX_LOG_LINES",
    "REDACTION_CONTEXT_LINES",
    "LogContext",
    "LogEntry",
    "LogPage",
    "LogReadError",
    "bound_context_line",
    "build_log_answer",
    "has_key_mark",
    "is_span",
    "log_read_parameter_schema",
    "parse_since",
    "parse_until",
    "redact_entries",
    "take_page",
]

#: Default and hard cap of ``lines``.
DEFAULT_LOG_LINES = 200
MAX_LOG_LINES = 5000

#: Most characters of one returned line. The filter looks at the whole line.
MAX_LINE_CHARS = 4096

#: How many lines before the first entry a connector reads, so a private
#: key that started there is still found. A PEM block of a 16384-bit RSA
#: key has about 200 lines.
REDACTION_CONTEXT_LINES = 256

#: How much of one line before the first entry is kept.
MAX_CONTEXT_LINE_CHARS = 4096

_SPAN_RE = re.compile(r"^(\d+)([smhd])$")
_SPAN_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_TIME_PATTERN = r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:[Zz]|[+-]\d{2}:\d{2})"
_FRACTION_RE = re.compile(r"\.(\d+)")

_PEM_MARK_RE = re.compile(r"-----(BEGIN|END) ((?:[A-Z0-9]+ )*)PRIVATE KEY-----")
_PEM_MARK_END = "PRIVATE KEY-----"
_PEM_MARK_CHARS = 120

#: A line that can be part of a key body: only base64 text, or a long piece of it.
_KEY_BODY_RE = re.compile(r"\A\s*[A-Za-z0-9+/=]+\s*\Z|[A-Za-z0-9+/=]{16}")
_HIDDEN_KEY_LINE = "[REDACTED:pem_private_key]"


class LogReadError(ValueError):
    """The log read cannot run with these params, or its answer is unclear."""


#: The answer of every log read.
LOG_READ_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "time": {"type": ["string", "null"]},
                    "line": {"type": "string"},
                    "line_truncated": {"type": "boolean"},
                },
                "required": ["time", "line", "line_truncated"],
            },
        },
        "total": {"type": "integer"},
        "truncated": {"type": "boolean"},
        "covered": {
            "type": ["object", "null"],
            "properties": {"from": {"type": "string"}, "to": {"type": "string"}},
        },
        "continuation": {"type": ["string", "null"]},
        "warning": {"type": "string"},
    },
    "required": ["rows", "total", "truncated", "covered", "continuation"],
    "additionalProperties": True,
}


def log_read_parameter_schema(
    source_description: str, *, marker_pattern: str | None = None
) -> dict[str, Any]:
    """Return the parameter schema of a log read.

    Only ``source`` differs per connector, and *marker_pattern*: a source
    whose continuation marker is not a time names the marker's form, and
    ``since`` and ``until`` then take it too.
    """
    marker = f"|{marker_pattern}" if marker_pattern else ""
    or_marker = ", or the 'continuation' of an earlier read" if marker_pattern else ""
    return {
        "type": "object",
        "properties": {
            "source": {"type": "string", "minLength": 1, "description": source_description},
            "since": {
                "type": "string",
                "pattern": rf"^(\d+[smhd]|{_TIME_PATTERN}{marker})$",
                "description": (
                    "Start of the read: a span such as '15m', '2h' or '1d' (counted back "
                    f"from now), or an exact RFC 3339 time{or_marker}. The read then goes "
                    "forward from there, oldest line first."
                ),
            },
            "until": {
                "type": "string",
                "pattern": rf"^({_TIME_PATTERN}{marker})$",
                "description": f"End of the read: an exact RFC 3339 time{or_marker}. Optional.",
            },
            "lines": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_LOG_LINES,
                "description": f"Most lines to return (default {DEFAULT_LOG_LINES}).",
            },
            "filter": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "description": (
                    "A plain text match (not a pattern). Only lines that contain it come "
                    "back. Upper and lower case count."
                ),
            },
        },
        "required": ["source"],
        "additionalProperties": False,
    }


def is_span(value: str) -> bool:
    """Say whether *value* is a span such as ``15m``."""
    return _SPAN_RE.match(value) is not None


def parse_since(value: str | None, now: datetime | None = None) -> datetime | None:
    """Return the start of the read as a UTC time, or ``None`` when *value* is unset.

    A time with nanoseconds is rounded up to the next microsecond, so an
    entry before it is never read.
    """
    if value is None:
        return None
    match = _SPAN_RE.match(value)
    if match is not None:
        seconds = int(match.group(1)) * _SPAN_SECONDS[match.group(2)]
        try:
            return (now or datetime.now(UTC)).replace(microsecond=0) - timedelta(seconds=seconds)
        except OverflowError:
            raise LogReadError(f"{value!r} is too long a span") from None
    fraction = _FRACTION_RE.search(value)
    parsed = _parse_time(value)
    if fraction is not None and fraction.group(1)[6:].strip("0"):
        parsed += timedelta(microseconds=1)
    return parsed


def parse_until(value: str | None) -> datetime | None:
    """Return the end of the read as a UTC time, or ``None`` when *value* is unset."""
    return None if value is None else _parse_time(value)


def _parse_time(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value[-1] in "Zz" else value
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            raise LogReadError(f"{value!r} has no time zone offset")
        return parsed.astimezone(UTC)
    except LogReadError:
        raise
    except (ValueError, OverflowError):
        raise LogReadError(f"{value!r} is not an RFC 3339 time or a span like '15m'") from None


@dataclass(frozen=True)
class LogEntry:
    """One entry of the source, oldest first in a list of them.

    ``group`` is set by a source whose continuation marker is a time: the
    entries of one time share it, and a page never ends between them.
    ``marker`` is the source's own marker of this entry.
    """

    time: str | None
    line: str
    group: tuple[int, int] | None = None
    marker: str | None = None


@dataclass(frozen=True)
class LogContext:
    """The lines right before the first entry, oldest first. They are never returned.

    ``complete`` is true when they reach back
    :data:`REDACTION_CONTEXT_LINES` lines or to the start of the log.
    """

    lines: list[str] = field(default_factory=list)
    complete: bool = True


@dataclass(frozen=True)
class LogPage:
    """What :func:`take_page` took from the entries.

    ``rows`` go back to the caller, oldest first. ``examined`` are all
    entries the page looked at, also the ones that do not match. ``rest``
    are the entries left for another page: after the page for a forward
    read, before it for a read of the newest lines.
    """

    rows: list[dict[str, Any]]
    examined: list[LogEntry]
    rest: list[LogEntry]


def _close_cut_keys(lines: list[str]) -> list[tuple[int, str]]:
    """Return ``(index, BEGIN line)`` for each private key whose start is not in *lines*.

    The read can start in the middle of a key: then there is an ``END``
    line with no ``BEGIN`` line before it, and no pattern finds the body.
    The ``BEGIN`` line belongs before the line at the index: at most
    :data:`REDACTION_CONTEXT_LINES` back, and never before the last mark.
    """
    added: list[tuple[int, str]] = []
    open_label: str | None = None
    floor = 0
    for index, line in enumerate(lines):
        for mark in _PEM_MARK_RE.finditer(line):
            kind, label = mark.groups()
            if kind == "BEGIN":
                open_label = label if open_label is None else open_label
                continue
            if open_label is None:
                start = min(max(floor, index - REDACTION_CONTEXT_LINES), index)
                added.append((start, f"-----BEGIN {label}PRIVATE KEY-----"))
            if open_label in (None, label):
                open_label = None
                floor = index + 1
    return added


def has_key_mark(line: str) -> bool:
    """Say whether *line* holds the first or the last line of a private key."""
    return _PEM_MARK_RE.search(line) is not None


def bound_context_line(line: str) -> str:
    """Shorten a line of the context; the last key mark in it is kept."""
    if len(line) <= MAX_CONTEXT_LINE_CHARS:
        return line
    head = line[:MAX_CONTEXT_LINE_CHARS]
    mark = line.rfind(_PEM_MARK_END)
    if mark < 0:
        return head
    return f"{head} {line[max(0, mark - _PEM_MARK_CHARS) : mark + len(_PEM_MARK_END)]}"


def _unclear_key_lines(lines: list[str], context: LogContext) -> set[int]:
    """Return the lines that can belong to a key whose start was not read.

    Only when the context ends early and holds no key mark: then nothing
    says whether the first lines stand inside a key.
    """
    if context.complete or any(map(has_key_mark, context.lines)):
        return set()
    unclear: set[int] = set()
    for index, line in enumerate(lines[: REDACTION_CONTEXT_LINES - len(context.lines)]):
        if has_key_mark(line):
            break
        if _KEY_BODY_RE.search(line):
            unclear.add(index)
    return unclear


def _redact_lines(
    lines: list[str], context: LogContext, labels: dict[str, str | None]
) -> list[str]:
    everything = context.lines + lines
    starts = _close_cut_keys(everything)
    text: list[str] = []
    real: list[bool] = []
    for index, line in enumerate(everything):
        while starts and starts[0][0] == index:
            text.append(starts.pop(0)[1])
            real.append(False)
        text.append(line)
        real.append(index >= len(context.lines))
    result = apply_connector_boundary_redaction(text, **labels)
    shown = [line for line, keep in zip(result.redacted, real, strict=True) if keep]
    for index in _unclear_key_lines(lines, context):
        shown[index] = _HIDDEN_KEY_LINE
    return shown


async def redact_entries(
    entries: list[LogEntry],
    *,
    context: LogContext,
    connector: Any,
    operator: Operator | None,
    op_id: str,
) -> list[LogEntry]:
    """Return *entries* with the secrets in their lines hidden.

    The lines of *context* and of the entries go through the redaction of
    the result boundary as one text, under the same policy. This runs
    before the filter and the line limit: they can take the ``BEGIN`` and
    ``END`` lines of a key away. The result boundary still redacts the
    answer.
    """
    tenant_id = None if operator is None else operator.tenant_id
    labels = {
        "connector_id": f"{connector.impl_id}-{connector.version}",
        "tenant": None if tenant_id is None else str(tenant_id),
        "op": op_id,
    }
    lines = await asyncio.to_thread(
        _redact_lines, [entry.line for entry in entries], context, labels
    )
    return [replace(entry, line=line) for entry, line in zip(entries, lines, strict=True)]


def _matches(entry: LogEntry, needle: str | None) -> bool:
    """Say whether the line holds *needle*, in the text that the caller may see."""
    if not needle:
        return True
    return needle in entry.line and needle in hide_secret_text(entry.line)


async def take_page(
    entries: list[LogEntry],
    *,
    lines: int,
    needle: str | None,
    keep: Literal["newest", "oldest"],
) -> LogPage:
    """Take the page in a worker thread (see :func:`_take_page`)."""
    return await asyncio.to_thread(_take_page, entries, lines, needle, keep)


def _take_page(
    entries: list[LogEntry], lines: int, needle: str | None, keep: Literal["newest", "oldest"]
) -> LogPage:
    """Take at most *lines* entries that match from one end of *entries*.

    With ``keep="oldest"`` a page ends between two groups. When the
    matches of the first group alone are more than *lines*, no page can
    be built and :class:`LogReadError` says so.
    """
    order = range(len(entries)) if keep == "oldest" else range(len(entries) - 1, -1, -1)
    taken: list[int] = []
    bound: int | None = None
    for index in order:
        if not _matches(entries[index], needle):
            continue
        if len(taken) == lines:
            bound = index
            break
        taken.append(index)
    if bound is None:
        return LogPage([_row(entries[i], needle) for i in sorted(taken)], list(entries), [])
    if keep == "newest":
        examined, rest = entries[bound + 1 :], entries[: bound + 1]
    else:
        bound = _group_start(entries, bound)
        if bound == 0:
            raise LogReadError(
                f"more than {lines} lines share the time {entries[0].time}, so 'lines' cannot "
                "cut them; raise 'lines', use a narrower 'filter', or set 'since' to a later time"
            )
        examined, rest = entries[:bound], entries[bound:]
        taken = [index for index in taken if index < bound]
    return LogPage([_row(entries[i], needle) for i in sorted(taken)], examined, rest)


def _group_start(entries: list[LogEntry], index: int) -> int:
    group = entries[index].group
    while group is not None and index > 0 and entries[index - 1].group == group:
        index -= 1
    return index


def _row(entry: LogEntry, needle: str | None) -> dict[str, Any]:
    line = entry.line
    cut = len(line) > MAX_LINE_CHARS
    if cut:
        # Hide first, cut after: a cut can part a secret from its name.
        line = hide_secret_text(line)
        start = 0
        if needle:
            start = max(0, line.find(needle) - MAX_LINE_CHARS // 2)
        cut = len(line) > MAX_LINE_CHARS
        line = line[start : start + MAX_LINE_CHARS]
    return {"time": entry.time, "line": line, "line_truncated": cut}


def build_log_answer(
    page: LogPage,
    *,
    source_cut: bool = False,
    continuation: str | None = None,
) -> dict[str, Any]:
    """Build the answer of a log read from one page.

    ``truncated`` is true when the page left entries out or *source_cut*
    says the source holds more than was read. ``covered`` is the time
    span of the entries that were looked at, also when none matches
    (``null`` for entries without times). ``continuation`` is the
    source's own marker to read on from, passed on as it is.
    """
    times = [entry.time for entry in page.examined if entry.time]
    return {
        "rows": page.rows,
        "total": len(page.rows),
        "truncated": bool(page.rest) or source_cut,
        "covered": {"from": times[0], "to": times[-1]} if times else None,
        "continuation": continuation,
    }
