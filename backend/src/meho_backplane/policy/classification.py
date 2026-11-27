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
* **The stricter of install and tenant counts** for ``risk`` and
  ``always_wait``. Each scope first finds its own value with the rules
  above, using only its own entries. Then the higher risk level counts,
  and the operation always waits when either scope says so. So a tenant
  can only make things stricter than the install
  (:func:`install_wait_undone`, :func:`install_risk_lowered`), and an
  install entry never ends a wait or lowers a level that a tenant set.
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

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any, Final, Literal

__all__ = [
    "DEFAULT_RULE_REF",
    "DEFAULT_TAXONOMY",
    "LABEL_KEY_MAX",
    "LABEL_VALUE_MAX",
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
    "has_wildcard",
    "install_floor_texts",
    "install_risk_lowered",
    "install_risk_text",
    "install_wait_text",
    "install_wait_undone",
    "label_text_ok",
    "taxonomy_from_documents",
]

Scope = Literal["install", "tenant"]

#: The four risk levels, lowest first.
RISK_LEVELS: Final[tuple[str, ...]] = ("safe", "caution", "dangerous", "destructive")

_LEVEL_RANK: Final[dict[str, int]] = {"connector": 1, "group": 2, "op": 3}
_SCOPE_RANK: Final[dict[str, int]] = {"install": 0, "tenant": 1}
_SCOPES: Final[tuple[Scope, ...]] = ("install", "tenant")
_GLOB_METACHARS: Final[frozenset[str]] = frozenset("*?[")
_LABEL_KEY_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._/-]+")

#: The longest label key and label value, in characters.
LABEL_KEY_MAX: Final[int] = 63
LABEL_VALUE_MAX: Final[int] = 255

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


def label_text_ok(key: str, value: str) -> bool:
    """Whether a label may be stored and shown: label text can reach the agent.

    A key has only letters, digits and ``. _ / -``. A value has only
    printable characters, so no line breaks and no control characters.
    A key has 1 to :data:`LABEL_KEY_MAX` characters and a value 1 to
    :data:`LABEL_VALUE_MAX`, the same limits a write has.
    """
    return _label_key_ok(key) and 0 < len(value) <= LABEL_VALUE_MAX and value.isprintable()


def _label_key_ok(key: str) -> bool:
    """Whether *key* passes the key part of :func:`label_text_ok`."""
    return len(key) <= LABEL_KEY_MAX and bool(_LABEL_KEY_RE.fullmatch(key))


@dataclass(frozen=True)
class OpCoordinates:
    """What an entry can match on: the connector id, the group key, the op id.

    ``dashed_connector_id`` is set only for a connector with no version,
    whose id is its product: it holds the form ``<impl_id>-<version>``
    (for example ``-``). An entry's connector pattern matches either form.
    Build coordinates with :func:`~meho_backplane.policy.store.coordinates_of`.
    """

    connector_id: str
    group_key: str | None
    op_id: str
    dashed_connector_id: str | None = None


class ClassificationConflictError(ValueError):
    """Two entries of the same rank give one operation different values."""


def has_wildcard(pattern: str) -> bool:
    """True when *pattern* holds ``*``, ``?`` or ``[``: it can match more than its own text."""
    return any(char in _GLOB_METACHARS for char in pattern)


def _literal_prefix(pattern: str | None) -> int:
    """Length of the text before the first wildcard; ``-1`` for no pattern."""
    if pattern is None:
        return -1
    for index, char in enumerate(pattern):
        if char in _GLOB_METACHARS:
            return index
    return len(pattern)


def _connector_matches(coords: OpCoordinates, pattern: str) -> bool:
    """True when *pattern* matches the connector id or its form with the dash."""
    if fnmatchcase(coords.connector_id, pattern):
        return True
    return coords.dashed_connector_id is not None and fnmatchcase(
        coords.dashed_connector_id, pattern
    )


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
        if self.connector is not None and not _connector_matches(coords, self.connector):
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
    ``labels`` holds only the labels that pass :func:`label_text_ok`.
    ``install_category`` is the category that the install's entries alone
    give the operation. The install's approver routing uses it, so a
    tenant entry that moves the operation to another category does not
    take it away from the install's routing. It is the empty text when
    the install's entries give the operation two categories: the install's
    routing then does not know the category (an operation registered after
    the install's import can match two such entries).
    """

    category: str | None
    labels: Mapping[str, str]
    risk: str | None
    always_wait: bool
    rule_ref: str
    conflict: str | None = None
    install_category: str | None = None


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


def _label_conflict(coords: OpCoordinates, matched: Sequence[ClassificationEntry]) -> str | None:
    """A tie with two values for one key, found on the labels as stored."""
    labels: dict[str, str] = {}
    source: dict[str, ClassificationEntry] = {}
    for entry in sorted(matched, key=lambda e: e.rank, reverse=True):
        for key, value in entry.labels.items():
            earlier = source.get(key)
            if earlier is None:
                labels[key] = value
                source[key] = entry
            elif earlier.rank == entry.rank and labels[key] != value:
                # A key MEHO does not show is not printed in the sentence either.
                name = f"label {key!r}" if _label_key_ok(key) else "a label that is not shown"
                return _conflict_text(coords.op_id, f"value for {name}", earlier, entry)
    return None


def _shown_labels(matched: Sequence[ClassificationEntry]) -> dict[str, str]:
    """Each key from the most specific entry whose label passes :func:`label_text_ok`."""
    labels: dict[str, str] = {}
    for entry in sorted(matched, key=lambda e: e.rank, reverse=True):
        for key, value in entry.labels.items():
            if key not in labels and label_text_ok(key, value):
                labels[key] = value
    return labels


def _scope_top(
    matched: Sequence[ClassificationEntry], scope: Scope, attr: str
) -> list[ClassificationEntry]:
    """The entries of *scope* that decide *attr*, with that scope's own specificity.

    So a more specific install entry with ``always_wait: false`` still
    beats a less specific install entry with ``true``.
    """
    return _top([e for e in matched if e.scope == scope], attr)


def _scope_risk(matched: Sequence[ClassificationEntry], scope: Scope) -> ClassificationEntry | None:
    """The entry that sets the risk level of *scope*; the higher level on a tie."""
    top = _scope_top(matched, scope, "risk")
    return max(top, key=lambda e: RISK_LEVELS.index(e.risk or "safe")) if top else None


def _scope_wait(matched: Sequence[ClassificationEntry], scope: Scope) -> ClassificationEntry | None:
    """The entry of *scope* that makes the operation always wait, or ``None``."""
    return next((e for e in _scope_top(matched, scope, "always_wait") if e.always_wait), None)


def install_wait_undone(
    entry: ClassificationEntry,
    coords: Iterable[OpCoordinates],
    entries: Sequence[ClassificationEntry],
) -> tuple[str, str] | None:
    """Where a tenant *entry* tries to switch off an install-wide "always wait".

    Returns ``(op_id, install_ref)`` for the first operation in *coords*
    that *entry* sets to ``always_wait: false`` while an install entry in
    *entries* makes it always wait. ``None`` when there is none.
    """
    if entry.scope != "tenant" or entry.always_wait is not False:
        return None
    for one in coords:
        if not entry.matches(one):
            continue
        install = _scope_wait([e for e in entries if e.matches(one)], "install")
        if install is not None:
            return one.op_id, install.ref
    return None


def install_wait_text(name: str, op_id: str, install_ref: str, *, single_edit: bool = False) -> str:
    """The sentences that refuse or mark an entry found by :func:`install_wait_undone`.

    *single_edit* is true for one edit sent on its own: it has no file
    entry, so the text says what not to send.
    """
    head = (
        f"{name} sets `always_wait: false` on `{op_id}`, but `{install_ref}` makes it "
        "always wait for the whole install. A tenant can make more operations wait, "
        "not fewer."
    )
    if single_edit:
        return f"{head} Do not send `always_wait: false`."
    return f"{head} Remove `always_wait: false` from the tenant entry; its other fields can stay."


def _tenant_risk_decided_by(
    entry: ClassificationEntry, matched: Sequence[ClassificationEntry]
) -> str | None:
    """The tenant's own risk level when *entry* is one that decides it, else ``None``.

    *matched* are the entries that match one operation. A more specific
    tenant entry that sets ``risk`` decides instead of *entry*.
    """
    tenant = [e for e in matched if e.scope == "tenant"]
    if not any(e is entry for e in tenant):
        tenant.append(entry)
    top = _scope_top(tenant, "tenant", "risk")
    if not any(e is entry for e in top):
        return None
    return max((e.risk or "safe" for e in top), key=RISK_LEVELS.index)


def install_risk_lowered(
    entry: ClassificationEntry,
    coords: Iterable[OpCoordinates],
    entries: Sequence[ClassificationEntry],
) -> tuple[OpCoordinates, str, str] | None:
    """Where a tenant *entry* decides a lower risk level than the install's policy.

    *entries* are the install's entries and the other entries of the
    tenant. Returns ``(coords, install_ref, install_level)`` for the first
    operation in *coords* that *entry* matches, where *entry* decides the
    tenant's level and an install entry sets a higher one. A more specific
    tenant entry that sets the install's level or higher decides that
    operation instead, so it is not returned. ``None`` when there is none.
    """
    if entry.scope != "tenant" or entry.risk is None:
        return None
    for one in coords:
        if not entry.matches(one):
            continue
        matched = [e for e in entries if e.matches(one)]
        install = _scope_risk(matched, "install")
        if install is None:
            continue
        tenant_level = _tenant_risk_decided_by(entry, matched)
        install_level = install.risk or "safe"
        if tenant_level is not None and RISK_LEVELS.index(tenant_level) < RISK_LEVELS.index(
            install_level
        ):
            return one, install.ref, install_level
    return None


def install_risk_text(
    name: str,
    risk: str,
    coords: OpCoordinates,
    install_ref: str,
    level: str,
    *,
    wide: bool,
    with_group: bool = False,
) -> str:
    """The sentences that refuse or mark an entry found by :func:`install_risk_lowered`.

    *wide* is true when the entry also matches other operations. Then the
    text keeps its risk level, because it still counts for those. The
    suggested entry names the connector too: the same op id can exist on
    another connector, and an entry without a connector would set its
    level as well. *with_group* is true when the entry names a group: the
    suggested entry then names the group too, so it is more specific than
    the entry and decides the operation's level.
    """
    op_id = coords.op_id
    where = f"connector `{coords.connector_id}`"
    if with_group and coords.group_key is not None:
        where += f" and group `{coords.group_key}`"
    head = (
        f"{name} sets `risk: {risk}`, but `{install_ref}` sets `{op_id}` to `{level}` "
        f"for the whole install, so `risk: {risk}` does not count for `{op_id}`."
    )
    if not wide:
        return f"{head} Set `risk: {level}` or higher in this entry; its other fields can stay."
    return (
        f"{head} The entry still counts for the other operations it matches. Add a "
        f"tenant entry for `{op_id}` of {where} with "
        f"`risk: {level}` or higher, or leave `{op_id}` out of this entry's match."
    )


def install_floor_texts(
    name: str,
    entry: ClassificationEntry,
    coords: Sequence[OpCoordinates],
    entries: Sequence[ClassificationEntry],
) -> list[str]:
    """Each part of a tenant *entry* that the install's policy overrules: one text each.

    The parts are ``always_wait: false`` against an install-wide wait and
    a ``risk`` below the install's level. *entries* are the install's
    entries and the tenant's entries. An empty list when neither.
    """
    texts: list[str] = []
    undone = install_wait_undone(entry, coords, entries)
    if undone is not None:
        texts.append(install_wait_text(name, *undone))
    lowered = install_risk_lowered(entry, coords, entries)
    if lowered is not None and entry.risk is not None:
        found = lowered[0]
        wide = any(
            entry.matches(one)
            and (one.connector_id, one.op_id) != (found.connector_id, found.op_id)
            for one in coords
        )
        texts.append(
            install_risk_text(
                name, entry.risk, *lowered, wide=wide, with_group=entry.group is not None
            )
        )
    return texts


def classify(
    coords: OpCoordinates,
    entries: Iterable[ClassificationEntry],
    taxonomy: Taxonomy,
) -> Classification:
    """Return the classified values of the operation at *coords*."""
    matched = [e for e in entries if e.matches(coords)]
    picked = [_scope_risk(matched, scope) for scope in _SCOPES]
    risks = [e.risk for e in picked if e is not None and e.risk is not None]
    risk = max(risks, key=RISK_LEVELS.index) if risks else None
    always_wait = any(_scope_wait(matched, scope) is not None for scope in _SCOPES)
    category, category_conflict = _category(coords, matched, taxonomy)
    # Conflicts are found on the labels as stored, so a label MEHO cannot show
    # can still make the call wait. What MEHO shows skips such a label.
    label_conflict = _label_conflict(coords, matched)
    labels = _shown_labels(matched)
    conflict = category_conflict or label_conflict
    rule_ref = max(matched, key=lambda e: e.rank).ref if matched else DEFAULT_RULE_REF
    install_category, install_conflict = _category(
        coords, [e for e in matched if e.scope == "install"], taxonomy
    )
    if install_conflict is not None:
        install_category = ""
    if conflict is not None:
        return Classification(None, labels, risk, True, rule_ref, conflict, install_category)
    return Classification(category, labels, risk, always_wait, rule_ref, None, install_category)


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
    entries = []
    for index, raw in enumerate(raw_entries):
        ref = f"{scope}:classifications[{index}]"
        if not isinstance(raw, Mapping):
            raise ValueError(f"{ref}: an entry must be a map.")
        entries.append(_entry_from_raw(raw, ref=ref, scope=scope))
    return entries


def taxonomy_from_documents(
    install: Mapping[str, Any] | None, tenant: Mapping[str, Any] | None
) -> Taxonomy:
    """Return the taxonomy for a tenant: the install's list plus the tenant's own categories.

    The install's ``categories`` and ``default`` each replace the one of
    :data:`DEFAULT_TAXONOMY` when set. The default category is always the
    install's.
    """
    base = DEFAULT_TAXONOMY
    install_tax = (install or {}).get("taxonomy")
    if isinstance(install_tax, Mapping):
        categories = install_tax.get("categories")
        base = Taxonomy(
            categories=tuple(str(c) for c in categories) if categories else base.categories,
            default=str(install_tax.get("default") or base.default),
        )
    tenant_tax = (tenant or {}).get("taxonomy")
    added: list[str] = []
    if isinstance(tenant_tax, Mapping):
        added = [str(c) for c in tenant_tax.get("categories") or [] if c not in base.categories]
    return Taxonomy(categories=(*base.categories, *added), default=base.default)
