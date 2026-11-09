# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A stricter ``matched`` outcome for ``search_operations``, behind a switch (#6014).

``matched`` tells an agent to call the top hit. A live search for "list
virtual machines" answered ``matched`` with a per-VM operation on top, and
the right operation fifth. The top hit had a weak keyword score, and two
hits below it were closer in meaning.

The switch ``SEARCH_CLEAR_LEAD``
(:attr:`~meho_backplane.settings.Settings.search_clear_lead`, off by
default) adds one rule: when another hit is closer in meaning than the top
hit, the top hit is not clearly ahead, so the search answers ``ambiguous``.
Only the outcome changes, never the hits or their order.

The hit that is closer in meaning can rank below any cut: below ten
keyword hits it ranks eleventh. So with the switch on, the search also
reads the candidate with the best meaning score
(``keep_best_meaning`` of :func:`~meho_backplane.operations._search.hybrid_search`),
and the rule compares the top hit against it. The returned list is still
cut to ``limit``.

A wider near-tie band is not used. In a local measurement it took
``matched`` away from six right answers for each wrong one. It also cannot
work for a search with no keyword hits: those top two hits are always
exactly one list position apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.operations.meta_tools import OperationSearchHit

__all__ = [
    "search_clear_lead",
    "top_hit_lead_is_unclear",
]


def search_clear_lead() -> bool:
    """Return the state of the ``SEARCH_CLEAR_LEAD`` switch."""
    return get_settings().search_clear_lead


def top_hit_lead_is_unclear(hits: Sequence[OperationSearchHit]) -> bool:
    """Return ``True`` when another hit is closer in meaning than the top hit.

    Only with the switch on. A top hit with no meaning score counts as
    behind any hit that has one: an unknown score is never a clear lead. A
    list where no hit has a meaning score says nothing, so it is not
    unclear.
    """
    if len(hits) < 2 or not search_clear_lead():
        return False
    top_score = hits[0].cosine_score
    return any(
        hit.cosine_score is not None and (top_score is None or hit.cosine_score > top_score)
        for hit in hits[1:]
    )
