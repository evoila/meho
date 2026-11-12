# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Category, labels, risk level and "always wait" for one operation.

A connector, an operation group and an operation can each be classified.
A classification entry names what it matches (``connector``, ``group``,
``op``; each a glob pattern, all given ones must match) and sets one or
more of ``category``, ``labels``, ``risk`` and ``always_wait``.

How the values of one operation are found:

* **Level.** An entry with ``op`` beats one with ``group``, which beats
  one with only ``connector``. So an operation takes the values of its
  group, and a group those of its connector, unless set lower down.
* **Inside one level** the longer literal text before the first wildcard
  wins (the rule of agent grants): first of ``op``, then ``group``, then
  ``connector``. Then a tenant's own entry beats one for the whole
  install.
* **Each field on its own.** The most specific entry that sets a field
  decides that field. Labels add up: a more specific entry replaces the
  value of a key that a less specific one set.
* **Ties.** Two entries of the same rank: for ``risk`` the higher level
  wins, for ``always_wait`` ``true`` wins. A different ``category`` (or a
  different value for one label key) is a conflict. A writer refuses it
  with :class:`ClassificationConflictError`; a reader that still meets one
  makes the operation wait for approval (:attr:`Classification.conflict`).
* **No entry** sets the category: the taxonomy's default category.

The module is pure: no database, no settings. The caller loads the
entries (:mod:`meho_backplane.policy.store`) and applies the floors
afterwards (:mod:`meho_backplane.operations.op_policy`): a risk level
never goes below the level in the code, and ``always_wait: false`` only
goes back to the code's value.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any, Final, Literal

__all__ = [
    "DEFAULT_RULE_REF",
    "DEFAULT_TAXONOMY",
    "RISK_LEVELS",
    "Classification",
    "ClassificationConflictError",
    "ClassificationEntry",
    "OpCoordinates",
    "Scope",
    "Taxonomy",
    "check_no_conflict",
    "classify",
    "entries_from_document",
    "taxonomy_from_documents",
]

Scope = Literal["install", "tenant"]

#: The four risk levels, lowest first.
RISK_LEVELS: Final[tuple[str, ...]] = ("safe", "caution", "dangerous", "destructive")

_LEVEL_RANK: Final[dict[str, int]] = {"connector": 1, "group": 2, "op": 3}
_SCOPE_RANK: Final[dict[str, int]] = {"install": 0, "tenant": 1}
_GLOB_METACHARS: Final[frozenset[str]] = frozenset("*?[")

#: The rule reference of an operation that no entry matches.
DEFAULT_RULE_REF: Final[str] = "taxonomy.default"


@dataclass(frozen=True)
class Taxonomy:
    """The categories an entry may use, and the category of an unclassified operation."""

    categories: tuple[str, ...]
    default: str


#: The install's taxonomy until a policy for the whole install is stored.
DEFAULT_TAXONOMY: Final[Taxonomy] = Taxonomy(
    categories=("network", "storage", "compute", "identity", "secrets", "other"),
    default="other",
)


@dataclass(frozen=True)
class OpCoordinates:
    """What an entry can match on: the connector id, the group key, the op id."""

    connector_id: str
    group_key: str | None
    op_id: str


class ClassificationConflictError(ValueError):
    """Two entries of the same rank give one operation different values."""


def _literal_prefix(pattern: str | None) -> int:
    """Length of the text before the first wildcard; ``-1`` for no pattern."""
    if pattern is None:
        return -1
    for index, char in enumerate(pattern):
        if char in _GLOB_METACHARS:
            return index
    return len(pattern)


@dataclass(frozen=True)
class ClassificationEntry:
    """One classification entry, with where it came from (``ref``)."""

    ref: str
    scope: Scope
    connector: str | None = None
    group: str | None = None
    op: str | None = None
    category: str | None = None
    risk: str | None = None
    always_wait: bool | None = None
    labels: Mapping[str, str] = field(default_factory=dict)

    @property
    def level(self) -> str:
        """``op``, ``group`` or ``connector``: the most specific field the entry matches on."""
        if self.op is not None:
            return "op"
        if self.group is not None:
            return "group"
        return "connector"

    @property
    def rank(self) -> tuple[int, int, int, int, int]:
        """Higher is more specific: level, then prefix lengths, then the scope."""
        return (
            _LEVEL_RANK[self.level],
            _literal_prefix(self.op),
            _literal_prefix(self.group),
            _literal_prefix(self.connector),
            _SCOPE_RANK[self.scope],
        )

    def matches(self, coords: OpCoordinates) -> bool:
        """True when every pattern the entry gives matches *coords*."""
        if self.connector is not None and not fnmatchcase(coords.connector_id, self.connector):
            return False
        if self.group is not None and (
            coords.group_key is None or not fnmatchcase(coords.group_key, self.group)
        ):
            return False
        return self.op is None or fnmatchcase(coords.op_id, self.op)


@dataclass(frozen=True)
class Classification:
    """The classified values of one operation, before the floors.

    ``risk`` is ``None`` when no entry sets a level, and ``always_wait``
    is ``False`` when no entry asks for it. ``rule_ref`` names the most
    specific entry that matched, or :data:`DEFAULT_RULE_REF`.
    ``conflict`` is the one-sentence reason when two entries of the same
    rank disagree; the operation then has no category and waits.
    """

    category: str | None
    labels: Mapping[str, str]
    risk: str | None
    always_wait: bool
    rule_ref: str
    conflict: str | None = None


def _top(entries: Sequence[ClassificationEntry], attr: str) -> list[ClassificationEntry]:
    """The entries of the highest rank among *entries* that set *attr*."""
    setting = [e for e in entries if getattr(e, attr) is not None]
    if not setting:
        return []
    best = max(e.rank for e in setting)
    return [e for e in setting if e.rank == best]


def _conflict_text(
    op_id: str, what: str, first: ClassificationEntry, second: ClassificationEntry
) -> str:
    return (
        f"{first.ref} and {second.ref} give {op_id} a different {what} at the same "
        "level, so make one of them more specific."
    )


def _category(
    coords: OpCoordinates, matched: Sequence[ClassificationEntry], taxonomy: Taxonomy
) -> tuple[str | None, str | None]:
    top = _top(matched, "category")
    if not top:
        return taxonomy.default, None
    first = top[0]
    for other in top[1:]:
        if other.category != first.category:
            return None, _conflict_text(coords.op_id, "category", first, other)
    return first.category, None


def _labels(
    coords: OpCoordinates, matched: Sequence[ClassificationEntry]
) -> tuple[dict[str, str], str | None]:
    """Each key from the most specific entry that sets it; a tie with two values is a conflict."""
    labels: dict[str, str] = {}
    source: dict[str, ClassificationEntry] = {}
    for entry in sorted(matched, key=lambda e: e.rank, reverse=True):
        for key, value in entry.labels.items():
            earlier = source.get(key)
            if earlier is None:
                labels[key] = value
                source[key] = entry
            elif earlier.rank == entry.rank and labels[key] != value:
                what = f"value for label {key!r}"
                return labels, _conflict_text(coords.op_id, what, earlier, entry)
    return labels, None


def classify(
    coords: OpCoordinates,
    entries: Iterable[ClassificationEntry],
    taxonomy: Taxonomy,
) -> Classification:
    """Return the classified values of the operation at *coords*."""
    matched = [e for e in entries if e.matches(coords)]
    risks = [e.risk for e in _top(matched, "risk") if e.risk is not None]
    risk = max(risks, key=RISK_LEVELS.index) if risks else None
    always_wait = any(e.always_wait for e in _top(matched, "always_wait"))
    category, category_conflict = _category(coords, matched, taxonomy)
    labels, label_conflict = _labels(coords, matched)
    conflict = category_conflict or label_conflict
    rule_ref = max(matched, key=lambda e: e.rank).ref if matched else DEFAULT_RULE_REF
    if conflict is not None:
        return Classification(None, labels, risk, True, rule_ref, conflict)
    return Classification(category, labels, risk, always_wait, rule_ref)


def check_no_conflict(
    coords: Iterable[OpCoordinates],
    entries: Sequence[ClassificationEntry],
    taxonomy: Taxonomy,
) -> None:
    """Raise :class:`ClassificationConflictError` when an operation in *coords* has a conflict."""
    for one in coords:
        conflict = classify(one, entries, taxonomy).conflict
        if conflict is not None:
            raise ClassificationConflictError(conflict)


# ---------------------------------------------------------------------------
# The stored document
# ---------------------------------------------------------------------------


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _entry_from_raw(raw: Mapping[str, Any], *, ref: str, scope: Scope) -> ClassificationEntry:
    match = raw.get("match")
    if not isinstance(match, Mapping):
        raise ValueError(f"{ref}: `match` must be a map with connector, group or op.")
    labels = raw.get("labels") or {}
    if not isinstance(labels, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in labels.items()
    ):
        raise ValueError(f"{ref}: `labels` must be a map of text to text.")
    risk = raw.get("risk")
    if risk is not None and risk not in RISK_LEVELS:
        raise ValueError(f"{ref}: `risk` {risk!r} is not a risk level.")
    always_wait = raw.get("always_wait")
    if always_wait is not None and not isinstance(always_wait, bool):
        raise ValueError(f"{ref}: `always_wait` must be true or false.")
    return ClassificationEntry(
        ref=ref,
        scope=scope,
        connector=_text(match.get("connector")),
        group=_text(match.get("group")),
        op=_text(match.get("op")),
        category=_text(raw.get("category")),
        risk=risk,
        always_wait=always_wait,
        labels=dict(labels),
    )


def entries_from_document(
    document: Mapping[str, Any] | None, *, scope: Scope
) -> list[ClassificationEntry]:
    """Return the classification entries of a stored policy document.

    Each entry's ``ref`` is ``<scope>:classifications[<index>]``, the
    place a person finds it in that scope's policy.
    """
    if not document:
        return []
    raw_entries = document.get("classifications") or []
    if not isinstance(raw_entries, list):
        raise ValueError(f"{scope}: `classifications` must be a list.")
    return [
        _entry_from_raw(raw, ref=f"{scope}:classifications[{index}]", scope=scope)
        for index, raw in enumerate(raw_entries)
        if isinstance(raw, Mapping)
    ]


def taxonomy_from_documents(
    install: Mapping[str, Any] | None, tenant: Mapping[str, Any] | None
) -> Taxonomy:
    """Return the taxonomy for a tenant: the install's list plus the tenant's own categories.

    Without an install policy, :data:`DEFAULT_TAXONOMY` is the install's
    list. The default category is always the install's.
    """
    base = DEFAULT_TAXONOMY
    install_tax = (install or {}).get("taxonomy")
    if isinstance(install_tax, Mapping) and install_tax.get("categories"):
        base = Taxonomy(
            categories=tuple(str(c) for c in install_tax["categories"]),
            default=str(install_tax.get("default") or base.default),
        )
    tenant_tax = (tenant or {}).get("taxonomy")
    added: list[str] = []
    if isinstance(tenant_tax, Mapping):
        added = [str(c) for c in tenant_tax.get("categories") or [] if c not in base.categories]
    return Taxonomy(categories=(*base.categories, *added), default=base.default)
