# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who may approve a call that waits: the login groups, and the rule that says so.

Used by ``meho policy explain`` (:mod:`meho_backplane.policy.explain`) and
by the preview (:func:`preview_policy_fields`, the fields ``policy_rule``
and ``approvers`` of ``preview_operation``).

The groups are the ones that may approve another person's call:

* A change of rights: only another admin (``MEHO-Admins``), as today. It
  is never routed.
* With the ``POLICY_APPROVER_ROUTING`` switch on, the first install entry
  that fits the category the install's policy alone gives (the floor),
  narrowed by the first tenant entry that fits
  (:func:`~meho_backplane.policy.routing.route`).
* Else today's rule (:func:`~meho_backplane.policy.routing.today_kinds`).

Whether the requester may approve their own call is today's rule, and is
not part of this answer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

import structlog

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
from meho_backplane.policy.store import InvalidStoredPolicyError

__all__ = [
    "RIGHTS_CHANGE_RULE",
    "Approvers",
    "approvers_for",
    "approvers_from_routes",
    "invalid_routing",
    "preview_policy_fields",
]

_log = structlog.get_logger(__name__)

#: The rule of the approver groups of a change of rights: only another admin.
RIGHTS_CHANGE_RULE: Final[str] = "rights_change"


@dataclass(frozen=True)
class Approvers:
    """The login groups that may approve, and the rule that says so.

    The rule is ``default``, ``rights_change`` or the routing entries that
    fit. ``floor`` is the install's entry that fits, or ``None``.
    """

    groups: tuple[str, ...]
    rule: str
    floor: str | None = None


def invalid_routing(exc: ValueError) -> InvalidStoredPolicyError:
    """The error for a stored ``routing`` section that is not valid, as for a classification."""
    return InvalidStoredPolicyError(
        f"The stored routing is not valid, so MEHO cannot say who may approve: {exc} "
        "Import a fixed policy file."
    )


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
    install_category: str | None,
) -> Approvers:
    """The approver groups of one call; *routes* is ``None`` while routing is off.

    *install_category* is the category the install's policy alone gives
    the call; the install's entries match on it.
    """
    if _rights_change(op_id):
        return Approvers(("MEHO-Admins",), RIGHTS_CHANGE_RULE)
    if routes is None:
        return Approvers(_names(today_kinds(level, op_id)), DEFAULT_RULE)
    found = route(
        routes,
        category=category,
        install_category=install_category,
        connector_id=connector_id,
        level=level,
        op_id=op_id,
    )
    return Approvers(_names(found.kinds), found.rule, found.floor)


async def approvers_for(
    *, tenant_id: UUID, connector_id: str, descriptor: EndpointDescriptor
) -> Approvers:
    """The approver groups of a call of *descriptor*, from the stored policy.

    Raises :class:`~meho_backplane.policy.store.InvalidStoredPolicyError`
    when the stored ``routing`` section is not valid.
    """
    level = descriptor.safety_level
    category = descriptor.policy_category
    install_category = descriptor.policy_install_category
    if not routing_enabled() or _rights_change(descriptor.op_id):
        return approvers_from_routes(
            None,
            connector_id=connector_id,
            op_id=descriptor.op_id,
            level=level,
            category=category,
            install_category=install_category,
        )
    async with get_sessionmaker()() as session:
        try:
            found = await approver_set_for(
                session,
                tenant_id=tenant_id,
                connector_id=connector_id,
                op_id=descriptor.op_id,
                level=level,
                category=category,
                install_category=install_category,
            )
        except ValueError as exc:
            raise invalid_routing(exc) from exc
    return Approvers(_names(found.kinds), found.rule, found.floor)


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
    waits, else it is empty. It is ``None`` when the stored routing is not
    valid: the preview still answers, but cannot say who may approve.
    """
    if not shadow_enabled():
        return {}
    approvers: list[str] | None = []
    if verdict is PermissionVerdict.NEEDS_APPROVAL:
        try:
            found = await approvers_for(
                tenant_id=tenant_id, connector_id=connector_id, descriptor=descriptor
            )
            approvers = list(found.groups)
        except InvalidStoredPolicyError as exc:
            _log.warning("preview_approvers_unknown", error=str(exc))
            approvers = None
    return {
        "policy_rule": stamp.as_json() if stamp is not None else None,
        "approvers": approvers,
    }
