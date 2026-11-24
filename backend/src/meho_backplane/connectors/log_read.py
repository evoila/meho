# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The shared shape of a governed log read (operation group ``logs``).

Every connector that reads logs takes the same parameters (``source``,
``since``, ``until``, ``lines``, ``filter``) and answers with the same
shape: rows of ``{time, line}``, ``total``, ``truncated``, the time span
the rows cover, and the source's own continuation marker when it has one.
The connector never builds a result handle: the normal result reduction
does that when the rows are many.

This module holds only what the connectors share: the parameter schema,
the time parsing and the answer builder. What a ``source`` means, and how
the lines are read, stays in each connector.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

__all__ = [
    "DEFAULT_LOG_LINES",
    "MAX_LOG_LINES",
    "LogReadError",
    "build_log_answer",
    "log_read_parameter_schema",
    "parse_since",
    "parse_until",
]

#: Default and hard cap of ``lines``.
DEFAULT_LOG_LINES = 200
MAX_LOG_LINES = 5000

_SPAN_RE = re.compile(r"^(\d+)([smhd])$")
_SPAN_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_TIME_PATTERN = r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:[Zz]|[+-]\d{2}:\d{2})"


class LogReadError(ValueError):
    """The log read params cannot be used. Raised before any read of the source."""


def log_read_parameter_schema(source_description: str) -> dict[str, Any]:
    """Return the parameter schema of a log read; only ``source`` differs per connector."""
    return {
        "type": "object",
        "properties": {
            "source": {"type": "string", "minLength": 1, "description": source_description},
            "since": {
                "type": "string",
                "pattern": rf"^(\d+[smhd]|{_TIME_PATTERN})$",
                "description": (
                    "Start of the read: a span such as '15m', '2h' or '1d' (counted back "
                    "from now), or an exact RFC 3339 time."
                ),
            },
            "until": {
                "type": "string",
                "pattern": rf"^{_TIME_PATTERN}$",
                "description": "End of the read: an exact RFC 3339 time. Optional.",
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


def parse_since(value: str | None, now: datetime | None = None) -> datetime | None:
    """Return the start of the read as a UTC time, or ``None`` when *value* is unset."""
    if value is None:
        return None
    match = _SPAN_RE.match(value)
    if match is not None:
        seconds = int(match.group(1)) * _SPAN_SECONDS[match.group(2)]
        return (now or datetime.now(UTC)).replace(microsecond=0) - timedelta(seconds=seconds)
    return _parse_time(value)


def parse_until(value: str | None) -> datetime | None:
    """Return the end of the read as a UTC time, or ``None`` when *value* is unset."""
    return None if value is None else _parse_time(value)


def _parse_time(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value[-1] in "Zz" else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise LogReadError(f"{value!r} is not an RFC 3339 time or a span like '15m'") from None
    if parsed.tzinfo is None:
        raise LogReadError(f"{value!r} has no time zone offset")
    return parsed.astimezone(UTC)


def build_log_answer(
    rows: list[dict[str, Any]],
    *,
    lines: int,
    keep: str,
    source_cut: bool = False,
    continuation: str | None = None,
) -> dict[str, Any]:
    """Build the answer of a log read from rows that are already filtered.

    *rows* are ``{time, line}``, oldest first. When there are more than
    *lines*, *keep* (``"newest"`` or ``"oldest"``) says which end stays.
    ``truncated`` is true when rows were dropped here or *source_cut* says
    the source holds more than was read. ``covered`` is the time span of
    the rows (``null`` for a row set without times), and ``continuation``
    is the source's own marker to read on from, passed on as it is.
    """
    cut = len(rows) > lines
    if cut:
        rows = rows[-lines:] if keep == "newest" else rows[:lines]
    times = [row["time"] for row in rows if row.get("time")]
    covered = {"from": times[0], "to": times[-1]} if times else None
    return {
        "rows": rows,
        "total": len(rows),
        "truncated": cut or source_cut,
        "covered": covered,
        "continuation": continuation,
    }
