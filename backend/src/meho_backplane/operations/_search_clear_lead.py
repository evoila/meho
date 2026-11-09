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

A wider near-tie band is not used. In a local measurement it took
``matched`` away from six right answers for each wrong one. It also cannot
work for a search with no keyword hits: those top two hits are always
exactly one list position apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.operations.meta_tools import OperationSearchHit

__all__ = [
    "SEARCH_CLEAR_LEAD_MIN_HITS",
    "clear_lead_min_hits",
    "search_clear_lead",
    "top_hit_lead_is_unclear",
]

#: Hits the search reads with the switch on, even for a smaller ``limit``.
#: The hit that is closer in meaning can sit below the top two: in the live
#: search it was fourth. The result is the same for every ``limit``.
SEARCH_CLEAR_LEAD_MIN_HITS: Final[int] = 10


def search_clear_lead() -> bool:
    """Return the state of the ``SEARCH_CLEAR_LEAD`` switch."""
    return get_settings().search_clear_lead


def clear_lead_min_hits() -> int:
    """Return the fewest hits the search must read for the switch's rule."""
    return SEARCH_CLEAR_LEAD_MIN_HITS if search_clear_lead() else 0


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
