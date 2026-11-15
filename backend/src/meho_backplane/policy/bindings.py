# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bindings: which login group is which team.

The ``bindings`` section of a tenant's policy file gives a login group one
of today's five group kinds (``Readers``, ``Operators``, ``SA-Operators``,
``Admins``, ``Approvers``) and a set of categories:

.. code-block:: yaml

    bindings:
      - match: {group: /MEHO-Operators/Network}
        kind: SA-Operators
        categories: [network]
      - match: {attribute: tenant-a-storage}
        kind: Operators
        categories: [storage]

A binding matches the group exactly:

* ``group`` is the group's full path. It matches an entry of the token's
  ``groups`` claim that is the same text, upper and lower case included.
  A short group name (an entry without a leading ``/``) never matches.
* ``attribute`` is a value of the group attribute ``meho_group``. The
  Keycloak mapper copies the values of all the person's groups into the
  token's ``meho_groups`` claim. The match is exact.

The rules (:func:`bind`):

* MEHO matches the bindings after it chose the current tenant, with the
  bindings of that tenant. The token parser reads no database.
* A login gets one kind per tenant, as today. When the bindings that
  match give two kinds, the login is refused with 401
  ``multiple_bound_kinds``. This counts every binding that matches, also
  one of another base level, so groups of two base levels never pass.
* One base level per login stays. Vault reads the ``tenant_role`` claim
  of the token, so a binding never changes the role. When the one kind
  that matches has another role than the token, it is left out and
  logged.
* The kind sets the ``approver`` and ``self_approver`` flags of the login
  (:func:`~meho_backplane.auth.operator.group_grant`). The categories of
  all matching bindings add up. ``"*"`` means every category.
* No binding matches: the login keeps the kind MEHO works out today.
* A tenant whose stored policy has no bindings (or that has no stored
  policy) uses the shipped bindings
  (:func:`shipped_bindings`): the groups of the ``JWT_ROLE_GROUPS`` and
  ``JWT_SELF_APPROVER_GROUP`` settings, as top-level full paths, with
  every category. They count only for a login without a tenant list
  (``meho_tenants``). The ``groups`` claim is not per tenant, so for a
  login with a list the list entry of the tenant decides, as with the
  switch off. A shipped operator group next to the shipped self-approval
  group gives ``SA-Operators``, as today.
* A ``groups`` claim that is not a list of texts matches no binding.

The import refuses a binding of an agent or a service to a kind that may
approve, any binding of a runner, an unknown category, and bindings in the
install's file (:func:`binding_mistakes`).

Everything here runs only while the ``POLICY_APPROVER_ROUTING`` switch is
on. With the switch off, the login path is today's path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import structlog
from sqlalchemy import select

from meho_backplane.auth.operator import (
    Operator,
    PrincipalKind,
    RoleGroup,
    TenantRole,
    group_grant,
    is_human_principal,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import TenantPolicy
from meho_backplane.policy.classification import Scope, Taxonomy
from meho_backplane.policy.document import ALL_CATEGORIES, GROUP_KINDS
from meho_backplane.settings import Settings, get_settings

__all__ = [
    "GROUPS_CLAIM",
    "GROUP_ATTRIBUTE_CLAIM",
    "BindingEntry",
    "BoundKindConflictError",
    "bind",
    "bind_current_tenant",
    "binding_mistakes",
    "bindings_from_document",
    "routing_enabled",
    "shipped_binding_documents",
    "shipped_bindings",
    "token_groups",
]

#: The claim of the Keycloak group-membership mapper.
GROUPS_CLAIM: Final[str] = "groups"

#: The claim that lists the ``meho_group`` attribute of the person's groups.
GROUP_ATTRIBUTE_CLAIM: Final[str] = "meho_groups"

_PRINCIPAL_NAMES: Final[dict[PrincipalKind, str]] = {
    PrincipalKind.USER: "person",
    PrincipalKind.AGENT: "agent",
    PrincipalKind.SERVICE: "service",
    PrincipalKind.RUNNER: "runner",
}

#: The kinds that may approve a change of another person today. Only
#: Readers never approve.
_APPROVING_KINDS: Final[frozenset[str]] = frozenset(GROUP_KINDS) - {RoleGroup.READERS.value}

#: Where a shipped binding came from (:func:`shipped_bindings`).
_SHIPPED: Final[str] = "shipped:"

#: An operator group and the self-approval group of the shipped bindings.
_OPERATOR_AND_SELF_APPROVAL: Final[list[RoleGroup]] = [
    RoleGroup.OPERATORS,
    RoleGroup.SA_OPERATORS,
]

#: The kind with the fewest rights for each base level.
_LOWEST_KIND: Final[dict[TenantRole, RoleGroup]] = {
    TenantRole.READ_ONLY: RoleGroup.READERS,
    TenantRole.OPERATOR: RoleGroup.OPERATORS,
    TenantRole.TENANT_ADMIN: RoleGroup.ADMINS,
}


class BoundKindConflictError(ValueError):
    """The bindings that match a login give it more than one group kind."""

    def __init__(self, kinds: Sequence[RoleGroup]) -> None:
        self.kinds = tuple(kinds)
        names = ", ".join(kind.value for kind in kinds)
        super().__init__(
            f"multiple_bound_kinds: this login matches bindings with {len(kinds)} group "
            f"kinds ({names}) in this tenant. A login gets one kind per tenant. Ask your "
            "admin to change the bindings of the policy file or your groups."
        )


def routing_enabled() -> bool:
    """True when the ``POLICY_APPROVER_ROUTING`` switch is on."""
    return get_settings().policy_approver_routing


@dataclass(frozen=True)
class BindingEntry:
    """One binding, with where it came from (``ref``)."""

    ref: str
    kind: RoleGroup
    categories: frozenset[str]
    group: str | None = None
    attribute: str | None = None
    principal: str = "person"

    def matches(self, principal: str, paths: frozenset[str], attributes: frozenset[str]) -> bool:
        """True when the login is of the binding's kind and has its group."""
        if principal != self.principal:
            return False
        if self.group is not None:
            return self.group in paths
        return self.attribute is not None and self.attribute in attributes


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _entry(raw: Any, ref: str) -> BindingEntry:
    match = raw.get("match") if isinstance(raw, Mapping) else None
    categories = raw.get("categories") if isinstance(raw, Mapping) else None
    if not isinstance(match, Mapping) or not isinstance(categories, list):
        raise ValueError(f"{ref}: a binding needs a `match` map and a `categories` list.")
    group, attribute = _text(match.get("group")), _text(match.get("attribute"))
    if (group is None) == (attribute is None) or (group is not None and group[:1] != "/"):
        raise ValueError(f"{ref}: `match` needs a full group path or an attribute.")
    try:
        kind = RoleGroup(raw.get("kind"))
    except ValueError as exc:
        raise ValueError(f"{ref}: `kind` is not a group kind.") from exc
    return BindingEntry(
        ref=ref,
        kind=kind,
        categories=frozenset(str(name) for name in categories),
        group=group,
        attribute=attribute,
        principal=str(raw.get("principal") or "person"),
    )


def bindings_from_document(document: Mapping[str, Any] | None) -> list[BindingEntry]:
    """The bindings of a stored tenant document.

    Raises :class:`ValueError` when the document holds a bad binding. The
    import refuses such a file, so this means the stored row was changed
    outside MEHO.
    """
    if document is not None and not isinstance(document, Mapping):
        raise ValueError("tenant: the stored policy is not a map.")
    raw_bindings = (document or {}).get("bindings") or []
    if not isinstance(raw_bindings, list):
        raise ValueError("tenant: `bindings` must be a list.")
    return [_entry(raw, f"tenant:bindings[{index}]") for index, raw in enumerate(raw_bindings)]


def _kind_of_name(name: str) -> str | None:
    """The group kind a group name ends with (``MEHO-SA-Operators`` → ``SA-Operators``)."""
    key = name.casefold()
    for kind in sorted(GROUP_KINDS, key=len, reverse=True):
        if key == kind.casefold() or key.endswith("-" + kind.casefold()):
            return kind
    return None


def shipped_binding_documents(settings: Settings) -> list[dict[str, Any]]:
    """The shipped bindings in the stored form, from the two login settings.

    Each group of ``JWT_ROLE_GROUPS`` whose name ends with a group kind
    gets that kind; the group of ``JWT_SELF_APPROVER_GROUP`` gets
    ``SA-Operators``. Each is matched by its top-level full path and gets
    every category.
    """
    kinds: dict[str, str] = {}
    for name in settings.jwt_role_groups:
        kind = _kind_of_name(name.strip("/"))
        if kind is not None:
            kinds["/" + name.strip("/")] = kind
    self_approver = settings.jwt_self_approver_group.strip("/")
    if self_approver:
        kinds["/" + self_approver] = RoleGroup.SA_OPERATORS.value
    return [
        {"match": {"group": path}, "kind": kind, "categories": [ALL_CATEGORIES]}
        for path, kind in kinds.items()
    ]


def shipped_bindings(settings: Settings) -> list[BindingEntry]:
    """The bindings of a tenant whose stored policy has none."""
    return [
        _entry(raw, f"{_SHIPPED}bindings[{index}]")
        for index, raw in enumerate(shipped_binding_documents(settings))
    ]


def _strings(value: Any, *, single: bool) -> tuple[str, ...] | None:
    """The texts of a claim, or ``None`` when the claim is not clear.

    A missing claim is empty. A list must hold only texts. A single text
    counts only when *single* is true.
    """
    if value is None:
        return ()
    if single and isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return None


def token_groups(claims: Mapping[str, Any]) -> tuple[frozenset[str], frozenset[str]]:
    """The full group paths and the group attribute values of the token.

    Only an entry of the ``groups`` claim that starts with ``/`` is a full
    path; a short name is left out. The ``groups`` claim must be a list of
    texts, as for the self-approval group today; the ``meho_groups`` claim
    may also be one text. When either claim is anything else, the login
    matches no binding at all: anything unclear gives no kind.
    """
    groups = _strings(claims.get(GROUPS_CLAIM), single=False)
    values = _strings(claims.get(GROUP_ATTRIBUTE_CLAIM), single=True)
    if groups is None or values is None:
        structlog.get_logger(__name__).warning(
            "policy_bindings_malformed_groups_claim",
            claim_name=GROUPS_CLAIM if groups is None else GROUP_ATTRIBUTE_CLAIM,
        )
        return frozenset(), frozenset()
    return frozenset(g for g in groups if g.startswith("/")), frozenset(a for a in values if a)


def bind(
    operator: Operator, claims: Mapping[str, Any], entries: Sequence[BindingEntry]
) -> Operator:
    """Return *operator* with the kind and the categories its bindings give.

    *operator* works in its current tenant and *entries* are that tenant's
    bindings. Raises :class:`BoundKindConflictError` when the matching
    bindings give two kinds.
    """
    if not operator.tenant_chosen:
        return operator
    paths, attributes = token_groups(claims)
    principal = _PRINCIPAL_NAMES[operator.principal_kind]
    matched = [entry for entry in entries if entry.matches(principal, paths, attributes)]
    log = structlog.get_logger(__name__)
    kinds = sorted({entry.kind for entry in matched}, key=lambda kind: GROUP_KINDS.index(kind))
    if kinds == _OPERATOR_AND_SELF_APPROVAL and all(e.ref.startswith(_SHIPPED) for e in matched):
        # Today a person in an operator group and in the self-approval group
        # is an operator who may self-approve. The shipped bindings keep that.
        kinds = [RoleGroup.SA_OPERATORS]
        matched = [entry for entry in matched if entry.kind is RoleGroup.SA_OPERATORS]
    if len(kinds) > 1:
        log.warning(
            "multiple_bound_kinds",
            operator_sub=operator.sub,
            kinds=[kind.value for kind in kinds],
            bindings=[entry.ref for entry in matched],
        )
        raise BoundKindConflictError(kinds)
    usable: list[BindingEntry] = []
    for entry in matched:
        if group_grant(entry.kind).role == operator.tenant_role:
            usable.append(entry)
            continue
        log.warning(
            "policy_binding_other_base_level",
            operator_sub=operator.sub,
            binding=entry.ref,
            kind=entry.kind.value,
            tenant_role=operator.tenant_role.value,
        )
    if not usable:
        return operator
    categories = frozenset().union(*(entry.categories for entry in usable))
    if ALL_CATEGORIES in categories:
        categories = frozenset({ALL_CATEGORIES})
    return _with_kind(operator, usable[0].kind, categories)


def _with_kind(operator: Operator, kind: RoleGroup, categories: frozenset[str]) -> Operator:
    grant = group_grant(kind)
    person = is_human_principal(operator)
    return operator.model_copy(
        update={
            "approver": grant.approver and person,
            "self_approver": grant.self_approval
            and person
            and not operator.tenants_claim_malformed,
            "bound_kind": kind,
            "bound_categories": categories,
        }
    )


async def _tenant_document(operator: Operator) -> Any:
    """The stored policy document of the operator's tenant, or ``None``."""
    async with get_sessionmaker()() as session:
        return (
            await session.execute(
                select(TenantPolicy.document).where(TenantPolicy.tenant_id == operator.tenant_id)
            )
        ).scalar_one_or_none()


async def bind_current_tenant(operator: Operator, claims: Mapping[str, Any]) -> Operator:
    """Apply the bindings of *operator*'s current tenant (:func:`bind`).

    Does nothing while the switch is off, or for a status answer without
    a chosen tenant. When the tenant has no stored bindings, the shipped
    bindings count only for a login without a tenant list
    (``meho_tenants``); a login with a list keeps the kind of its list
    entry. When the stored bindings cannot be read, the login
    gets the kind with the fewest rights of its base level and no
    category, and an error is logged.
    """
    if not routing_enabled() or not operator.tenant_chosen:
        return operator
    document = await _tenant_document(operator)
    settings = get_settings()
    try:
        entries = bindings_from_document(document)
    except ValueError as exc:
        structlog.get_logger(__name__).error(
            "policy_bindings_unreadable",
            operator_sub=operator.sub,
            tenant_id=str(operator.tenant_id),
            reason=str(exc),
        )
        return _with_kind(operator, _LOWEST_KIND[operator.tenant_role], frozenset())
    if not entries:
        if settings.jwt_tenants_claim_name in claims:
            # The ``groups`` claim is not per tenant: a group of another
            # tenant must not decide the kind here. The list entry decides.
            return operator
        entries = shipped_bindings(settings)
    return bind(operator, claims, entries)


def binding_mistakes(document: Mapping[str, Any], *, scope: Scope, taxonomy: Taxonomy) -> list[str]:
    """The import mistakes of the ``bindings`` of a valid file's stored form."""
    raw_bindings: list[dict[str, Any]] = list(document.get("bindings") or [])
    if not raw_bindings:
        return []
    if scope == "install":
        return [
            "`bindings` belongs to a tenant's policy file. Each tenant binds its own login groups."
        ]
    mistakes: list[str] = []
    known = ", ".join(taxonomy.categories)
    for index, raw in enumerate(raw_bindings):
        principal = raw.get("principal") or "person"
        if principal == "runner":
            mistakes.append(
                f"`bindings[{index}]`: a runner cannot get a role. A runner only runs "
                "what MEHO has already decided."
            )
        elif principal != "person" and raw.get("kind") in _APPROVING_KINDS:
            mistakes.append(
                f"`bindings[{index}]` gives an agent or a service a group kind that may "
                "approve. Only a person approves, with a click in the web interface or a "
                "fresh sign-in."
            )
        for name in raw.get("categories") or []:
            if name != ALL_CATEGORIES and name not in taxonomy.categories:
                mistakes.append(
                    f"`bindings[{index}].categories` `{name}` is not in the taxonomy ({known})."
                )
    return mistakes
