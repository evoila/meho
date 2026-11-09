# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who gets a push message about a request that waits for approval.

The login groups are in the login token, not in the database. So each
subscription keeps what its person may approve (``approve_scope``), read
from their login when they switch the notifications on, and again each time
they open the account page (:func:`approve_scope_for`):

* ``"all"`` -- a ``tenant_admin`` or a MEHO-SA-Operator. They may approve
  every request of the tenant, also their own and the ``dangerous`` and
  ``destructive`` ones.
* ``"lower_risk"`` -- a MEHO-Approver. They may approve another person's
  ``safe`` or ``caution`` change.
* ``None`` -- anyone else. No message.

:func:`subscriptions_for` picks the rows of one request with the same group
rule as ``approve_request`` (``operations/approval_queue.py``). It is only
the message list: the approve itself checks the person again.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole, is_human_principal
from meho_backplane.web_push.models import PushSubscription

__all__ = [
    "SCOPE_ALL",
    "SCOPE_LOWER_RISK",
    "PushTarget",
    "approve_scope_for",
    "subscriptions_for",
    "subscriptions_of_person",
]

SCOPE_ALL = "all"
SCOPE_LOWER_RISK = "lower_risk"


@dataclass(frozen=True)
class PushTarget:
    """One browser to send to. ``endpoint`` and the keys are never logged."""

    id: uuid.UUID
    operator_sub: str
    endpoint: str
    p256dh: str
    auth: str


def approve_scope_for(operator: Operator | None) -> str | None:
    """Return the ``approve_scope`` of *operator* in its current tenant."""
    if operator is None or not is_human_principal(operator):
        return None
    if operator.may_self_approve or operator.tenant_role == TenantRole.TENANT_ADMIN:
        return SCOPE_ALL
    if operator.approver:
        return SCOPE_LOWER_RISK
    return None


async def subscriptions_for(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    requester_sub: str,
    high_risk: bool,
) -> list[PushTarget]:
    """Return the browsers of the people who may approve this request.

    Only rows of *tenant_id*. A ``lower_risk`` person gets no message for a
    ``dangerous`` or ``destructive`` change, and none for their own request.
    """
    lower_risk_ok = and_(
        PushSubscription.approve_scope == SCOPE_LOWER_RISK,
        PushSubscription.operator_sub != requester_sub,
    )
    allowed = PushSubscription.approve_scope == SCOPE_ALL
    if not high_risk:
        allowed = or_(allowed, lower_risk_ok)
    rows = await db.execute(
        select(PushSubscription).where(PushSubscription.tenant_id == tenant_id).where(allowed)
    )
    return [_target(row) for row in rows.scalars()]


async def subscriptions_of_person(db: AsyncSession, *, operator_sub: str) -> list[PushTarget]:
    """Return the browsers of one person that get approval messages (for the summary)."""
    rows = await db.execute(
        select(PushSubscription)
        .where(PushSubscription.operator_sub == operator_sub)
        .where(PushSubscription.approve_scope.is_not(None))
    )
    return [_target(row) for row in rows.scalars()]


def _target(row: PushSubscription) -> PushTarget:
    return PushTarget(
        id=row.id,
        operator_sub=row.operator_sub,
        endpoint=row.endpoint,
        p256dh=row.p256dh,
        auth=row.auth,
    )
