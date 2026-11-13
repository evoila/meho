# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approver routing: who may approve a change that waits.

The ``routing`` section of the policy file maps a category, a connector or
a risk level to login group kinds (``Readers``, ``Operators``,
``SA-Operators``, ``Admins``, ``Approvers``):

.. code-block:: yaml

    routing:
      - match: {category: network}
        approvers: [SA-Operators, Admins]

When a request starts to wait, :func:`approver_set_for` works out the
group kinds that may approve it and the request stores them
(``approval_request.approver_roles`` and ``approver_rule``). At the click,
:func:`routing_refusal` checks the person against the stored kinds. Today's
checks still run too: a person only, the fresh sign-in, the preview
binding, the role floor, the rules on own changes.

The rules:

* The tenant's entries come first, then the install's. The first entry
  whose ``match`` fits wins. All fields of a ``match`` must fit.
* Routing only narrows. The stored kinds are the entry's kinds that may
  approve the change today (:func:`today_kinds`). A file never lets a
  group approve what it may not approve today.
* No entry fits: today's rule. Everyone who may approve today may approve
  (``approver_rule`` ``"default"``).
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
from meho_backplane.policy.classification import RISK_LEVELS, Taxonomy
from meho_backplane.policy.document import GROUP_KINDS
from meho_backplane.policy.evaluate import approver_groups
from meho_backplane.policy.store import _documents
from meho_backplane.settings import get_settings

__all__ = [
    "DEFAULT_RULE",
    "ApproverSet",
    "RouteEntry",
    "approver_set_for",
    "group_names",
    "kind_of",
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


def routing_enabled() -> bool:
    """True when the ``POLICY_APPROVER_ROUTING`` switch is on."""
    return get_settings().policy_approver_routing


@dataclass(frozen=True)
class RouteEntry:
    """One ``routing`` entry, with where it came from (``ref``)."""

    ref: str
    approvers: tuple[str, ...]
    category: str | None = None
    connector: str | None = None
    risk: str | None = None

    def matches(self, *, category: str | None, connector_id: str, level: str | None) -> bool:
        """True when every field of the entry's ``match`` fits the change."""
        if self.category is not None and self.category != category:
            return False
        if self.connector is not None and not fnmatchcase(connector_id, self.connector):
            return False
        return self.risk is None or self.risk == level


@dataclass(frozen=True)
class ApproverSet:
    """The group kinds that may approve a change, and the rule that set them."""

    kinds: tuple[str, ...]
    rule: str


def _ordered(kinds: Sequence[str] | set[str] | frozenset[str]) -> tuple[str, ...]:
    """*kinds* in the order of :data:`GROUP_KINDS`, without unknown names."""
    return tuple(kind for kind in GROUP_KINDS if kind in kinds)


def today_kinds(level: str | None, op_id: str) -> tuple[str, ...]:
    """The group kinds that may approve another person's change today.

    The same rule as :func:`~meho_backplane.policy.evaluate.approver_groups`:
    a ``dangerous`` or ``destructive`` change only SA-Operators and Admins;
    a lower one also Operators and Approvers; an operation with the
    ``tenant_admin`` role floor only Admins. Readers never approve.
    """
    groups = approver_groups(level or "", op_id)
    return _ordered({group.removeprefix("MEHO-") for group in groups})


def kind_of(operator: Operator) -> str:
    """The login group kind of *operator* in its current tenant.

    Admins have the ``tenant_admin`` role (also a platform admin who works
    in a tenant without a role group there). SA-Operators are operators
    who may approve their own changes. Approvers are ``read_only`` with the
    approver flag.
    """
    if operator.tenant_role == TenantRole.TENANT_ADMIN:
        return RoleGroup.ADMINS.value
    if operator.tenant_role == TenantRole.OPERATOR:
        return (RoleGroup.SA_OPERATORS if operator.self_approver else RoleGroup.OPERATORS).value
    return (RoleGroup.APPROVERS if operator.approver else RoleGroup.READERS).value


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _routes(document: Mapping[str, Any] | None, scope: str) -> list[RouteEntry]:
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
            )
        )
    return entries


def routes_from_documents(
    install: Mapping[str, Any] | None, tenant: Mapping[str, Any] | None
) -> list[RouteEntry]:
    """The routing entries for a tenant: its own first, then the install's.

    Raises :class:`ValueError` when a stored document holds a bad entry.
    The import refuses such a file, so this means the stored row was
    changed outside MEHO.
    """
    return [*_routes(tenant, "tenant"), *_routes(install, "install")]


def route(
    entries: Sequence[RouteEntry],
    *,
    category: str | None,
    connector_id: str,
    level: str | None,
    op_id: str,
) -> ApproverSet:
    """Return the group kinds that may approve the change, and the rule.

    The first entry that fits wins; its kinds are cut to the kinds that
    may approve the change today. No entry fits: today's kinds.
    """
    allowed = today_kinds(level, op_id)
    for entry in entries:
        if entry.matches(category=category, connector_id=connector_id, level=level):
            return ApproverSet(
                kinds=tuple(k for k in allowed if k in entry.approvers), rule=entry.ref
            )
    return ApproverSet(kinds=allowed, rule=DEFAULT_RULE)


async def approver_set_for(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    connector_id: str,
    op_id: str,
    level: str | None,
    category: str | None,
) -> ApproverSet:
    """Load the stored policies of *tenant_id* and route the change."""
    install, tenant = await _documents(session, tenant_id)
    entries = routes_from_documents(
        install.document if install is not None else None,
        tenant.document if tenant is not None else None,
    )
    return route(entries, category=category, connector_id=connector_id, level=level, op_id=op_id)


async def stamp_approver_set(
    session: AsyncSession, request: ApprovalRequest, *, category: str | None
) -> None:
    """Store on *request* the group kinds that may approve it, and the rule.

    Does nothing while the switch is off and for a change of rights (only
    another admin approves it, as today). The risk level is the one the
    dispatcher stamped on ``proposed_effect``. *category* is the one the
    classification gave the looked-up row (``policy_category``, set while
    the ``POLICY_CLASSIFICATION`` switch is on); a request without a
    category matches only entries without one.
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
    )
    request.approver_roles = list(found.kinds)
    request.approver_rule = found.rule


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
    """``MEHO-SA-Operators or MEHO-Admins``: the kinds as group names."""
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
    kind = kind_of(operator)
    if kind in kinds:
        return None
    return (
        f"Only {group_names(kinds)} may approve this change (routing rule `{rule}`). "
        f"You are in MEHO-{kind}."
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
        if not any(kind in today_kinds(level, "") for level in levels)
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
        if not set(approvers) & set(today_kinds(level, ""))
    )
    return mistakes


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
