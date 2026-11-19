# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The install's approver routing is a floor: a tenant can only narrow it.

The install's policy file and a tenant's policy file can both hold
``routing`` entries (:mod:`meho_backplane.policy.routing`). An entry names
group kinds (``Readers``, ``Operators``, ``SA-Operators``, ``Admins``,
``Approvers``). A login group, matched by its full path or its group
attribute, counts as the kind its binding gives it
(:mod:`meho_backplane.policy.bindings`). So routing compares kinds only.

**Narrower**, exactly: a tenant entry is narrower than an install entry
when every kind it names is also named by the install entry. The same
kinds count as narrower too.

**Which install entries a tenant entry is checked against.** Every install
entry that can route a change the tenant entry also routes. Two entries
can share a change unless one field of their ``match`` tells them apart:

* ``category`` and ``risk``: both entries set the field to different
  values.
* ``connector``: both set a pattern, and the patterns cannot fit the same
  connector id. When one of the two patterns has no wildcard (``*``,
  ``?``, ``[``), MEHO checks that text against the other pattern. When
  both have wildcards and differ, MEHO counts them as sharing a change.

A field that one of the two entries leaves out fits everything, so it
never tells them apart. All of this is decided from the two files alone,
when the tenant's file is imported.

So a tenant entry is refused at import (:func:`wider_entries`) when it
names a kind that the install entry does not name. This covers a wider
entry, an entry that sends the category to another group, an entry that
adds a group, and an entry that would take the category away from the
install's routing (for example ``match: {risk: caution}`` with every
kind). A category that no install entry routes is free.

**At the run.** The install's entry counts first, then the tenant's; a
person must be in the kinds of both
(:func:`~meho_backplane.policy.routing.route`). The install's entries match
on the category the install's policy alone gives the change. A tenant may
give an operation another category for its own rules, also one the install
does not route, but the install's routing for the operation still applies.
A request without stored group kinds started to wait while the switch was
off: it keeps today's rule. At the approve click MEHO
routes the request again with the install's current routing
(:func:`install_floor_refusal`). So a tenant entry stored before this rule,
or an install file that changed later, never widens anything: where the
tenant entry is wider, its extra kinds do not count. When MEHO does not
know the install's category of a request (the empty text: the install's
classification gives the change two categories, or the request started to
wait before MEHO stored it), the click is refused when an install entry
that matches on a category could fit it. ``meho policy
explain --request`` uses the same install routing (:func:`install_floor`).

**Install import.** The import diff of the install's file lists the
tenants whose stored routing is wider than the new file
(:func:`tenants_wider`).

Everything here runs only while the ``POLICY_APPROVER_ROUTING`` switch is
on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import ApprovalRequest, TenantPolicy
from meho_backplane.policy.routing import (
    CATEGORY_NOT_STORED,
    DEFAULT_RULE,
    ApproverSet,
    RouteEntry,
    _routes,
    group_names,
    kind_of,
    route,
    routing_enabled,
)
from meho_backplane.policy.store import _documents, classification_enabled

__all__ = [
    "CATEGORY_NOT_STORED",
    "InstallFloor",
    "Wider",
    "current_install_floor",
    "floor_warnings",
    "install_floor",
    "install_floor_refusal",
    "load_install_floor",
    "may_share",
    "tenant_floor_mistakes",
    "tenant_floor_warnings",
    "tenant_wider_notes",
    "tenants_wider",
    "wider_entries",
    "wider_findings",
]

_WILDCARDS: Final[frozenset[str]] = frozenset("*?[")


def _literal(pattern: str) -> bool:
    return not set(pattern) & _WILDCARDS


def _connectors_may_share(tenant: str | None, install: str | None) -> bool:
    if tenant is None or install is None or tenant == install:
        return True
    if _literal(tenant):
        return fnmatchcase(tenant, install)
    if _literal(install):
        return fnmatchcase(install, tenant)
    return True


def may_share(tenant: RouteEntry, install: RouteEntry) -> bool:
    """True when *tenant* and *install* can both fit one change."""
    for field in ("category", "risk"):
        mine, floor = getattr(tenant, field), getattr(install, field)
        if mine is not None and floor is not None and mine != floor:
            return False
    return _connectors_may_share(tenant.connector, install.connector)


def _describe(entry: RouteEntry) -> str:
    parts = [
        f"{field} `{value}`"
        for field in ("category", "connector", "risk")
        if (value := getattr(entry, field)) is not None
    ]
    return " and ".join(parts)


def _place(entry: RouteEntry) -> str:
    return entry.ref.split(":", 1)[-1]


def _both_wildcards(tenant: RouteEntry, install: RouteEntry) -> bool:
    """True when MEHO cannot tell whether the two connector patterns fit one connector."""
    mine, floor = tenant.connector, install.connector
    return (
        mine is not None
        and floor is not None
        and mine != floor
        and not _literal(mine)
        and not _literal(floor)
    )


def _field_covers(earlier: str | None, *values: str | None) -> bool:
    """True when an *earlier* match field fits every change one of *values* fits."""
    return earlier is None or any(value is not None and earlier == value for value in values)


def _connector_covers(earlier: str | None, *patterns: str | None) -> bool:
    """Like :func:`_field_covers`; a pattern without a wildcard is also tested against *earlier*."""
    if earlier is None or _field_covers(earlier, *patterns):
        return True
    return any(p is not None and _literal(p) and fnmatchcase(p, earlier) for p in patterns)


def _covered_by(earlier: RouteEntry, tenant: RouteEntry, install: RouteEntry) -> bool:
    """True when *earlier* fits every change that both *tenant* and *install* fit.

    Then *tenant* does not count for those changes: the first tenant entry
    that fits counts. This holds while the tenant gives the operation the
    install's category. A tenant that moves an operation to another
    category can make *tenant* count after all, but the run still takes
    the install's groups as the floor, so nothing gets wider.
    """
    return (
        _field_covers(earlier.category, tenant.category, install.category)
        and _field_covers(earlier.risk, tenant.risk, install.risk)
        and _connector_covers(earlier.connector, tenant.connector, install.connector)
    )


@dataclass(frozen=True)
class Wider:
    """A tenant entry that names more group kinds than an install entry it may share a change with.

    ``index`` is the place of the tenant entry in its ``routing`` list.
    ``sure`` is false when the tenant entry may never count for those
    changes: an earlier tenant entry fits each of them first, or MEHO
    cannot tell whether the two connector patterns fit one connector. The
    import refuses an entry only when ``sure`` is true and warns
    otherwise. Either way the run takes the install's groups as the floor,
    so the entry never widens anything.
    """

    index: int
    text: str
    sure: bool


def wider_findings(tenant: Sequence[RouteEntry], install: Sequence[RouteEntry]) -> list[Wider]:
    """One finding per tenant entry and install entry where the tenant's is wider."""
    found: list[Wider] = []
    for index, mine in enumerate(tenant):
        for floor in install:
            extra = [kind for kind in mine.approvers if kind not in floor.approvers]
            if not extra or not may_share(mine, floor):
                continue
            text = (
                f"`{_place(mine)}` lets {group_names(extra)} approve changes with "
                f"{_describe(floor)}, but the install's routing (`{floor.ref}`) lets only "
                f"{group_names(floor.approvers)} approve them. A tenant can only narrow the "
                "install's routing: name only these groups, or fewer."
            )
            first = next((e for e in tenant[:index] if _covered_by(e, mine, floor)), None)
            unsure = _both_wildcards(mine, floor)
            if first is not None:
                text += (
                    f" `{_place(first)}` comes first and fits each of these changes, so "
                    f"`{_place(mine)}` does not count for them."
                )
            elif unsure:
                text += (
                    " MEHO cannot tell whether the two connector patterns fit the same "
                    "connector. Where they do, the entry does not count where it is wider."
                )
            found.append(Wider(index=index, text=text, sure=first is None and not unsure))
    return found


def wider_entries(tenant: Sequence[RouteEntry], install: Sequence[RouteEntry]) -> list[str]:
    """One sentence per tenant entry and install entry where the tenant's is surely wider."""
    return [found.text for found in wider_findings(tenant, install) if found.sure]


def _tenant_findings(
    document: Mapping[str, Any], install_document: Mapping[str, Any] | None
) -> list[Wider]:
    if not routing_enabled():
        return []
    install = _routes(install_document, "install", install=True)
    if not install:
        return []
    return wider_findings(_routes(document, "tenant", install=False), install)


def tenant_floor_mistakes(
    document: Mapping[str, Any], install_document: Mapping[str, Any] | None
) -> list[str]:
    """The import mistakes of a tenant file whose routing is wider than the install's.

    Empty while the ``POLICY_APPROVER_ROUTING`` switch is off. Raises
    :class:`ValueError` when the install's stored routing is not valid.
    """
    return [found.text for found in _tenant_findings(document, install_document) if found.sure]


def tenant_floor_warnings(
    document: Mapping[str, Any], install_document: Mapping[str, Any] | None
) -> list[str]:
    """The import warnings of a tenant file: wider entries that may never count.

    See :class:`Wider`. Empty while the ``POLICY_APPROVER_ROUTING`` switch
    is off. Raises :class:`ValueError` when the install's stored routing is
    not valid.
    """
    return [found.text for found in _tenant_findings(document, install_document) if not found.sure]


def tenant_wider_notes(
    document: Mapping[str, Any], install_document: Mapping[str, Any] | None
) -> dict[int, list[str]]:
    """Each finding of :class:`Wider` for a tenant's stored routing, by entry place.

    ``meho policy export`` writes them above the entries. Empty while the
    switch is off, and when a stored routing is not valid.
    """
    try:
        findings = _tenant_findings(document, install_document)
    except ValueError:
        return {}
    notes: dict[int, list[str]] = {}
    for found in findings:
        notes.setdefault(found.index, []).append(found.text)
    return notes


async def tenants_wider(
    session: AsyncSession, install_document: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """The tenants whose stored routing is wider than *install_document*.

    One item per tenant: ``tenant_id`` and ``entries`` (one sentence each).
    It lists every finding of :func:`wider_findings`, also the ones a
    tenant import only warns about: the install's change can still take
    approval rights from a group of the tenant. Empty while the switch is
    off. A tenant whose stored routing is not valid gets one sentence that
    says so.
    """
    if not routing_enabled():
        return []
    install = _routes(install_document, "install", install=True)
    if not install:
        return []
    rows = await session.execute(select(TenantPolicy).where(TenantPolicy.tenant_id.is_not(None)))
    found: list[dict[str, Any]] = []
    for row in rows.scalars():
        try:
            tenant = _routes(row.document, "tenant", install=False)
            sentences = [found.text for found in wider_findings(tenant, install)]
        except ValueError as exc:
            sentences = [f"The stored routing is not valid: {exc}"]
        if sentences:
            found.append({"tenant_id": str(row.tenant_id), "entries": sentences})
    found.sort(key=lambda item: item["tenant_id"])
    return found


def floor_warnings(found: Sequence[Mapping[str, Any]]) -> list[str]:
    """One import warning per entry of :func:`tenants_wider`.

    Those entries stay stored, but where they are wider they do not count.
    """
    return [
        f"Tenant {item['tenant_id']}: {sentence} Where it is wider, the entry does not count."
        for item in found
        for sentence in item["entries"]
    ]


def _level(request: ApprovalRequest) -> str | None:
    level = (request.proposed_effect or {}).get("safety_level")
    return level if isinstance(level, str) and level else None


def _install_category(request: ApprovalRequest) -> str | None:
    """The install's category of *request*, as the floor uses it.

    The stored ``approver_install_category``. A request with stored group
    kinds but no stored install category started to wait while
    ``POLICY_CLASSIFICATION`` was off, or was written by a build that did
    not store it yet. With ``POLICY_CLASSIFICATION`` on, its category is
    then not known (the empty text). With the switch off, no change has a
    category, so ``None`` stays: an entry that matches on a category fits
    nothing, as when the request started to wait.
    """
    stored = request.approver_install_category
    if stored is None and classification_enabled():
        return ""
    return stored


def _route_install(entries: Sequence[RouteEntry], request: ApprovalRequest) -> ApproverSet:
    """Route *request* with the install's *entries* and the install's category it stored.

    The category is the one the install's policy alone gave the change
    (:func:`_install_category`), not the tenant's. The empty text means
    MEHO does not know it: the install's classification entries gave the
    change two categories, or the request started to wait before MEHO
    stored categories (migration ``0173``). Then :func:`route` lets no group
    approve it when an install entry that matches on a category could fit
    it (rule :data:`CATEGORY_NOT_STORED`).
    """
    return route(
        entries,
        category=None,
        install_category=_install_category(request),
        connector_id=request.connector_id,
        level=_level(request),
        op_id=request.op_id,
    )


def _not_routed(request: ApprovalRequest) -> bool:
    """True for a request the floor does not check: a change of rights, or no stored kinds."""
    from meho_backplane.operations.grant_approval import is_rights_change

    return is_rights_change(request) or request.approver_roles is None


_CATEGORY_NOT_KNOWN_REFUSAL: Final[str] = (
    "MEHO does not know the category the install's policy gives this change: the "
    "install's classification gives it two categories, or the request started to wait "
    "before MEHO stored categories. The install's routing matches on a category, so MEHO "
    "cannot check who may approve it. Reject it. When the install's classification gives "
    "the operation two categories, give it one in the install's policy file, then start "
    "the call again."
)


@dataclass(frozen=True)
class InstallFloor:
    """The install's stored routing as it is now, loaded once for many requests.

    ``invalid`` is the reason when the stored routing is not valid: then
    every routed request is refused (fail closed). The approval click, the
    approval card and the push recipients use the same answer
    (:meth:`refusal`).
    """

    entries: tuple[RouteEntry, ...] = ()
    invalid: str | None = None

    def check(self, request: ApprovalRequest) -> ApproverSet | None:
        """Route *request* with the install's entries; ``None`` when the floor does not apply.

        Raises :class:`ValueError` when the stored routing is not valid.
        """
        if _not_routed(request):
            return None
        if self.invalid is not None:
            raise ValueError(self.invalid)
        return _route_install(self.entries, request)

    def refusal(self, operator: Operator, request: ApprovalRequest) -> str | None:
        """One sentence when the install's routing does not let *operator* approve *request*."""
        try:
            found = self.check(request)
        except ValueError as exc:
            return (
                "The install's stored routing is not valid, so MEHO cannot check who may "
                f"approve: {exc} Import a fixed policy file."
            )
        if found is None or found.rule == DEFAULT_RULE:
            return None
        if found.rule == CATEGORY_NOT_STORED:
            return _CATEGORY_NOT_KNOWN_REFUSAL
        kind = kind_of(operator)
        if kind in found.kinds:
            return None
        if not found.kinds:
            return (
                f"The install's routing (`{found.rule}`) lets no group approve this change, so "
                "it waits until it expires."
            )
        return (
            f"The install's routing (`{found.rule}`) lets only {group_names(found.kinds)} "
            f"approve this change. You are in MEHO-{kind}. A tenant's routing can only narrow it."
        )


async def load_install_floor(session: AsyncSession) -> InstallFloor | None:
    """The install's stored routing as it is now; ``None`` while the switch is off."""
    if not routing_enabled():
        return None
    install_row, _ = await _documents(session, None)
    try:
        entries = _routes(
            install_row.document if install_row is not None else None, "install", install=True
        )
    except ValueError as exc:
        return InstallFloor(invalid=str(exc))
    return InstallFloor(entries=tuple(entries))


async def current_install_floor() -> InstallFloor | None:
    """:func:`load_install_floor` in a session of its own; no query while the switch is off."""
    if not routing_enabled():
        return None
    from meho_backplane.db.engine import get_sessionmaker

    async with get_sessionmaker()() as session:
        return await load_install_floor(session)


async def install_floor(session: AsyncSession, request: ApprovalRequest) -> ApproverSet | None:
    """Route *request* again with the install's stored routing as it is now.

    Uses the request's risk level and the category the install's policy
    alone gave it (:func:`_install_category`). ``None`` while the
    switch is off, for a change of rights (it is not routed), and for a
    request without stored group kinds: it started to wait while the
    switch was off, so it keeps today's rule until it is decided or
    expires. The rule is ``default`` when no install entry fits, and
    :data:`CATEGORY_NOT_STORED` when MEHO cannot check it. Raises
    :class:`ValueError` when the install's stored routing is not valid.
    """
    if not routing_enabled() or _not_routed(request):
        return None
    floor = await load_install_floor(session)
    return floor.check(request) if floor is not None else None


async def install_floor_refusal(
    session: AsyncSession, operator: Operator, request: ApprovalRequest
) -> str | None:
    """One sentence when the install's current routing does not let *operator* approve.

    See :meth:`InstallFloor.refusal`. ``None`` while the switch is off, for
    a change of rights, for a request without stored group kinds, and when
    no install entry fits. A stored install routing that is not valid
    refuses (fail closed), and so does a request whose category MEHO
    cannot know.
    """
    if not routing_enabled() or _not_routed(request):
        return None
    floor = await load_install_floor(session)
    return floor.refusal(operator, request) if floor is not None else None
