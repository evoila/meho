# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Short, query-relevant excerpts of a memory body for search hits (#3751).

A search hit carries a bounded excerpt, never the whole body. The full
body is served only by the selected read on the hit's ``locator``.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["SNIPPET_MAX_CHARS", "query_snippet"]

#: Most characters of body text one snippet holds. Cut ends get one
#: ``…`` each, so a snippet is at most this length plus two.
SNIPPET_MAX_CHARS: Final[int] = 240

#: Query words shorter than this are too common to pick a window.
_MIN_TERM_CHARS: Final[int] = 2

_WORD_RE: Final[re.Pattern[str]] = re.compile(r"\w+")


def _first_match(body: str, query: str) -> int:
    """Return where the first query word appears in *body*, or ``0``."""
    terms = {word for word in _WORD_RE.findall(query) if len(word) >= _MIN_TERM_CHARS}
    if not terms:
        return 0
    pattern = "|".join(re.escape(term) for term in sorted(terms, key=len, reverse=True))
    match = re.search(pattern, body, flags=re.IGNORECASE)
    return match.start() if match else 0


def query_snippet(body: str, query: str, max_chars: int = SNIPPET_MAX_CHARS) -> tuple[str, bool]:
    """Return ``(snippet, truncated)`` for *body*.

    A body that fits is returned whole with ``truncated=False``. A longer
    body is cut to a *max_chars* window that starts a little before the
    first query word it contains (or at the start when no word matches).
    Each cut end is marked with ``…``.
    """
    if len(body) <= max_chars:
        return body, False
    anchor = _first_match(body, query)
    start = max(0, min(anchor - max_chars // 4, len(body) - max_chars))
    end = start + max_chars
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(body) else ""
    return f"{prefix}{body[start:end]}{suffix}", True
