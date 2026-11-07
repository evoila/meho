# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The lesson header filters shared by ``search_knowledge`` and ``search_memory`` (#5726).

Both tools take the same named filters on the lesson header
(:mod:`meho_backplane.lesson_header`):

* ``product``, ``version``, ``status``: the entry must have this exact value.
* ``tags``: the entry must have every listed tag.
* ``confirmed_after`` / ``confirmed_before``: limits on ``confirmed_at``.
* ``updated_after`` / ``updated_before``: limits on the time of the last write.

The first four become metadata containment terms; the four time limits
become one :class:`~meho_backplane.retrieval.time_window.TimeWindow`.
Both are checked in the search SQL, before ranking cuts the candidates.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.lesson_header import LESSON_STATUSES
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.retrieval.time_window import TimeWindow, parse_time_limit

__all__ = ["FILTER_PROPERTIES", "FILTER_TOOL_TEXT", "header_filter_terms", "time_window_from"]

_TIME_NAMES: Final[tuple[str, ...]] = (
    "confirmed_after",
    "confirmed_before",
    "updated_after",
    "updated_before",
)


def _time_property(what: str) -> dict[str, Any]:
    return {
        "type": ["string", "null"],
        "minLength": 1,
        "description": (
            f"{what} An ISO 8601 date or time, for example '2026-08-01' "
            "(a date means midnight UTC; a time without an offset is UTC). "
            "Inclusive."
        ),
    }


#: JSON schema properties of the shared filters, added to both tools' input schema.
FILTER_PROPERTIES: Final[dict[str, Any]] = {
    "product": {
        "type": ["string", "null"],
        "minLength": 1,
        "description": "Only entries whose header `product` is exactly this text.",
    },
    "version": {
        "type": ["string", "null"],
        "minLength": 1,
        "description": (
            "Only entries whose header `version` is exactly this text (for example '9.0')."
        ),
    },
    "status": {
        "type": ["string", "null"],
        "enum": [*sorted(LESSON_STATUSES), None],
        "description": "Only entries with this header `status`.",
    },
    "tags": {
        "type": ["array", "null"],
        "items": {"type": "string", "minLength": 1},
        "minItems": 1,
        "description": "Only entries that have every one of these tags.",
    },
    "confirmed_after": _time_property(
        "Only entries whose header `confirmed_at` is on or after this."
    ),
    "confirmed_before": _time_property(
        "Only entries whose header `confirmed_at` is on or before this."
    ),
    "updated_after": _time_property("Only entries last written on or after this."),
    "updated_before": _time_property("Only entries last written on or before this."),
}

#: The sentence both tool descriptions use to name the filters.
FILTER_TOOL_TEXT: Final[str] = (
    "Filters work in the search itself, before ranking: `product`, "
    "`version` and `status` match the lesson header exactly, `tags` "
    "needs every listed tag, `confirmed_after`/`confirmed_before` limit "
    "the header's `confirmed_at` (an entry without it is left out), and "
    "`updated_after`/`updated_before` limit the time of the last write. "
    "Example: confirmed lessons for one version from the last 90 days = "
    "`status='confirmed'`, `product`, `version`, `confirmed_after=<date "
    "90 days ago>`. "
)


def header_filter_terms(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return the metadata containment terms for ``product``, ``version``, ``status`` and ``tags``.

    A filter that is absent or null adds no term.
    """
    terms: dict[str, Any] = {
        name: arguments[name]
        for name in ("product", "version", "status")
        if arguments.get(name) is not None
    }
    tags = arguments.get("tags")
    if tags is not None:
        terms["tags"] = list(tags)
    return terms


def time_window_from(tool: str, arguments: dict[str, Any]) -> TimeWindow | None:
    """Return the time window of the four time filters; ``None`` when none is set.

    A value that is not a date, or an ``after`` later than its
    ``before``, raises :class:`McpInvalidParamsError` (JSON-RPC ``-32602``).
    """
    try:
        window = TimeWindow(
            **{name: parse_time_limit(name, arguments.get(name)) for name in _TIME_NAMES}
        )
    except ValueError as exc:
        raise McpInvalidParamsError(f"{tool}: {exc}") from exc
    return None if window.is_empty() else window
