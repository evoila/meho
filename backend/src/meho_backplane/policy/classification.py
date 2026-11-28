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

import json
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

    ``dashed_connector_id`` is set only with ``CUSTOM_POLICY`` on, for a
    connector with no version, whose id is then its product: it holds the
    form ``<impl_id>-<version>`` (for example ``-``). Such an operation is
    classified once per form (:meth:`forms`), and the stricter result
    counts (:func:`classify`). Build coordinates with
    :func:`~meho_backplane.policy.store.coordinates_of`. ``form_of`` is set
    only on a form made by :meth:`forms`: the connector id of the operation
    the form belongs to.
    """

    connector_id: str
    group_key: str | None
    op_id: str
    dashed_connector_id: str | None = None
    form_of: str | None = None

    def forms(self) -> tuple[OpCoordinates, ...]:
        """Each form of the connector id alone: the form with the dash first."""
        if self.dashed_connector_id is None:
            return (self,)
        return (
            OpCoordinates(
                self.dashed_connector_id, self.group_key, self.op_id, form_of=self.connector_id
            ),
            OpCoordinates(self.connector_id, self.group_key, self.op_id, form_of=self.connector_id),
        )

    @property
    def owner_id(self) -> str:
        """The connector id of the operation, also for one form of it."""
        return self.form_of or self.connector_id


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


@dataclass(frozen=True)
class ClassificationEntry:
    """One classification entry, with where it came from (``ref``).

    ``for_connector`` is set on the entries of an override row: the entry
    then matches only the operations of that connector id
    (:attr:`OpCoordinates.owner_id`), in each form of the id. So two
    connectors with no version that share the form with the dash and an
    op id each keep their own override.
    """

    ref: str
    scope: Scope
    connector: str | None = None
    group: str | None = None
    op: str | None = None
    category: str | None = None
    risk: str | None = None
    always_wait: bool | None = None
    labels: Mapping[str, str] = field(default_factory=dict)
    for_connector: str | None = None

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
        """True when every pattern the entry gives matches one form of *coords*."""
        return any(self._matches_form(form) for form in coords.forms())

    def _matches_form(self, coords: OpCoordinates) -> bool:
        if self.for_connector is not None and coords.owner_id != self.for_connector:
            return False
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


def _label_sources(matched: Sequence[ClassificationEntry]) -> dict[str, ClassificationEntry]:
    """Each label key MEHO shows, with the most specific entry that gives it."""
    sources: dict[str, ClassificationEntry] = {}
    for entry in sorted(matched, key=lambda e: e.rank, reverse=True):
        for key, value in entry.labels.items():
            if key not in sources and label_text_ok(key, value):
                sources[key] = entry
    return sources


def _shown_labels(matched: Sequence[ClassificationEntry]) -> dict[str, str]:
    """Each key from the most specific entry whose label passes :func:`label_text_ok`."""
    return {key: entry.labels[key] for key, entry in _label_sources(matched).items()}


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
    *entries* makes it always wait. For an operation with two forms of its
    connector id, *entry* may match one form and the install entry the
    other: the install's entries make the operation wait when they make
    either form wait. ``None`` when there is none.
    """
    if entry.scope != "tenant" or entry.always_wait is not False:
        return None
    for one in coords:
        if not entry.matches(one):
            continue
        for form in one.forms():
            install = _scope_wait([e for e in entries if e.matches(form)], "install")
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


def _tenant_form_risk(
    entry: ClassificationEntry, form: OpCoordinates, matched: Sequence[ClassificationEntry]
) -> tuple[str | None, bool]:
    """The tenant's own risk level in one *form*, and whether *entry* decides it.

    *matched* are the entries that match *form*. *entry* counts when it
    matches *form*. The level is ``None`` when no tenant entry sets one.
    """
    tenant = [e for e in matched if e.scope == "tenant" and e is not entry]
    if entry.matches(form):
        tenant.append(entry)
    top = _scope_top(tenant, "tenant", "risk")
    if not top:
        return None, False
    level = max((e.risk or "safe" for e in top), key=RISK_LEVELS.index)
    return level, any(e is entry for e in top)


def install_risk_lowered(
    entry: ClassificationEntry,
    coords: Iterable[OpCoordinates],
    entries: Sequence[ClassificationEntry],
) -> tuple[OpCoordinates, str, str] | None:
    """Where a tenant *entry* decides a lower risk level than the install's policy.

    *entries* are the install's entries and the other entries of the
    tenant. Returns ``(coords, install_ref, install_level)`` for the first
    operation in *coords* that *entry* matches, where *entry* decides the
    tenant's level and that level is below the install's. A more specific
    tenant entry that sets the install's level or higher decides that
    operation instead, so it is not returned.

    For an operation with two forms of its connector id, each side's
    level is the higher of its two forms, each form decided by that
    side's own entries. *entry* is returned when it decides the tenant's
    level in one form and the tenant's level is below the install's; the
    returned coordinates are those of the operation, named by its
    product. ``None`` when there is none.
    """
    if entry.scope != "tenant" or entry.risk is None:
        return None
    for one in coords:
        found = _risk_lowered_at(entry, one, entries)
        if found is not None:
            return one, *found
    return None


def _risk_lowered_at(
    entry: ClassificationEntry, one: OpCoordinates, entries: Sequence[ClassificationEntry]
) -> tuple[str, str] | None:
    """``(install_ref, install_level)`` when *entry* lowers the install's level of *one*."""
    if not entry.matches(one):
        return None
    forms = one.forms()
    matched = {form: [e for e in entries if e.matches(form)] for form in forms}
    picked = [_scope_risk(matched[form], "install") for form in forms]
    install = max(
        (e for e in picked if e is not None),
        key=lambda e: RISK_LEVELS.index(e.risk or "safe"),
        default=None,
    )
    if install is None:
        return None
    install_level = install.risk or "safe"
    tenant = [_tenant_form_risk(entry, form, matched[form]) for form in forms]
    decided = any(by_entry for _, by_entry in tenant)
    tenant_level = max((level for level, _ in tenant), key=_risk_index)
    if decided and _risk_index(tenant_level) < RISK_LEVELS.index(install_level):
        return install.ref, install_level
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
    if lowered is None or entry.risk is None:
        return texts
    product = _product_risk_text(name, entry, coords, entries)
    if product is not None:
        texts.append(product)
        return texts
    found = (lowered[0].connector_id, lowered[0].op_id)
    wide = any(entry.matches(one) and (one.connector_id, one.op_id) != found for one in coords)
    texts.append(
        install_risk_text(name, entry.risk, *lowered, wide=wide, with_group=entry.group is not None)
    )
    return texts


#: How many operations the sentence of :func:`_product_risk_text` names at most.
_MAX_NAMED_OPS = 5


def _product_risk_text(
    name: str,
    entry: ClassificationEntry,
    coords: Sequence[OpCoordinates],
    entries: Sequence[ClassificationEntry],
) -> str | None:
    """The sentence of :func:`install_floor_texts` when *entry* lowers a product's operation.

    It names each operation whose install level *entry* lowers, of every
    connector, each with its connector: up to :data:`_MAX_NAMED_OPS` of
    them, and how many more there are. It gives a fix for every one of
    them, one per match (:func:`_lowered_by_match`). For each it says to
    set ``risk`` on the tenant entry that already matches exactly that
    operation (:func:`_exact_entry`), or it suggests a new entry. It
    suggests a change of *entry* only when *entry* matches that one
    operation alone, because *entry* can also match the operation of
    another product that shares the form with the dash. ``None`` when
    *entry* lowers the level of no operation named by its product.
    """
    risk = entry.risk or "safe"
    with_group = entry.group is not None
    found = [
        (one, *at) for one in coords if (at := _risk_lowered_at(entry, one, entries)) is not None
    ]
    if not any(one.dashed_connector_id is not None for one, _, _ in found):
        return None
    lowered = _lowered_by_match(found, with_group=with_group)
    shown = lowered[:_MAX_NAMED_OPS]
    parts = [
        f"`{ref}` sets `{one.op_id}` of `{one.connector_id}` to `{level}`"
        for one, ref, level in shown
    ]
    if len(lowered) > len(shown):
        parts.append(f"{len(lowered) - len(shown)} more operations")
    them = f"`{shown[0][0].op_id}` of `{shown[0][0].connector_id}`" if len(lowered) == 1 else "them"
    head = (
        f"{name} sets `risk: {risk}`, but {' and '.join(parts)} for the whole install, "
        f"so `risk: {risk}` does not count for {them}."
    )
    one, _, level = lowered[0]
    key = _match_key(one, with_group=with_group)
    alone = not any(
        entry.matches(o) and _match_key(o, with_group=with_group) != key for o in coords
    )
    if len(lowered) == 1 and alone:
        return f"{head} Set `risk: {level}` or higher in this entry; its other fields can stay."
    return f"{head} This entry can stay. {_fix_text(entry, lowered, entries)}"


def _match_key(one: OpCoordinates, *, with_group: bool) -> tuple[str, str, str | None]:
    """The match of the entry that :func:`_entry_text` suggests for *one*."""
    return one.connector_id, one.op_id, one.group_key if with_group else None


def _lowered_by_match(
    found: Sequence[tuple[OpCoordinates, str, str]], *, with_group: bool
) -> list[tuple[OpCoordinates, str, str]]:
    """*found* with one row per match, each at the highest level that match needs.

    Two rows of one operation (a built-in row and the tenant's own) have
    the same match, and a suggested entry for each would repeat it.
    """
    by_key: dict[tuple[str, str, str | None], tuple[OpCoordinates, str, str]] = {}
    for one, ref, level in found:
        key = _match_key(one, with_group=with_group)
        kept = by_key.get(key)
        if kept is None or RISK_LEVELS.index(level) > RISK_LEVELS.index(kept[2]):
            by_key[key] = (kept[0] if kept else one, ref, level)
    return list(by_key.values())


def _fix_text(
    entry: ClassificationEntry,
    lowered: Sequence[tuple[OpCoordinates, str, str]],
    entries: Sequence[ClassificationEntry],
) -> str:
    """The fix for each lowered operation: ``risk`` on the exact entry, else a new entry."""
    with_group = entry.group is not None
    on_entry: dict[str, str] = {}
    new: list[str] = []
    for one, _, level in lowered:
        exact = _exact_entry(entry, one, entries, with_group=with_group)
        if exact is None:
            new.append(f"`{_entry_text(one, level, with_group=with_group)}`")
            continue
        ref = exact.ref.removeprefix("tenant:")
        if RISK_LEVELS.index(level) > _risk_index(on_entry.get(ref)):
            on_entry[ref] = level
    texts = [
        f"Set `risk: {level}` or higher on `{ref}`; its other fields can stay."
        for ref, level in on_entry.items()
    ]
    if new:
        what = "a tenant entry" if len(new) == 1 else "a tenant entry for each operation"
        texts.append(f"Add {what}, with this risk level or higher: {', '.join(new)}.")
    return " ".join(texts)


def _exact_entry(
    entry: ClassificationEntry,
    one: OpCoordinates,
    entries: Sequence[ClassificationEntry],
    *,
    with_group: bool,
) -> ClassificationEntry | None:
    """The tenant's own entry with the match that :func:`_entry_text` would suggest for *one*.

    Its connector and op are exactly those of *one*, and so is its group
    when it names one. When *with_group* is true, it must name the group:
    a suggested entry then names it too. ``None`` when the tenant's file
    has no such entry; the most specific one when it has more.
    """
    found = [
        e
        for e in entries
        if e is not entry
        and e.scope == "tenant"
        and e.for_connector is None
        and e.ref.startswith("tenant:classifications[")
        and e.connector == one.connector_id
        and e.op == one.op_id
        and (e.group == one.group_key or (e.group is None and not with_group))
    ]
    return max(found, key=lambda e: e.rank) if found else None


def _entry_text(coords: OpCoordinates, level: str, *, with_group: bool) -> str:
    """A tenant entry for the one operation at *coords*, in the form of the policy file.

    Each value is quoted (as JSON text, which YAML reads), so an op id
    with ``{``, ``}``, ``:`` or ``/`` parses as written.
    """
    group = f"group: {_quoted(coords.group_key)}, " if with_group and coords.group_key else ""
    connector, op = _quoted(coords.connector_id), _quoted(coords.op_id)
    return f"{{match: {{connector: {connector}, {group}op: {op}}}, risk: {_quoted(level)}}}"


def _quoted(value: str) -> str:
    """*value* as a double-quoted YAML text."""
    return json.dumps(value, ensure_ascii=False)


def _risk_index(risk: str | None) -> int:
    """The place of *risk* in :data:`RISK_LEVELS`; no level is below ``safe``."""
    return RISK_LEVELS.index(risk) if risk in RISK_LEVELS else -1


def _shown_ref(matched: Sequence[ClassificationEntry], shown: Classification) -> str:
    """An entry that gave a value of *shown*: the category, else the most specific shown label.

    A category cleared by a conflict names no entry. :data:`DEFAULT_RULE_REF`
    when no entry gave a shown value: the category is the taxonomy's
    default or cleared, and there are no labels.
    """
    top = _top(matched, "category") if shown.category is not None else []
    if top:
        return top[0].ref
    labelled = [e for key, e in _label_sources(matched).items() if key in shown.labels]
    return max(labelled, key=lambda e: e.rank).ref if labelled else DEFAULT_RULE_REF


def _stricter(dashed: Classification, product: Classification, shown_ref: str) -> Classification:
    """The two forms of one operation's connector id, field by field the stricter.

    The wait and the risk level are the stricter of the two, and a
    conflict of either form counts. The category and the labels are those
    of the form with the dash, so the product form never changes who
    approves. ``rule_ref`` is *shown_ref*: the entry of the form with the
    dash that gave the shown category, or else a shown label (:func:`_shown_ref`).
    The entry that makes the operation wait is the one ``meho policy
    explain`` names.
    """
    return Classification(
        category=dashed.category,
        labels=dashed.labels,
        risk=max((dashed.risk, product.risk), key=_risk_index),
        always_wait=dashed.always_wait or product.always_wait,
        rule_ref=shown_ref,
        conflict=dashed.conflict or product.conflict,
        install_category=dashed.install_category,
    )


def classify(
    coords: OpCoordinates,
    entries: Iterable[ClassificationEntry],
    taxonomy: Taxonomy,
) -> Classification:
    """Return the classified values of the operation at *coords*.

    An operation with two forms of its connector id (:meth:`OpCoordinates.forms`)
    is classified once per form; the result is the stricter of the two
    (:func:`_stricter`). An entry that matches only the product form can
    add a wait or raise the level, never take a wait away, lower the level
    or change the category or the labels.
    """
    listed = list(entries)
    forms = coords.forms()
    first = _classify_form(forms[0], listed, taxonomy)
    if len(forms) == 1:
        return first
    shown_ref = _shown_ref([e for e in listed if e.matches(forms[0])], first)
    return _stricter(first, _classify_form(forms[1], listed, taxonomy), shown_ref)


def _classify_form(
    coords: OpCoordinates, entries: Sequence[ClassificationEntry], taxonomy: Taxonomy
) -> Classification:
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
