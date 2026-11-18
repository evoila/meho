# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Help for a ``result_query`` filter that matched no row (#4174).

Part of the small form of ``result_query`` (``RESULT_QUERY_SMALL_FORM``,
off by default). A model often filters on a value that is not in the data:
the wrong case (``poweredOff`` for ``POWERED_OFF``), a short name, or a
guess. The answer is then an empty list, and the model cannot tell why.

With the switch on, a query whose filter matched no stored row also gets a
``no_match`` block: a few of the values each filter field really has in the
stored rows, so the model can fix the value and ask again. The values come
from the same stored rows the caller can already read by paging, so the
block shows nothing new.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from meho_backplane.jsonflux.query.contract import ResultQuerySpec

__all__ = ["MAX_VALUES_PER_FIELD", "MAX_VALUE_CHARS", "no_match_hint", "returns_one_row_always"]

#: Most values shown for one filter field.
MAX_VALUES_PER_FIELD: Final[int] = 10

#: A longer text value is cut to this many characters and ends with "…".
MAX_VALUE_CHARS: Final[int] = 64

_CUT_MARK: Final[str] = "…"


def returns_one_row_always(spec: ResultQuerySpec) -> bool:
    """True when the query gives one row even if no row matched.

    An aggregate without ``group_by`` (for example ``COUNT(*)``) always
    answers one row, so an empty answer cannot show that nothing matched.
    """
    return bool(spec.aggregate) and not spec.group_by


def _shown(value: Any) -> tuple[Any, bool]:
    """Return the value as shown, and whether it was cut."""
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        return value[:MAX_VALUE_CHARS] + _CUT_MARK, True
    return value, False


def _field_values(rows: Sequence[Mapping[str, Any]], field: str) -> tuple[list[Any], bool, bool]:
    """Return up to :data:`MAX_VALUES_PER_FIELD` values of *field*, in stored order.

    Also returns whether the field has more values than shown, and whether
    a shown text was cut. Null, object and list values are left out: a
    filter cannot match them by value. ``True`` and ``1`` count as two
    values.
    """
    shown: list[Any] = []
    seen: set[tuple[str, Any]] = set()
    cut = False
    for row in rows:
        value = row.get(field)
        if value is None or isinstance(value, (dict, list)):
            continue
        key = (type(value).__name__, value)
        if key in seen:
            continue
        if len(shown) == MAX_VALUES_PER_FIELD:
            return shown, True, cut
        seen.add(key)
        text, was_cut = _shown(value)
        shown.append(text)
        cut = cut or was_cut
    return shown, False, cut


def no_match_hint(rows: Sequence[Mapping[str, Any]], spec: ResultQuerySpec) -> dict[str, Any]:
    """Build the ``no_match`` block for a filter that matched no stored row.

    ``values`` maps each filter field, in the order of the filter, to the
    values it has in the stored rows. ``note`` says what to do, and names
    the fields that have more values than shown.
    """
    values: dict[str, list[Any]] = {}
    more: list[str] = []
    any_cut = False
    for field in dict.fromkeys(pred.field for pred in spec.filter):
        shown, has_more, cut = _field_values(rows, field)
        values[field] = shown
        if has_more:
            more.append(field)
        any_cut = any_cut or cut
    note = (
        "No row matched the filter. `values` lists values each filter field "
        "has in the stored rows. Check the value, its spelling and its case, "
        "then query again."
    )
    if more:
        note += f" Only the first {MAX_VALUES_PER_FIELD} values are shown for: {', '.join(more)}."
    if any_cut:
        note += f' Texts longer than {MAX_VALUE_CHARS} characters are cut and end with "…".'
    return {"note": note, "values": values}
