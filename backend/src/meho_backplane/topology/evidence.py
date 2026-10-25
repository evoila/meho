# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What a topology edge means, and how fresh a row is (#3760).

Two small, pure rules that topology answers share:

* **Relation class.** An edge kind is mapped to one of four classes:
  ``containment``, ``placement``, ``dependency`` or ``unknown``. Only
  reviewed kinds are mapped. Every other kind is ``unknown``. MEHO never
  guesses that an unmapped kind is a dependency, and containment (a pod
  in a namespace) is never a service dependency.
* **Freshness.** ``last_seen`` is MEHO's own time, never the vendor's
  timestamp. For a row a refresh found (``source='auto'``) it is the
  last refresh that reported the row. For a row an operator stated
  (``source='curated'``) it is the time the operator entered or
  re-stated the row; a later refresh that also observes the row moves
  it forward, but a refresh never changes or removes the row. Rows from
  different targets come from different refreshes, so a topology answer
  is never one consistent snapshot. A row is:

  * ``stale`` when the latest refresh no longer reported it
    (``last_seen`` is NULL);
  * ``asserted`` when an operator stated it (``source='curated'``);
  * ``old_observation`` when MEHO last saw it more than two refresh
    cycles ago;
  * ``observed`` otherwise.

* **Stale view.** The closure and path queries skip stale rows when
  ``include_stale`` is false, except the rows the caller named: the
  closure root and the two path endpoints are always returned, even
  when stale. :func:`stale_rows_evidence` says so in the answer.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final, Literal

__all__ = [
    "FRESHNESS_COPY",
    "MAPPED_RELATION_KINDS",
    "OBSERVATION_NOTE",
    "RELATION_CLASSES",
    "STALE_SHOWN_COPY",
    "Freshness",
    "RelationClass",
    "classify_freshness",
    "relation_class_for",
    "relation_class_kind_filter",
    "stale_rows_evidence",
]

RelationClass = Literal["containment", "placement", "dependency", "unknown"]

#: The four relation classes, in the order the tool schema lists them.
RELATION_CLASSES: Final[tuple[RelationClass, ...]] = (
    "containment",
    "placement",
    "dependency",
    "unknown",
)

#: The reviewed edge kinds. Any kind not listed here is ``unknown``.
MAPPED_RELATION_KINDS: Final[dict[str, RelationClass]] = {
    "belongs-to": "containment",
    "runs-on": "placement",
    "authenticates-via": "dependency",
}

Freshness = Literal["observed", "old_observation", "stale", "asserted"]

#: Plain-language meaning of each freshness value, returned with
#: topology answers so a reader does not have to guess.
FRESHNESS_COPY: Final[dict[Freshness, str]] = {
    "observed": "MEHO saw this in a recent topology refresh.",
    "old_observation": (
        "MEHO last saw this more than two refresh cycles ago. It is still "
        "shown, but the source may have changed since."
    ),
    "stale": "The latest refresh no longer reported this.",
    "asserted": (
        "An operator stated this. A refresh never changes or removes it. "
        "last_seen is when the operator entered or re-stated it, or a "
        "later refresh that also observed it."
    ),
}

#: Why a ``stale`` row is in the answer, keyed by ``include_stale``.
#: Appended to the ``stale`` entry of ``freshness_meaning``.
STALE_SHOWN_COPY: Final[dict[bool, str]] = {
    True: "It is shown because include_stale is true.",
    False: "It is shown only because the call named it; other stale rows are excluded.",
}

#: What ``last_seen`` means. Returned with every topology answer.
OBSERVATION_NOTE: Final[str] = (
    "last_seen is MEHO's own time, not the vendor's own timestamp. For a "
    "row a refresh found (source auto) it is the last refresh that "
    "reported the row. For a row an operator stated (source curated) it "
    "is when the operator entered or re-stated the row, or a later "
    "refresh that also observed it. Rows come from separate refreshes of "
    "separate systems, so the answer is not one consistent snapshot."
)

#: A row is ``old_observation`` when MEHO last saw it more than this many refresh
#: intervals ago (it missed at least one full refresh).
_OLD_AFTER_INTERVALS: Final[int] = 2


def relation_class_for(edge_kind: str | None) -> RelationClass | None:
    """Return the relation class of *edge_kind*, ``None`` when there is no edge."""
    if edge_kind is None:
        return None
    return MAPPED_RELATION_KINDS.get(edge_kind, "unknown")


def relation_class_kind_filter(
    relation_class: str | None,
) -> tuple[list[str] | None, list[str] | None]:
    """Turn a relation class into SQL ``(include_kinds, exclude_kinds)`` binds.

    ``None`` means no filter. A mapped class keeps only its kinds.
    ``unknown`` keeps every kind that is not mapped. Raises
    :class:`ValueError` on a value outside :data:`RELATION_CLASSES`.
    """
    if relation_class is None:
        return None, None
    if relation_class not in RELATION_CLASSES:
        raise ValueError(
            f"relation_class must be one of {list(RELATION_CLASSES)}; got {relation_class!r}"
        )
    if relation_class == "unknown":
        return None, sorted(MAPPED_RELATION_KINDS)
    return sorted(k for k, c in MAPPED_RELATION_KINDS.items() if c == relation_class), None


def stale_rows_evidence(*, include_stale: bool, named_rows: str | None) -> dict[str, str]:
    """Describe the stale view one answer used, for its ``evidence`` block.

    *named_rows* names the rows the caller asked for by name and the
    query never filters: ``"the root you named"`` for a closure,
    ``"the two endpoints you named"`` for a path. ``None`` means the
    answer has no such rows (the edge list never lists stale edges).
    """
    if include_stale:
        return {
            "stale_rows": "included",
            "stale_rows_note": (
                "include_stale is true. Rows the latest refresh no longer "
                "reported are walked and shown."
            ),
        }
    if named_rows is None:
        note = "Rows the latest refresh no longer reported are never listed."
    else:
        note = (
            "include_stale is false. Rows the latest refresh no longer "
            f"reported are not walked and not shown. The only exception is "
            f"{named_rows}, which MEHO always shows, even when stale."
        )
    return {"stale_rows": "excluded", "stale_rows_note": note}


def classify_freshness(
    source: str | None,
    last_seen: datetime | None,
    *,
    now: datetime,
    refresh_interval_seconds: int,
) -> Freshness:
    """Classify one node or edge row. See the module docstring for the rules."""
    if last_seen is None:
        return "stale"
    if source == "curated":
        return "asserted"
    old_after = timedelta(seconds=refresh_interval_seconds * _OLD_AFTER_INTERVALS)
    if now - last_seen > old_after:
        return "old_observation"
    return "observed"
