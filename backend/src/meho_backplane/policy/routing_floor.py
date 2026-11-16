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
(:func:`~meho_backplane.policy.routing.route`). At the approve click MEHO
routes the request again with the install's current routing
(:func:`install_floor_refusal`). So a tenant entry stored before this rule,
or an install file that changed later, never widens anything: where the
tenant entry is wider, its extra kinds do not count. A request that
started to wait before MEHO stored its category is refused when an
install entry that matches on a category could fit it. ``meho policy
explain --request`` uses the same install routing (:func:`install_floor`).

**Install import.** The import diff of the install's file lists the
tenants whose stored routing is wider than the new file
(:func:`tenants_wider`).

Everything here runs only while the ``POLICY_APPROVER_ROUTING`` switch is
on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from fnmatch import fnmatchcase
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import ApprovalRequest, TenantPolicy
from meho_backplane.policy.routing import (
    DEFAULT_RULE,
    ApproverSet,
    RouteEntry,
    _routes,
    group_names,
    kind_of,
    route,
    routing_enabled,
)
from meho_backplane.policy.store import _documents

__all__ = [
    "CATEGORY_NOT_STORED",
    "floor_warnings",
    "install_floor",
    "install_floor_refusal",
    "may_share",
    "tenant_floor_mistakes",
    "tenants_wider",
    "wider_entries",
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


def wider_entries(tenant: Sequence[RouteEntry], install: Sequence[RouteEntry]) -> list[str]:
    """One sentence per tenant entry and install entry where the tenant's is wider."""
    sentences: list[str] = []
    for mine in tenant:
        for floor in install:
            extra = [kind for kind in mine.approvers if kind not in floor.approvers]
            if not extra or not may_share(mine, floor):
                continue
            sentences.append(
                f"`{_place(mine)}` lets {group_names(extra)} approve changes with "
                f"{_describe(floor)}, but the install's routing (`{floor.ref}`) lets only "
                f"{group_names(floor.approvers)} approve them. A tenant can only narrow the "
                "install's routing: name only these groups, or fewer."
            )
    return sentences


def tenant_floor_mistakes(
    document: Mapping[str, Any], install_document: Mapping[str, Any] | None
) -> list[str]:
    """The import mistakes of a tenant file whose routing is wider than the install's.

    Empty while the ``POLICY_APPROVER_ROUTING`` switch is off. Raises
    :class:`ValueError` when the install's stored routing is not valid.
    """
    if not routing_enabled():
        return []
    install = _routes(install_document, "install", install=True)
    if not install:
        return []
    return wider_entries(_routes(document, "tenant", install=False), install)


async def tenants_wider(
    session: AsyncSession, install_document: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """The tenants whose stored routing is wider than *install_document*.

    One item per tenant: ``tenant_id`` and ``entries`` (one sentence each).
    Empty while the switch is off. A tenant whose stored routing is not
    valid gets one sentence that says so.
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
            sentences = wider_entries(_routes(row.document, "tenant", install=False), install)
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


CATEGORY_NOT_STORED: Final[str] = "category_not_stored"
"""The rule of :func:`install_floor` when MEHO cannot check the install's routing."""


def _level(request: ApprovalRequest) -> str | None:
    level = (request.proposed_effect or {}).get("safety_level")
    return level if isinstance(level, str) and level else None


def _route_install(entries: Sequence[RouteEntry], request: ApprovalRequest) -> ApproverSet:
    """Route *request* with the install's *entries* and the category it stored.

    A request that started to wait before MEHO stored categories has the
    empty text (migration ``0171``). Its category is not known. When an
    install entry that matches on a category could fit it, no group may
    approve it (rule :data:`CATEGORY_NOT_STORED`). Else the category does
    not change the answer.
    """
    level = _level(request)
    category = request.approver_category
    if category == "":
        if any(
            entry.category is not None
            and entry.matches(
                category=entry.category, connector_id=request.connector_id, level=level
            )
            for entry in entries
        ):
            return ApproverSet(kinds=(), rule=CATEGORY_NOT_STORED)
        category = None
    return route(
        entries,
        category=category,
        connector_id=request.connector_id,
        level=level,
        op_id=request.op_id,
    )


async def install_floor(session: AsyncSession, request: ApprovalRequest) -> ApproverSet | None:
    """Route *request* again with the install's stored routing as it is now.

    Uses the request's risk level and the category it stored
    (``approver_category``). ``None`` while the switch is off and for a
    change of rights (it is not routed). The rule is ``default`` when no
    install entry fits, and :data:`CATEGORY_NOT_STORED` when MEHO cannot
    check it. Raises :class:`ValueError` when the install's stored routing
    is not valid.
    """
    from meho_backplane.operations.grant_approval import is_rights_change

    if not routing_enabled() or is_rights_change(request):
        return None
    install_row, _ = await _documents(session, None)
    entries = _routes(
        install_row.document if install_row is not None else None, "install", install=True
    )
    return _route_install(entries, request)


async def install_floor_refusal(
    session: AsyncSession, operator: Operator, request: ApprovalRequest
) -> str | None:
    """One sentence when the install's current routing does not let *operator* approve.

    See :func:`install_floor`. ``None`` while the switch is off, for a
    change of rights, and when no install entry fits. A stored install
    routing that is not valid refuses (fail closed), and so does a request
    whose category MEHO cannot know.
    """
    try:
        found = await install_floor(session, request)
    except ValueError as exc:
        return (
            f"The install's stored routing is not valid, so MEHO cannot check who may approve: "
            f"{exc} Import a fixed policy file."
        )
    if found is None or found.rule == DEFAULT_RULE:
        return None
    if found.rule == CATEGORY_NOT_STORED:
        return (
            "This request started to wait before MEHO stored the category of a change, and "
            "the install's routing matches on a category. So MEHO cannot check who may approve "
            "it. Reject it and start the call again."
        )
    kind = kind_of(operator)
    if kind in found.kinds:
        return None
    if not found.kinds:
        return (
            f"The install's routing (`{found.rule}`) lets no group approve this change, so it "
            "waits until it expires."
        )
    return (
        f"The install's routing (`{found.rule}`) lets only {group_names(found.kinds)} approve "
        f"this change. You are in MEHO-{kind}. A tenant's routing can only narrow it."
    )
