# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who gets a push message about a request that waits for approval.

The login groups are in the login token, not in the database. So each
subscription keeps the person's login facts in its tenant
(:class:`LoginFacts`: role, approver flag, SA-Operator flag, platform admin
flag) and when they were read. MEHO reads them from the login when the
person switches the notifications on, at each console sign-in and each
time the account page opens.

:func:`subscriptions_for` builds an :class:`~meho_backplane.auth.operator.Operator`
from each row's facts and asks
:func:`~meho_backplane.operations.approval_rights.approve_right_any_status`
whether that person may approve the request. That is the same rule as the
approve itself, with the group rule, the rights-change rule and the role
floor of the operation. The answer leaves out only the "still pending"
check, so a decision message goes to the same people as the first message.

A row whose facts are older than :data:`FACTS_MAX_AGE` gets no message: the
person's groups may have changed since. It is only the message list: the
approve itself checks the person again.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole, is_human_principal
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations.approval_rights import approve_right_any_status
from meho_backplane.web_push.models import PushSubscription

__all__ = [
    "FACTS_MAX_AGE",
    "LoginFacts",
    "PushTarget",
    "login_facts_for",
    "subscriptions_for",
    "subscriptions_of_person",
]

#: A row whose login facts are older than this gets no message.
FACTS_MAX_AGE = timedelta(days=7)


@dataclass(frozen=True)
class PushTarget:
    """One browser to send to. ``endpoint`` and the keys are never logged."""

    id: uuid.UUID
    operator_sub: str
    endpoint: str
    p256dh: str
    auth: str


@dataclass(frozen=True)
class LoginFacts:
    """What a subscription keeps of the person's login in its tenant.

    ``tenant_role`` is ``None`` for a login that is not a person, or one
    without a chosen tenant: such a row gets no message.
    """

    tenant_role: str | None
    approver: bool = False
    self_approver: bool = False
    platform_admin: bool = False


def login_facts_for(operator: Operator | None) -> LoginFacts:
    """Return the login facts of *operator* in its current tenant."""
    if operator is None or not is_human_principal(operator) or not operator.tenant_chosen:
        return LoginFacts(tenant_role=None)
    return LoginFacts(
        tenant_role=operator.tenant_role.value,
        approver=operator.approver,
        self_approver=operator.self_approver,
        platform_admin=operator.platform_admin,
    )


def _fresh_rows(tenant_id: uuid.UUID, now: datetime) -> Select[tuple[PushSubscription]]:
    return (
        select(PushSubscription)
        .where(PushSubscription.tenant_id == tenant_id)
        .where(PushSubscription.tenant_role.is_not(None))
        .where(PushSubscription.facts_read_at >= now - FACTS_MAX_AGE)
    )


def _operator_of(row: PushSubscription) -> Operator | None:
    """The person of *row*, as their stored login facts describe them."""
    if row.tenant_role is None:
        return None
    try:
        role = TenantRole(row.tenant_role)
    except ValueError:
        return None
    # web_session: the message opens the web interface, where the person
    # approves with a click.
    return Operator(
        sub=row.operator_sub,
        raw_jwt="",
        tenant_id=row.tenant_id,
        tenant_role=role,
        approver=row.approver,
        self_approver=row.self_approver,
        platform_admin=row.platform_admin,
        web_session=True,
    )


async def subscriptions_for(
    db: AsyncSession, *, request: ApprovalRequest, now: datetime | None = None
) -> list[PushTarget]:
    """Return the browsers of the people who may approve *request*.

    Only rows of the request's tenant, with login facts read in the last
    :data:`FACTS_MAX_AGE`.
    """
    rows = await db.execute(_fresh_rows(request.tenant_id, now or datetime.now(UTC)))
    targets: list[PushTarget] = []
    for row in rows.scalars():
        operator = _operator_of(row)
        if operator is not None and approve_right_any_status(operator, request).may_approve:
            targets.append(_target(row))
    return targets


async def subscriptions_of_person(
    db: AsyncSession, *, operator_sub: str, tenant_id: uuid.UUID, now: datetime | None = None
) -> list[PushTarget]:
    """Return the browsers of one person in one tenant that get messages (for the summary)."""
    rows = await db.execute(
        _fresh_rows(tenant_id, now or datetime.now(UTC)).where(
            PushSubscription.operator_sub == operator_sub
        )
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
