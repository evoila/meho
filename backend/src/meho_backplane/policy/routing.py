# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approver routing: who may approve a change that waits.

The ``routing`` section of the policy file maps a category, a connector or
a risk level to login group kinds (``Readers``, ``Operators``,
``Admins``):

.. code-block:: yaml

    routing:
      - match: {category: network}
        approvers: [Operators, Admins]

When a request starts to wait, :func:`approver_set_for` works out the
group kinds that may approve it and the request stores them
(``approval_request.approver_roles`` and ``approver_rule``). At the click,
:func:`routing_refusal` checks the person against the stored kinds. Today's
checks still run too: a person only, the fresh sign-in, the preview
binding, the role floor, the rules on own changes.

The rules:

* The install's routing is a floor. The first install entry whose
  ``match`` fits counts first, then the first tenant entry that fits. All
  fields of a ``match`` must fit. A person must be in the kinds of both,
  so a tenant entry can only narrow the install's entry, never widen it
  (:mod:`meho_backplane.policy.routing_floor`).
* An install entry matches on the category the install's policy alone
  gives the change (``install_category``), a tenant entry on the category
  the classification gives it. So a tenant that moves an operation to
  another category for its own rules does not take it away from the
  install's routing.
* The install's category is not known (the empty text) when the install's
  classification entries give the change two categories, and on a request
  that started to wait before MEHO stored it. Then no group may approve
  the change when an install entry that matches on a category could fit
  it (``approver_rule`` ``"category_not_stored"``).
* Routing only narrows. The stored kinds are the kinds of the fitting
  entries that may approve the change today (:func:`today_kinds`). A file
  never lets a group approve what it may not approve today.
* ``approver_rule`` names every entry that fits, the install's first, for
  example ``install:routing[0], tenant:routing[1]``.
* No entry fits: today's rule. Everyone who may approve today may approve
  (``approver_rule`` ``"default"``).
* A file may still name the old kinds ``SA-Operators`` and ``Approvers``.
  An old kind matches only the members of that old login group, by the
  group in the token (:attr:`~meho_backplane.auth.operator.Operator.old_group`),
  never the whole new role. It is narrower than the kind it is read as
  (:func:`narrow`): ``SA-Operators`` may approve only what an Operator may
  approve, and ``Approvers`` (read as Readers) never approves. A request
  keeps the kinds it was stored with, read by the same rule.
* A change of rights is not routed. Only another admin approves it, as
  today.
* The stored value holds group kinds, never people. The requester is not
  asked; whether they may approve their own change is today's rule.

Everything here runs only while the ``POLICY_APPROVER_ROUTING`` switch is
on. A request stored without kinds (made while the switch was off, or
before it existed) keeps today's rule. With the switch off, stored kinds
are not checked either.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, RoleGroup, TenantRole
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.features import CATEGORY_WITHOUT_CLASSIFICATION
from meho_backplane.policy.classification import RISK_LEVELS, Taxonomy
from meho_backplane.policy.document import ACCEPTED_KINDS, read_kind
from meho_backplane.policy.evaluate import approver_groups
from meho_backplane.policy.store import _documents
from meho_backplane.settings import get_settings

__all__ = [
    "CATEGORY_NOT_STORED",
    "DEFAULT_RULE",
    "ROUTING_INVALID_RULE",
    "ApproverSet",
    "RouteEntry",
    "approver_set_for",
    "category_routing_warnings",
    "covers",
    "group_names",
    "in_kinds",
    "kind_of",
    "narrow",
    "route",
    "routes_from_documents",
    "routing_enabled",
    "routing_mistakes",
    "routing_refusal",
    "stamp_approver_set",
    "stored_kinds",
    "today_kinds",
    "who_can_approve",
]

#: ``approver_rule`` when no routing entry fits: today's rule.
DEFAULT_RULE: Final[str] = "default"

#: ``approver_rule`` when the install's category of the change is not known
#: (the empty text) and an install entry that matches on a category could
#: fit it: no group may approve the change.
CATEGORY_NOT_STORED: Final[str] = "category_not_stored"

#: ``approver_rule`` on the audit row of a park refused because the stored
#: routing is not valid.
ROUTING_INVALID_RULE: Final[str] = "invalid"


def routing_enabled() -> bool:
    """True when the ``POLICY_APPROVER_ROUTING`` switch is on."""
    return get_settings().policy_approver_routing


@dataclass(frozen=True)
class RouteEntry:
    """One ``routing`` entry, with where it came from (``ref``).

    ``install`` is true for an entry of the install's policy: the floor.
    """

    ref: str
    approvers: tuple[str, ...]
    category: str | None = None
    connector: str | None = None
    risk: str | None = None
    install: bool = False

    def matches(self, *, category: str | None, connector_id: str, level: str | None) -> bool:
        """True when every field of the entry's ``match`` fits the change."""
        if self.category is not None and self.category != category:
            return False
        if self.connector is not None and not fnmatchcase(connector_id, self.connector):
            return False
        return self.risk is None or self.risk == level


@dataclass(frozen=True)
class ApproverSet:
    """The group kinds that may approve a change, and the rule that set them.

    ``floor`` is the install's entry that fits, or ``None``.
    """

    kinds: tuple[str, ...]
    rule: str
    floor: str | None = None


def _ordered(kinds: Sequence[str] | set[str] | frozenset[str]) -> tuple[str, ...]:
    """*kinds* in the order of :data:`ACCEPTED_KINDS`, without unknown names.

    An old kind (``SA-Operators``, ``Approvers``) stays as it is.
    """
    return tuple(kind for kind in ACCEPTED_KINDS if kind in kinds)


def covers(kinds: Sequence[str], kind: str) -> bool:
    """True when every member of *kind* is in one of *kinds*.

    *kind* itself, or for an old kind the kind it is read as: every member
    of ``SA-Operators`` is an Operator. A new kind is never covered by an
    old one.
    """
    return kind in kinds or read_kind(kind) in kinds


def narrow(first: Sequence[str], second: Sequence[str]) -> tuple[str, ...]:
    """The kinds whose members are in both *first* and *second*.

    ``[Operators, Admins]`` and ``[SA-Operators, Admins]`` give
    ``[SA-Operators, Admins]``: the old kind is the narrower one.
    """
    both = {kind for kind in (*first, *second) if covers(first, kind) and covers(second, kind)}
    return _ordered(both)


def in_kinds(operator: Operator, kinds: Sequence[str]) -> bool:
    """True when *operator* is a member of one of *kinds*.

    A new kind matches by :func:`kind_of`. An old kind matches only a
    person whose token names that old login group for the current tenant
    (:attr:`~meho_backplane.auth.operator.Operator.old_group`), and whose
    kind is the kind the old one is read as.
    """
    kind = kind_of(operator)
    if kind in kinds:
        return True
    old = operator.old_group
    return old is not None and old.value in kinds and read_kind(old.value) == kind


def today_kinds(level: str | None, op_id: str) -> tuple[str, ...]:
    """The group kinds that may approve another person's change today.

    The same rule as :func:`~meho_backplane.policy.evaluate.approver_groups`:
    a ``dangerous`` or ``destructive`` change only Admins;
    a lower one also Operators; an operation with the
    ``tenant_admin`` role floor only Admins. Readers never approve.
    """
    groups = approver_groups(level or "", op_id)
    return _ordered({group.removeprefix("MEHO-") for group in groups})


def kind_of(operator: Operator) -> str:
    """The login group kind of *operator* in its current tenant.

    Admins have the ``tenant_admin`` role (also a platform admin who works
    in a tenant without a role group there). Operators have the ``operator``
    role, or the ``approver`` flag: such a person approves what an
    Operator approves. No role gives the flag today; roles that customers
    define come later. Everyone else is a Reader.
    """
    if operator.tenant_role == TenantRole.TENANT_ADMIN:
        return RoleGroup.ADMINS.value
    if operator.tenant_role == TenantRole.OPERATOR or operator.approver:
        return RoleGroup.OPERATORS.value
    return RoleGroup.READERS.value


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _routes(
    document: Mapping[str, Any] | None, scope: str, *, install: bool | None = None
) -> list[RouteEntry]:
    """The ``routing`` entries of *document*; *scope* starts each ``ref``.

    *install* marks the entries as the install's (the floor). By default
    they are when *scope* is ``install``.
    """
    is_install = scope == "install" if install is None else install
    raw_routes = (document or {}).get("routing") or []
    if not isinstance(raw_routes, list):
        raise ValueError(f"{scope}: `routing` must be a list.")
    entries: list[RouteEntry] = []
    for index, raw in enumerate(raw_routes):
        ref = f"{scope}:routing[{index}]"
        match = raw.get("match") if isinstance(raw, Mapping) else None
        approvers = raw.get("approvers") if isinstance(raw, Mapping) else None
        if not isinstance(match, Mapping) or not isinstance(approvers, list):
            raise ValueError(f"{ref}: an entry needs a `match` map and an `approvers` list.")
        entries.append(
            RouteEntry(
                ref=ref,
                approvers=_ordered({str(kind) for kind in approvers}),
                category=_text(match.get("category")),
                connector=_text(match.get("connector")),
                risk=_text(match.get("risk")),
                install=is_install,
            )
        )
    return entries


def routes_from_documents(
    install: Mapping[str, Any] | None, tenant: Mapping[str, Any] | None
) -> list[RouteEntry]:
    """The routing entries for a tenant: its own, then the install's (the floor).

    Raises :class:`ValueError` when a stored document holds a bad entry.
    The import refuses such a file, so this means the stored row was
    changed outside MEHO.
    """
    return [*_routes(tenant, "tenant"), *_routes(install, "install")]


def route(
    entries: Sequence[RouteEntry],
    *,
    category: str | None,
    install_category: str | None,
    connector_id: str,
    level: str | None,
    op_id: str,
) -> ApproverSet:
    """Return the group kinds that may approve the change, and the rule.

    The first install entry that fits counts, then the first tenant entry
    that fits. The kinds are the kinds of both entries that may approve
    the change today: a tenant entry only narrows the install's. An
    install entry matches on *install_category*, a tenant entry on
    *category*. No entry fits: today's kinds. An *install_category* of
    the empty text is not known: when an install entry that matches on a
    category could fit, no kind may approve (:data:`CATEGORY_NOT_STORED`).
    """
    if install_category == "" and any(
        e.install
        and e.category is not None
        and e.matches(category=e.category, connector_id=connector_id, level=level)
        for e in entries
    ):
        return ApproverSet(kinds=(), rule=CATEGORY_NOT_STORED)
    kinds = today_kinds(level, op_id)
    fitting: list[RouteEntry] = []
    for install, fit_category in ((True, install_category), (False, category)):
        entry = next(
            (
                e
                for e in entries
                if e.install is install
                and e.matches(category=fit_category, connector_id=connector_id, level=level)
            ),
            None,
        )
        if entry is not None:
            fitting.append(entry)
            kinds = narrow(kinds, entry.approvers)
    if not fitting:
        return ApproverSet(kinds=kinds, rule=DEFAULT_RULE)
    floor = fitting[0].ref if fitting[0].install else None
    return ApproverSet(kinds=kinds, rule=", ".join(e.ref for e in fitting), floor=floor)


async def approver_set_for(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    connector_id: str,
    op_id: str,
    level: str | None,
    category: str | None,
    install_category: str | None,
) -> ApproverSet:
    """Load the stored policies of *tenant_id* and route the change."""
    install, tenant = await _documents(session, tenant_id)
    entries = routes_from_documents(
        install.document if install is not None else None,
        tenant.document if tenant is not None else None,
    )
    return route(
        entries,
        category=category,
        install_category=install_category,
        connector_id=connector_id,
        level=level,
        op_id=op_id,
    )


async def stamp_approver_set(
    session: AsyncSession,
    request: ApprovalRequest,
    *,
    category: str | None,
    install_category: str | None,
) -> None:
    """Store on *request* the group kinds that may approve it, and the rule.

    Does nothing while the switch is off and for a change of rights (only
    another admin approves it, as today). The risk level is the one the
    dispatcher stamped on ``proposed_effect``. *category* is the one the
    classification gave the looked-up row (``policy_category``, set while
    the ``POLICY_CLASSIFICATION`` switch is on); a request without a
    category matches only entries without one. *install_category* is the
    category the install's policy alone gives it
    (``policy_install_category``); the install's entries match on it.
    """
    if not routing_enabled() or request.rights_change:
        return
    effect = request.proposed_effect or {}
    found = await approver_set_for(
        session,
        tenant_id=request.tenant_id,
        connector_id=request.connector_id,
        op_id=request.op_id,
        level=_text(effect.get("safety_level")),
        category=category,
        install_category=install_category,
    )
    request.approver_roles = list(found.kinds)
    request.approver_rule = found.rule
    request.approver_category = category
    request.approver_install_category = install_category


def stored_kinds(value: Any) -> tuple[str, ...] | None:
    """The group kinds stored on a request, or ``None`` when it has none.

    A stored value that is not a list of text counts as no kind at all, so
    nobody may approve (fail closed).
    """
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(kind, str) for kind in value):
        return ()
    return _ordered(set(value))


def group_names(kinds: Sequence[str]) -> str:
    """``MEHO-Operators or MEHO-Admins``: the kinds as group names."""
    names = [f"MEHO-{kind}" for kind in kinds]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + f" or {names[-1]}"


def who_can_approve(approver_roles: Any) -> str | None:
    """The "Who can approve" text of the web interface, or ``None``.

    ``None`` while the switch is off, or when the request has no stored
    group kinds (today's rule).
    """
    if not routing_enabled():
        return None
    kinds = stored_kinds(approver_roles)
    if kinds is None:
        return None
    if not kinds:
        return "Nobody: no group may approve this change, so it waits until it expires."
    return f"Members of {group_names(kinds)}."


def routing_refusal(
    operator: Operator, approver_roles: Any, approver_rule: str | None
) -> str | None:
    """One sentence when *operator* is not in the stored kinds, else ``None``.

    ``None`` too while the switch is off, or when the request has no
    stored kinds (today's rule).
    """
    if not routing_enabled():
        return None
    kinds = stored_kinds(approver_roles)
    if kinds is None:
        return None
    rule = approver_rule or DEFAULT_RULE
    if not kinds:
        return (
            f"No group may approve this change (routing rule `{rule}`), so it waits until "
            "it expires."
        )
    if in_kinds(operator, kinds):
        return None
    return (
        f"Only {group_names(kinds)} may approve this change (routing rule `{rule}`). "
        f"You are in MEHO-{kind_of(operator)}."
    )


# ---------------------------------------------------------------------------
# Import checks
# ---------------------------------------------------------------------------


def _levels(risk: str | None) -> tuple[str, ...]:
    return (risk,) if risk is not None else RISK_LEVELS


def _entry_mistakes(index: int, raw: Mapping[str, Any], taxonomy: Taxonomy) -> list[str]:
    match = raw.get("match") or {}
    category, risk = match.get("category"), match.get("risk")
    mistakes: list[str] = []
    if category is not None and category not in taxonomy.categories:
        known = ", ".join(taxonomy.categories)
        mistakes.append(
            f"`routing[{index}].match.category` `{category}` is not in the taxonomy ({known})."
        )
    if risk is not None and risk not in RISK_LEVELS:
        mistakes.append(
            f"`routing[{index}].match.risk` `{risk}` is not a risk level; use "
            "safe, caution, dangerous or destructive."
        )
    return mistakes


def _approve_mistakes(index: int, raw: Mapping[str, Any]) -> list[str]:
    match = raw.get("match") or {}
    levels = _levels(match.get("risk"))
    approvers = [str(kind) for kind in raw.get("approvers") or []]
    mistakes = [
        f"`routing[{index}]` asks `{kind}`, but `{kind}` has no `approve` rule for these changes."
        for kind in approvers
        if not any(covers(today_kinds(level, ""), kind) for level in levels)
    ]
    where = (
        f"in category {match['category']}"
        if match.get("category") is not None
        else f"matched by `routing[{index}]`"
    )
    mistakes.extend(
        f"A {level} change {where} can wait, but no role may approve it. It would wait "
        "until it expires."
        for level in levels
        if not any(covers(today_kinds(level, ""), kind) for kind in approvers)
    )
    return mistakes


def category_routing_warnings(raw_routes: Sequence[Mapping[str, Any]]) -> list[str]:
    """One warning per ``routing`` entry that matches on a category no change has.

    A change has a category only while the ``POLICY_CLASSIFICATION``
    switch is on. With routing on and the classification off, such an
    entry fits nothing. Empty in every other case.
    """
    settings = get_settings()
    if not settings.policy_approver_routing or settings.policy_classification:
        return []
    return [
        f"`routing[{index}]` matches on `category`: {CATEGORY_WITHOUT_CLASSIFICATION}"
        for index, raw in enumerate(raw_routes)
        if (raw.get("match") or {}).get("category") is not None
    ]


def routing_mistakes(raw_routes: Sequence[Mapping[str, Any]], taxonomy: Taxonomy) -> list[str]:
    """Check the ``routing`` entries of a valid file's stored form.

    An unknown category or risk level, a group kind that may approve none
    of the changes the entry routes, and a risk level that nobody of the
    entry may approve: each is one sentence.
    """
    mistakes: list[str] = []
    for index, raw in enumerate(raw_routes):
        found = _entry_mistakes(index, raw, taxonomy)
        mistakes.extend(found or _approve_mistakes(index, raw))
    return mistakes
