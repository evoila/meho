# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Answer-ready reduced replies: a short table, value counts and one read-more line.

The JSONFlux reducer stores a large list and returns a summary plus a
handle. With the ``JSONFLUX_ANSWER_READY`` setting on (it is off by
default), the summary also gets:

* ``items`` -- every row, cut to 1 to 3 short columns, when that table fits
  in about 3 KB (:data:`ITEMS_BYTE_BUDGET`);
* ``counts`` -- for each short column with 10 or fewer different values and
  at least one repeated value, how many rows have each value.

The two long how-to texts on the handle are replaced by one line
(:func:`read_more_line`).

This module only builds those values from the rows. The reducer decides
whether they fit in its inline byte budget. Everything here is a pure
function and never raises on odd rows: a value that is not short text, a
number, a bool or null makes its column unusable, nothing more.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from meho_backplane.operations.result_document import json_text_bytes

__all__ = [
    "ANSWER_READY_OUTPUT_PROPERTIES",
    "COUNTS_BYTE_BUDGET",
    "ITEMS_BYTE_BUDGET",
    "column_counts",
    "compact_items",
    "fit_items",
    "pick_columns",
    "read_more_line",
]

#: Most bytes (JSON text) the ``items`` table may take inline.
ITEMS_BYTE_BUDGET = 3072

#: Most bytes (JSON text) the ``counts`` block may take inline.
COUNTS_BYTE_BUDGET = 1024

#: Most columns in the ``items`` table.
MAX_ITEM_COLUMNS = 3

#: A column is counted only when it has at most this many different values.
MAX_COUNT_VALUES = 10

#: A text value longer than this makes its column too long for the table.
SHORT_TEXT_CHARS = 64

#: Column names that usually say which row is which, best first. The first
#: one that is a short column becomes the first column of the table.
_IDENTITY_NAMES = (
    "name",
    "display_name",
    "displayName",
    "title",
    "hostname",
    "connector_id",
    "key",
    "id",
    "value",
)

#: Output-schema entries for the two summary keys, for a tool whose output
#: schema lists the reduced-summary keys (``list_targets``, ``query_topology``).
ANSWER_READY_OUTPUT_PROPERTIES: dict[str, Any] = {
    "items": {
        "type": "array",
        "items": {"type": "object"},
        "description": (
            "Only when the answer-ready setting is on: every row, cut to 1 to 3 "
            "short columns. Absent when the table does not fit."
        ),
    },
    "counts": {
        "type": "object",
        "description": (
            "Only when the answer-ready setting is on: for each column with 10 or "
            "fewer different values, how many rows have each value."
        ),
    },
}

_MISSING = object()


def _is_short(value: Any) -> bool:
    if value is None or isinstance(value, bool | int | float):
        return True
    return isinstance(value, str) and len(value) <= SHORT_TEXT_CHARS


def _short_columns(rows: Sequence[dict[str, Any]]) -> list[str]:
    """Columns whose every value is short (text, number, bool or null), in first-seen order.

    A column with only nulls or missing values is left out: it says nothing.
    """
    seen: dict[str, bool] = {}
    has_value: set[str] = set()
    for row in rows:
        for key, value in row.items():
            if not isinstance(key, str):
                continue
            ok = seen.setdefault(key, True)
            if ok and not _is_short(value):
                seen[key] = False
            if value is not None:
                has_value.add(key)
    return [key for key, ok in seen.items() if ok and key in has_value]


def _count_key(value: Any) -> str:
    """The text a value is counted under (JSON spelling for non-text values)."""
    if value is _MISSING or value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return value if isinstance(value, str) else str(value)


def _value_counts(rows: Sequence[dict[str, Any]], column: str) -> dict[str, int] | None:
    """Counts per value of *column*, or ``None`` when it has too many values or none repeat."""
    counts: dict[str, int] = {}
    for row in rows:
        key = _count_key(row.get(column, _MISSING))
        if key not in counts and len(counts) >= MAX_COUNT_VALUES:
            return None
        counts[key] = counts.get(key, 0) + 1
    if len(counts) >= len(rows):
        return None
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def column_counts(
    rows: Sequence[dict[str, Any]], *, byte_budget: int = COUNTS_BYTE_BUDGET
) -> dict[str, dict[str, int]]:
    """Value counts for each short column with few different values.

    A column is counted when it has at most :data:`MAX_COUNT_VALUES`
    different values and at least one value is repeated (a column where
    every row differs, like a name, is not counted). A missing value counts
    as ``"null"``. Columns are added in first-seen order while the block
    stays within *byte_budget* bytes of JSON text; the rest are left out.
    The counts cover every row in *rows*.
    """
    result: dict[str, dict[str, int]] = {}
    for column in _short_columns(rows):
        counts = _value_counts(rows, column)
        if counts is None:
            continue
        if json_text_bytes({**result, column: counts}) > byte_budget:
            continue
        result[column] = counts
    return result


def pick_columns(
    rows: Sequence[dict[str, Any]],
    counts: Mapping[str, Mapping[str, int]],
    preferred: Sequence[str] = (),
) -> list[str]:
    """Pick up to :data:`MAX_ITEM_COLUMNS` short columns for the ``items`` table, best first.

    *preferred* are columns an operation names itself (today the name and
    status fields of its ``result_digest`` hint). Each one that is a short
    column is used first. Otherwise the first column is the best identity
    column (:data:`_IDENTITY_NAMES`). The rest are the counted columns
    (*counts*: few different values, such as a state), in their order; a
    column with one value for every row is left out, because ``counts``
    already says it. With no usable column the list is empty.
    """
    short = _short_columns(rows)
    picked = [name for name in preferred if name in short]
    if not picked:
        identity = next((name for name in _IDENTITY_NAMES if name in short), None)
        if identity is not None:
            picked.append(identity)
    for name, values in counts.items():
        if len(picked) >= MAX_ITEM_COLUMNS:
            break
        if len(values) > 1 and name in short and name not in picked:
            picked.append(name)
    return picked[:MAX_ITEM_COLUMNS]


def compact_items(
    rows: Sequence[dict[str, Any]], columns: Sequence[str], *, byte_budget: int
) -> list[dict[str, Any]] | None:
    """Every row cut to *columns*, or ``None`` when the table is over *byte_budget*.

    A row that lacks a column gets ``null`` for it, so every item has the
    same keys. The table is all rows or nothing: a table that would need to
    be cut is not returned, because a cut table reads like the whole list.
    """
    if not columns or not rows:
        return None
    items: list[dict[str, Any]] = []
    used = 2  # the brackets
    for row in rows:
        item = {column: row.get(column) for column in columns}
        used += json_text_bytes(item) + (1 if items else 0)
        if used > byte_budget:
            return None
        items.append(item)
    return items


def fit_items(
    rows: Sequence[dict[str, Any]], columns: Sequence[str], *, byte_budget: int
) -> tuple[list[dict[str, Any]], list[str]] | None:
    """The widest table of all rows that fits *byte_budget*, and its columns.

    Tries every column of *columns*, then drops the last column until the
    table fits. ``None`` when not even the first column fits.
    """
    for width in range(len(columns), 0, -1):
        picked = list(columns[:width])
        items = compact_items(rows, picked, byte_budget=byte_budget)
        if items is not None:
            return items, picked
    return None


def read_more_line(
    *,
    handle_id: str,
    total_rows: int,
    items_columns: Sequence[str],
    sample_rows: int,
    has_counts: bool,
    stored_rows: int | None,
    unavailable_reason: str | None,
) -> str:
    """The one line that replaces the two long how-to texts on the handle.

    It says what the summary shows, whether the counts cover every row, and
    how to read more. When only some rows were stored it says how many, and
    when none were stored it says ``result_query`` cannot read them.
    """
    if items_columns:
        shown = (
            f"Showing all {total_rows} rows this call returned in items "
            f"({', '.join(items_columns)})."
        )
    else:
        shown = f"Showing {sample_rows} of {total_rows} rows this call returned."
    if has_counts:
        shown += f" The counts cover all {total_rows} rows."
    if stored_rows is None:
        why = unavailable_reason or "result_store_unavailable"
        return (
            f"{shown} The rows were not stored ({why}), so result_query cannot read them; "
            "call the operation again with narrower params."
        )
    tail = (
        ""
        if stored_rows >= total_rows
        else f" Only the first {stored_rows} rows are stored; the rest cannot be read."
    )
    return (
        f"{shown} For other columns or a filter, call result_query with "
        f"handle_id={handle_id}.{tail}"
    )
