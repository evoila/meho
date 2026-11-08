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
    return {"type": ["string", "null"], "minLength": 1, "description": what}


#: JSON schema properties of the shared filters, added to both tools' input schema.
#: Kept short: the default tool list has a size limit (#4112).
FILTER_PROPERTIES: Final[dict[str, Any]] = {
    "product": {"type": ["string", "null"], "minLength": 1, "description": "Exact header product."},
    "version": {"type": ["string", "null"], "minLength": 1, "description": "Exact header version."},
    "status": {
        "type": ["string", "null"],
        "enum": [*sorted(LESSON_STATUSES), None],
        "description": "Header status.",
    },
    "tags": {
        "type": ["array", "null"],
        "items": {"type": "string", "minLength": 1},
        "minItems": 1,
        "description": "Has every tag.",
    },
    "confirmed_after": _time_property("confirmed_at on or after this."),
    "confirmed_before": _time_property("confirmed_at on or before this."),
    "updated_after": _time_property("Last write on or after this."),
    "updated_before": _time_property("Last write on or before this."),
}

#: The sentence both tool descriptions use to name the filters. The long
#: example lives in ``docs/codebase/kb.md``.
FILTER_TOOL_TEXT: Final[str] = (
    "The header filters run in the search, before ranking. Times are "
    "ISO 8601; a date alone is midnight UTC. "
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
