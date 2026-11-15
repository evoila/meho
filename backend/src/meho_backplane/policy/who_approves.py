# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who may approve a call that waits: the login groups, and the rule that says so.

Used by ``meho policy explain`` (:mod:`meho_backplane.policy.explain`) and
by the preview (:func:`preview_policy_fields`, the fields ``policy_rule``
and ``approvers`` of ``preview_operation``).

The groups are the ones that may approve another person's call:

* A change of rights: only another admin (``MEHO-Admins``), as today. It
  is never routed.
* With the ``POLICY_APPROVER_ROUTING`` switch on, the first ``routing``
  entry of the policy that fits (:func:`~meho_backplane.policy.routing.route`).
* Else today's rule (:func:`~meho_backplane.policy.routing.today_kinds`).

Whether the requester may approve their own call is today's rule, and is
not part of this answer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, PermissionVerdict
from meho_backplane.policy.routing import (
    DEFAULT_RULE,
    RouteEntry,
    approver_set_for,
    route,
    routing_enabled,
    today_kinds,
)
from meho_backplane.policy.shadow import shadow_enabled
from meho_backplane.policy.stamp import RuleStamp

__all__ = [
    "RIGHTS_CHANGE_RULE",
    "Approvers",
    "approvers_for",
    "approvers_from_routes",
    "preview_policy_fields",
]

#: The rule of the approver groups of a change of rights: only another admin.
RIGHTS_CHANGE_RULE: Final[str] = "rights_change"


@dataclass(frozen=True)
class Approvers:
    """The login groups that may approve, and the rule that says so.

    The rule is ``default``, ``rights_change`` or a routing entry.
    """

    groups: tuple[str, ...]
    rule: str


def _names(kinds: Sequence[str]) -> tuple[str, ...]:
    return tuple(f"MEHO-{kind}" for kind in kinds)


def _rights_change(op_id: str) -> bool:
    from meho_backplane.operations.grant_approval import is_rights_change_op

    return is_rights_change_op(op_id)


def approvers_from_routes(
    routes: Sequence[RouteEntry] | None,
    *,
    connector_id: str,
    op_id: str,
    level: str | None,
    category: str | None,
) -> Approvers:
    """The approver groups of one call; *routes* is ``None`` while routing is off."""
    if _rights_change(op_id):
        return Approvers(("MEHO-Admins",), RIGHTS_CHANGE_RULE)
    if routes is None:
        return Approvers(_names(today_kinds(level, op_id)), DEFAULT_RULE)
    found = route(routes, category=category, connector_id=connector_id, level=level, op_id=op_id)
    return Approvers(_names(found.kinds), found.rule)


async def approvers_for(
    *, tenant_id: UUID, connector_id: str, descriptor: EndpointDescriptor
) -> Approvers:
    """The approver groups of a call of *descriptor*, from the stored policy."""
    level = descriptor.safety_level
    category = descriptor.policy_category
    if not routing_enabled() or _rights_change(descriptor.op_id):
        return approvers_from_routes(
            None, connector_id=connector_id, op_id=descriptor.op_id, level=level, category=category
        )
    async with get_sessionmaker()() as session:
        found = await approver_set_for(
            session,
            tenant_id=tenant_id,
            connector_id=connector_id,
            op_id=descriptor.op_id,
            level=level,
            category=category,
        )
    return Approvers(_names(found.kinds), found.rule)


async def preview_policy_fields(
    *,
    tenant_id: UUID,
    connector_id: str,
    descriptor: EndpointDescriptor,
    verdict: PermissionVerdict,
    stamp: RuleStamp | None,
) -> dict[str, Any]:
    """``policy_rule`` and ``approvers`` of a preview; ``{}`` while ``POLICY_ENGINE_SHADOW`` is off.

    ``policy_rule`` is the rule the evaluator matched for the call's own
    policy gate (*stamp*), or ``None`` when the evaluator failed.
    ``approvers`` lists the login groups that may approve when the call
    waits, else it is empty.
    """
    if not shadow_enabled():
        return {}
    groups: tuple[str, ...] = ()
    if verdict is PermissionVerdict.NEEDS_APPROVAL:
        found = await approvers_for(
            tenant_id=tenant_id, connector_id=connector_id, descriptor=descriptor
        )
        groups = found.groups
    return {
        "policy_rule": stamp.as_json() if stamp is not None else None,
        "approvers": list(groups),
    }
